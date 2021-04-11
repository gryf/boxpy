#!/usr/bin/env python

import argparse
import os
import shutil
import string
import subprocess
import sys
import tempfile
import time
import uuid
import xml


CACHE_DIR = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
CLOUD_IMAGE = "ci.iso"
META_DATA_TPL = string.Template('''\
instance-id: $instance_id
local-hostname: $vmhostname
''')
UBUNTU_VERSION = '20.04'
USER_DATA_TPL = string.Template('''\
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
''')


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

    def create(self, cpus, memory):
        self.uuid = None

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
                            '--natpf1', 'guestssh,tcp,,2222,,22']) != 0:
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
    def __init__(self, hostname, ssh_key, vbox=None):
        self._tmp = tempfile.mkdtemp()
        self.vbox = vbox
        self.hostname = hostname
        self.ssh_key_path = ssh_key

        if not self.ssh_key_path.endswith('.pub'):
            self.ssh_key_path += '.pub'
        if not os.path.exists(self.ssh_key_path):
            self.ssh_key_path = os.path.join(os.path.expanduser("~/.ssh"),
                                             self.ssh_key_path)
        if not os.path.exists(self.ssh_key_path):
            raise BoxNotFound(f'Cannot find default ssh public key: '
                              f'{self.ssh_key_path}')

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
            fobj.write(USER_DATA_TPL.substitute({'ssh_key': ssh_pub_key}))

        mkiso = 'mkisofs' if shutil.which('mkisofs') else 'genisoimage'

        # create ISO image
        if subprocess.call([mkiso, '-J', '-R', '-V', 'cidata', '-o',
                            os.path.join(self._tmp, CLOUD_IMAGE),
                            os.path.join(self._tmp, 'user-data'),
                            os.path.join(self._tmp, 'meta-data')]) != 0:
            raise BoxSysCommandError('Cannot create ISO image for config '
                                     'drive')


def vmcreate(args):
    vbox = VBoxManage(args.name)
    vbox.create(args.cpus, args.memory)
    vbox.create_controller('IDE', 'ide')
    vbox.create_controller('SATA', 'sata')

    image = Image(vbox, args.version)
    path_to_disk = image.convert_to_vdi(args.name + '.vdi', args.disk_size)

    iso = IsoImage(args.hostname, args.key)
    path_to_iso = iso.get_generated_image()
    vbox.storageattach('SATA', 0, 'hdd', path_to_disk)
    vbox.storageattach('IDE', 1, 'dvddrive', path_to_iso)

    vbox.poweron()
    # give VBox some time to actually change the state of the VM before query
    time.sleep(3)

    # than, let's try to see if boostraping process has finished
    print('Waiting for cloud init to finish')
    while True:
        if vbox.vm_info['uuid'] in vbox.get_running_vms():
            time.sleep(3)
        else:
            print('Done')
            break
    # dettach ISO image
    vbox.storageattach('IDE', 1, 'dvddrive', 'none')
    vbox.closemedium('dvd', path_to_iso)
    iso.cleanup()
    image.cleanup()
    vbox.poweron()
    print('You can access your VM by issuing:')
    print(f'ssh -p 2222 -i {iso.ssh_key_path[:-4]} ubuntu@localhost')


def vmdestroy(args):
    VBoxManage(args.name).destroy()


def vmlist(args):
    VBoxManage().vmlist(args.running, args.long)


def vmrebuild(args):
    vbox = VBoxManage(args.name)

    vbox.poweroff(silent=True)

    disk_path = vbox.get_disk_path()

    if not disk_path:
        # no disks, return
        return 1

    vm_info = vbox.get_vm_info()

    args.cpus = args.cpus or vm_info['cpus']
    args.memory = args.memory or vm_info['memory']

    if not args.disk_size:
        args.disk_size = vbox.get_media_size(disk_path)

    vmdestroy(args)
    vmcreate(args)


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
    create.add_argument('-c', '--cpus', default=6, type=int,
                        help="amount of CPUs to be configured. Default 6.")
    create.add_argument('-d', '--disk-size', default=32768, type=int,
                        help="disk size to be expanded to. By default to 32GB")
    create.add_argument('-k', '--key',
                        default=os.path.expanduser("~/.ssh/id_rsa"),
                        help="SSH key to be add to the config drive. Default "
                        "~/.ssh/id_rsa")
    create.add_argument('-m', '--memory', default=12288, type=int,
                        help="amount of memory in Megabytes, default 12GB")
    create.add_argument('-n', '--hostname', default="ubuntu",
                        help="VM hostname. Default ubuntu")
    create.add_argument('-v', '--version', default=UBUNTU_VERSION,
                        help=f"Ubuntu server version. Default "
                        f"{UBUNTU_VERSION}")

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

    rebuild = subparsers.add_parser('rebuild', help='Rebuild VM')
    rebuild.add_argument('name', help='name or UUID of the VM')
    rebuild.add_argument('-c', '--cpus', type=int,
                         help="amount of CPUs to be configured. If not "
                         "provided, it will be the same as in rebuilding VM.")
    rebuild.add_argument('-d', '--disk-size',
                         help="disk size to be expanded to. If not provided, "
                         "it will be the same as the existing image.")
    rebuild.add_argument('-k', '--key',
                         default=os.path.expanduser("~/.ssh/id_rsa"),
                         help="SSH key to be add to the config drive. Default "
                         "~/.ssh/id_rsa")
    rebuild.add_argument('-m', '--memory', type=int,
                         help="amount of memory in Megabytes, if not provided "
                         "it will be taken form rebuilding machine.")
    rebuild.add_argument('-n', '--hostname', default="ubuntu",
                         help="VM hostname. Default ubuntu")
    rebuild.add_argument('-v', '--version', default=UBUNTU_VERSION,
                         help=f"Ubuntu server version. Default "
                         f"{UBUNTU_VERSION}")
    rebuild.set_defaults(func=vmrebuild)


    args = parser.parse_args()

    try:
        return args.func(args)
    except AttributeError:
        parser.print_help()
        parser.exit()


if __name__ == '__main__':
    main()
