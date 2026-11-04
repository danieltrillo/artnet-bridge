"""Discover the IPv4 addresses present on this host.

Uses getifaddrs(3) through ctypes so it works on any Linux box without
iproute2. Falls back to `ip -j addr` when ctypes is not usable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import ipaddress
import json
import socket
import subprocess
from dataclasses import dataclass

AF_INET = socket.AF_INET


@dataclass(frozen=True)
class Address:
    interface: str
    index: int
    ip: str
    prefix: int
    mac: str = ""

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.prefix}"


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_uint16),
        ("sin_port", ctypes.c_uint16),
        ("sin_addr", ctypes.c_uint8 * 4),
        ("sin_zero", ctypes.c_uint8 * 8),
    ]


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.POINTER(_SockaddrIn)),
    ("ifa_netmask", ctypes.POINTER(_SockaddrIn)),
    ("ifa_broadaddr", ctypes.POINTER(_SockaddrIn)),
    ("ifa_data", ctypes.c_void_p),
]


def _mac_of(interface: str) -> str:
    try:
        with open(f"/sys/class/net/{interface}/address", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _index_of(interface: str) -> int:
    try:
        return socket.if_nametoindex(interface)
    except OSError:
        return 0


def _via_getifaddrs() -> list[Address]:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "getifaddrs failed")
    found: list[Address] = []
    try:
        node = head
        while node:
            entry = node.contents
            if entry.ifa_addr and entry.ifa_addr.contents.sin_family == AF_INET:
                name = entry.ifa_name.decode("utf-8", "replace")
                ip = ".".join(str(b) for b in entry.ifa_addr.contents.sin_addr)
                prefix = 32
                if entry.ifa_netmask:
                    mask = ".".join(str(b) for b in entry.ifa_netmask.contents.sin_addr)
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                found.append(Address(name, _index_of(name), ip, prefix, _mac_of(name)))
            node = entry.ifa_next
    finally:
        libc.freeifaddrs(head)
    return found


def _via_iproute() -> list[Address]:
    out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5, check=True)
    found: list[Address] = []
    for link in json.loads(out.stdout):
        name = link.get("ifname", "")
        for info in link.get("addr_info", []):
            if info.get("family") != "inet":
                continue
            found.append(
                Address(name, int(link.get("ifindex", 0)), info["local"], int(info.get("prefixlen", 32)), _mac_of(name))
            )
    return found


def list_addresses() -> list[Address]:
    """All IPv4 addresses on this host (loopback included)."""
    try:
        return _via_getifaddrs()
    except Exception:  # pragma: no cover - platform specific fallback
        try:
            return _via_iproute()
        except Exception:
            return []


def find_address(ip: str, addresses: list[Address] | None = None) -> Address | None:
    for addr in addresses if addresses is not None else list_addresses():
        if addr.ip == ip:
            return addr
    return None
