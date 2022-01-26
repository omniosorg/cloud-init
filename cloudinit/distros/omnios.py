# This file is part of cloud-init. See LICENSE file for license information.

from cloudinit import log as logging
from cloudinit import subp
from cloudinit import util

from cloudinit.distros import illumos
from cloudinit.settings import PER_INSTANCE

LOG = logging.getLogger(__name__)


class Distro(illumos.Distro):

    def install_packages(self, pkglist):
        self.update_package_sources()
        (out, _) = self.package_command('install', args=['--parsable=0'],
            pkgs=pkglist)
        try:
            j = util.load_json(out.splitlines()[0])
        except:
            return

        for pkg in j['add-packages']:
            LOG.info(f'Installed {pkg}')

        if j['be-name']:
            LOG.info('Package installation requires reboot')
            util.ensure_file('/var/run/reboot-required')

    def upgrade_packages(self):
        self.update_package_sources()
        (out, _) = self.package_command('update', '-f', args=['--parsable=0'])
        try:
            j = util.load_json(out.splitlines()[0])
        except:
            return

        if j['be-name']:
            LOG.info('Package update requires reboot')
            util.ensure_file('/var/run/reboot-required')

    def package_command(self, command, args=None, pkgs=None):

        # Called directly from cc_package_update_upgrade_install
        if command == 'upgrade':
            self.upgrade_packages()
            return

        cmd = ['pkg', command]

        if args and isinstance(args, str):
            cmd.append(args)
        elif args and isinstance(args, list):
            cmd.extend(args)

        if pkgs:
            pkglist = util.expand_package_list('%s@%s', pkgs)
            if pkglist:
                cmd.extend(pkglist)

        # Exit status 4 is "No changes were made, nothing to do"
        return subp.subp(cmd, rcs=[0, 4])

    def update_package_sources(self):
        self._runner.run("update-sources", self.package_command,
                         ["refresh"], freq=PER_INSTANCE)

# vi: ts=4 sw=4 expandtab
