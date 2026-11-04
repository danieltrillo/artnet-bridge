"""UDP transport with per-packet interface information.

One socket per protocol port is bound to 0.0.0.0 so it receives unicast,
directed broadcast, limited broadcast (255.255.255.255) and multicast on every
interface. IP_PKTINFO tells us which local address and interface each packet
arrived on, and lets us pick the source address and interface when sending.
No special privileges are needed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Callable

log = logging.getLogger(__name__)

# Handler signature: (data, src_ip, src_port, dst_ip, local_ip, ifindex)
PacketHandler = Callable[[bytes, str, int, str, str, int], None]

# Python only grew socket.IP_PKTINFO in 3.12; Debian 12 ships 3.11. The
# value is fixed on Linux (linux/in.h).
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)

_PKTINFO_FMT = "i4s4s"  # ipi_ifindex, ipi_spec_dst, ipi_addr
_PKTINFO_SIZE = struct.calcsize(_PKTINFO_FMT)
_ANC_SPACE = socket.CMSG_SPACE(_PKTINFO_SIZE)
_MAX_BATCH = 256


class UdpTransport:
    def __init__(self, port: int, handler: PacketHandler, *, multicast_ttl: int = 1, bind_ip: str = "") -> None:
        self.port = port
        self.handler = handler
        self.bind_ip = bind_ip
        self._ttl = multicast_ttl
        self._sock: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.rx_packets = 0
        self.tx_packets = 0
        self.tx_errors = 0
        self._last_error = ""

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def bound_port(self) -> int:
        return self._sock.getsockname()[1] if self._sock is not None else 0

    @property
    def last_error(self) -> str:
        return self._last_error

    def open(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        sock.setblocking(False)
        sock.bind((self.bind_ip, self.port))
        self._sock = sock
        self._loop = loop or asyncio.get_event_loop()
        self._loop.add_reader(sock.fileno(), self._readable)

    def close(self) -> None:
        if self._sock is None:
            return
        if self._loop is not None:
            try:
                self._loop.remove_reader(self._sock.fileno())
            except Exception:
                pass
        self._sock.close()
        self._sock = None

    # -- receive ----------------------------------------------------------

    def _readable(self) -> None:
        sock = self._sock
        if sock is None:
            return
        for _ in range(_MAX_BATCH):
            try:
                data, ancdata, _flags, addr = sock.recvmsg(2048, _ANC_SPACE)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self._last_error = str(exc)
                log.warning("udp/%d receive error: %s", self.port, exc)
                return
            ifindex = 0
            local_ip = ""
            dst_ip = ""
            for level, ctype, cdata in ancdata:
                if level == socket.IPPROTO_IP and ctype == IP_PKTINFO and len(cdata) >= _PKTINFO_SIZE:
                    ifindex, spec_dst, hdr_dst = struct.unpack_from(_PKTINFO_FMT, cdata)
                    local_ip = socket.inet_ntoa(spec_dst)
                    dst_ip = socket.inet_ntoa(hdr_dst)
            self.rx_packets += 1
            try:
                self.handler(data, addr[0], addr[1], dst_ip, local_ip, ifindex)
            except Exception:
                log.exception("unhandled error while processing a packet on port %d", self.port)

    # -- send -------------------------------------------------------------

    def send(self, data: bytes, dest_ip: str, dest_port: int, src_ip: str = "", ifindex: int = 0) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            if src_ip or ifindex:
                anc = [
                    (
                        socket.IPPROTO_IP,
                        IP_PKTINFO,
                        struct.pack(
                            _PKTINFO_FMT, ifindex, socket.inet_aton(src_ip) if src_ip else b"\x00" * 4, b"\x00" * 4
                        ),
                    )
                ]
                sock.sendmsg([data], anc, 0, (dest_ip, dest_port))
            else:
                sock.sendto(data, (dest_ip, dest_port))
        except OSError as exc:
            self.tx_errors += 1
            self._last_error = f"send to {dest_ip}: {exc.strerror or exc}"
            return False
        self.tx_packets += 1
        return True

    # -- multicast --------------------------------------------------------

    def set_multicast_ttl(self, ttl: int) -> None:
        if self._sock is not None and ttl != self._ttl:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        self._ttl = ttl

    def _mreqn(self, group: str, local_ip: str, ifindex: int) -> bytes:
        return socket.inet_aton(group) + socket.inet_aton(local_ip or "0.0.0.0") + struct.pack("i", ifindex)

    def join_multicast(self, group: str, local_ip: str, ifindex: int = 0) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self._mreqn(group, local_ip, ifindex))
        except OSError as exc:
            if exc.errno == 98:  # EADDRINUSE: already a member
                return True
            self._last_error = f"join {group} on {local_ip}: {exc.strerror or exc}"
            log.warning("cannot join multicast group %s on %s: %s", group, local_ip, exc)
            return False
        return True

    def leave_multicast(self, group: str, local_ip: str, ifindex: int = 0) -> None:
        if self._sock is None:
            return
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, self._mreqn(group, local_ip, ifindex))
        except OSError:
            pass
