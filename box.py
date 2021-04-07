#!/usr/bin/env python

import argparse
import os
import subprocess
import tempfile


class VMCreate:
    CLOUD_IMAGE = "ci.iso"
    CLOUD_INIT_FINISHED_CMD = "test /var/lib/cloud/instance/boot-finished"

    def __init__(self, args):
        self.vm_name = args.name
        self.cpus = args.cpus
        self.memory = args.memory
        self.disk_size = args.disk_size
        self.ubuntu_version = args.version
        self._temp_path = None
        self._disk_img = self.vm_name + '.vdi'
        self._tmp = None

    def run(self):
        try:
            self._prepare_temp()
            self._download_image()
            # self._convert_and_resize()
        finally:
            self._cleanup()

    def _prepare_temp(self):
        self._tmp = tempfile.mkdtemp()

    def _download_image(self):
        url = "https://cloud-images.ubuntu.com/releases/"
        url += f"{self.ubuntu_version}/release/"
        img = f"ubuntu-{self.ubuntu_version}-server-cloudimg-amd64.img"
        url += img
        print(url)

        subprocess.call(['wget', url, '-O', os.path.join(self._tmp, img)])

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
    create.add_argument('-d', '--disk-size', default=20480, type=int,
                        help="disk size to be expanded to. By default to 20GB")
    create.add_argument('-v', '--version', default="18.04",
                        help="Ubuntu server version. Default 18.04")

    completion = subparsers.add_parser('completion')
    completion.add_argument('shell', choices=['bash'],
                            help="pick shell to generate completions for")

    args = parser.parse_args()

    return args.func(args)
    try:
        # __import__('ipdb').set_trace()
        return args.func(args)
    except AttributeError:
        parser.print_help()
        parser.exit()


if __name__ == '__main__':
    main()
