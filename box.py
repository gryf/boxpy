#!/usr/bin/env python

import argparse
import collections.abc
import os
import random
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
FEDORA_RELEASE_MAP = {'32': '1.6', '33': '1.2', '34': '1.2'}
META_DATA_TPL = string.Template('''\
instance-id: $instance_id
local-hostname: $vmhostname
''')
USER_DATA = '''\
#cloud-config
users:
  - default
  - name: ${username}
    ssh_authorized_keys:
      - $ssh_key
    chpasswd: { expire: False }
    gecos: ${realname}
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

    opts="create destroy rebuild list completion ssh"
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
            items=(--cpus --disk-size --distro --key --memory --hostname
                --port --config --version)
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
                    --config)
                        COMPREPLY=( $(compgen -f -- ${cur}) )
                        compopt -o plusdirs
                        ;;
                    --key)
                        _ssh_identityfile
                        ;;
                    --distro)
                        COMPREPLY=( $(compgen -W "ubuntu fedora" -- ${cur}) )
                        ;;
                    --*)
                        COMPREPLY=( )
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
        ssh)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp vms
            fi
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

    result = None

    if size.isnumeric():
        result = str(size)

    if size.lower().endswith('m') and size[:-1].isnumeric():
        result = str(size[:-1])

    if size.lower().endswith('g') and size[:-1].isnumeric():
        result = str(int(size[:-1]) * 1024)

    if size.lower().endswith('mb') and size[:-2].isnumeric():
        result = str(size[:-2])

    if size.lower().endswith('gb') and size[:-2].isnumeric():
        result = str(int(size[:-2]) * 1024)

    return result


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
    ATTRS = ('cpus', 'config', 'disk_size', 'distro', 'hostname', 'key',
             'memory', 'name', 'port', 'version')

    def __init__(self, args, vbox=None):
        self.advanced = None
        self.distro = None
        self.cpus = None
        self.disk_size = None
        self.hostname = None
        self.key = None
        self.memory = None
        self.name = args.name  # this one is not stored anywhere
        self.port = None       # at least is not even tried to be retrieved
        self.version = None
        self._conf = {}

        # set defaults stored in hard coded yaml
        self._set_defaults()

        # look at VM metadata, and gather known attributes, and update it
        # accordingly
        vm_info = vbox.get_vm_info() if vbox else {}
        for attr in self.ATTRS:
            if not vm_info.get(attr):
                continue
            setattr(self, attr, vm_info[attr])

        # next, grab the cloud config file
        if 'config' in args and args.config:
            self.user_data = os.path.abspath(args.config)
        else:
            self.user_data = vm_info.get('user_data')

        # combine it with the defaults, set attributes by boxpy_data
        # definition, if found
        self._combine_cc()

        # than, override all of the attributes with provided arguments from
        # the command line
        vm_info = vbox.get_vm_info() if vbox else {}
        for attr in self.ATTRS:
            val = getattr(args, attr, None) or vm_info.get(attr)
            if not val:
                continue
            setattr(self, attr, str(val))

        if not self.version and self.distro:
            self.version = DISTROS[self.distro]['default_version']

        # finally, figure out host name
        self.hostname = self.hostname or self._normalize_name()
        self._set_ssh_key_path()

    def get_cloud_config(self):
        # 1. process template
        tpl = string.Template(yaml.safe_dump(self._conf))

        with open(self.ssh_key_path) as fobj:
            ssh_pub_key = fobj.read().strip()

        conf = yaml.safe_load(tpl.substitute(
            {'ssh_key': ssh_pub_key,
             'username': DISTROS[self.distro]['username'],
             'realname': DISTROS[self.distro]['realname']}))

        # 2. process 'write_files' items, so that things with '$' will not go
        # in a way for templates.
        if conf.get('write_files'):
            new_list = []
            for file_data in conf['write_files']:
                fname = file_data.get('filename')
                if not fname:
                    new_list.append(file_data)
                    continue

                fname = os.path.expanduser(os.path.expandvars(fname))
                if not os.path.exists(fname):
                    print(f"WARNING: file '{file_data['filename']}' doesn't "
                          f"exists.")
                    continue

                with open(fname) as fobj:
                    file_data['content'] = fobj.read()
                del file_data['filename']
                new_list.append(file_data)

            conf['write_files'] = new_list

        # 3. finally dump it again.
        return "#cloud-config\n" + yaml.safe_dump(conf)

    def _set_ssh_key_path(self):
        self.ssh_key_path = self.key

        if not self.ssh_key_path.endswith('.pub'):
            self.ssh_key_path += '.pub'
        if not os.path.exists(self.ssh_key_path):
            self.ssh_key_path = os.path.join(os.path
                                             .expanduser(self.ssh_key_path))
        if not os.path.exists(self.ssh_key_path):
            self.ssh_key_path = os.path.join(os.path.expanduser("~/.ssh"),
                                             self.ssh_key_path)
        if not os.path.exists(self.ssh_key_path):
            raise BoxNotFound(f'Cannot find ssh public key: {self.key}')

    def _set_defaults(self):
        conf = yaml.safe_load(USER_DATA)

        # update attributes with default values
        for key, val in conf['boxpy_data'].items():
            setattr(self, key, str(val))

    def _normalize_name(self):
        name = self.name.replace(' ', '-')
        name = name.encode('ascii', errors='ignore')
        name = name.decode('utf-8')
        return ''.join(x for x in name if x.isalnum() or x == '-')

    def _combine_cc(self):
        conf = yaml.safe_load(USER_DATA)

        # read user custom cloud config (if present) and update config dict
        if self.user_data:
            if os.path.exists(self.user_data):
                with open(self.user_data) as fobj:
                    custom_conf = yaml.safe_load(fobj)
                    conf = self._update(conf, custom_conf)
            else:
                print(f"WARNING: Provided user_data: {self.user_data} doesn't"
                      " exists.")

        # update the attributes with data from read user cloud config
        for key, val in conf.get('boxpy_data', {}).items():
            if not val:
                continue
            setattr(self, key, str(val))

        # remove boxpy_data since it will be not needed on the guest side
        if conf.get('boxpy_data'):
            if conf['boxpy_data'].get('advanced'):
                self.advanced = conf['boxpy_data']['advanced']
            del conf['boxpy_data']

        self._conf = conf

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

                return line.split(' ')[0].strip()

    def get_vm_info(self):
        try:
            out = subprocess.check_output(['vboxmanage', 'showvminfo',
                                           self.name_or_uuid],
                                          encoding=sys.getdefaultencoding(),
                                          stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return {}

        self.vm_info = {}

        for line in out.split('\n'):
            if line.startswith('Config file:'):
                self.vm_info['config_file'] = line.split('Config '
                                                         'file:')[1].strip()
                break

        dom = xml.dom.minidom.parse(self.vm_info['config_file'])
        gebtn = dom.getElementsByTagName

        self.vm_info['cpus'] = gebtn('CPU')[0].getAttribute('count')
        self.vm_info['uuid'] = gebtn('Machine')[0].getAttribute('uuid')[1:-1]
        self.vm_info['memory'] = gebtn('Memory')[0].getAttribute('RAMSize')

        for extradata in gebtn('ExtraDataItem'):
            key = extradata.getAttribute('name')
            val = extradata.getAttribute('value')
            self.vm_info[key] = val

        if len(gebtn('Forwarding')):
            fw = gebtn('Forwarding')[0].getAttribute('hostport')
            self.vm_info['port'] = fw

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

    def create(self, cpus, memory, port=None):
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

        if not port:
            port = self._find_unused_port()

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

    def add_nic(self, nic, kind):
        if subprocess.call(['vboxmanage', 'modifyvm', self.name_or_uuid,
                            f'--{nic}', kind]) != 0:
            raise BoxVBoxFailure(f'Cannot modify VM "{self.name_or_uuid}".')

    def is_port_in_use(self, port):
        used_ports = self._get_defined_ports()
        for vmname, vmport in used_ports.items():
            if vmport == port:
                return vmname
        return False

    def _find_unused_port(self):
        used_ports = self._get_defined_ports()

        while True:
            port = random.randint(2000, 2999)
            if port not in used_ports.values():
                self.vm_info['port'] = port
                return port

    def _get_defined_ports(self):
        self.get_vm_info()
        try:
            out = subprocess.check_output(['vboxmanage', 'list', 'vms'],
                                          encoding=sys.getdefaultencoding(),
                                          stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return {}

        used_ports = {}
        for line in out.split('\n'):
            if not line:
                continue
            vm_name = line.split('"')[1]
            vm_uuid = line.split('{')[1][:-1]
            if self.vm_info.get('uuid') and self.vm_info['uuid'] == vm_uuid:
                continue

            try:
                info = (subprocess
                        .check_output(['vboxmanage', 'showvminfo', vm_uuid],
                                      encoding=sys.getdefaultencoding(),
                                      stderr=subprocess.DEVNULL))
            except subprocess.CalledProcessError:
                continue

            for line in info.split('\n'):
                if line.startswith('Config file:'):
                    config = line.split('Config ' 'file:')[1].strip()

            dom = xml.dom.minidom.parse(config)
            gebtn = dom.getElementsByTagName

            if len(gebtn('Forwarding')):
                used_ports[vm_name] = (gebtn('Forwarding')[0]
                                       .getAttribute('hostport'))
        return used_ports

    def _get_vm_config(self):
        if self.vm_info.get('config_file'):
            return self.vm_info['config_file']

        self.get_vm_info()

        if self.vm_info.get('config_file'):
            return self.vm_info['config_file']


class Image:
    URL = ""
    IMG = ""

    def __init__(self, vbox, version, arch, release):
        self.vbox = vbox
        self._tmp = tempfile.mkdtemp(prefix='boxpy_')
        self._img_fname = None

    def convert_to_vdi(self, disk_img, size):
        self._download_image()
        self._convert_to_raw()
        raw_path = os.path.join(self._tmp, self._img_fname + ".raw")
        vdi_path = os.path.join(self._tmp, disk_img)
        self.vbox.convertfromraw(raw_path, vdi_path)
        return self.vbox.move_and_resize_image(vdi_path, disk_img, size)

    def cleanup(self):
        subprocess.call(['rm', '-fr', self._tmp])

    def _convert_to_raw(self):
        img_path = os.path.join(CACHE_DIR, self._img_fname)
        raw_path = os.path.join(self._tmp, self._img_fname + ".raw")
        if subprocess.call(['qemu-img', 'convert', '-O', 'raw',
                            img_path, raw_path]) != 0:
            raise BoxConvertionError(f'Cannot convert image {self._img_fname} '
                                     'to RAW.')

    def _download_image(self):
        raise NotImplementedError()


class Ubuntu(Image):
    URL = "https://cloud-images.ubuntu.com/releases/%s/release/%s"
    IMG = "ubuntu-%s-server-cloudimg-%s.img"

    def __init__(self, vbox, version, arch, release):
        super().__init__(vbox, version, arch, release)
        self._img_fname = self.IMG % (version, arch)
        self._img_url = self.URL % (version, self._img_fname)
        self._checksum_file = 'SHA256SUMS'
        self._checksum_url = self.URL % (version, self._checksum_file)

    def _checksum(self):
        """
        Get and check checkusm for downloaded image. Return True if the
        checksum is correct, False otherwise.
        """
        if not os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            return False

        expected_sum = None
        fname = os.path.join(self._tmp, self._checksum_file)
        # TODO: make the verbosity switch be dependent from verbosity of the
        # script.
        subprocess.call(['wget', self._checksum_url, '-q', '-O', fname])

        with open(fname) as fobj:
            for line in fobj.readlines():
                if self._img_fname in line:
                    expected_sum = line.split(' ')[0]
                    break

        if not expected_sum:
            raise BoxError('Cannot find provided cloud image')

        if os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            cmd = 'sha256sum ' + os.path.join(CACHE_DIR, self._img_fname)
            calulated_sum = subprocess.getoutput(cmd).split(' ')[0]
            return calulated_sum == expected_sum

        return False

    def _download_image(self):
        if self._checksum():
            print(f'Image already downloaded: {self._img_fname}')
            return

        fname = os.path.join(CACHE_DIR, self._img_fname)
        print(f'Downloading image {self._img_fname}')
        subprocess.call(['wget', '-q', self._img_url, '-O', fname])

        if not self._checksum():
            # TODO: make some retry mechanism?
            raise BoxSysCommandError('Checksum for downloaded image differ '
                                     'from expected.')
        else:
            print(f'Downloaded image {self._img_fname}')


class Fedora(Image):
    URL = ("https://download.fedoraproject.org/pub/fedora/linux/releases/%s/"
           "Cloud/%s/images/%s")
    IMG = "Fedora-Cloud-Base-%s-%s.%s.qcow2"
    CHKS = "Fedora-Cloud-%s-%s-%s-CHECKSUM"

    def __init__(self, vbox, version, arch, release):
        super().__init__(vbox, version, arch, release)
        self._img_fname = self.IMG % (version, release, arch)
        self._img_url = self.URL % (version, arch, self._img_fname)
        self._checksum_file = self.CHKS % (version, release, arch)
        self._checksum_url = self.URL % (version, arch, self._checksum_file)

    def _checksum(self):
        """
        Get and check checkusm for downloaded image. Return True if the
        checksum is correct, False otherwise.
        """
        if not os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            return False

        expected_sum = None
        # TODO: make the verbosity switch be dependent from verbosity of the
        # script.
        fname = os.path.join(self._tmp, self._checksum_file)
        subprocess.call(['wget', self._checksum_url, '-q', '-O', fname])

        with open(fname) as fobj:
            for line in fobj.readlines():
                if line.startswith('#'):
                    continue
                if self._img_fname in line:
                    expected_sum = line.split('=')[1].strip()
                    break

        if not expected_sum:
            raise BoxError('Cannot find provided cloud image')

        if os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            cmd = 'sha256sum ' + os.path.join(CACHE_DIR, self._img_fname)
            calulated_sum = subprocess.getoutput(cmd).split(' ')[0]
            return calulated_sum == expected_sum

        return False

    def _download_image(self):
        if self._checksum():
            print(f'Image already downloaded: {self._img_fname}')
            return

        fname = os.path.join(CACHE_DIR, self._img_fname)
        subprocess.call(['wget', '-q', self._img_url, '-O', fname])

        if not self._checksum():
            # TODO: make some retry mechanism?
            raise BoxSysCommandError('Checksum for downloaded image differ '
                                     'from expected.')
        else:
            print(f'Downloaded image {self._img_fname}')


DISTROS = {'ubuntu': {'username': 'ubuntu',
                      'realname': 'ubuntu',
                      'img_class': Ubuntu,
                      'amd64': 'amd64',
                      'default_version': '20.04'},
           'fedora': {'username': 'fedora',
                      'realname': 'fedora',
                      'img_class': Fedora,
                      'amd64': 'x86_64',
                      'default_version': '34'}}


def get_image(vbox, version, image='ubuntu', arch='amd64'):
    release = None
    if image == 'fedora':
        release = FEDORA_RELEASE_MAP[version]
    return DISTROS[image]['img_class'](vbox, version, DISTROS[image]['amd64'],
                                       release)


class IsoImage:
    def __init__(self, conf):
        self._tmp = tempfile.mkdtemp()
        self.hostname = conf.hostname
        self._cloud_conf = conf.get_cloud_config()

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
        with open(os.path.join(self._tmp, 'user-data'), 'w') as fobj:
            fobj.write(self._cloud_conf)

        mkiso = 'mkisofs' if shutil.which('mkisofs') else 'genisoimage'

        # create ISO image
        if subprocess.call([mkiso, '-J', '-R', '-V', 'cidata', '-o',
                            os.path.join(self._tmp, CLOUD_IMAGE),
                            os.path.join(self._tmp, 'user-data'),
                            os.path.join(self._tmp, 'meta-data')]) != 0:
            raise BoxSysCommandError('Cannot create ISO image for config '
                                     'drive')


def vmcreate(args, conf=None):

    if not args.distro:
        args.distro = 'ubuntu'

    if not conf:
        conf = Config(args)

    vbox = VBoxManage(conf.name)
    if conf.port:
        used = vbox.is_port_in_use(conf.port)
        if used:
            print(f'Error: Port {conf.port} is in use by "{used}" VM.')
            return 11

    if not vbox.create(conf.cpus, conf.memory, conf.port):
        return 10

    vbox.create_controller('IDE', 'ide')
    vbox.create_controller('SATA', 'sata')

    vbox.setextradata('distro', conf.distro)
    vbox.setextradata('key', conf.key)
    vbox.setextradata('hostname', conf.hostname)
    vbox.setextradata('version', conf.version)
    if conf.user_data:
        vbox.setextradata('user_data', conf.user_data)

    image = get_image(vbox, conf.version, image=args.distro)
    path_to_disk = image.convert_to_vdi(conf.name + '.vdi', conf.disk_size)

    iso = IsoImage(conf)
    path_to_iso = iso.get_generated_image()
    vbox.storageattach('SATA', 0, 'hdd', path_to_disk)
    vbox.storageattach('IDE', 1, 'dvddrive', path_to_iso)

    # advanced options, currnetly pretty hardcoded
    if conf.advanced:
        for key, val in conf.advanced.items():
            if key.startswith('nic'):
                vbox.add_nic(key, val)

    # start the VM and wait for cloud-init to finish
    vbox.poweron()
    # give VBox some time to actually change the state of the VM before query
    time.sleep(3)

    def _cleanup(vbox, iso, image, path_to_iso):
        time.sleep(1)  # wait a bit, for VM shutdown to complete
        vbox.storageattach('IDE', 1, 'dvddrive', 'none')
        vbox.closemedium('dvd', path_to_iso)
        iso.cleanup()
        image.cleanup()

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
        vbox.poweroff(silent=True)
        _cleanup(vbox, iso, image, path_to_iso)
        vbox.destroy()
        return 1

    # dettach ISO image
    _cleanup(vbox, iso, image, path_to_iso)
    vbox.poweron()

    # reread config to update fields
    conf = Config(args, vbox)
    print('You can access your VM by issuing:')
    print(f'ssh -p {conf.port} -i {conf.ssh_key_path[:-4]} '
          f'{DISTROS[conf.distro]["username"]}@localhost')
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
    vmcreate(args, conf)
    return 0


def shell_completion(args):
    sys.stdout.write(COMPLETIONS[args.shell])
    return 0


def connect(args):
    vbox = VBoxManage(args.name)
    conf = Config(args, vbox)
    try:
        subprocess.call(['ssh', '-o', 'StrictHostKeyChecking=no',
                         '-o', 'UserKnownHostsFile=/dev/null',
                         '-i', conf.ssh_key_path[:-4],
                         f'ssh://{DISTROS[conf.distro]["username"]}'
                         f'@localhost:{conf.port}'])
    except subprocess.CalledProcessError:
        return None

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
    create.add_argument('-c', '--config',
                        help="Alternative user-data template filepath")
    create.add_argument('-d', '--distro', help="Image name. 'ubuntu' is "
                        "default")
    create.add_argument('-k', '--key', help="SSH key to be add to the config "
                        "drive. Default ~/.ssh/id_rsa")
    create.add_argument('-m', '--memory', help="amount of memory in "
                        "Megabytes, default 2GB")
    create.add_argument('-n', '--hostname',
                        help="VM hostname. Default same as vm name")
    create.add_argument('-p', '--port', help="set ssh port for VM, default "
                        "random port from range 2000-2999")
    create.add_argument('-s', '--disk-size', help="disk size to be expanded "
                        "to. By default to 10GB")
    create.add_argument('-u', '--cpus', type=int, help="amount of CPUs to be "
                        "configured. Default 1.")
    create.add_argument('-v', '--version', help=f"Ubuntu server version. "
                        f"Default {DISTROS['ubuntu']['default_version']}")

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
    rebuild.add_argument('-c', '--config',
                         help="Alternative user-data template filepath")
    rebuild.add_argument('-d', '--distro', help="Image name.")
    rebuild.add_argument('-k', '--key',
                         help='SSH key to be add to the config drive')
    rebuild.add_argument('-m', '--memory', help='amount of memory in '
                         'Megabytes')
    rebuild.add_argument('-n', '--hostname', help="set VM hostname")
    rebuild.add_argument('-p', '--port', help="set ssh port for VM")
    rebuild.add_argument('-s', '--disk-size',
                         help='disk size to be expanded to')
    rebuild.add_argument('-u', '--cpus', type=int,
                         help='amount of CPUs to be configured')
    rebuild.add_argument('-v', '--version', help='Ubuntu server version')
    rebuild.set_defaults(func=vmrebuild)

    completion = subparsers.add_parser('completion', help='generate shell '
                                       'completion')
    completion.add_argument('shell', choices=['bash'],
                            help="pick shell to generate completions for")
    completion.set_defaults(func=shell_completion)

    ssh = subparsers.add_parser('ssh', help='Connect to the machine via SSH')
    ssh.add_argument('name', help='name or UUID of the VM')
    ssh.set_defaults(func=connect)

    args = parser.parse_args()

    if hasattr(args, 'func'):
        return args.func(args)

    parser.print_help()
    parser.exit()


if __name__ == '__main__':
    sys.exit(main())
