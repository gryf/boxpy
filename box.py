#!/usr/bin/env python

import argparse
import collections.abc
import os
import shutil
import string
import subprocess
import sys
import tempfile
import time
import uuid
import xml.dom.minidom

import yaml


CACHE_DIR = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
CLOUD_IMAGE = "ci.iso"
META_DATA_TPL = string.Template('''\
instance-id: $instance_id
local-hostname: $vmhostname
''')
UBUNTU_VERSION = '20.04'
USER_DATA = '''\
#cloud-config
users:
  - default
  - name: ubuntu
    ssh_authorized_keys:
      - $ssh_key
    chpasswd: { expire: False }
    gecos: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, admin
power_state:
  mode: poweroff
  timeout: 10
  condition: True
boxpy_data:
  cpus: 1
  disk_size: 10240
  key: ~/.ssh/id_rsa
  memory: 2048
  port: 2222
  version: 20.04
'''
COMPLETIONS = {'bash': '''\
_boxpy() {
    local cur prev words cword _GNUSED
    _GNUSED=${GNUSED:-sed}

    # Complete registered VM names.
    # Issues are the same as in above function.
    _vms_comp() {
        local command=$1
        local exclude_running=false
        local vms
        local running_vms
        local item

        compopt -o filenames
        if [[ $# == 2 ]]
        then
            exclude_running=true
            running_vms=$(VBoxManage list runningvms | \
                awk -F ' {' '{ print $1 }' | \
                tr '\n' '|' | \
                $_GNUSED 's/|$//' | \
                $_GNUSED 's/"//g')
            IFS='|' read -ra running_vms <<< "$running_vms"
        fi

        vms=$(VBoxManage list $command | \
            awk -F ' {' '{ print $1 }' | \
            tr '\n' '|' | \
            $_GNUSED 's/|$//' | \
            $_GNUSED 's/"//g')
        IFS='|' read -ra vms <<< "$vms"
        for item in "${vms[@]}"
        do
            if $exclude_running
            then
                _is_in_array "$item" "${running_vms[@]}"
                [[ $? == 0 ]] && continue
            fi

            [[ ${item^^} == ${cur^^}* ]] && COMPREPLY+=("$item")
        done
    }

    _get_excluded_items() {
        local i

        result=""
        for i in $@; do
            [[ " ${COMP_WORDS[@]} " == *" $i "* ]] && continue
            result="$result $i"
        done
    }

    _ssh_identityfile() {
        [[ -z $cur && -d ~/.ssh ]] && cur=~/.ssh/id
        _filedir
        if ((${#COMPREPLY[@]} > 0)); then
            COMPREPLY=($(compgen -W '${COMPREPLY[@]}' \
                -X "${1:+!}*.pub" -- "$cur"))
        fi
    }

    COMP_WORDBREAKS=${COMP_WORDBREAKS//|/}  # remove pipe from comp word breaks
    COMPREPLY=()

    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    if [[ COMP_CWORD -ge 2 ]]; then
        cmd="${COMP_WORDS[1]}"
        if [[ $cmd == "-q" ]]; then
                cmd="${COMP_WORDS[2]}"
        fi
    fi

    opts="create destroy rebuild list completion"
    if [[ ${cur} == "-q" || ${cur} == "-v" || ${COMP_CWORD} -eq 1 ]] ; then
        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
        return 0
    fi

    case "${cmd}" in
        completion)
            if [[ ${prev} == ${cmd} ]]; then
                COMPREPLY=( $(compgen -W "bash" -- ${cur}) )
            fi
            ;;
        create|rebuild)
            items=(--cpus --disk-size --key --memory --hostname
                --port --cloud-config --version)
            if [[ ${prev} == ${cmd} ]]; then
                if [[ ${cmd} = "rebuild" ]]; then
                    _vms_comp vms
                else
                    COMPREPLY=( $(compgen -W "${items[*]}" -- ${cur}) )
                fi
            else
                _get_excluded_items "${items[@]}"
                COMPREPLY=( $(compgen -W "$result" -- ${cur}) )

                case "${prev}" in
                    --cloud-config)
                        COMPREPLY=( $(compgen -f -- ${cur}) )
                        ;;
                    --key)
                        _ssh_identityfile
                        ;;
                esac
            fi

            ;;
        destroy)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp vms
            fi
            ;;
        list)
            items=(--long --running)
            _get_excluded_items "${items[@]}"
            COMPREPLY=( $(compgen -W "$result" -- ${cur}) )
            ;;
    esac

}
complete -o default -F _boxpy boxpy
'''}

def convert_to_mega(size):
    """
    Vritualbox uses MB as a common denominator for amount of memory or disk
    size. This function will return string of MB from string which have human
    readable suffix, like M or G. Case insensitive.
    """

    if size.isnumeric():
        return str(size)

    if size.lower().endswith('m') and size[:-1].isnumeric():
        return str(size[:-1])

    if size.lower().endswith('g') and size[:-1].isnumeric():
        return str(int(size[:-1]) * 1024)

    if size.lower().endswith('mb') and size[:-2].isnumeric():
        return str(size[:-2])

    if size.lower().endswith('gb') and size[:-2].isnumeric():
        return str(int(size[:-2]) * 1024)


class BoxError(Exception):
    pass


class BoxNotFound(BoxError):
    pass


class BoxVBoxFailure(BoxError):
    pass


class BoxConvertionError(BoxError):
    pass


class BoxSysCommandError(BoxError):
    pass


class Config:
    ATTRS = ('cpus', 'cloud_config', 'disk_size', 'hostname', 'key',
             'memory', 'name', 'port', 'version')

    def __init__(self, args, vbox=None):
        self.cloud_config = None
        self.cpus = None
        self.disk_size = None
        self.hostname = None
        self.key = None
        self.memory = None
        self.name = None
        self.port = None
        self.version = None

        # first, grab the cloud config file
        self._custom_file = args.cloud_config

        # initialize default from yaml file(s) first
        self._combine_cc(vbox)

        # than override all of the attrs with provided args from commandline.
        # If the value of rhe
        # in case we have vbox object provided.
        # this means that we need to read params stored on the VM attributes.
        vm_info = vbox.get_vm_info() if vbox else {}
        for attr in self.ATTRS:
            val = getattr(args, attr, None) or vm_info.get(attr)
            if not val:
                continue
            setattr(self, attr, str(val))

        self.hostname = self.hostname or self._normalize_name()

    def _normalize_name(self):
        name = self.name.replace(' ', '-')
        name = name.encode('ascii', errors='ignore')
        name = name.decode('utf-8')
        return ''.join(x for x in name if x.isalnum() or x == '-')

    def _combine_cc(self, vbox):
        # that's default config
        conf = yaml.safe_load(USER_DATA)

        if vbox and not self._custom_file:
            # in case of not provided (new) custom cloud config, and vbox
            # object is present, read information out of potentially stored
            # file in VM attributes.
            vm_info = vbox.get_vm_info()
            if os.path.exists(vm_info.get('cloud_config')):
                self._custom_file = vm_info['cloud_config']

        # read user custom cloud config (if present) and update config dict
        if self._custom_file and os.path.exists(self._custom_file):
            with open(self._custom_file) as fobj:
                custom_conf = yaml.safe_load(fobj)
                conf = self._update(conf, custom_conf)

        # set the attributes.
        for key, val in conf.get('boxpy_data', {}).items():
            if not val:
                continue
            setattr(self, key, str(val))

        if conf.get('boxpy_data'):
            del conf['boxpy_data']

        self._conf = "#cloud-config\n" + yaml.safe_dump(conf)

    def get_cloud_config_tpl(self):
        return string.Template(self._conf)

    def _update(self, source, update):
        for key, val in update.items():
            if isinstance(val, collections.abc.Mapping):
                source[key] = self._update(source.get(key, {}), val)
            else:
                source[key] = val
        return source


class VBoxManage:
    """
    Class for dealing with vboxmanage commands
    """
    def __init__(self, name_or_uuid=None):
        self.name_or_uuid = name_or_uuid
        self.vm_info = {}

    def get_vm_base_path(self):
        path = self._get_vm_config()
        if not path:
            return

        return os.path.dirname(path)

    def get_disk_path(self):
        path = self._get_vm_config()
        if not path:
            return

        dom = xml.dom.minidom.parse(path)
        if len(dom.getElementsByTagName('HardDisk')) != 1:
            # don't know what to do with multiple discs
            raise BoxError('Cannot deal with multiple attached disks, perhaps '
                           'you need to do this manually')
        disk = dom.getElementsByTagName('HardDisk')[0]
        location = disk.getAttribute('location')
        if location.startswith('/'):
            disk_path = location
        else:
            disk_path = os.path.join(self.get_vm_base_path(), location)

        return disk_path

    def get_media_size(self, media_path):
        try:
            out = subprocess.check_output(['vboxmanage', 'showmediuminfo',
                                           media_path],
                                          encoding=sys.getdefaultencoding(),
                                          stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return None

        for line in out.split('\n'):
            if line.startswith('Capacity:'):
                line = line.split('Capacity:')[1].strip()

                if line.isnumeric():
                    return line
                else:
                    return line.split(' ')[0].strip()

    def get_vm_info(self):
        try:
            out = subprocess.check_output(['vboxmanage', 'showvminfo',
                                           self.name_or_uuid],
                                          encoding=sys.getdefaultencoding(),
                                          stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return None

        self.vm_info = {}

        for line in out.split('\n'):
            if line.startswith('Config file:'):
                self.vm_info['config_file'] = line.split('Config '
                                                         'file:')[1].strip()
                continue

            if line.startswith('Memory size'):
                mem = line.split('Memory size')[1].strip()
                if mem.isnumeric():
                    self.vm_info['memory'] = mem
                else:
                    # 12288MB
                    self.vm_info['memory'] = mem[:-2]
                continue

            if line.startswith('Number of CPUs:'):
                self.vm_info['cpus'] = line.split('Number of CPUs:')[1].strip()
                continue

            if line.startswith('UUID:'):
                self.vm_info['uuid'] = line.split('UUID:')[1].strip()

        dom = xml.dom.minidom.parse(self.vm_info['config_file'])

        for extradata in dom.getElementsByTagName('ExtraDataItem'):
            key = extradata.getAttribute('name')
            val = extradata.getAttribute('value')
            self.vm_info[key] = val

        if len(dom.getElementsByTagName('Forwarding')):
            fw = dom.getElementsByTagName('Forwarding')[0]
            self.vm_info['port'] = fw.getAttribute('hostport')

        return self.vm_info

    def poweroff(self, silent=False):
        cmd = ['vboxmanage', 'controlvm', self.name_or_uuid, 'poweroff']
        if silent:
            subprocess.call(cmd, stderr=subprocess.DEVNULL)
        else:
            subprocess.call(cmd)

    def vmlist(self, only_running=False, long_list=False):
        subcommand = 'runningvms' if only_running else 'vms'
        long_list = '-l' if long_list else '-s'
        subprocess.call(['vboxmanage', 'list', subcommand, long_list])

    def get_running_vms(self):
        return subprocess.getoutput('vboxmanage list runningvms')

    def destroy(self):
        self.poweroff(silent=True)
        if subprocess.call(['vboxmanage', 'unregistervm', self.name_or_uuid,
                            '--delete']) != 0:
            raise BoxVBoxFailure(f'Removing VM {self.name_or_uuid} failed')

    def create(self, cpus, memory, port):
        self.uuid = None
        memory = convert_to_mega(memory)

        try:
            out = subprocess.check_output(['vboxmanage', 'createvm', '--name',
                                           self.name_or_uuid, '--register'],
                                          encoding=sys.getdefaultencoding())
        except subprocess.CalledProcessError:
            return None

        for line in out.split('\n'):
            print(line)
            if line.startswith('UUID:'):
                self.uuid = line.split('UUID:')[1].strip()

        if not self.uuid:
            raise BoxVBoxFailure(f'Cannot create VM "{self.name_or_uuid}".')

        if subprocess.call(['vboxmanage', 'modifyvm', self.name_or_uuid,
                            '--memory', str(memory),
                            '--cpus', str(cpus),
                            '--boot1', 'disk',
                            '--acpi', 'on',
                            '--audio', 'none',
                            '--nic1', 'nat',
                            '--natpf1', f'guestssh,tcp,,{port},,22']) != 0:
            raise BoxVBoxFailure(f'Cannot modify VM "{self.name_or_uuid}".')

        return self.uuid

    def convertfromraw(self, src, dst):
        if subprocess.call(["vboxmanage", "convertfromraw", src, dst]) != 0:
            os.unlink(src)
            raise BoxVBoxFailure('Cannot convert image to VDI.')
        os.unlink(src)

    def closemedium(self, type_, mediumpath):
        if subprocess.call(['vboxmanage', 'closemedium', type_,
                            mediumpath]) != 0:
            raise BoxVBoxFailure(f'Failed close medium {mediumpath}.')

    def create_controller(self, name, type_):
        if subprocess.call(['vboxmanage', 'storagectl', self.name_or_uuid,
                            '--name', name, '--add', type_]) != 0:
            raise BoxVBoxFailure(f'Adding controller {type_} has failed.')

    def move_and_resize_image(self, src, dst, size):
        fullpath = os.path.join(self.get_vm_base_path(), dst)
        size = convert_to_mega(size)

        if subprocess.call(['vboxmanage', 'modifymedium', 'disk', src,
                            '--resize', str(size), '--move', fullpath]) != 0:
            raise BoxVBoxFailure(f'Resizing and moving image {dst} has '
                                 f'failed')
        return fullpath

    def storageattach(self, controller_name, port, type_, image):
        if subprocess.call(['vboxmanage', 'storageattach', self.name_or_uuid,
                            '--storagectl', controller_name,
                            '--port', str(port),
                            '--device', '0',
                            '--type', type_,
                            '--medium', image]) != 0:
            raise BoxVBoxFailure(f'Attaching {image} to VM has failed.')

    def poweron(self):
        if subprocess.call(['vboxmanage', 'startvm', self.name_or_uuid,
                            '--type', 'headless']) != 0:
            raise BoxVBoxFailure(f'Failed to start: {self.name_or_uuid}.')

    def setextradata(self, key, val):
        if subprocess.call(['vboxmanage', 'setextradata', self.name_or_uuid,
                            key, val]) != 0:
            raise BoxVBoxFailure(f'Failed to start: {self.name_or_uuid}.')

    def _get_vm_config(self):
        if self.vm_info.get('config_file'):
            return self.vm_info['config_file']

        self.get_vm_info()

        if self.vm_info.get('config_file'):
            return self.vm_info['config_file']


class Image:
    def __init__(self, vbox, version, arch='amd64'):
        self.version = version
        self.arch = arch
        self.vbox = vbox
        self._tmp = tempfile.mkdtemp()
        self._img = f"ubuntu-{self.version}-server-cloudimg-{self.arch}.img"

    def convert_to_vdi(self, disk_img, size):
        self._download_image()
        self._convert_to_raw()
        raw_path = os.path.join(self._tmp, self._img + ".raw")
        vdi_path = os.path.join(self._tmp, disk_img)
        self.vbox.convertfromraw(raw_path, vdi_path)
        return self.vbox.move_and_resize_image(vdi_path, disk_img, size)

    def _checksum(self):
        """
        Get and check checkusm for downloaded image. Return True if the
        checksum is correct, False otherwise.
        """
        if not os.path.exists(os.path.join(CACHE_DIR, self._img)):
            return False

        expected_sum = None
        fname = 'SHA256SUMS'
        url = "https://cloud-images.ubuntu.com/releases/"
        url += f"{self.version}/release/{fname}"
        # TODO: make the verbosity switch be dependent from verbosity of the
        # script.
        subprocess.call(['wget', url, '-q', '-O',
                         os.path.join(self._tmp, fname)])

        with open(os.path.join(self._tmp, fname)) as fobj:
            for line in fobj.readlines():
                if self._img in line:
                    expected_sum = line.split(' ')[0]
                    break

        if not expected_sum:
            raise BoxError('Cannot find provided cloud image')

        if os.path.exists(os.path.join(CACHE_DIR, self._img)):
            cmd = 'sha256sum ' + os.path.join(CACHE_DIR, self._img)
            calulated_sum = subprocess.getoutput(cmd).split(' ')[0]
            return calulated_sum == expected_sum

        return False

    def cleanup(self):
        subprocess.call(['rm', '-fr', self._tmp])

    def _convert_to_raw(self):
        img_path = os.path.join(CACHE_DIR, self._img)
        raw_path = os.path.join(self._tmp, self._img + ".raw")
        if subprocess.call(['qemu-img', 'convert', '-O', 'raw',
                            img_path, raw_path]) != 0:
            raise BoxConvertionError(f'Cannot convert image {self._img} to '
                                     'RAW.')

    def _download_image(self):
        if self._checksum():
            print(f'Image already downloaded: {self._img}')
            return

        url = "https://cloud-images.ubuntu.com/releases/"
        url += f"{self.version}/release/"
        url += self._img
        print(f'Downloading image {self._img}')
        subprocess.call(['wget', '-q', url, '-O', os.path.join(CACHE_DIR,
                                                               self._img)])

        if not self._checksum():
            # TODO: make some retry mechanism?
            raise BoxSysCommandError('Checksum for downloaded image differ '
                                     'from expected.')
        else:
            print(f'Downloaded image {self._img}')


class IsoImage:
    def __init__(self, conf):
        self._tmp = tempfile.mkdtemp()
        self.hostname = conf.hostname
        self.ssh_key_path = conf.key

        if not self.ssh_key_path.endswith('.pub'):
            self.ssh_key_path += '.pub'
        if not os.path.exists(self.ssh_key_path):
            self.ssh_key_path = os.path.join(os.path.expanduser("~/.ssh"),
                                             self.ssh_key_path)
        if not os.path.exists(self.ssh_key_path):
            raise BoxNotFound(f'Cannot find ssh public key: {conf.key}')

        self.ud_tpl = conf.get_cloud_config_tpl()

    def get_generated_image(self):
        self._create_cloud_image()
        return os.path.join(self._tmp, CLOUD_IMAGE)

    def cleanup(self):
        subprocess.call(['rm', '-fr', self._tmp])

    def _create_cloud_image(self):
        # meta-data
        with open(os.path.join(self._tmp, 'meta-data'), 'w') as fobj:
            fobj.write(META_DATA_TPL
                       .substitute({'instance_id': str(uuid.uuid4()),
                                    'vmhostname': self.hostname}))

        # user-data
        with open(self.ssh_key_path) as fobj:
            ssh_pub_key = fobj.read().strip()

        with open(os.path.join(self._tmp, 'user-data'), 'w') as fobj:
            fobj.write(self.ud_tpl.substitute({'ssh_key': ssh_pub_key}))

        mkiso = 'mkisofs' if shutil.which('mkisofs') else 'genisoimage'

        # create ISO image
        if subprocess.call([mkiso, '-J', '-R', '-V', 'cidata', '-o',
                            os.path.join(self._tmp, CLOUD_IMAGE),
                            os.path.join(self._tmp, 'user-data'),
                            os.path.join(self._tmp, 'meta-data')]) != 0:
            raise BoxSysCommandError('Cannot create ISO image for config '
                                     'drive')


def vmcreate(args):
    conf = Config(args)
    vbox = VBoxManage(conf.name)
    if not vbox.create(conf.cpus, conf.memory, conf.port):
        return 10
    vbox.create_controller('IDE', 'ide')
    vbox.create_controller('SATA', 'sata')

    vbox.setextradata('key', conf.key)
    vbox.setextradata('hostname', conf.hostname)
    vbox.setextradata('version', conf.version)
    if conf.cloud_config:
        vbox.setextradata('cloud_config', conf.cloud_config)

    image = Image(vbox, conf.version)
    path_to_disk = image.convert_to_vdi(conf.name + '.vdi', conf.disk_size)

    iso = IsoImage(conf)
    path_to_iso = iso.get_generated_image()
    vbox.storageattach('SATA', 0, 'hdd', path_to_disk)
    vbox.storageattach('IDE', 1, 'dvddrive', path_to_iso)

    vbox.poweron()
    # give VBox some time to actually change the state of the VM before query
    time.sleep(3)

    # than, let's try to see if boostraping process has finished
    print('Waiting for cloud init to finish ', end='')
    try:
        while True:
            if vbox.vm_info['uuid'] in vbox.get_running_vms():
                print('.', end='')
                sys.stdout.flush()
                time.sleep(3)
            else:
                print(' done.')
                break
    except KeyboardInterrupt:
        print('\nIterrupted, cleaning up.')
        VBoxManage(args.name).destroy()
        return 1

    # dettach ISO image
    vbox.storageattach('IDE', 1, 'dvddrive', 'none')
    vbox.closemedium('dvd', path_to_iso)
    iso.cleanup()
    image.cleanup()
    vbox.poweron()
    print('You can access your VM by issuing:')
    print(f'ssh -p {args.port} -i {iso.ssh_key_path[:-4]} ubuntu@localhost')
    return 0


def vmdestroy(args):
    VBoxManage(args.name).destroy()
    return 0


def vmlist(args):
    VBoxManage().vmlist(args.running, args.long)
    return 0


def vmrebuild(args):
    vbox = VBoxManage(args.name)
    conf = Config(args, vbox)

    vbox.poweroff(silent=True)

    disk_path = vbox.get_disk_path()

    if not disk_path:
        # no disks, return
        return 1

    if not conf.disk_size:
        conf.disk_size = vbox.get_media_size(disk_path)

    vmdestroy(args)
    vmcreate(args)
    return 0


def shell_completion(args):
    sys.stdout.write(COMPLETIONS[args.shell])
    return 0


def main():
    parser = argparse.ArgumentParser(description="Automate deployment and "
                                     "maintenance of Ubuntu VMs using "
                                     "VirtualBox and Ubuntu cloud images")

    subparsers = parser.add_subparsers(help='supported commands')

    create = subparsers.add_parser('create', help='create and configure VM, '
                                   'create corresponding assets, config '
                                   'drive and run')
    create.set_defaults(func=vmcreate)
    create.add_argument('name', help='name of the VM')
    create.add_argument('-c', '--cpus', type=int, help="amount of CPUs to be "
                        "configured. Default 1.")
    create.add_argument('-d', '--disk-size', help="disk size to be expanded "
                        "to. By default to 10GB")
    create.add_argument('-k', '--key', help="SSH key to be add to the config "
                        "drive. Default ~/.ssh/id_rsa")
    create.add_argument('-m', '--memory', help="amount of memory in "
                        "Megabytes, default 2GB")
    create.add_argument('-n', '--hostname',
                        help="VM hostname. Default same as vm name")
    create.add_argument('-p', '--port', help="set ssh port for VM, default "
                        "2222")
    create.add_argument('-u', '--cloud-config',
                        help="Alternative user-data template filepath")
    create.add_argument('-v', '--version', help=f"Ubuntu server version. "
                        f"Default {UBUNTU_VERSION}")

    destroy = subparsers.add_parser('destroy', help='destroy VM')
    destroy.add_argument('name', help='name or UUID of the VM')
    destroy.set_defaults(func=vmdestroy)

    list_vms = subparsers.add_parser('list', help='list VMs')
    list_vms.add_argument('-l', '--long', action='store_true',
                          help='show detailed information '
                          'about VMs')
    list_vms.add_argument('-r', '--running', action='store_true',
                          help='show only running VMs')
    list_vms.set_defaults(func=vmlist)

    rebuild = subparsers.add_parser('rebuild', help='Rebuild VM, all options '
                                    'besides vm name are optional, and their '
                                    'values will be taken from vm definition.')
    rebuild.add_argument('name', help='name or UUID of the VM')
    rebuild.add_argument('-c', '--cpus', type=int,
                         help='amount of CPUs to be configured')
    rebuild.add_argument('-d', '--disk-size',
                         help='disk size to be expanded to')
    rebuild.add_argument('-k', '--key',
                         help='SSH key to be add to the config drive')
    rebuild.add_argument('-m', '--memory', help='amount of memory in '
                         'Megabytes')
    rebuild.add_argument('-n', '--hostname', help="set VM hostname")
    rebuild.add_argument('-p', '--port', help="set ssh port for VM")
    rebuild.add_argument('-u', '--cloud-config',
                         help="Alternative user-data template filepath")
    rebuild.add_argument('-v', '--version', help='Ubuntu server version')
    rebuild.set_defaults(func=vmrebuild)

    completion = subparsers.add_parser('completion', help='generate shell '
                                       'completion')
    completion.add_argument('shell', choices=['bash'],
                            help="pick shell to generate completions for")
    completion.set_defaults(func=shell_completion)

    args = parser.parse_args()

    if hasattr(args, 'func'):
        return args.func(args)

    parser.print_help()
    parser.exit()


if __name__ == '__main__':
    sys.exit(main())
