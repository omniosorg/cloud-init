# This file is part of cloud-init. See LICENSE file for license information.

import logging, re

from cloudinit import net
from cloudinit import subp
from cloudinit import util
from cloudinit.distros.parsers.resolv_conf import ResolvConf

from . import renderer

LOG = logging.getLogger(__name__)

from pprint import pprint, pformat


class Renderer(renderer.Renderer):
    resolv_conf_fn = '/etc/resolv.conf'

    def __init__(self, config=None):
        super(Renderer, self).__init__()

    def _ipadm(self, device_name, cmd, rcs=[0], instance=None):
        if type(cmd) == str:
            cmd = [cmd]

        if instance is not None:
            device_name += f'/{instance}'

        cmd.insert(0, '/usr/sbin/ipadm')
        cmd.append(device_name)

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error(f'ipadm command failed: {e}')


    def _dladm(self, device_name, cmd, rcs=[0]):
        if type(cmd) == str:
            cmd = [cmd]

        cmd.insert(0, '/usr/sbin/dladm')
        cmd.append(device_name)

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error(f'dladm command failed: {e}')

    def _interfaces(self, settings):
        ifname_by_mac = net.get_interfaces_by_mac()
        interface_config = {}

        for interface in settings.iter_interfaces():
            device_name = interface.get("name")
            device_mac = interface.get("mac_address")
            if device_name and re.match(r'^lo\d+$', device_name):
                continue
            if device_mac not in ifname_by_mac:
                LOG.info('Cannot find any device with MAC %s', device_mac)
            elif device_mac and device_name:
                cur_name = ifname_by_mac[device_mac]
                if cur_name != device_name:
                    LOG.info(f'rename {cur_name} to {device_name}')
                    if net.illumos_intf_in_use(cur_name):
                        LOG.warning(
                            f'Interface {cur_name} is in use; cannot rename')
                    else:
                        self._ipadm(device_name, ['delete-if', cur_name],
                            rcs=[0, 1])
                        self._dladm(device_name, ['rename-link', cur_name])
                        device_name = cur_name
            else:
                device_name = ifname_by_mac[device_mac]

            LOG.info(f'Configuring interface {device_name}')

            interface_config[device_name] = 'DHCP'

            for subnet in interface.get("subnets", []):
                if subnet.get('type') == 'static':
                    addr = subnet.get('address')
                    prefix = subnet.get('prefix')
                    LOG.debug('Configuring dev %s with %s/%s', device_name,
                              addr, prefix)

                    interface_config[device_name] = {
                        'address': addr,
                        'netmask': prefix,
                        'mtu': subnet.get('mtu') or interface.get('mtu'),
                    }

            dhcp_done = False
            for device_name, v in interface_config.items():
                self._ipadm(device_name, ['create-if'], rcs=[0, 1])
                if v == 'DHCP':
                    self._ipadm(device_name, ['create-addr', '-T', 'dhcp',
                        '-w', '15'], instance='dhcp')
                    dhcp_done = True
                else:
                    addr = v.get('address')
                    mask = v.get('netmask')
                    mtu = v.get('mtu')
                    if mtu:
                        self._dladm(device_name, ['set-linkprop', '-p',
                            f'mtu={mtu}'])
                    self._ipadm(device_name, ['create-addr', '-T', 'static',
                        '-a', f'local={addr}/{mask}'], instance='ci')

            if dhcp_done:
                subp.subp(['/usr/sbin/svcadm', 'restart', 'network/service'])

    def _routes(self, settings):
        routes = list(settings.iter_routes())
        for interface in settings.iter_interfaces():
            subnets = interface.get("subnets", [])
            for subnet in subnets:
                if subnet.get('type') != 'static':
                    continue
                routes += subnet.get('routes', [])
                gateway = subnet.get('gateway')
                if gateway and len(gateway.split('.')) == 4:
                    util.write_file('/etc/defaultrouter', f"{gateway}\n")
                    routes.append({
                        'network': '0.0.0.0',
                        'prefix': '0',
                        'gateway': gateway})
        for route in routes:
            network = route.get('network')
            prefix = route.get('prefix')
            gateway = route.get('gateway')
            if not network:
                LOG.debug('Skipping a bad route entry')
                continue

            subp.subp(['route', '-p', 'add', '-net', f'{network}/{prefix}',
                gateway], rcs=[0,1])

    def _resolv_conf(self, settings):
        nameservers = settings.dns_nameservers
        searchdomains = settings.dns_searchdomains
        for interface in settings.iter_interfaces():
            for subnet in interface.get("subnets", []):
                if 'dns_nameservers' in subnet:
                    nameservers.extend(subnet['dns_nameservers'])
                if 'dns_search' in subnet:
                    searchdomains.extend(subnet['dns_search'])

        try:
            resolvconf = ResolvConf(util.load_file(self.resolv_conf_fn))
        except (IOError, FileNotFoundError):
            util.logexc(LOG, "Failed to parse %s, use new empty file",
                self.resolv_conf_fn)
            resolvconf = ResolvConf('')

        resolvconf.parse()

        for server in nameservers:
            try:
                resolvconf.add_nameserver(server)
            except ValueError:
                util.logexc(LOG, "Failed to add nameserver %s", server)

        for domain in searchdomains:
            try:
                resolvconf.add_search_domain(domain)
            except ValueError:
                util.logexc(LOG, "Failed to add search domain %s", domain)

        util.write_file(self.resolv_conf_fn, str(resolvconf), 0o644)

        subp.subp(['/usr/sbin/svcadm', 'refresh', 'network/dns/client'])

    def render_network_state(self, network_state, templates=None, target=None):
        if target:
            self.target = target
        self._interfaces(settings=network_state)
        self._routes(settings=network_state)
        self._resolv_conf(settings=network_state)

def available(target=None):
    return util.is_illumos()

# vi: ts=4 sw=4 expandtab
