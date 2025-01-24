# This file is part of cloud-init. See LICENSE file for license information.

import errno
import functools
import logging
import os
from typing import Any, Optional

from cloudinit import distros, helpers, subp, util
from cloudinit.net import dhcp
from cloudinit.net.netops.illumos_netops import illumosNetOps

from .networking import illumosNetworking

try:
    import crypt  # pylint: disable=W4901

    sha512_hash: Any = functools.partial(
        crypt.crypt,
        salt=crypt.mksalt(crypt.METHOD_SHA512),  # pylint: disable=E1101
    )
except (ImportError, AttributeError):
    try:
        from passlib.hash import sha512_crypt

        sha512_hash = sha512_crypt.hash
    except ImportError:

        def sha512_hash(_):
            """Raise when called so that importing this module doesn't throw
            ImportError when this module is not used. In this case, crypt
            and passlib are not needed.
            """
            raise ImportError(
                "crypt and passlib not found, missing dependency"
            )


LOG = logging.getLogger(__name__)


class Distro(distros.Distro):
    networking_cls = illumosNetworking
    net_ops = illumosNetOps

    hostname_conf_fn = "/etc/nodename"
    hosts_fn = "/etc/inet/hosts"
    tz_zone_dir = "/usr/share/lib/zoneinfo"
    home_dir = "/home"
    init_cmd = ["svcadm"]

    shutdown_options_map = {
        "halt": ["-i", "0"],
        "poweroff": ["-i", "5"],
        "reboot": ["-i", "6"],
    }

    def __init__(self, name, cfg, paths):
        super().__init__(name, cfg, paths)
        self._runner = helpers.Runners(paths)
        self.osfamily = "illumos"
        self.dhcp_client_priority = [dhcp.illumosDhcp]
        self.net_ops = illumosNetOps
        self.is_linux = False

    def _unpickle(self, ci_pkl_version: int) -> None:
        super()._unpickle(ci_pkl_version)

        # this needs to be after the super class _unpickle to override it
        self.net_ops = illumosNetOps
        self.is_linux = False

    def shutdown_command(self, *, mode, delay, message):
        command = ["shutdown", "-y"]
        command.extend(self.shutdown_options_map[mode])
        if delay == "now":
            delay = 0
        else:
            try:
                delay = int(delay)
            except ValueError as e:
                raise TypeError(
                    "power_state[delay] must be 'now' or '+m' (minutes)."
                    " found '%s'." % (delay,)
                ) from e

        command.extend(["-g", str(delay)])
        if message:
            command.append(message)

        return command

    def manage_service(
        self, action: str, service: str, *extra_args: str, rcs=None
    ):
        if action == "status":
            cmd = ["svcs", "-H", "-o", "state", service]
            (out, err) = subp.subp(cmd, capture=True)

            # This emulates what the callers expect (since they still mostly
            # assume Linux). Successful execution if the service is online,
            # otherwise a non-zero exit code.
            if out.strip() == "online":
                return (None, None)

            # Callers do not actually check the exit status unless
            # distro.uses_systemd() is True but exit code 3 would mean
            # 'not running', so we use that.
            raise subp.ProcessExecutionError(
                cmd=cmd, stdout=out, stderr=err, exit_code=3
            )

        cmds = {
            "stop": ["disable", "-st", service],
            "start": ["enable", "-st", service],
            "enable": ["enable", service],
            "disable": ["disable", service],
            "restart": ["restart", service],
            "reload": ["refresh", service],
            "try-reload": ["refresh", service],
        }
        cmd = list(self.init_cmd) + list(cmds[action])
        return subp.subp(cmd, capture=True, rcs=rcs)

    def generate_fallback_config(self):
        return self.networking.generate_fallback_config()

    def _read_system_hostname(self):
        sys_hostname = self._read_hostname(self.hostname_conf_fn)
        return (self.hostname_conf_fn, sys_hostname)

    def _read_hostname(self, filename, default=None):
        return util.load_text_file(filename).strip()

    def _write_hostname(self, hostname, filename):
        content = hostname + "\n"
        util.write_file(filename, content)

    def _write_profiles(self, user, profiles):
        pfile = "/etc/user_attr.d/cloud-init-users"

        lines = [
            "",
            "# User rules for %s" % user,
            "%s::::type=normal;profiles=%s" % (user, profiles),
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

        self.manage_service("restart", "system/rbac")

    def create_user(self, name, **kwargs):
        super().create_user(name, **kwargs)

        # Configure profiles
        if "profiles" in kwargs and kwargs["profiles"] is not False:
            self._write_profiles(name, kwargs["profiles"])

    def create_group(self, name, members=None):
        group_add_cmd = ["groupadd", name]

        # Check if group exists, and then add if it doesn't
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

        useradd_cmd = ["useradd"]

        useradd_opts = {
            "homedir": "-d",
            "gecos": "-c",
            "primary_group": "-g",
            "groups": "-G",
            "shell": "-s",
            "inactive": "-f",
            "expiredate": "-e",
            "uid": "-u",
        }

        if "create_groups" in kwargs:
            create_groups = kwargs.pop("create_groups")
        else:
            create_groups = True

        # support kwargs having groups=[list] or groups="g1,g2"
        groups = kwargs.get("groups")
        if groups:
            if isinstance(groups, str):
                groups = groups.split(",")

            # remove any white spaces in group names, most likely
            # that came in as a string like: groups: group1, group2
            groups = [g.strip() for g in groups]

            # kwargs.items loop below wants a comma delimited string
            # that can go right through to the command.
            kwargs["groups"] = ",".join(groups)

            primary_group = kwargs.get("primary_group")
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

        if "no_create_home" in kwargs or "system" in kwargs:
            pass
        else:
            useradd_cmd.extend(
                [
                    "-m",
                    "-z",
                    "-d",
                    "{home_dir}/{name}".format(
                        home_dir=self.home_dir, name=name
                    ),
                ]
            )

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
        passwd_val = kwargs.get("passwd", None)
        if passwd_val is not None:
            self.set_passwd(name, passwd_val, hashed=True)

    def expire_passwd(self, user):
        try:
            subp.subp(["passwd", "-f", user])
        except Exception:
            util.logexc(LOG, "Failed to expire password for %s", user)
            raise

    def lock_passwd(self, user):
        try:
            subp.subp(["passwd", "-N", user])
        except Exception:
            util.logexc(LOG, "Failed to disable password for user %s", user)
            raise

    def set_passwd(self, user, passwd, hashed=False):
        if hashed:
            hashed_pw = passwd
        else:
            hashed_pw = sha512_hash(passwd)

        try:
            subp.subp(
                ["/usr/lib/passmgmt", "-m", "-p", hashed_pw, user],
                logstring=f"/usr/lib/passmgmt -m -p <hash> {user}",
            )
        except Exception:
            util.logexc(LOG, "Failed to set password for %s", user)
            raise

    def install_packages(self, pkglist):
        raise NotImplementedError()

    def package_command(self, command, args=None, pkgs=None):
        raise NotImplementedError()

    def update_package_sources(self, *, force=False):
        raise NotImplementedError()

    def _update_init(self, key, val, prefixes=None):
        out_fn = "/etc/default/init"

        if prefixes is None:
            prefixes = (f"{key}=",)

        try:
            content = util.load_text_file(out_fn).splitlines()
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
            content = []
        content = [a for a in content if not a.startswith(prefixes)]
        LOG.debug("Setting %s=%s in %s", key, val, out_fn)
        content.append(f"{key}={val}")
        content.append("")
        util.write_file(out_fn, "\n".join(content))

    def apply_locale(self, locale, out_fn=None):
        self._update_init("LC_ALL", locale, ("LC_", "LANG"))

    def set_timezone(self, tz):
        self._update_init("TZ", tz)

    def chpasswd(self, plist_in: list, hashed: bool):
        for name, password in plist_in:
            self.set_passwd(name, password, hashed=hashed)

    @staticmethod
    def _dhcpattr(nic: str, attr: str) -> Optional[str]:
        try:
            (out, _err) = subp.subp(["/sbin/dhcpinfo", "-i", nic, attr])
            out = out.strip()
            return out if out else None
        except subp.ProcessExecutionError:
            return None

    @staticmethod
    def obtain_dhcp_lease(nic: str) -> Optional[dict]:
        """Temporarily bring up an address via DHCP on 'nic' and return the
        lease information. The address is removed again before returning;
        the caller (EphemeralIPv4Network) re-applies it statically for the
        duration of the ephemeral network."""
        instance = "ephdhcp"
        illumosNetOps._ipadm(nic, "create-if", rcs=[0, 1])
        illumosNetOps._ipadm(
            nic,
            instance=instance,
            cmd=["create-addr", "-T", "dhcp", "-w", "15"],
        )

        lease = {
            "interface": nic,
            "fixed-address": Distro._dhcpattr(nic, "Yiaddr"),
            "subnet-mask": Distro._dhcpattr(nic, "Subnet"),
            "routers": Distro._dhcpattr(nic, "Router"),
        }
        illumosNetOps._ipadm(nic, instance=instance, cmd="delete-addr")
        return lease if lease["fixed-address"] else None
