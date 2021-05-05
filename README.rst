======
box.py
======

Box.py is a simple automation tool meant to run Ubuntu cloud image on top of
VirtualBox.

What it does is simply download official cloud image for Ubuntu server, set up
VM, tweak it up and do the initial pre-configuration using generated config
drive.

I've wrote this little tool just to not click myself to death using web browser
for downloading cloud images, and going through VirtualBox GUI (or figuring out
weird named options for ``vboxmanage`` ;P)


Perhaps other distros would be supported in the future.


Requirements
------------

- Python 3.x
  - `pyyaml`_
- Virtualbox (obviously)
- ``mkisofs`` or ``genisoimage`` command for generating iso image
- ``wget`` command for fetching images
- ``sha256sum`` command for checksum check
- ``qemu-img`` from *qemu-utils* package command for converting between images
  formats


How to run it
-------------

First, make sure you fulfill the requirements, than you can issue:

.. code:: shell-session

   $ alias boxpy='python /path/to/box.py'

or simply link it somewhere in the path:

.. code:: shell-session

   $ ln -s /path/to/box.py ~/bin/boxpy
   $ chmod +x ~/bin/boxpy

and now you can issue some command. There are four command for simple managing
VMs, maybe some other will be available in the future. Who knows.

For your convenience there is a bash completion for each command, so you can
use it ad-hoc, or place on your ``.bashrc`` or whatever:

.. code:: shell-session

   $ source <(boxpy completion bash)

currently there are four commands available:

- ``list`` - for quickly listing running VMs
- ``destroy`` - that is probably obvious one
- ``create`` and ``rebuild``

The latter two accepts several options besides required vm name. You can
examine it by using ``--help``.

What is more interesting though, is the fact, that you can pass your own
`cloud-init`_ yaml file, so that VM can be provisioned in easy way.

Default user-script looks as follows:

.. code:: yaml

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

It is really simple, and use ``string.Template`` for exchanging token
``$ssh_key`` with default, or provided public key, so that you will be able to
log in into the VM using that key. Section ``power_state`` is used internally
for making sure the cloud-init finish up and the VM will be started again.

What is more interesting is the fact, that you could use whatever cloud-init
accepts, and a special section, for keeping configuration, so that you don't
need to provide all the option every time you boot up similar VM. For example:

.. code:: yaml

   packages:
     - jq
     - silversearcher-ag
     - tmux
     - vim-nox
   runcmd:
     - [su, -, ubuntu, -c, "echo 'set nocompatible' > .vimrc"]
   boxpy_data:
     ssh_key: vm
     cpus: 4
     memory: 4GB
     disk-size: 20GB

Contents of the user script will be merged with the default one, so expect,
that user ``ubuntu`` will be there, and magically you'll be able to connect to
the machine using ssh.

Providing file with this content using ``--cloud-config``, will build a VM with
4 CPUs, 4GB of RAM, expand Ubuntu-server image to 20GB (it'll be dynamically
allocated VDI image, so it will not swallow all 20 gigs of space) and pass the
``vm`` ssh key, which will be looked in ``~/.ssh`` directory, if path to the
key is not provided.

Moreover, there will be some tools installed and simple vim config
initialized, just to make you an idea, what could be done with it.


License
-------

This work is licensed under GPL-3.


.. _pyyaml: https://github.com/yaml/pyyaml
.. _cloud-init: https://cloudinit.readthedocs.io
