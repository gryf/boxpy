#!/usr/bin/env python

import argparse
import collections.abc
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import uuid
import xml.dom.minidom

import requests
import yaml


__version__ = "1.3"

CACHE_DIR = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
CLOUD_IMAGE = "ci.iso"
FEDORA_RELEASE_MAP = {'32': '1.6', '33': '1.2', '34': '1.2'}
TYPE_MAP = {'HardDisk': 'disk', 'DVD': 'dvd', 'Floppy': 'floppy'}
DISTRO_MAP = {'ubuntu': 'Ubuntu', 'fedora': 'Fedora',
              'centos': 'Centos Stream'}
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
no_ssh_fingerprints: true
ssh:
  emit_keys_to_console: false
boxpy_data:
  cpus: 1
  disk_size: 6144
  key: ~/.ssh/id_rsa
  memory: 1024
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

    opts="create destroy rebuild info list completion ssh start stop"
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
            items=(--cpus --disable-nested --disk-size --default-user --distro
                --forwarding --image --key --memory --hostname --port --config
                --version --type)
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
                        COMPREPLY=( $(compgen -W "ubuntu fedora centos" \
                                -- ${cur}) )
                        ;;
                    --type)
                        COMPREPLY=( $(compgen -W "gui headless sdl separate" \
                            -- ${cur}) )
                        ;;
                    --*)
                        COMPREPLY=( )
                        ;;
                esac
            fi

            ;;
        info)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp vms
            fi
            ;;
        destroy)
            _vms_comp vms
            _get_excluded_items "${COMPREPLY[@]}"
            COMPREPLY=( $(compgen -W "$result" -- ${cur}) )
            ;;
        list)
            items=(--long --running --run-by-boxpy)
            _get_excluded_items "${items[@]}"
            COMPREPLY=( $(compgen -W "$result" -- ${cur}) )
            ;;
        ssh)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp runningvms
            fi
            ;;
        start)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp vms
            fi
            ;;
        stop)
            if [[ ${prev} == ${cmd} ]]; then
                _vms_comp runningvms
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


class Run:
    """
    Helper class on subprocess.run()
    command is a list with command and its params to execute
    """
    def __init__(self, command, capture_output=True):
        result = subprocess.run(command, encoding='utf-8',
                                capture_output=capture_output)
        if result.stdout:
            LOG.debug2(result.stdout)
        if result.stderr:
            LOG.debug2(result.stderr)

        self.returncode = result.returncode
        self.stdout = result.stdout.strip() if result.stdout else ''
        self.stderr = result.stderr.strip() if result.stderr else ''


class BoxError(Exception):
    pass


class BoxNotFound(BoxError):
    pass


class BoxVBoxFailure(BoxError):
    pass


class BoxConfError(BoxError):
    pass


class FakeLogger:
    """
    print based "logger" class. I like to use 'end' parameter of print
    function to get pseudo activity/progress thing.

    There are 5 levels (similar to just as in original logger) of logging:

    debug2 = 0
    debug = 1
    details = 2
    info = 3
    header = 4
    warning = 5
    fatal = 6
    """

    def __init__(self, colors=False):
        """
        Initialize named logger
        """
        self._level = 3
        self._colors = colors

    def debug2(self, msg, *args, end='\n'):
        if self._level > 0:
            return
        self._print_msg(msg, 0, end, *args)

    def debug(self, msg, *args, end='\n'):
        if self._level > 1:
            return
        self._print_msg(msg, 1, end, *args)

    def details(self, msg, *args, end='\n'):
        if self._level > 2:
            return
        self._print_msg(msg, 2, end, *args)

    def info(self, msg, *args, end='\n'):
        if self._level > 3:
            return
        self._print_msg(msg, 3, end, *args)

    def header(self, msg, *args, end='\n'):
        if self._level > 4:
            return
        self._print_msg(msg, 4, end, *args)

    def warning(self, msg, *args, end='\n'):
        if self._level > 5:
            return
        self._print_msg(msg, 5, end, *args)

    def fatal(self, msg, *args, end='\n'):
        if self._level > 6:
            return
        self._print_msg(msg, 6, end, *args)

    def _print_msg(self, msg, level, end, *args):
        reset = "\x1b[0m"
        colors = {0: "\x1b[90m",
                  1: "\x1b[36m",
                  2: "\x1b[94m",
                  3: "\x1b[0m",
                  4: "\x1b[92m",
                  5: "\x1b[93m",
                  6: "\x1b[91m"}

        message = msg
        if args:
            message = msg % args

        if self._colors:
            message = colors[level] + message + reset

        print(message, end=end)

    def set_verbose(self, verbose_level, quiet_level):
        """
        Change verbosity level. Default level is warning.
        """

        if quiet_level:
            self._level += quiet_level

        if verbose_level:
            self._level -= verbose_level


class Config:
    ATTRS = ('cpus', 'config', 'creator', 'disable_nested', 'disk_size',
             'distro', 'default_user', 'forwarding', 'hostname', 'image',
             'key', 'memory', 'name', 'port', 'version', 'username')

    def __init__(self, args, vbox=None):
        self.advanced = None
        self.distro = None
        self.default_user = None
        self.cpus = None
        self.creator = None
        self.disable_nested = 'False'
        self.disk_size = None
        self.forwarding = {}
        self.hostname = None
        self.image = None
        self.key = None
        self.memory = None
        self.name = args.name  # this one is not stored anywhere
        self.port = None       # at least is not even tried to be retrieved
        self.version = None
        self.username = None
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
        for attr in self.ATTRS:
            val = getattr(args, attr, None)
            if not val:
                continue
            if attr == 'forwarding':
                for ports in val:
                    key, value = ports.split(':')
                    self.forwarding[key] = value
                continue
            setattr(self, attr, str(val))

        # sort out case, where there is image/default-user provided
        if self.image:
            self._update_distros_with_custom_image()

        # set distribution and version if not specified by user
        if not self.distro:
            self.distro = 'ubuntu'

        if not self.version:
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
                content = None
                fname = file_data.get('filename')
                url = file_data.get('url')
                if not any((fname, url)):
                    new_list.append(file_data)
                    continue

                if fname:
                    key = 'filename'
                    content = self._read_filename(fname)
                    if content is None:
                        LOG.warning("File '%s' doesn't exists", fname)
                        continue

                if url:
                    key = 'url'
                    code, content = self._get_url(url)
                    if content is None:
                        LOG.warning("Getting url '%s' returns %s code",
                                    url, code)
                        continue

                if content:
                    file_data['content'] = content
                    del file_data[key]
                    new_list.append(file_data)

            conf['write_files'] = new_list

        # 3. finally dump it again.
        return "#cloud-config\n" + yaml.safe_dump(conf)

    def _get_url(self, url):
        response = requests.get(url)
        if response.status_code != 200:
            return response.status_code, None
        return response.status_code, response.text

    def _read_filename(self, fname):
        fullpath = os.path.expanduser(os.path.expandvars(fname))
        if not os.path.exists(fullpath):
            return

        with open(fname) as fobj:
            return fobj.read()

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
            raise BoxConfError(f'Cannot find ssh public key: {self.key}')

    def _set_defaults(self):
        conf = yaml.safe_load(USER_DATA)

        # update attributes with default values
        for key, val in conf['boxpy_data'].items():
            setattr(self, key, str(val))

        self._conf = conf

    def _normalize_name(self):
        name = self.name.replace(' ', '-')
        name = name.encode('ascii', errors='ignore')
        name = name.decode('utf-8')
        return ''.join(x for x in name if x.isalnum() or x == '-')

    def _combine_cc(self):
        """
        Read user custom cloud config (if present) and update config dict
        """
        if not self.user_data:
            LOG.debug("No user data has been provided")
            return

        if not os.path.exists(self.user_data):
            LOG.warning("Provided user_data: '%s' doesn't exists",
                        self.user_data)
            return

        conf = yaml.safe_load(USER_DATA)

        with open(self.user_data) as fobj:
            custom_conf = yaml.safe_load(fobj)
            conf = self._update(conf, custom_conf)

        # update the attributes with data from read user cloud config
        for key, val in conf.get('boxpy_data', {}).items():
            if not val:
                continue
            if key == 'forwarding':
                for ports in val:
                    k, v = ports.split(':')
                    self.forwarding[k] = v
                continue
            setattr(self, key, str(val))

        # remove boxpy_data since it will be not needed on the guest side
        if conf.get('boxpy_data'):
            if conf['boxpy_data'].get('advanced'):
                self.advanced = conf['boxpy_data']['advanced']
            del conf['boxpy_data']

        self._conf = conf

    def _update_distros_with_custom_image(self):
        self.image = os.path.abspath(self.image)
        self.distro = 'custom'
        if not self.username:
            self.username = self.default_user
        DISTROS['custom'] = {'username': self.default_user,
                             'realname': 'custom os',
                             'img_class': CustomImage,
                             'amd64': 'x86_64',
                             'image': self.image,
                             'default_version': '0'}

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
        self.uuid = None
        self.running = False

    def get_vm_base_path(self):
        path = self._get_vm_config()
        if not path:
            return

        return os.path.dirname(path)

    def get_disk_path(self):
        path = self._get_vm_config()
        if not path:
            LOG.warning('Configuration for "%s" not found', self.name_or_uuid)
            return

        dom = xml.dom.minidom.parse(path)
        if len(dom.getElementsByTagName('HardDisk')) != 1:
            # don't know what to do with multiple discs
            raise BoxError()

        disk = dom.getElementsByTagName('HardDisk')[0]
        location = disk.getAttribute('location')
        if location.startswith('/'):
            disk_path = location
        else:
            disk_path = os.path.join(self.get_vm_base_path(), location)

        return disk_path

    def get_media_size(self, media_path, type_='disk'):
        out = Run(['vboxmanage', 'showmediuminfo', type_, media_path]).stdout

        for line in out.split('\n'):
            if line.startswith('Capacity:'):
                line = line.split('Capacity:')[1].strip()

                if line.isnumeric():
                    return line

                return line.split(' ')[0].strip()

    def get_vm_info(self):
        out = Run(['vboxmanage', 'showvminfo', self.name_or_uuid])
        if out.returncode != 0:
            return {}

        self.vm_info = {}

        for line in out.stdout.split('\n'):
            if line.startswith('Config file:'):
                self.vm_info['config_file'] = line.split('Config '
                                                         'file:')[1].strip()

            if line.startswith('State:'):
                self.running = line.split(':')[1].strip().startswith('running')
                break

        dom = xml.dom.minidom.parse(self.vm_info['config_file'])
        gebtn = dom.getElementsByTagName

        self.vm_info['cpus'] = gebtn('CPU')[0].getAttribute('count') or '1'
        self.vm_info['uuid'] = gebtn('Machine')[0].getAttribute('uuid')[1:-1]
        self.vm_info['memory'] = gebtn('Memory')[0].getAttribute('RAMSize')

        for extradata in gebtn('ExtraDataItem'):
            key = extradata.getAttribute('name')
            val = extradata.getAttribute('value')
            self.vm_info[key] = val

        images = []
        for storage in gebtn('StorageController'):
            for adev in storage.getElementsByTagName('AttachedDevice'):
                if not adev.getElementsByTagName('Image'):
                    continue
                image = adev.getElementsByTagName('Image')[0]
                type_ = adev.getAttribute('type')
                uuid_ = image.getAttribute('uuid')[1:-1]
                images.append({'type': type_, 'uuid': uuid_})

        self.vm_info['media'] = images

        # get ssh port
        if len(gebtn('Forwarding')):
            for rule in gebtn('Forwarding'):
                if rule.getAttribute('name') == 'boxpyssh':
                    self.vm_info['port'] = rule.getAttribute('hostport')
                else:
                    if not self.vm_info.get('forwarding'):
                        self.vm_info['forwarding'] = {}
                    hostport = rule.getAttribute('hostport')
                    guestport = rule.getAttribute('guestport')
                    self.vm_info['forwarding'][hostport] = guestport

        return self.vm_info

    def poweroff(self):
        Run(['vboxmanage', 'controlvm', self.name_or_uuid, 'poweroff'])

    def acpipowerbutton(self):
        Run(['vboxmanage', 'controlvm', self.name_or_uuid, 'acpipowerbutton'])

    def vmlist(self, only_running=False, long_list=False, only_boxpy=False):
        subcommand = 'runningvms' if only_running else 'vms'
        machines = {}
        for line in Run(['vboxmanage', 'list', subcommand]).stdout.split('\n'):
            if not line:
                continue
            _, name, vm_uuid = line.split('"')
            vm_uuid = vm_uuid.split('{')[1][:-1]
            info = line
            if only_boxpy:
                info_ = VBoxManage(vm_uuid).get_vm_info()
                if info_.get('creator') != 'boxpy':
                    continue
            if long_list:
                info = "\n".join(Run(['vboxmanage', 'showvminfo',
                                      name]).stdout.split('\n'))
            machines[name] = info
        return machines

    def get_running_vms(self):
        return Run(['vboxmanage', 'list', 'runningvms']).stdout

    def destroy(self):
        self.get_vm_info()
        if not self.vm_info:
            LOG.fatal("Cannot remove VM \"%s\" - it doesn't exist",
                      self.name_or_uuid)
            return 4

        self.poweroff()
        time.sleep(1)  # wait a bit, for VM shutdown to complete
        # detach cloud image.
        self.storageattach('IDE', 1, 'dvddrive', 'none')
        if self.vm_info.get('iso_path'):
            self.closemedium('dvd', self.vm_info['iso_path'])
        if Run(['vboxmanage', 'unregistervm', self.name_or_uuid,
                '--delete']).returncode != 0:
            LOG.fatal('Removing VM "%s" failed', self.name_or_uuid)
            return 7

    def create(self, conf):
        memory = convert_to_mega(conf.memory)

        out = Run(['vboxmanage', 'createvm', '--name', self.name_or_uuid,
                   '--register'])
        if out.returncode != 0:
            LOG.fatal('Failed to create VM:\n%s', out.stderr)
            return None

        if out.stdout.startswith('WARNING:'):
            LOG.fatal('Created crippled VM:\n%s\nFix the issue with '
                      'VirtualBox, remove the dead VM and start over.',
                      out.stdout)
            return None

        for line in out.stdout.split('\n'):
            if line.startswith('UUID:'):
                self.uuid = line.split('UUID:')[1].strip()

        if not self.uuid:
            raise BoxVBoxFailure(f'Cannot create VM "{self.name_or_uuid}".')

        port = conf.port if conf.port else self._find_unused_port()

        cmd = ['vboxmanage', 'modifyvm', self.name_or_uuid,
               '--memory', str(memory),
               '--cpus', str(conf.cpus),
               '--boot1', 'disk',
               '--acpi', 'on',
               '--audio', 'none',
               '--nic1', 'nat',
               '--natpf1', f'boxpyssh,tcp,,{port},,22']
        for count, (hostport, vmport) in enumerate(conf.forwarding.items(),
                                                   start=1):
            cmd.extend(['--natpf1', f'custom-pf-{count},tcp,,{hostport},'
                        f',{vmport}'])

        if Run(cmd).returncode != 0:
            LOG.fatal(f'Cannot modify VM "{self.name_or_uuid}"')
            raise BoxVBoxFailure()

        if conf.disable_nested == 'False':
            if Run(['vboxmanage', 'modifyvm', self.name_or_uuid,
                    '--nested-hw-virt', 'on']).returncode != 0:
                LOG.fatal(f'Cannot set nested virtualization for VM '
                          f'"{self.name_or_uuid}"')
                raise BoxVBoxFailure()

        return self.uuid

    def convertfromraw(self, src, dst):
        LOG.info('Converting image "%s" to VDI', src)
        res = Run(["vboxmanage", "convertfromraw", src, dst])
        os.unlink(src)
        if res.returncode != 0:
            LOG.fatal('Cannot convert image to VDI:\n%s', res.stderr)
            return False
        return True

    def closemedium(self, type_, mediumpath):
        res = Run(['vboxmanage', 'closemedium', type_, mediumpath])
        if res.returncode != 0:
            LOG.fatal('Failed close medium %s:\n%s', mediumpath, res.stderr)
            return False
        return True

    def create_controller(self, name, type_):
        res = Run(['vboxmanage', 'storagectl', self.name_or_uuid, '--name',
                   name, '--add', type_])
        if res.returncode != 0:
            LOG.fatal('Adding controller %s has failed:\n%s', type_,
                      res.stderr)
            return False
        return True

    def move_and_resize_image(self, src, dst, size):
        fullpath = os.path.join(self.get_vm_base_path(), dst)
        size = convert_to_mega(size)

        if Run(['vboxmanage', 'modifymedium', 'disk', src, '--resize',
                str(size), '--move', fullpath]).returncode != 0:
            LOG.fatal('Resizing and moving image %s has failed', dst)
            raise BoxVBoxFailure()
        return fullpath

    def storageattach(self, controller_name, port, type_, image):
        if Run(['vboxmanage', 'storageattach', self.name_or_uuid,
                '--storagectl', controller_name,
                '--port', str(port),
                '--device', '0',
                '--type', type_,
                '--medium', image]).returncode != 0:
            if image == 'none':
                # detaching images from drive are nonfatal
                LOG.warning('Detaching image form %s on VM "%s" has failed',
                            controller_name, self.name_or_uuid)
            else:
                LOG.fatal('Attaching %s to VM "%s" has failed', image,
                          self.name_or_uuid)
            return False
        return True

    def poweron(self, type_='headless'):
        if Run(['vboxmanage', 'startvm', self.name_or_uuid, '--type',
                type_]).returncode != 0:
            LOG.fatal('Failed to start: %s', self.name_or_uuid)
            raise BoxVBoxFailure()

    def setextradata(self, key, val):
        res = Run(['vboxmanage', 'setextradata', self.name_or_uuid, key, val])
        if res.returncode != 0:
            LOG.fatal('Failed to set extra data: %s: %s\n%s', key, val,
                      res.stderr)
            return False
        return True

    def add_nic(self, nic, kind):
        if Run(['vboxmanage', 'modifyvm', self.name_or_uuid, f'--{nic}',
                kind]).returncode != 0:
            LOG.fatal('Cannot modify VM "%s"', self.name_or_uuid)
            raise BoxVBoxFailure()

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
        out = Run(['vboxmanage', 'list', 'vms'])
        if out.returncode != 0:
            return {}

        used_ports = {}
        for line in out.stdout.split('\n'):
            if not line:
                continue
            vm_name = line.split('"')[1]
            vm_uuid = line.split('{')[1][:-1]
            if self.vm_info.get('uuid') and self.vm_info['uuid'] == vm_uuid:
                continue

            info = Run(['vboxmanage', 'showvminfo', vm_uuid])
            if info.returncode != 0:
                continue

            for info_line in info.stdout.split('\n'):
                if info_line.startswith('Config file:'):
                    config = info_line.split('Config ' 'file:')[1].strip()

            dom = xml.dom.minidom.parse(config)
            gebtn = dom.getElementsByTagName

            if gebtn('Forwarding'):
                for rule in gebtn('Forwarding'):
                    used_ports[vm_name] = rule.getAttribute('hostport')
        return used_ports

    def _get_vm_config(self):
        if self.vm_info.get('config_file'):
            return self.vm_info['config_file']

        self.get_vm_info()
        return self.vm_info['config_file']


class Image:
    URL = ""
    IMG = ""

    def __init__(self, vbox, version, arch, release, fname=None):
        self.vbox = vbox
        self._tmp = tempfile.mkdtemp(prefix='boxpy_')
        self._img_fname = fname

    def convert_to_vdi(self, disk_img, size):
        LOG.info('Converting and resizing "%s", new size: %s', disk_img, size)
        if not self._download_image():
            return None
        if not self._convert_to_raw():
            return None
        raw_path = os.path.join(self._tmp, self._img_fname + ".raw")
        vdi_path = os.path.join(self._tmp, disk_img)
        if not self.vbox.convertfromraw(raw_path, vdi_path):
            return None
        return self.vbox.move_and_resize_image(vdi_path, disk_img, size)

    def cleanup(self):
        LOG.info('Image: Cleaning up temporary files from "%s"', self._tmp)
        Run(['rm', '-fr', self._tmp])

    def _convert_to_raw(self):
        LOG.info('Converting "%s" to RAW', self._img_fname)
        img_path = os.path.join(CACHE_DIR, self._img_fname)
        raw_path = os.path.join(self._tmp, self._img_fname + ".raw")
        if Run(['qemu-img', 'convert', '-O', 'raw', img_path,
                raw_path]).returncode != 0:
            LOG.fatal('Converting image %s to RAW failed', self._img_fname)
            return False
        return True

    def _checksum(self):
        """
        Get and check checkusm for downloaded image. Return True if the
        checksum is correct, False otherwise.
        """
        if not os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            LOG.debug('Image %s not downloaded yet', self._img_fname)
            return False

        LOG.info('Calculating checksum for "%s"', self._img_fname)
        fname = os.path.join(self._tmp, self._checksum_file)
        expected_sum = self._get_checksum(fname)

        if not expected_sum:
            LOG.fatal('Cannot find checksum for provided cloud image')
            return False

        if os.path.exists(os.path.join(CACHE_DIR, self._img_fname)):
            cmd = ['sha256sum', os.path.join(CACHE_DIR, self._img_fname)]
            calulated_sum = Run(cmd).stdout.split(' ')[0]
            LOG.details('Checksum for image: %s, expected: %s', calulated_sum,
                        expected_sum)
            return calulated_sum == expected_sum

        return False

    def _download_image(self):
        if self._checksum():
            LOG.details('Image already downloaded: %s', self._img_fname)
            return True

        fname = os.path.join(CACHE_DIR, self._img_fname)
        LOG.header('Downloading image %s from %s', self._img_fname,
                   self._img_url)
        Run(['wget', '-q', self._img_url, '-O', fname])

        if not self._checksum():
            # TODO: make some retry mechanism?
            LOG.fatal('Checksum for downloaded image differ from expected')
            return False

        LOG.header('Downloaded image %s', self._img_fname)
        return True

    def _get_checksum(self, fname):
        raise NotImplementedError()


class Ubuntu(Image):
    URL = "https://cloud-images.ubuntu.com/releases/%s/release/%s"
    IMG = "ubuntu-%s-server-cloudimg-%s.img"

    def __init__(self, vbox, version, arch, release, fname=None):
        super().__init__(vbox, version, arch, release)
        self._img_fname = self.IMG % (version, arch)
        self._img_url = self.URL % (version, self._img_fname)
        self._checksum_file = 'SHA256SUMS'
        self._checksum_url = self.URL % (version, self._checksum_file)

    def _get_checksum(self, fname):
        expected_sum = None
        Run(['wget', self._checksum_url, '-q', '-O', fname])
        with open(fname) as fobj:
            for line in fobj.readlines():
                if self._img_fname in line:
                    expected_sum = line.split(' ')[0]
                    break

        return expected_sum


class Fedora(Image):
    URL = ("https://download.fedoraproject.org/pub/fedora/linux/releases/%s/"
           "Cloud/%s/images/%s")
    IMG = "Fedora-Cloud-Base-%s-%s.%s.qcow2"
    CHKS = "Fedora-Cloud-%s-%s-%s-CHECKSUM"

    def __init__(self, vbox, version, arch, release, fname=None):
        super().__init__(vbox, version, arch, release)
        self._img_fname = self.IMG % (version, release, arch)
        self._img_url = self.URL % (version, arch, self._img_fname)
        self._checksum_file = self.CHKS % (version, release, arch)
        self._checksum_url = self.URL % (version, arch, self._checksum_file)

    def _get_checksum(self, fname):
        expected_sum = None
        Run(['wget', self._checksum_url, '-q', '-O', fname])

        with open(fname) as fobj:
            for line in fobj.readlines():
                if line.startswith('#'):
                    continue
                if self._img_fname in line:
                    expected_sum = line.split('=')[1].strip()
                    break
        return expected_sum


class CentosStream(Image):
    URL = "https://cloud.centos.org/centos/%s-stream/%s/images/%s"
    IMG = '.*(CentOS-Stream-GenericCloud-%s-[0-9]+.[0-9].%s.qcow2).*'
    CHKS = "CHECKSUM"

    def __init__(self, vbox, version, arch, release, fname=None):
        super().__init__(vbox, version, arch, release)
        self._checksum_file = '%s-centos-stream-%s-%s' % (self.CHKS, version,
                                                          arch)
        self._checksum_url = self.URL % (version, arch, self.CHKS)
        # there is assumption, that we always need latest relese for specific
        # version and architecture.
        self._img_fname = self._get_image_name(version, arch)
        self._img_url = self.URL % (version, arch, self._img_fname)

    def _get_image_name(self, version, arch):
        fname = os.path.join(self._tmp, self._checksum_file)
        Run(['wget', self._checksum_url, '-q', '-O', fname])

        pat = re.compile(self.IMG % (version, arch))

        images = []
        with open(fname) as fobj:
            for line in fobj.read().strip().split('\n'):
                line = line.strip()
                if line.startswith('#'):
                    continue
                match = pat.match(line)
                if match and match.groups():
                    images.append(match.groups()[0])

        Run(['rm', fname])
        images.reverse()
        if images:
            return images[0]

    def _get_checksum(self, fname):
        expected_sum = None
        Run(['wget', self._checksum_url, '-q', '-O', fname])

        with open(fname) as fobj:
            for line in fobj.readlines():
                if line.startswith('#'):
                    continue
                if self._img_fname in line:
                    expected_sum = line.split('=')[1].strip()
                    break
        return expected_sum


class CustomImage(Image):

    def _download_image(self):
        # just use provided image
        return True


DISTROS = {'ubuntu': {'username': 'ubuntu',
                      'realname': 'ubuntu',
                      'img_class': Ubuntu,
                      'amd64': 'amd64',
                      'default_version': '22.04'},
           'fedora': {'username': 'fedora',
                      'realname': 'fedora',
                      'img_class': Fedora,
                      'amd64': 'x86_64',
                      'default_version': '34'},
           'centos': {'username': 'centos',
                      'realname': 'centos',
                      'img_class': CentosStream,
                      'amd64': 'x86_64',
                      'default_version': '8'}}


def get_image_object(vbox, version, image='ubuntu', arch='amd64'):
    release = None
    if image == 'fedora':
        release = FEDORA_RELEASE_MAP[version]
    return DISTROS[image]['img_class'](vbox, version, DISTROS[image]['amd64'],
                                       release, DISTROS[image].get('image'))


class IsoImage:
    def __init__(self, conf):
        self._tmp = tempfile.mkdtemp(prefix='boxpy_')
        self.hostname = conf.hostname
        self._cloud_conf = conf.get_cloud_config()

    def get_generated_image(self):
        if not self._create_cloud_image():
            return None
        return os.path.join(self._tmp, CLOUD_IMAGE)

    def cleanup(self):
        LOG.info('IsoImage: Cleaning up temporary files from "%s"', self._tmp)
        Run(['rm', '-fr', self._tmp])

    def _create_cloud_image(self):
        # meta-data
        LOG.header('Creating ISO image with cloud config')

        with open(os.path.join(self._tmp, 'meta-data'), 'w') as fobj:
            fobj.write(META_DATA_TPL
                       .substitute({'instance_id': str(uuid.uuid4()),
                                    'vmhostname': self.hostname}))

        # user-data
        with open(os.path.join(self._tmp, 'user-data'), 'w') as fobj:
            fobj.write(self._cloud_conf)

        mkiso = 'mkisofs' if shutil.which('mkisofs') else 'genisoimage'

        # create ISO image
        if Run([mkiso, '-J', '-R', '-V', 'cidata', '-o',
                os.path.join(self._tmp, CLOUD_IMAGE),
                os.path.join(self._tmp, 'user-data'),
                os.path.join(self._tmp, 'meta-data')]).returncode != 0:
            LOG.fatal('Cannot create ISO image for config drive')
            return False
        return True


LOG = FakeLogger(colors=True)


def vmcreate(args, conf=None):

    if not conf:
        try:
            conf = Config(args)
        except BoxConfError as err:
            LOG.fatal(f'Configuration error: {err.args[0]}.')
            return 7
        except yaml.YAMLError:
            LOG.fatal(f'Cannot read or parse file `{args.config}` as YAML '
                      f'file')
            return 14
    LOG.header('Creating VM: %s', conf.name)

    vbox = VBoxManage(conf.name)
    if conf.port:
        LOG.info('Trying to use provided port: %s', conf.port)
        used = vbox.is_port_in_use(conf.port)
        if used:
            LOG.fatal('Error: Port %s is in use by VM "%s"', conf.port, used)
            return 1

    if not vbox.create(conf):
        return 2

    if not vbox.create_controller('IDE', 'ide'):
        return 3
    if not vbox.create_controller('SATA', 'sata'):
        return 4

    for key in ('distro', 'hostname', 'key', 'version', 'image', 'username'):
        if getattr(conf, key) is None:
            continue
        if not vbox.setextradata(key, getattr(conf, key)):
            return 5

    if conf.user_data:
        if not vbox.setextradata('user_data', conf.user_data):
            return 6

    if not vbox.setextradata('creator', 'boxpy'):
        return 13

    image = get_image_object(vbox, conf.version, image=conf.distro)
    path_to_disk = image.convert_to_vdi(conf.name + '.vdi', conf.disk_size)

    if not path_to_disk:
        return 21

    iso = IsoImage(conf)
    path_to_iso = iso.get_generated_image()
    if not path_to_iso:
        return 12
    vbox.setextradata('iso_path', path_to_iso)
    vbox.storageattach('SATA', 0, 'hdd', path_to_disk)
    vbox.storageattach('IDE', 1, 'dvddrive', path_to_iso)

    # advanced options, currnetly pretty hardcoded
    if conf.advanced:
        for key, val in conf.advanced.items():
            if key.startswith('nic'):
                vbox.add_nic(key, val)

    # start the VM and wait for cloud-init to finish
    vbox.poweron(args.type)
    # give VBox some time to actually change the state of the VM before query
    time.sleep(3)

    # than, let's try to see if boostraping process has finished
    LOG.info('Waiting for cloud init to finish ', end='')
    username = DISTROS[conf.distro]["username"]
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no',
           '-o', 'UserKnownHostsFile=/dev/null',
           '-o', 'ConnectTimeout=2',
           '-i', conf.ssh_key_path[:-4],
           f'ssh://{username}@localhost:{vbox.vm_info["port"]}',
           'sudo cloud-init status']
    try:
        while True:
            out = Run(cmd)
            LOG.debug('Out: %s', out.stdout)

            if (not out.stdout) or ('status' in out.stdout and
                                    'running' in out.stdout):
                LOG.info('.', end='')
                sys.stdout.flush()
                if 'Permission denied (publickey)' in out.stderr:
                    if conf.username and conf.username != username:
                        username = conf.username
                        vbox.setextradata('username', username)
                        cmd[9] = (f'ssh://{username}'
                                  f'@localhost:{vbox.vm_info["port"]}')
                        continue
                    raise PermissionError(f'There is an issue with accessing '
                                          f'VM with ssh for user {username}. '
                                          f'Check output in debug mode.')
                time.sleep(3)
                continue

            LOG.info(' done')
            break
        out = out.stdout.split(':')[1].strip()
        if out != 'done':
            cmd = cmd[:-1]
            cmd.append('cloud-init status -l')
            LOG.warning('Cloud init finished with "%s" status:\n%s', out,
                        Run(cmd).stdout)

    except PermissionError:
        LOG.info('\n')
        iso.cleanup()
        image.cleanup()
        vbox.destroy()
        raise
    except KeyboardInterrupt:
        LOG.warning('\nInterrupted, cleaning up')
        iso.cleanup()
        image.cleanup()
        vbox.destroy()
        return 1

    # cleanup
    iso.cleanup()
    image.cleanup()

    # reread config to update fields
    conf = Config(args, vbox)
    username = DISTROS[conf.distro]["username"]
    LOG.info('You can access your VM by issuing:')
    if conf.username and conf.username != username:
        LOG.info(f'ssh -p {conf.port} -i {conf.ssh_key_path[:-4]} '
                 f'{conf.username}@localhost')
    else:
        LOG.info(f'ssh -p {conf.port} -i {conf.ssh_key_path[:-4]} '
                 f'{username}@localhost')
    LOG.info('or simply:')
    LOG.info(f'boxpy ssh {conf.name}')
    return 0


def vmdestroy(args):
    if isinstance(args.name, list):
        vm_names = args.name
    else:
        vm_names = [args.name]

    for name in vm_names:
        vbox = VBoxManage(name)
        if not vbox.get_vm_info():
            LOG.fatal(f'Cannot remove VM "{name}" - it doesn\'t exists.')
            return 18
        LOG.header('Removing VM: %s', name)
        res = VBoxManage(name).destroy()
        if res:
            return res
    return 0


def vmlist(args):
    vms = VBoxManage().vmlist(args.running, args.long, args.run_by_boxpy)

    if args.running:
        if args.run_by_boxpy:
            LOG.header('Running VMs created by boxpy:')
        else:
            LOG.header('Running VMs:')
    else:
        if args.run_by_boxpy:
            LOG.header('All VMs created by boxpy:')
        else:
            LOG.header('All VMs:')

    for key in sorted(vms):
        if args.long:
            LOG.header(f"\n{key}")
        LOG.info(vms[key])

    return 0


def vminfo(args):
    vbox = VBoxManage(args.name)
    info = vbox.get_vm_info()
    if not info:
        LOG.fatal(f'Cannot show details of VM "{args.name}" - '
                  f'it doesn\'t exists.')
        return 19

    LOG.header('Details for VM: %s', args.name)
    LOG.info('Creator:\t\t%s', info.get('creator', 'unknown/manual'))
    LOG.info('Number of CPU cores:\t%s', info['cpus'])

    memory = int(info['memory'])
    if memory//1024 == 0:
        memory = f"{memory}MB"
    else:
        memory = memory // 1024
        memory = f"{memory}GB"
    LOG.info('Memory:\t\t\t%s', memory)

    if info.get('media'):
        LOG.info('Attached images:')
        images = []
        for img in info['media']:
            size = int(vbox.get_media_size(img['uuid'], TYPE_MAP[img['type']]))
            if size//1024 == 0:
                size = f"{size}MB"
            else:
                size = size // 1024
                size = f"{size}GB"
            if img['type'] == 'DVD':
                images.append(f"  {img['type']}:\t\t\t{size}")
            else:
                images.append(f"  {img['type']}:\t\t{size}")

        images.sort()
        for line in images:
            LOG.info(line)

    if 'distro' in info:
        LOG.info('Operating System:\t%s %s', DISTRO_MAP[info['distro']],
                 info['version'])
    if 'key' in info:
        LOG.info('SSH key:\t\t%s', info['key'])

    if 'port' in info:
        LOG.info('SSH port:\t\t%s', info['port'])

    if 'forwarding' in info:
        LOG.info('Additional port mappings:')
        ports = []
        for hostport, vmport in info['forwarding'].items():
            ports.append(f"  {hostport}:{vmport}")
        ports.sort()
        for line in ports:
            LOG.info(line)

    if 'user_data' in info:
        LOG.info(f'User data file path:\t{info["user_data"]}')


def vmrebuild(args):
    vbox = VBoxManage(args.name)
    if not vbox.get_vm_info():
        LOG.fatal(f'Cannot rebuild VM "{args.name}" - it doesn\'t exists.')
        return 20
    else:
        LOG.header('Rebuilding VM: %s', args.name)

    try:
        conf = Config(args, vbox)
    except BoxNotFound as ex:
        LOG.fatal(f'Error with parsing config: {ex}')
        return 8
    except yaml.YAMLError:
        LOG.fatal(f'Cannot read or parse file `{args.config}` as YAML '
                  f'file')
        return 15

    vbox.poweroff()

    try:
        disk_path = vbox.get_disk_path()
    except BoxError:
        LOG.fatal('Cannot deal with multiple attached disks, perhaps you need '
                  'to do this manually')
        return 9

    if not disk_path:
        # no disks, return
        return 10

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
    if not vbox.get_vm_info():
        LOG.fatal(f'No machine has been found with a name `{args.name}`.')
        return 17

    try:
        conf = Config(args, vbox)
    except BoxNotFound:
        return 11
    except yaml.YAMLError:
        LOG.fatal(f'Cannot read or parse file `{args.config}` as YAML '
                  f'file.')
        return 16

    username = conf.username or DISTROS[conf.distro]["username"]
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no',
           '-o', 'UserKnownHostsFile=/dev/null',
           '-i', conf.ssh_key_path[:-4],
           f'ssh://{username}'
           f'@localhost:{conf.port}']
    LOG.debug('Connecting to vm `%s` using command:\n%s', args.name,
              ' '.join(cmd))
    return Run(cmd, False).returncode


def _set_vmstate(name, state):

    vbox = VBoxManage(name)
    if not vbox.get_vm_info():
        LOG.fatal(f'No machine has been found with a name `{name}`.')
        return 20

    if vbox.running and state == "start":
        LOG.info(f'VM "{name}" is already running.')
        return

    if not vbox.running and state == "stop":
        LOG.info(f'VM "{name}" is already stopped.')
        return

    if state == "start":
        vbox.poweron()
    else:
        vbox.acpipowerbutton()


def vmstart(args):
    _set_vmstate(args.name, 'start')


def vmstop(args):
    _set_vmstate(args.name, 'stop')


def main():
    parser = argparse.ArgumentParser(description="Automate deployment and "
                                     "maintenance of VMs using cloud config,"
                                     "VirtualBox and Fedora or Ubuntu cloud "
                                     "images")

    group = parser.add_mutually_exclusive_group()
    group.add_argument('-v', '--verbose', action='count', default=0,
                       help='be verbose. Adding more "v" will increase '
                       'verbosity')
    group.add_argument('-q', '--quiet', action='count', default=0,
                       help='suppress output. Adding more "q" will make '
                       'boxpy to shut up.')
    parser.add_argument('-V', '--version', action='store_true',
                        help="show boxpy version and exit")

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
    create.add_argument('-e', '--default-user', help="Default cloud-init user "
                        "to be used with custom image (--image param). "
                        "Without image it will make no effect.")
    create.add_argument('-f', '--forwarding', action='append', help="expose "
                        "port from VM to the host. It should be in format "
                        "'hostport:vmport'. this option can be used multiple "
                        "times for multiple ports.")
    create.add_argument('-i', '--image', help="custom qcow2 image filepath. "
                        "Note, that it requires to provide --default-user as "
                        "well.")
    create.add_argument('-k', '--key', help="SSH key to be add to the config "
                        "drive. Default ~/.ssh/id_rsa")
    create.add_argument('-m', '--memory', help="amount of memory in "
                        "Megabytes, default 2GB")
    create.add_argument('-n', '--hostname',
                        help="VM hostname. Default same as vm name")
    create.add_argument('-p', '--port', help="set ssh port for VM, default "
                        "random port from range 2000-2999")
    create.add_argument('-r', '--disable-nested', action='store_true',
                        help="disable nested virtualization")
    create.add_argument('-s', '--disk-size', help="disk size to be expanded "
                        "to. By default to 10GB")
    create.add_argument('-t', '--type', default='headless',
                        help="VM run type, headless by default.",
                        choices=['gui', 'headless', 'sdl', 'separate'])
    create.add_argument('-u', '--cpus', type=int, help="amount of CPUs to be "
                        "configured. Default 1.")
    create.add_argument('-v', '--version', help=f"distribution version. "
                        f"Default {DISTROS['ubuntu']['default_version']}")

    destroy = subparsers.add_parser('destroy', help='destroy VM')
    destroy.add_argument('name', nargs='+', help='name or UUID of the VM')
    destroy.set_defaults(func=vmdestroy)

    list_vms = subparsers.add_parser('list', help='list VMs')
    list_vms.add_argument('-b', '--run-by-boxpy', action='store_true',
                          help='show only those machines created by boxpy')
    list_vms.add_argument('-l', '--long', action='store_true',
                          help='show detailed information '
                          'about VMs')
    list_vms.add_argument('-r', '--running', action='store_true',
                          help='show only running VMs')
    list_vms.set_defaults(func=vmlist)

    rebuild = subparsers.add_parser('rebuild', help='rebuild VM, all options '
                                    'besides vm name are optional, and their '
                                    'values will be taken from vm definition.')
    rebuild.add_argument('name', help='name or UUID of the VM')
    rebuild.add_argument('-c', '--config',
                         help="Alternative user-data template filepath")
    rebuild.add_argument('-d', '--distro', help="Image name.")
    rebuild.add_argument('-e', '--default-user', help="Default cloud-init "
                         "user to be used with custom image (--image param). "
                         "Without image it will make no effect.")
    rebuild.add_argument('-f', '--forwarding', action='append', help="expose "
                         "port from VM to the host. It should be in format "
                         "'hostport:vmport'. this option can be used multiple "
                         "times for multiple ports.")
    rebuild.add_argument('-i', '--image', help="custom qcow2 image filepath. "
                         "Note, that it requires to provide --default-user as "
                         "well.")
    rebuild.add_argument('-k', '--key',
                         help='SSH key to be add to the config drive')
    rebuild.add_argument('-m', '--memory', help='amount of memory in '
                         'Megabytes')
    rebuild.add_argument('-n', '--hostname', help="set VM hostname")
    rebuild.add_argument('-p', '--port', help="set ssh port for VM")
    rebuild.add_argument('-r', '--disable-nested', action="store_true",
                         help="disable nested virtualization")
    rebuild.add_argument('-s', '--disk-size',
                         help='disk size to be expanded to')
    rebuild.add_argument('-t', '--type', default='headless',
                         help="VM run type, headless by default.",
                         choices=['gui', 'headless', 'sdl', 'separate'])
    rebuild.add_argument('-u', '--cpus', type=int,
                         help='amount of CPUs to be configured')
    rebuild.add_argument('-v', '--version', help='distribution version')
    rebuild.set_defaults(func=vmrebuild)

    start = subparsers.add_parser('start', help='start VM')
    start.add_argument('name', help='name or UUID of the VM')
    start.set_defaults(func=vmstart)

    stop = subparsers.add_parser('stop', help='stop VM')
    stop.add_argument('name', help='name or UUID of the VM')
    stop.set_defaults(func=vmstop)

    completion = subparsers.add_parser('completion', help='generate shell '
                                       'completion')
    completion.add_argument('shell', choices=['bash'],
                            help="pick shell to generate completions for")
    completion.set_defaults(func=shell_completion)

    ssh = subparsers.add_parser('ssh', help='connect to the machine via SSH')
    ssh.add_argument('name', help='name or UUID of the VM')
    ssh.set_defaults(func=connect)

    info = subparsers.add_parser('info', help='details about VM')
    info.add_argument('name', help='name or UUID of the VM')
    info.set_defaults(func=vminfo)

    args = parser.parse_args()

    if 'image' in args and 'default_user' not in args:
        parser.error('Parameter --image requires --default-user')
        return 22

    LOG.set_verbose(args.verbose, args.quiet)

    if 'func' not in args and args.version:
        LOG.info(f'boxpy {__version__}')
        parser.exit()

    if hasattr(args, 'func'):
        return args.func(args)

    parser.print_help()
    parser.exit()


if __name__ == '__main__':
    sys.exit(main())
