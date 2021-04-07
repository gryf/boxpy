#!/usr/bin/env python

import argparse
import os
import subprocess
import tempfile
import sys


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
    CLOUD_INIT_FINISHED_CMD = "test /var/lib/cloud/instance/boot-finished"
    CACHE_DIR = os.environ.get('XDG_CACHE_HOME',
                               os.path.expanduser('~/.cache'))

    def __init__(self, args):
        self.vm_name = args.name
        self.cpus = args.cpus
        self.memory = args.memory
        self.disk_size = args.disk_size
        self.ubuntu_version = args.version
        self._img = f"ubuntu-{self.ubuntu_version}-server-cloudimg-amd64.img"
        self._temp_path = None
        self._disk_img = self.vm_name + '.vdi'
        self._tmp = None
        self._vm_base_path = None

    def run(self):
        try:
            self._prepare_temp()
            self._download_image()
            self._convert_and_resize()
            self._create_and_setup_vm()
        finally:
            self._cleanup()

    def _create_and_setup_vm(self):
        if subprocess.call(['vboxmanage', 'createvm', '--name', self.vm_name,
                            '--register']) != 0:
            raise OSError(f'Cannot create VM "{self.vm_name}".')
        if subprocess.call(['vboxmanage', 'modifyvm', self.vm_name,
                            '--memory', str(self.memory),
                            '--cpus', str(self.cpus),
                            '--boot1', 'disk',
                            '--acpi', 'on',
                            '--audio', 'none',
                            '--nic1', 'nat',
                            '--natpf1', 'guestssh,tcp,,2222,,22']) != 0:
            raise OSError(f'Cannot modify VM "{self.vm_name}".')
        out = subprocess.check_output(['vboxmanage', 'showvminfo',
                                       self.vm_name],
                                      encoding=sys.getdefaultencoding())
        path = None
        for line in out.split('\n'):
            if line.startswith('Config file:'):
                path = os.path.dirname(line.split('Config file:').strip())

        if not path:
            raise AttributeError(f'There is something wrong doing VM '
                                 f'"{self.vm_name}" creation and registration')

        self._vm_base_path = path

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

        if subprocess.call(['vboxmanage', 'modifyhd', vdi_path, '--resize',
                            str(self.disk_size)]) != 0:
            raise AttributeError(f'Cannot resize image {self._disk_img} to '
                                 '{self.disk_size}.')

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
