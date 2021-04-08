#!/usr/bin/env python

import argparse
import os
import subprocess
import tempfile
import sys
META_DATA_TPL = string.Template('''\
instance-id: $instance_id
local-hostname: $vmhostname
''')

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


class VMCreate:
    """
    Create vbox VM of Ubuntu server from cloud image with the following steps:
        - grab the image, unless it exists in XDG_CACHE_HOME
        - convert it to raw, than to VDI, remove raw
        - resize it to the right size
        - create cloud ISO image with some basic bootstrap
        - create and register VM definition
        - tweak its params
        - move disk image to the Machine directory
        - attach disk and iso images to it
        - run and wait for initial bootstrap, than acpishutdown
        - detach iso image and remove it
    """
    CLOUD_IMAGE = "ci.iso"
    CACHE_DIR = os.environ.get('XDG_CACHE_HOME',
                               os.path.expanduser('~/.cache'))

    def __init__(self, args):
        self.vm_name = args.name
        self.cpus = args.cpus
        self.memory = args.memory
        self.disk_size = args.disk_size
        self.ubuntu_version = args.version
        self.hostname = args.hostname
        self.ssh_key_path = args.key

        if not self.ssh_key_path.endswith('.pub'):
            self.ssh_key_path += '.pub'
        if not os.path.exists(self.ssh_key_path):
            raise AttributeError(f'Cannot find default ssh public key: '
                                 f'{self.ssh_key_path}')

        self._img = f"ubuntu-{self.ubuntu_version}-server-cloudimg-amd64.img"
        self._temp_path = None
        self._disk_img = self.vm_name + '.vdi'
        self._tmp = None
        self._vm_base_path = None
        self._vm_uuid = None

    def run(self):
        try:
            self._prepare_temp()
            self._download_image()
            self._convert_and_resize()
            self._create_and_setup_vm()
            self._create_cloud_image()
            self._attach_images_to_vm()
            self._power_on_and_wait_for_ci_finish()
        finally:
            self._cleanup()

    def _attach_images_to_vm(self):
        vdi_path = os.path.join(self._tmp, self._disk_img)

        # couple of commands for changing the disk size, creating controllers
        # and attaching disk and config drive to the vm.
        # NOTE: modifymedium will register the disk image in Virtual Media
        # Manager, while convertfromraw not.
        commands = [['vboxmanage', 'modifymedium', 'disk', vdi_path,
                     '--resize', str(self.disk_size), '--move',
                     os.path.join(self._vm_base_path, self._disk_img)],
                    ['vboxmanage', 'storagectl', self._vm_uuid, '--name',
                     'IDE', '--add', 'ide'],
                    ['vboxmanage', 'storagectl', self._vm_uuid, '--name',
                     'SATA', '--add', 'sata'],
                    ['vboxmanage', 'storageattach', self._vm_uuid,
                     '--storagectl', 'SATA',
                     '--port', '0',
                     '--device', '0',
                     '--type', 'hdd',
                     '--medium',
                     os.path.join(self._vm_base_path, self._disk_img)],
                    ['vboxmanage', 'storageattach', self._vm_uuid,
                     '--storagectl', 'IDE',
                     '--port', '1',
                     '--device', '0',
                     '--type', 'dvddrive',
                     '--medium',
                     os.path.join(self._tmp, self.CLOUD_IMAGE)]]
        for cmd in commands:
            if subprocess.call(cmd) != 0:
                cmd = ' '.join(cmd)
                raise AttributeError(f'command: {cmd} has failed')

    def _create_and_setup_vm(self):
        out = subprocess.check_output(['vboxmanage', 'createvm', '--name',
                                       self.vm_name, '--register'],
                                      encoding=sys.getdefaultencoding())
        for line in out.split('\n'):
            print(line)
            if line.startswith('UUID:'):
                self._vm_uuid = line.split('UUID:')[1].strip()

        if not self._vm_uuid:
            raise OSError(f'Cannot create VM "{self.vm_name}".')

        if subprocess.call(['vboxmanage', 'modifyvm', self._vm_uuid,
                            '--memory', str(self.memory),
                            '--cpus', str(self.cpus),
                            '--boot1', 'disk',
                            '--acpi', 'on',
                            '--audio', 'none',
                            '--nic1', 'nat',
                            '--natpf1', 'guestssh,tcp,,2222,,22']) != 0:
            raise OSError(f'Cannot modify VM "{self._vm_uuid}".')
        out = subprocess.check_output(['vboxmanage', 'showvminfo',
                                       self._vm_uuid],
                                      encoding=sys.getdefaultencoding())
        path = None
        for line in out.split('\n'):
            if line.startswith('Config file:'):
                path = os.path.dirname(line.split('Config file:')[1].strip())

        if not path:
            raise AttributeError(f'There is something wrong doing VM '
                                 f'"{self.vm_name}" creation and registration')

        self._vm_base_path = path

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

        # create ISO image
        if subprocess.call(['mkisofs', '-J', '-R', '-V', 'cidata', '-o',
                            os.path.join(self._tmp, self.CLOUD_IMAGE),
                            os.path.join(self._tmp, 'user-data'),
                            os.path.join(self._tmp, 'meta-data')]) != 0:
            raise AttributeError('Cannot create ISO image for config drive')

    def _prepare_temp(self):
        self._tmp = tempfile.mkdtemp()

    def _checksum(self):
        expected_sum = None
        fname = 'SHA256SUMS'
        url = "https://cloud-images.ubuntu.com/releases/"
        url += f"{self.ubuntu_version}/release/{fname}"
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
            raise AttributeError('Cannot find provided cloud image')

        if os.path.exists(os.path.join(self.CACHE_DIR, self._img)):
            cmd = 'sha256sum ' + os.path.join(self.CACHE_DIR, self._img)
            calulated_sum = subprocess.getoutput(cmd).split(' ')[0]
            return calulated_sum == expected_sum

        return False

    def _convert_to_raw(self):
        img_path = os.path.join(self.CACHE_DIR, self._img)
        raw_path = os.path.join(self._tmp, self._img + ".raw")
        if subprocess.call(['qemu-img', 'convert', '-O', 'raw',
                            img_path, raw_path]) != 0:
            raise AttributeError(f'Cannot convert image {self._img} to RAW.')

    def _convert_and_resize(self):
        self._convert_to_raw()
        raw_path = os.path.join(self._tmp, self._img + ".raw")
        vdi_path = os.path.join(self._tmp, self._disk_img)
        if subprocess.call(["vboxmanage", "convertfromraw", raw_path,
                            vdi_path]) != 0:
            raise AttributeError(f'Cannot convert image {self._disk_img} '
                                 'to VDI.')
        os.unlink(raw_path)

    def _download_image(self):
        if self._checksum():
            print(f'Image already downloaded: {self._img}')
            return

        url = "https://cloud-images.ubuntu.com/releases/"
        url += f"{self.ubuntu_version}/release/"
        img = f"ubuntu-{self.ubuntu_version}-server-cloudimg-amd64.img"
        url += img
        print(f'Downloading image {self._img}')
        subprocess.call(['wget', '-q', url, '-O',
                         os.path.join(self.CACHE_DIR, self._img)])

        if not self._checksum():
            # TODO: make some retry mechanism?
            raise AttributeError('Checksum for downloaded image differ from'
                                 ' expected')
        else:
            print(f'Downloaded image {self._img}')

    def _cleanup(self):
        subprocess.call(['vboxmanage', 'closemedium', 'dvd',
                         os.path.join(self._tmp, self.CLOUD_IMAGE)])
        subprocess.call(['rm', '-fr', self._tmp])


def _create(args):
    return VMCreate(args).run()


def main():
    parser = argparse.ArgumentParser(description="Automate deployment and "
                                     "maintenance of Ubuntu VMs using "
                                     "VirtualBox and Ubuntu cloud images")
    subparsers = parser.add_subparsers(help='supported commands')
    create = subparsers.add_parser('create')
    create.add_argument('name')
    create.set_defaults(func=_create)
    create.add_argument('-m', '--memory', default=12288, type=int,
                        help="amount of memory in Megabytes, default 12GB")
    create.add_argument('-c', '--cpus', default=6, type=int,
                        help="amount of CPUs to be configured. Default 6.")
    create.add_argument('-d', '--disk-size', default=32768, type=int,
                        help="disk size to be expanded to. By default to 32GB")
    create.add_argument('-v', '--version', default="18.04",
                        help="Ubuntu server version. Default 18.04")
    create.add_argument('-n', '--hostname', default="ubuntu",
                        help="VM hostname. Default ubuntu")
    create.add_argument('-k', '--key',
                        default=os.path.expanduser("~/.ssh/id_rsa"),
                        help="SSH key to be add to the config drive. Default "
                        "~/.ssh/id_rsa")
    completion = subparsers.add_parser('completion')
    completion.add_argument('shell', choices=['bash'],
                            help="pick shell to generate completions for")

    args = parser.parse_args()

    try:
        return args.func(args)
    except AttributeError:
        parser.print_help()
        parser.exit()


if __name__ == '__main__':
    main()
