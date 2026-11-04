"""Test helpers: a fake console (`artbridge send`) and a fake node (`artbridge listen`).

Run `listen` on a laptop in the destination network and `send` on a laptop in
the source network to prove a bridge works without touching real gear.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
import time
import uuid

from . import artnet, sacn
from .artnet import ARTNET_PORT
from .frame import ARTNET, SACN
from .sacn import SACN_PORT
from .transport import UdpTransport

log = logging.getLogger(__name__)


def _parse_universes(text: str, protocol: str) -> list[int]:
    result: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            first = artnet.parse_universe(a) if protocol == ARTNET else sacn.parse_universe(a)
            last = artnet.parse_universe(b) if protocol == ARTNET else sacn.parse_universe(b)
            result.extend(range(first, last + 1))
        else:
            result.append(artnet.parse_universe(part) if protocol == ARTNET else sacn.parse_universe(part))
    return result


def _label(protocol: str, universe: int) -> str:
    return artnet.format_universe(universe) if protocol == ARTNET else str(universe)


# ----------------------------------------------------------------------------
# send
# ----------------------------------------------------------------------------


def _pattern(name: str, tick: int, level: int) -> bytes:
    if name == "static":
        return bytes([level] * 512)
    if name == "chase":
        data = bytearray(512)
        data[tick % 512] = level
        data[(tick + 1) % 512] = level // 2
        return bytes(data)
    if name == "sine":
        return bytes(int((math.sin((tick + i) / 10.0) + 1) / 2 * level) for i in range(512))
    # ramp: every channel counts up together
    return bytes([(tick % 256) * level // 255] * 512)


async def _send(args: argparse.Namespace) -> int:
    protocol = SACN if args.sacn else ARTNET
    universes = _parse_universes(args.universe, protocol)
    transport = UdpTransport(0, lambda *a: None, bind_ip=args.from_ip or "")
    transport.open(asyncio.get_running_loop())
    cid = uuid.uuid4().bytes
    dest_port = SACN_PORT if protocol == SACN else ARTNET_PORT
    interval = 1.0 / args.fps
    tick = 0
    seqs = {u: 0 for u in universes}
    print(
        f"sending {protocol} universes {', '.join(_label(protocol, u) for u in universes)} to {args.to or 'multicast'} at {args.fps} fps, pattern {args.pattern}"
    )
    next_time = time.monotonic()
    try:
        while True:
            data = _pattern(args.pattern, tick, args.level)[: args.channels]
            for u in universes:
                seqs[u] = seqs[u] % 255 + 1
                if protocol == ARTNET:
                    packet = artnet.encode_dmx(u, data, seqs[u])
                    dest = args.to or "255.255.255.255"
                else:
                    packet = sacn.encode_data(u, data, cid, "artbridge send", args.priority, seqs[u] & 0xFF)
                    dest = args.to or sacn.multicast_address(u)
                transport.send(packet, dest, dest_port, args.from_ip or "")
            if args.sync and protocol == ARTNET:
                transport.send(artnet.encode_sync(), args.to or "255.255.255.255", ARTNET_PORT, args.from_ip or "")
            tick += 1
            if tick % args.fps == 0:
                print(f"\r{transport.tx_packets} packets sent, {transport.tx_errors} errors   ", end="", flush=True)
            next_time += interval
            await asyncio.sleep(max(0.0, next_time - time.monotonic()))
    except asyncio.CancelledError:
        pass
    finally:
        transport.close()
        print()
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_send(args))
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------------
# listen
# ----------------------------------------------------------------------------


class _Listener:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.protocol = SACN if args.sacn else ARTNET
        self.universes = _parse_universes(args.universes, self.protocol) if args.universes else []
        self.seen: dict[tuple[str, int], dict] = {}
        self.polls = 0
        self.transport: UdpTransport | None = None
        self.own_ip = args.bind_ip or ""

    def on_packet(self, data: bytes, src_ip: str, src_port: int, dst_ip: str, local_ip: str, ifindex: int) -> None:
        now = time.monotonic()
        if self.protocol == ARTNET:
            op = artnet.opcode(data)
            if op == artnet.OP_POLL:
                self.polls += 1
                if not self.args.no_reply:
                    self.reply(src_ip, local_ip, ifindex)
                return
            if op == artnet.OP_POLL_REPLY:
                r = artnet.decode_poll_reply(data)
                if r and self.args.verbose:
                    print(
                        f"\nArtPollReply from {src_ip}: {r.short_name!r} outputs {[artnet.format_universe(u) for u in r.output_universes()]}"
                    )
                return
            dmx = artnet.decode_dmx(data)
            if dmx is None:
                return
            universe, slots, seq = dmx.universe, dmx.data, dmx.sequence
        else:
            pkt = sacn.decode_data(data)
            if pkt is None:
                return
            universe, slots, seq = pkt.universe, pkt.data, pkt.sequence
        if self.universes and universe not in self.universes:
            return
        entry = self.seen.setdefault(
            (src_ip, universe),
            {"count": 0, "window": 0, "fps": 0.0, "t0": now, "last": now, "seq": 0, "lost": 0, "to": dst_ip},
        )
        if entry["count"] and seq and entry["seq"] and seq != entry["seq"] % 255 + 1 and self.protocol == ARTNET:
            entry["lost"] += 1
        if entry["count"] and self.protocol == SACN and seq != (entry["seq"] + 1) & 0xFF:
            entry["lost"] += 1
        entry["seq"] = seq
        entry["count"] += 1
        entry["window"] += 1
        entry["last"] = now
        entry["to"] = dst_ip
        entry["first8"] = " ".join(f"{b:3d}" for b in slots[:8])

    def reply(self, src_ip: str, local_ip: str, ifindex: int) -> None:
        if self.transport is None:
            return
        ip = self.own_ip or local_ip or "0.0.0.0"
        universes = self.universes or [0]
        groups: dict[tuple[int, int], list[int]] = {}
        for pa in universes:
            net, sub, _ = artnet.split_port_address(pa)
            groups.setdefault((net, sub), []).append(pa)
        index = 1
        for (net, sub), items in groups.items():
            for i in range(0, len(items), 4):
                chunk = items[i : i + 4]
                reply = artnet.ArtPollReply(
                    ip=ip,
                    net_switch=net,
                    sub_switch=sub,
                    oem=0x00FF,
                    esta_man=0x7FF0,
                    status1=0xE0,
                    short_name=self.args.name[:17],
                    long_name=f"{self.args.name} (artbridge listen)",
                    node_report=artnet.node_report(artnet.RC_POWER_OK, self.polls, "fake node"),
                    num_ports=len(chunk),
                    port_types=bytes([artnet.PORT_TYPE_OUTPUT] * len(chunk)).ljust(4, b"\x00"),
                    sw_out=bytes(pa & 0x0F for pa in chunk).ljust(4, b"\x00"),
                    bind_ip=ip,
                    bind_index=index,
                    status2=artnet.STATUS2_PORT_ADDRESS_15BIT,
                )
                dest = src_ip if self.args.unicast_reply else "255.255.255.255"
                self.transport.send(
                    artnet.encode_poll_reply(reply), dest, ARTNET_PORT, self.own_ip, ifindex if not self.own_ip else 0
                )
                index += 1

    def render(self) -> None:
        now = time.monotonic()
        lines = [
            f"artbridge listen ({self.protocol}) - {len(self.seen)} stream(s), {self.polls} ArtPoll(s) answered. Ctrl-C to quit."
        ]
        lines.append(
            f"{'source':<16} {'universe':<9} {'to':<16} {'fps':>6} {'packets':>9} {'lost':>5}  first 8 channels"
        )
        for (src, universe), e in sorted(self.seen.items()):
            age = now - e["last"]
            fps = f"{e['fps']:.1f}" if age < 2 else "stale"
            lines.append(
                f"{src:<16} {_label(self.protocol, universe):<9} {e['to']:<16} {fps:>6} {e['count']:>9} {e['lost']:>5}  {e.get('first8', '')}"
            )
        sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines) + "\n")
        sys.stdout.flush()

    async def run(self) -> int:
        port = SACN_PORT if self.protocol == SACN else ARTNET_PORT
        self.transport = UdpTransport(port, self.on_packet)
        self.transport.open(asyncio.get_running_loop())
        if self.protocol == SACN:
            for u in self.universes:
                self.transport.join_multicast(sacn.multicast_address(u), self.own_ip or "0.0.0.0")
        last = time.monotonic()
        try:
            while True:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                for e in self.seen.values():
                    e["fps"] = e["window"] / (now - last)
                    e["window"] = 0
                last = now
                self.render()
        except asyncio.CancelledError:
            pass
        finally:
            self.transport.close()
        return 0


def cmd_listen(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_Listener(args).run())
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------------


def add_test_commands(sub: argparse._SubParsersAction) -> None:
    send = sub.add_parser("send", help="act like a console: send test DMX")
    send.add_argument("--to", default="", help="destination IP (default: broadcast, or sACN multicast)")
    send.add_argument("--from-ip", default="", help="send from this local IP (picks the network)")
    send.add_argument("-u", "--universe", default="0.0.0", help="universe(s), e.g. 0.0.0 or 0.0.0-0.0.3 or 1,2,3")
    send.add_argument("--fps", type=float, default=30.0)
    send.add_argument("--pattern", default="ramp", choices=["ramp", "chase", "sine", "static"])
    send.add_argument("--level", type=int, default=255)
    send.add_argument("--channels", type=int, default=512)
    send.add_argument("--sync", action="store_true", help="send ArtSync after each frame")
    send.add_argument("--sacn", action="store_true", help="send sACN instead of Art-Net")
    send.add_argument("--priority", type=int, default=100, help="sACN priority")
    send.set_defaults(func=cmd_send)

    listen = sub.add_parser("listen", help="act like a node: answer ArtPoll and show incoming DMX")
    listen.add_argument("--bind-ip", default="", help="this host's IP on the network to listen on (optional)")
    listen.add_argument(
        "-u", "--universes", default="", help="universes to advertise/show, e.g. 0.0.0-0.0.3 (default: show all)"
    )
    listen.add_argument("--name", default="FakeNode")
    listen.add_argument("--no-reply", action="store_true", help="do not answer ArtPoll")
    listen.add_argument("--unicast-reply", action="store_true", help="answer ArtPoll by unicast instead of broadcast")
    listen.add_argument("--sacn", action="store_true", help="listen for sACN instead of Art-Net")
    listen.add_argument("-v", "--verbose", action="store_true")
    listen.set_defaults(func=cmd_listen)
