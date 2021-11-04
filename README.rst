======
box.py
======

Box.py is a simple automation tool meant to run Ubuntu, Fedora or Centos Stream
cloud images on top of VirtualBox.

What it does is simply download official cloud image, set up VM, tweak it up
and do the initial pre-configuration using generated config drive.

I've wrote this little tool just to not click myself to death using web browser
for downloading cloud images, and going through VirtualBox GUI (or figuring out
weird named options for ``vboxmanage`` ;P)


Requirements
------------

- Python >=3.7

  - `pyyaml`_
  - `requests`_

- Virtualbox (obviously)
- ``mkisofs`` or ``genisoimage`` command for generating ISO image
- ``wget`` command for fetching images
- ``sha256sum`` command for checksum check
- ``qemu-img`` from *qemu-utils* package command for converting between images
  formats


How to run it
-------------

First, make sure you fulfill the requirements; either by using packages from
your operating system, or by using virtualenv for Python requirements, i.e.:

.. code:: shell-session

   $ python -m virtualenv .venv
   $ . .venv/bin/activate
   (.venv) $ pip install requirements.txt

then you can issue:

.. code:: shell-session

   $ alias boxpy='python /path/to/box.py'

or simply link it somewhere in the path:

.. code:: shell-session

   $ ln -s /path/to/box.py ~/bin/boxpy
   $ chmod +x ~/bin/boxpy

and now you can issue some command. For example, to spin up a VM with Ubuntu
18.04 with one CPU, 2GB of memory and 10GB of disk:

.. code:: shell-session

   $ boxpy create --version 18.04 myvm

note, that Ubuntu is default distribution you don't need to specify
``--distro`` nor ``--version`` it will pick up latest LTS version. Now, let's
recreate it with 20.04:

.. code:: shell-session

   $ boxpy rebuild --version 20.04 myvm

or recreate it with Fedora and add additional CPU:

.. code:: shell-session

   $ boxpy rebuild --distro fedora --version 34 --cpu 2 myvm

now, let's connect to the VM using either ssh command, which is printed out at
as last ``boxpy`` output line, or simply by using ssh boxpy command:

.. code:: shell-session

   $ boxpy ssh myvm

For your convenience there is a bash completion for each command, so you can
use it ad-hoc, or place on your ``.bashrc`` or whatever:

.. code:: shell-session

   $ source <(boxpy completion bash)

Currently, following commands are available:

- ``list`` - for quickly listing all/running VMs
- ``info`` - to get summary about VM
- ``destroy`` - that is probably obvious one
- ``create`` - create new VM
- ``rebuild`` - recreate specified VM
- ``ssh`` - connect to the VM using ssh
- ``completion`` - as described above

All of the commands have a range of options, and can be examined by using
``--help`` option.

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

It is really simple, and use ``string.Template`` for exchanging token
``$ssh_key`` with default, or provided public key, so that you will be able to
log in into the VM using that key.

Note, that you need to be extra careful regarding ``$`` sign. As explained
above ``$ssh_key`` will be used as a "variable" for the template to substitute
with the real value of public key. Every ``$`` sign, especially in
``write_files.contents``, should be escaped with another dollar, so the ``$``
will become a ``$$``. Perhaps I'll change the approach for writing ssh key,
since that's a little bit annoying.

For that reason, a little improvement has been done, so now its possible to
pass filenames to the custom config, instead of filling up
``write_files.contents``:

.. code:: yaml

   write_files:
     - path: /opt/somefile.txt
       permissions: '0644'
       filename: /path/to/local/file.txt

or

.. code:: yaml

   write_files:
     - path: /opt/somefile.txt
       permissions: '0644'
       url: https://some.url/content

during processing this file, boxpy will look for ``filename`` or ``url`` keys
in the yaml file for the ``write_files`` sections, and it will remove that key,
read the file and put its contents under ``content`` key. What is more
important, that will be done after template processing, so there will be no
interference for possible ``$`` characters.

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
     key: vm
     cpus: 4
     memory: 4GB
     disk_size: 20GB

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

You can find some real world examples of the yaml cloud-init files that I use
in examples directory.

There is special section ``boxpy_data``, where you can place all the
configuration for the VM. Keys are the same as in ``create`` command options.
There is one additional key ``advanced`` which for now can be used for
configuration additional NIC for virtual machine, i.e:

.. code:: yaml

   …
   boxpy_data:
     advanced:
       nic2: intnet


License
-------

This work is licensed under GPL-3.


.. _pyyaml: https://github.com/yaml/pyyaml
.. _cloud-init: https://cloudinit.readthedocs.io
.. _requests: https://docs.python-requests.org
