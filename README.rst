======
box.py
======

Box.py is a simple automation tool meant to run Ubuntu cloud image on top of
VirtualBox.

What it does is simply download official cloud image for Ubuntu server, set up
VM, tweak it up and do the initial pre-configuration using generated config
drive.

Perhaps other distros would be supported int the future.


Requirements
------------

- Python 3.x
- Virtualbox (obviously)
- ``mkisofs`` or ``genisoimage`` command for generating iso image
- ``wget`` command for fetching images
- ``sha256sum`` command for checksum check
- ``qemu-img`` from *qemu-utils* package command for converting between images
  formats
