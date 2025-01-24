# This file is part of cloud-init. See LICENSE file for license information.

import logging
import re
from typing import Optional

import cloudinit.net.netops as netops
from cloudinit import subp

LOG = logging.getLogger(__name__)

IPADM = "/usr/sbin/ipadm"
DLADM = "/usr/sbin/dladm"


class illumosNetOps(netops.NetOps):
    @staticmethod
    def _ipadm(device_name, cmd, rcs=None, instance=None):
        if isinstance(cmd, str):
            cmd = [cmd]

        if instance is not None:
            device_name += f"/{instance}"

        cmd = [IPADM] + cmd + [device_name]

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error("ipadm command failed: %s", e)

    @staticmethod
    def _dladm(device_name, cmd, rcs=None):
        if isinstance(cmd, str):
            cmd = [cmd]

        cmd = [DLADM] + cmd + [device_name]

        try:
            subp.subp(cmd, rcs=rcs)
        except subp.ProcessExecutionError as e:
            LOG.error("dladm command failed: %s", e)

    @staticmethod
    def _parse_show_addr(out: str):
        """Parse `ipadm show-addr -p` output; the field separator is ':'
        and literal colons within fields (IPv6 addresses) are escaped
        with a backslash."""
        for line in out.splitlines():
            fields = [
                f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)
            ]
            yield fields

    @staticmethod
    def _addrobj_exists(interface: str, instance: str) -> bool:
        addrobj = f"{interface}/{instance}"
        try:
            (out, _err) = subp.subp(
                [IPADM, "show-addr", "-po", "ADDROBJ", addrobj]
            )
            return out.strip() == addrobj
        except subp.ProcessExecutionError:
            return False

    @staticmethod
    def _find_address(interface: str, address: str) -> Optional[str]:
        try:
            (out, _err) = subp.subp(
                [IPADM, "show-addr", "-po", "ADDROBJ,ADDR", f"{interface}/"]
            )
        except subp.ProcessExecutionError:
            return None
        for fields in illumosNetOps._parse_show_addr(out):
            if len(fields) != 2:
                continue
            (addrobj, addr) = fields
            if addr == address:
                return addrobj
        return None

    @staticmethod
    def _intf_in_use(devname: str) -> bool:
        (out, _err) = subp.subp([IPADM, "show-addr", "-p", "-o", "addrobj"])
        for addr in out.splitlines():
            if addr.startswith(f"{devname}/"):
                return True
        return False

    @staticmethod
    def link_up(interface: str, family: Optional[str] = None):
        illumosNetOps._ipadm(interface, ["enable-if", "-t"], rcs=[0, 1])

    @staticmethod
    def link_down(interface: str, family: Optional[str] = None):
        # Disabling the interface would remove all of its addresses, some
        # of which may be wanted. Deliberately a no-op.
        pass

    @staticmethod
    def link_rename(current_name: str, new_name: str):
        subp.subp([DLADM, "rename-link", current_name, new_name])

    @staticmethod
    def add_route(
        interface: str,
        route: str,
        *,
        gateway: Optional[str] = None,
        source_address: Optional[str] = None,
    ):
        cmd = ["route", "add", "-inet"]
        if not gateway or gateway == "0.0.0.0":
            # An interface route; the "gateway" argument to route(8) is the
            # local address on that interface.
            cmd.extend(["-iface", "-ifp", interface, "-host", route])
            cmd.append(source_address or route)
        else:
            cmd.extend([route, gateway])
        subp.subp(cmd)

    @staticmethod
    def append_route(interface: str, address: str, gateway: str):
        return illumosNetOps.add_route(
            interface, route=address, gateway=gateway
        )

    @staticmethod
    def del_route(
        interface: str,
        address: str,
        *,
        gateway: Optional[str] = None,
        source_address: Optional[str] = None,
    ):
        cmd = ["route", "delete", "-inet"]
        if address in ("default", "0.0.0.0/0") or (
            gateway and gateway != "0.0.0.0"
        ):
            cmd.append(address)
            if gateway and gateway != "0.0.0.0":
                cmd.append(gateway)
        else:
            cmd.extend(["-iface", "-ifp", interface, "-host", address])
            if source_address:
                cmd.append(source_address)
        subp.subp(cmd, rcs=[0, 1])

    @staticmethod
    def get_default_route() -> str:
        try:
            (out, _) = subp.subp(["route", "-n", "get", "default"])
            m = re.search(r"^\s+gateway:\s+(\S+)", out, re.MULTILINE)
            if m:
                # Callers check for the string 'default' in the output
                return f"default via {m.group(1)}"
        except subp.ProcessExecutionError:
            pass
        return ""

    @staticmethod
    def add_addr(
        interface: str, address: str, broadcast: Optional[str] = None
    ):
        # The interface may already exist, so allow rc 1
        illumosNetOps._ipadm(interface, "create-if", rcs=[0, 1])
        inum = 0
        while illumosNetOps._addrobj_exists(interface, f"ci{inum}"):
            inum += 1
        illumosNetOps._ipadm(
            interface,
            ["create-addr", "-T", "static", "-a", f"local={address}"],
            instance=f"ci{inum}",
        )

    @staticmethod
    def del_addr(interface: str, address: str):
        addrobj = illumosNetOps._find_address(interface, address)
        if addrobj:
            instance = addrobj.split("/")[-1]
            illumosNetOps._ipadm(interface, "delete-addr", instance=instance)
        if not illumosNetOps._intf_in_use(interface):
            illumosNetOps._ipadm(interface, "delete-if", rcs=[0, 1])
