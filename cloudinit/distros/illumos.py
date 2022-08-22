import os, platform

from cloudinit import distros
from cloudinit import helpers
from cloudinit import log as logging
from cloudinit import net
from cloudinit import subp
from cloudinit import util
from .networking import illumosNetworking

LOG = logging.getLogger(__name__)


class Distro(distros.Distro):
    networking_cls = illumosNetworking

    hostname_conf_fn = "/etc/nodename"
    hosts_fn = "/etc/inet/hosts"
    tz_zone_dir = "/usr/share/lib/zoneinfo"
    home_dir = '/home'
    init_cmd = ['svcadm']

    def __init__(self, name, cfg, paths):
        super().__init__(name, cfg, paths)
        self._runner = helpers.Runners(paths)
        self.osfamily = 'illumos'

    shutdown_options_map = {
        'halt':     ['-i', '0'],
        'poweroff': ['-i', '5'],
        'reboot':   ['-i', '6'],
    }

    def shutdown_command(self, *, mode, delay, message):
        command = ['shutdown', '-y']
        command.extend(self.shutdown_options_map[mode])
        if delay == 'now':
            delay = 0
        else:
            try:
                delay = int(delay)
            except ValueError as e:
                raise TypeError(
                    "power_state[delay] must be 'now' or '+m' (minutes)."
                    " found '%s'." % (delay,)
                ) from e

        command.extend(['-g', str(delay)])
        if message:
            command.append(message)

        return command

    def manage_service(self, action, service):
        init_cmd = self.init_cmd

        if action == 'status':
            cmd = ['svcs', '-H', '-o', 'state', service]
            try:
                (out, err) = subp.subp(cmd, capture=True)
            except subp.ProcessExecutionError:
                # Pass back to caller
                raise

            # This emulates what the callers expect (since they still mostly
            # assume Linux). Successful execution if the service is online,
            # otherwise a non-zero exit code.
            if out == "online":
                return (None, None)

            # Callers do not actually check the exit status unless
            # distro.uses_systemd() is True but exit code 3 would mean
            # 'not running', so we use that.
            raise subp.ProcessExecutionError(
                cmd=cmd, stdout=out, stderr=err, exit_code=3
            )

        cmds = {'stop': ['stop', service],
                'start': ['start', service],
                'enable': ['enable', service],
                'restart': ['restart', service],
                'reload': ['restart', service],
                'try-reload': ['restart', service],
                }
        cmd = list(init_cmd) + list(cmds[action])
        return subp.subp(cmd, capture=True)

    def generate_fallback_config(self):
        return self.networking.generate_fallback_config()

    def _read_system_hostname(self):
        sys_hostname = self._read_hostname(self.hostname_conf_fn)
        return (self.hostname_conf_fn, sys_hostname)

    def _read_hostname(self, filename, default=None):
        return util.load_file(filename).strip()

    def _write_hostname(self, hostname, filename):
        content = hostname + '\n'
        util.write_file(filename, content)

    def _write_profiles(self, user, profiles):
        pfile = '/etc/user_attr.d/cloud-init-users'

        lines = [
            "",
            "# User rules for %s" % user,
            "%s::::type=normal;profiles=%s" % (user, profiles)
        ]
        content = "\n".join(lines) + "\n"

        if not os.path.exists(pfile):
            contents = [
                util.make_header(),
                content,
            ]
            try:
                util.write_file(pfile, "\n".join(contents), 0o440)
            except IOError as e:
                util.logexc(LOG, "Failed to write user attr file %s", pfile)
                raise e
        else:
            try:
                util.append_file(pfile, content)
            except IOError as e:
                util.logexc(LOG, "Failed to append user attr file %s", pfile)
                raise e

        self.manage_service('restart', 'system/rbac')

    def create_user(self, name, **kwargs):
        super().create_user(name, **kwargs);

        # Configure profiles
        if "profiles" in kwargs and kwargs["profiles"] is not False:
            self._write_profiles(name, kwargs["profiles"])

    def create_group(self, name, members=None):
        group_add_cmd = ['groupadd', name]

        # Check if group exists, and then add it doesn't
        if util.is_group(name):
            LOG.warning("Skipping creation of existing group '%s'", name)
        else:
            try:
                subp.subp(group_add_cmd)
                LOG.info("Created new group %s", name)
            except Exception:
                util.logexc(LOG, "Failed to create group %s", name)

    def add_user(self, name, **kwargs):
        if util.is_user(name):
            LOG.info("User %s already exists, skipping.", name)
            return False

        useradd_cmd = ['useradd']

        useradd_opts = {
            'homedir': '-d',
            'gecos': '-c',
            'primary_group': '-g',
            'groups': '-G',
            'shell': '-s',
            'inactive': '-f',
            'expiredate': '-e',
            'uid': '-u',
        }

        if 'create_groups' in kwargs:
            create_groups = kwargs.pop('create_groups')
        else:
            create_groups = True

        # support kwargs having groups=[list] or groups="g1,g2"
        groups = kwargs.get('groups')
        if groups:
            if isinstance(groups, str):
                groups = groups.split(",")

            # remove any white spaces in group names, most likely
            # that came in as a string like: groups: group1, group2
            groups = [g.strip() for g in groups]

            # kwargs.items loop below wants a comma delimited string
            # that can go right through to the command.
            kwargs['groups'] = ",".join(groups)

            primary_group = kwargs.get('primary_group')
            if primary_group:
                groups.append(primary_group)

        if create_groups and groups:
            for group in groups:
                if not util.is_group(group):
                    self.create_group(group)
                    LOG.debug("created group '%s' for user '%s'", group, name)

        for key, val in kwargs.items():
            if key in useradd_opts and val and isinstance(val, str):
                useradd_cmd.extend([useradd_opts[key], val])

        if 'no_create_home' in kwargs or 'system' in kwargs:
            pass
        else:
            useradd_cmd.extend(['-m', '-z',
                '-d', '{home_dir}/{name}'.format(
                home_dir=self.home_dir, name=name)])

        useradd_cmd.append(name)

        # Run the command
        LOG.info("Adding user %s", name)
        try:
            subp.subp(useradd_cmd)
        except Exception:
            util.logexc(LOG, "Failed to create user %s", name)
            raise
        # Set the password if it is provided
        # For security consideration, only hashed passwd is assumed
        passwd_val = kwargs.get('passwd', None)
        if passwd_val is not None:
            self.set_passwd(name, passwd_val, hashed=True)

    def expire_passwd(self, user):
        try:
            subp.subp(['passwd', '-f', user])
        except Exception:
            util.logexc(LOG, "Failed to expire password for %s", user);
            raise

    def lock_passwd(self, user):
        try:
            subp.subp(['passwd', '-N', user])
        except Exception:
            util.logexc(LOG, 'Failed to disable password for user %s', user)
            raise

    def set_passwd(self, user, passwd, hashed=False):
        if hashed:
            hashed_pw = passwd
        else:
            method = crypt.METHOD_SHA512
            hashed_pw = crypt.crypt(
                passwd,
                crypt.mksalt(method)
            )

        try:
            subp.subp(['/usr/lib/passmgmt', '-m', '-p', hashed_pw, user],
                logstring=f'/usr/lib/passmgmt -m -p <hash> {user}')
        except Exception:
            util.logexc(LOG, "Failed to set password for %s", user)
            raise

    def install_packages(self, pkglist):
        raise NotImplementedError()

    def package_command(self, command, args=None, pkgs=None):
        raise NotImplementedError()

    def update_package_sources(self):
        raise NotImplementedError()

    def _update_init(self, key, val, prefixes=None):
        out_fn = '/etc/default/init'

        if prefixes is None:
            prefixes = (f'{key}=')

        try:
            content = util.load_file(out_fn).splitlines()
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
            content = []
        content = [a for a in content if not a.startswith(prefixes)]
        LOG.debug(f'Setting {key}={val} in {out_fn}')
        content.append(f'{key}={val}')
        content.append('')
        util.write_file(out_fn, "\n".join(content))

    def apply_locale(self, locale, out_fn=None):
        self._update_init('LC_ALL', locale, ('LC_', 'LANG'))

    def set_timezone(self, tz):
        self._update_init('TZ', tz)

    def chpasswd(self, plist_in: list, hashed: bool):
        for name, password in plist_in:
            self.set_passwd(name, password, hashed=hashed)

# vim:ts=4:sw=4:et:fdm=marker
