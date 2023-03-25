import logging, re

from typing import Optional

from cloudinit import subp
from cloudinit import net
from cloudinit import util
import cloudinit.net.netops as netops

LOG = logging.getLogger(__name__)

from pprint import pprint, pformat

IPADM = "/usr/sbin/ipadm"
DLADM = "/usr/sbin/dladm"

class illumosNetOps(netops.NetOps):

    @staticmethod
    def _ipadm(device_name, cmd, rcs=[0], instance=None):
        if type(cmd) == str:
            cmd = [cmd]

        if instance is not None:
            device_name += f'/{instance}'

        cmd.insert(0, IPADM)
        cmd.append(device_name)

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error(f'ipadm command failed: {e}')

    @staticmethod
    def _dladm(device_name, cmd, rcs=[0]):
        if type(cmd) == str:
            cmd = [cmd]

        cmd.insert(0, DLADM)
        cmd.append(device_name)

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error(f'dladm command failed: {e}')

    @staticmethod
    def _addrobj_exists(interface: str, instance: str) -> bool:
        addrobj = f'{interface}/{instance}'
        try:
            (out, _err) = subp.subp(
                [IPADM, "show-addr", "-po", "ADDROBJ", addrobj])
            return out.strip() == addrobj
        except:
            return False

    @staticmethod
    def _find_address(interface: str, address: str) -> str:
        try:
            (out, _err) = subp.subp([IPADM, "show-addr", "-po",
                "ADDROBJ,ADDR", f'{interface}/'])
            for line in out.splitlines():
                (addrobj, addr) = line.split(':')
                if addr == address:
                    return addrobj
            return None
        except:
            return None

    @staticmethod
    def _intf_in_use(devname):
        (out, _err) = subp.subp([IPADM, 'show-addr', '-p',
            '-o', 'addrobj'])
        for addr in out.splitlines():
            if addr.startswith(f'{devname}/'):
                return True
        return False

    @staticmethod
    def link_up(interface: str, family: Optional[str] = None):
        illumosNetOps._ipadm(interface, ["enable-if", "-t"], rcs=[0,1])

    @staticmethod
    def link_down(interface: str, family: Optional[str] = None):
        #illumosNetOps._ipadm(interface, ["disable-if", "-t"])
        pass

    @staticmethod
    def add_route(
        interface: str,
        route: str,
        *,
        gateway: Optional[str] = None,
        source_address: Optional[str] = None
    ):
        cmd = ["route", "-p", "add", "-inet"]
        if not gateway or gateway == "0.0.0.0":
            cmd.extend(["-iface", "-ifp", interface, "-host"])
        cmd.extend([route, gateway])
        subp.subp(cmd);

    @staticmethod
    def append_route(interface: str, address: str, gateway: str):
        return illumosNetOps.add_route(interface, route=address,
            gateway=gateway)

    @staticmethod
    def del_route(
        interface: str,
        address: str,
        *,
        gateway: Optional[str] = None,
        source_address: Optional[str] = None
    ):
        cmd = ["route", "-p", "delete", "-inet"]
        if not gateway or gateway == "0.0.0.0":
            cmd.extend(["-iface", "-ifp", interface, "-host"])
        cmd.extend([route, gateway])
        subp.subp(cmd);

    @staticmethod
    def get_default_route() -> str:
        try:
            (out, _) = subp.subp(['route', '-n', 'get', 'default'])
            m = re.search(rf'^\s+gateway:\s+([0-9.]+)', out, re.MULTILINE)
            if m:
                return m.group(1)
        except:
            pass
        return None

    @staticmethod
    def add_addr(interface: str, address: str, broadcast: str):
        # The interface may already exist, so allow rc 1
        illumosNetOps._ipadm(interface, "create-if", rcs=[0,1])
        inum = 0
        while illumosNetOps._addrobj_exists(interface, f'ci{inum}'):
            inum += 1
        illumosNetOps._ipadm(interface, ["create-addr", "-T", "static",
           "-a", f'local={address}'], instance=f'ci{inum}')

    @staticmethod
    def del_addr(interface: str, address: str):
        addrobj = illumosNetOps._find_address(interface, address)
        if addrobj:
            instance = addrobj.split('/')[-1]
            illumosNetOps._ipadm(interface, "delete-addr", instance=instance)
        if not illumosNetOps._intf_in_use(interface):
            illumosNetOps._ipadm(interface, "delete-if", rcs=[0,1])

# vim:ts=4:sw=4:et:fdm=marker
