"""The bridge engine: sockets, routing, discovery and ArtPoll replies.

Data flow for one DMX frame:

    packet in  ->  classify network  ->  decode  ->  DmxFrame
       ->  route table lookup  ->  encode per destination  ->  packet(s) out

Loop protection: a packet whose source IP is one of this host's configured
network IPs is dropped before anything else happens, so the bridge never
re-forwards its own output (Linux hands our own broadcasts back to us).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import __version__, artnet, netinfo, sacn
from .artnet import ARTNET_PORT, ArtPoll, ArtPollReply
from .config import Config, NetworkConfig, OutputConfig, RouteConfig, format_universe
from .discovery import NodeTable
from .frame import ARTNET, SACN, DmxFrame
from .sacn import SACN_PORT
from .stats import RateCounter
from .transport import UdpTransport

log = logging.getLogger(__name__)

ACTIVE_WINDOW = 4.0  # a universe counts as live if seen within this many seconds
UNROUTED_TTL = 20.0
INTERFACE_REFRESH = 10.0
POLL_TICK = 0.5

# Opcodes passed through untouched (broadcast to the other networks) when
# forward_other is on.
PASS_THROUGH_OPS = (artnet.OP_TIME_CODE, artnet.OP_TRIGGER, artnet.OP_COMMAND)


@dataclass
class NetworkState:
    cfg: NetworkConfig
    ifindex: int = 0
    interface: str = ""
    mac: bytes = b"\x00" * 6
    present: bool = False
    ports: list[tuple[int, bool, bool]] = field(default_factory=list)  # (port address, is output, is input)
    artnet_rx: RateCounter = field(default_factory=RateCounter)
    artnet_tx: RateCounter = field(default_factory=RateCounter)
    sacn_rx: RateCounter = field(default_factory=RateCounter)
    sacn_tx: RateCounter = field(default_factory=RateCounter)
    polls_rx: RateCounter = field(default_factory=RateCounter)
    replies_rx: RateCounter = field(default_factory=RateCounter)
    replies_tx: RateCounter = field(default_factory=RateCounter)
    last_rx: dict[tuple[str, int], float] = field(default_factory=dict)
    last_tx: dict[tuple[str, int], float] = field(default_factory=dict)
    other_ops: dict[int, int] = field(default_factory=dict)
    joined: set[int] = field(default_factory=set)
    wanted_groups: set[int] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def ip(self) -> str:
        return self.cfg.ip

    @property
    def broadcast(self) -> str:
        return self.cfg.broadcast


@dataclass
class OutputState:
    cfg: OutputConfig
    net: NetworkState
    seq: int = 0
    tx: RateCounter = field(default_factory=RateCounter)
    errors: int = 0
    current_targets: list[str] = field(default_factory=list)


@dataclass
class RouteState:
    cfg: RouteConfig
    outputs: list[OutputState]
    rx: RateCounter = field(default_factory=RateCounter)
    last_source: str = ""


@dataclass
class Unrouted:
    network: str
    protocol: str
    universe: int
    source: str
    counter: RateCounter = field(default_factory=RateCounter)


class Engine:
    def __init__(
        self,
        config: Config,
        *,
        transport_factory: Callable[..., UdpTransport] = UdpTransport,
        address_source: Callable[[], list[netinfo.Address]] = netinfo.list_addresses,
        hostname: str | None = None,
    ) -> None:
        self.config = config
        self.hostname = hostname or socket.gethostname()
        self.nodes = NodeTable()
        self._transport_factory = transport_factory
        self._address_source = address_source
        self._cid = config.cid_bytes
        self.networks: dict[str, NetworkState] = {name: NetworkState(cfg=net) for name, net in config.networks.items()}
        self._by_ip: dict[str, NetworkState] = {s.ip: s for s in self.networks.values()}
        self._own_ips: set[str] = set(self._by_ip)
        self._by_ifindex: dict[int, list[NetworkState]] = {}
        self.routes: list[RouteState] = []
        self._table: dict[tuple[str, str, int], list[RouteState]] = {}
        self.unrouted: dict[tuple[str, str, int], Unrouted] = {}
        self.dropped: dict[str, int] = {}
        self.artnet_transport: UdpTransport | None = None
        self.sacn_transport: UdpTransport | None = None
        self._tasks: list[asyncio.Task] = []
        self._poll_counter = 0
        self._last_discovery = 0.0
        self._last_refresh = 0.0
        self.started_at = 0.0
        self.running = False
        self._build_routes()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _build_routes(self) -> None:
        self.routes = []
        self._table = {}
        for rcfg in self.config.routes:
            outputs = [OutputState(cfg=o, net=self.networks[o.network]) for o in rcfg.outputs]
            state = RouteState(cfg=rcfg, outputs=outputs)
            self.routes.append(state)
            if rcfg.enabled:
                self._table.setdefault(rcfg.source.key, []).append(state)
        for net in self.networks.values():
            outs: set[int] = set()
            ins: set[int] = set()
            groups: set[int] = set()
            for route in self.routes:
                if not route.cfg.enabled:
                    continue
                src = route.cfg.source
                if src.network == net.name:
                    if src.protocol == ARTNET:
                        outs.add(src.universe)
                    else:
                        groups.add(src.universe)
                for out in route.outputs:
                    if out.cfg.network == net.name and out.cfg.protocol == ARTNET:
                        ins.add(out.cfg.universe)
            net.ports = [(pa, pa in outs, pa in ins) for pa in sorted(outs | ins)]
            net.wanted_groups = groups

    @property
    def uses_artnet(self) -> bool:
        return any(n.cfg.artnet.enabled for n in self.networks.values())

    @property
    def uses_sacn(self) -> bool:
        return any(n.cfg.sacn.enabled for n in self.networks.values())

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.refresh_interfaces()
        if self.uses_artnet:
            self.artnet_transport = self._transport_factory(ARTNET_PORT, self._on_artnet)
            self.artnet_transport.open(loop)
        if self.uses_sacn:
            self.sacn_transport = self._transport_factory(SACN_PORT, self._on_sacn)
            self.sacn_transport.open(loop)
            self._join_groups()
        self.running = True
        self.started_at = time.monotonic()
        self.announce()
        self._tasks = [loop.create_task(self._poll_loop()), loop.create_task(self._tick_loop())]
        log.info(
            "engine started: %d network(s), %d route(s), Art-Net %s, sACN %s",
            len(self.networks),
            len(self.routes),
            "on" if self.artnet_transport else "off",
            "on" if self.sacn_transport else "off",
        )

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for transport in (self.artnet_transport, self.sacn_transport):
            if transport is not None:
                transport.close()
        self.artnet_transport = None
        self.sacn_transport = None
        log.info("engine stopped")

    def refresh_interfaces(self) -> None:
        """Re-read the host's addresses and map each network to an interface."""
        addresses = self._address_source()
        by_ifindex: dict[int, list[NetworkState]] = {}
        for state in self.networks.values():
            match = None
            for addr in addresses:
                if addr.ip != state.ip:
                    continue
                if state.cfg.interface and addr.interface != state.cfg.interface:
                    continue
                match = addr
                break
            was_present = state.present
            if match is not None:
                state.present = True
                state.ifindex = match.index
                state.interface = match.interface
                state.mac = _parse_mac(match.mac)
            else:
                state.present = False
                if state.cfg.interface:
                    try:
                        state.ifindex = socket.if_nametoindex(state.cfg.interface)
                        state.interface = state.cfg.interface
                    except OSError:
                        state.ifindex = 0
            if state.present != was_present or not self._last_refresh:
                if state.present:
                    log.info("network '%s': %s is up on %s", state.name, state.cfg.cidr, state.interface)
                else:
                    log.warning(
                        "network '%s': address %s is not on any interface; nothing will be received or sent for it",
                        state.name,
                        state.ip,
                    )
            if state.ifindex:
                by_ifindex.setdefault(state.ifindex, []).append(state)
        self._by_ifindex = by_ifindex
        self._last_refresh = time.monotonic()

    def _join_groups(self) -> None:
        transport = self.sacn_transport
        if transport is None:
            return
        for state in self.networks.values():
            if not state.cfg.sacn.enabled or not state.present:
                continue
            for universe in state.wanted_groups - state.joined:
                if transport.join_multicast(sacn.multicast_address(universe), state.ip, state.ifindex):
                    state.joined.add(universe)
                    log.info("network '%s': listening for sACN universe %d", state.name, universe)

    # ------------------------------------------------------------------
    # Packet classification
    # ------------------------------------------------------------------

    def _classify(self, src_ip: str, dst_ip: str, local_ip: str, ifindex: int) -> NetworkState | None:
        state = self._by_ip.get(local_ip)
        if state is not None:
            return state
        candidates = self._by_ifindex.get(ifindex)
        if candidates and len(candidates) == 1:
            return candidates[0]
        pool = candidates or list(self.networks.values())
        for state in pool:
            if dst_ip == state.ip or dst_ip == state.broadcast:
                return state
        for state in pool:
            if state.cfg.contains(src_ip):
                return state
        return None

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    # ------------------------------------------------------------------
    # Art-Net
    # ------------------------------------------------------------------

    def _on_artnet(self, data: bytes, src_ip: str, src_port: int, dst_ip: str, local_ip: str, ifindex: int) -> None:
        if src_ip in self._own_ips:
            return
        op = artnet.opcode(data)
        if op is None:
            self._drop("not art-net")
            return
        state = self._classify(src_ip, dst_ip, local_ip, ifindex)
        if state is None:
            self._drop("unknown network")
            return
        if not state.cfg.artnet.enabled:
            self._drop("art-net disabled")
            return
        now = time.monotonic()
        state.artnet_rx.hit(now)
        if op == artnet.OP_DMX or op == artnet.OP_NZS:
            dmx = artnet.decode_dmx(data)
            if dmx is None:
                self._drop("bad ArtDmx")
                return
            state.last_rx[(ARTNET, dmx.universe)] = now
            frame = DmxFrame(state.name, ARTNET, dmx.universe, dmx.data, dmx.start_code, 100, dmx.sequence, src_ip)
            self._route(state, frame, now)
        elif op == artnet.OP_POLL:
            poll = artnet.decode_poll(data)
            if poll is not None:
                state.polls_rx.hit(now)
                self._reply_to_poll(state, poll, src_ip, now)
        elif op == artnet.OP_POLL_REPLY:
            reply = artnet.decode_poll_reply(data)
            if reply is not None:
                state.replies_rx.hit(now)
                node = self.nodes.update(state.name, src_ip, reply, now)
                if node.replies == 1:
                    log.info(
                        "network '%s': found %s '%s' at %s (outputs %s)",
                        state.name,
                        reply.style_name.lower(),
                        reply.short_name,
                        src_ip,
                        ", ".join(artnet.format_universe(u) for u in node.output_universes) or "none",
                    )
        elif op == artnet.OP_SYNC:
            if self.config.forward_sync:
                self._broadcast_others(state, data, now)
        elif op in PASS_THROUGH_OPS and self.config.forward_other:
            self._broadcast_others(state, data, now)
        else:
            state.other_ops[op] = state.other_ops.get(op, 0) + 1

    def _broadcast_others(self, origin: NetworkState, data: bytes, now: float) -> None:
        transport = self.artnet_transport
        if transport is None:
            return
        for state in self.networks.values():
            if state is origin or not state.cfg.artnet.enabled or not state.present:
                continue
            if transport.send(data, state.broadcast, ARTNET_PORT, state.ip, state.ifindex):
                state.artnet_tx.hit(now)

    def _reply_to_poll(self, state: NetworkState, poll: ArtPoll, src_ip: str, now: float) -> None:
        transport = self.artnet_transport
        if transport is None:
            return
        dest = src_ip if state.cfg.artnet.reply == "unicast" else state.broadcast
        for packet in self.build_poll_replies(state, poll, now):
            if transport.send(packet, dest, ARTNET_PORT, state.ip, state.ifindex):
                state.replies_tx.hit(now)
                state.artnet_tx.hit(now)

    def announce(self) -> None:
        """Broadcast an unsolicited ArtPollReply on every Art-Net network."""
        transport = self.artnet_transport
        if transport is None:
            return
        now = time.monotonic()
        for state in self.networks.values():
            if not state.cfg.artnet.enabled or not state.present:
                continue
            for packet in self.build_poll_replies(state, None, now):
                if transport.send(packet, state.broadcast, ARTNET_PORT, state.ip, state.ifindex):
                    state.replies_tx.hit(now)

    def long_name(self, state: NetworkState) -> str:
        return state.cfg.artnet.long_name or f"ArtBridge on {self.hostname} ({state.name})"

    def build_poll_replies(self, state: NetworkState, poll: ArtPoll | None, now: float) -> list[bytes]:
        """One ArtPollReply per group of up to four ports sharing Net and Sub-Net."""
        ports = [p for p in state.ports if poll is None or poll.wants(p[0])]
        if not ports and poll is not None and poll.target_top is not None:
            return []
        groups: dict[tuple[int, int], list[tuple[int, bool, bool]]] = {}
        for pa, is_out, is_in in ports:
            net, sub, _ = artnet.split_port_address(pa)
            groups.setdefault((net, sub), []).append((pa, is_out, is_in))
        chunks: list[tuple[int, int, list[tuple[int, bool, bool]]]] = []
        for (net, sub), items in groups.items():
            for i in range(0, len(items), 4):
                chunks.append((net, sub, items[i : i + 4]))
        if not chunks:
            chunks = [(0, 0, [])]
        self._poll_counter += 1
        version = _version_word(__version__)
        status2 = artnet.STATUS2_PORT_ADDRESS_15BIT | artnet.STATUS2_WEB_CONFIG
        if state.cfg.sacn.enabled:
            status2 |= artnet.STATUS2_SACN_CAPABLE
        report = artnet.node_report(artnet.RC_POWER_OK, self._poll_counter, f"ArtBridge ok, {len(self._table)} routes")
        replies: list[bytes] = []
        for index, (net, sub, items) in enumerate(chunks, start=1):
            port_types = bytearray(4)
            good_in = bytearray(4)
            good_out = bytearray(4)
            sw_in = bytearray(4)
            sw_out = bytearray(4)
            for i, (pa, is_out, is_in) in enumerate(items):
                uni = pa & 0x0F
                if is_out:
                    port_types[i] |= artnet.PORT_TYPE_OUTPUT
                    sw_out[i] = uni
                    if now - state.last_rx.get((ARTNET, pa), -1e9) < ACTIVE_WINDOW:
                        good_out[i] |= artnet.GOOD_OUTPUT_DATA_TRANSMITTED
                if is_in:
                    port_types[i] |= artnet.PORT_TYPE_INPUT
                    sw_in[i] = uni
                    if now - state.last_tx.get((ARTNET, pa), -1e9) < ACTIVE_WINDOW:
                        good_in[i] |= artnet.GOOD_INPUT_DATA_RECEIVED
            reply = ArtPollReply(
                ip=state.ip,
                version=version,
                net_switch=net,
                sub_switch=sub,
                oem=0x00FF,
                status1=0xE0,
                esta_man=0x7FF0,
                short_name=state.cfg.artnet.short_name,
                long_name=self.long_name(state),
                node_report=report,
                num_ports=len(items),
                port_types=bytes(port_types),
                good_input=bytes(good_in),
                good_output=bytes(good_out),
                sw_in=bytes(sw_in),
                sw_out=bytes(sw_out),
                style=artnet.STYLE_NODE,
                mac=state.mac,
                bind_ip=state.ip,
                bind_index=index,
                status2=status2,
            )
            replies.append(artnet.encode_poll_reply(reply))
        return replies

    # ------------------------------------------------------------------
    # sACN
    # ------------------------------------------------------------------

    def _on_sacn(self, data: bytes, src_ip: str, src_port: int, dst_ip: str, local_ip: str, ifindex: int) -> None:
        if src_ip in self._own_ips:
            return
        if not sacn.is_sacn(data):
            self._drop("not sacn")
            return
        state = self._classify(src_ip, dst_ip, local_ip, ifindex)
        if state is None:
            self._drop("unknown network")
            return
        if not state.cfg.sacn.enabled:
            self._drop("sacn disabled")
            return
        packet = sacn.decode_data(data)
        if packet is None:
            return  # sync or discovery packet
        if packet.preview or packet.terminated:
            return
        now = time.monotonic()
        state.sacn_rx.hit(now)
        state.last_rx[(SACN, packet.universe)] = now
        frame = DmxFrame(
            state.name, SACN, packet.universe, packet.data, packet.start_code, packet.priority, packet.sequence, src_ip
        )
        self._route(state, frame, now)

    def _send_discovery(self, now: float) -> None:
        transport = self.sacn_transport
        if transport is None:
            return
        for state in self.networks.values():
            if not state.cfg.sacn.enabled or not state.present:
                continue
            universes = sorted(
                {
                    out.cfg.universe
                    for route in self.routes
                    if route.cfg.enabled
                    for out in route.outputs
                    if out.cfg.network == state.name and out.cfg.protocol == SACN
                }
            )
            if not universes:
                continue
            pages = [universes[i : i + 512] for i in range(0, len(universes), 512)]
            transport.set_multicast_ttl(state.cfg.sacn.ttl)
            for page, chunk in enumerate(pages):
                packet = sacn.encode_discovery(self._cid, self.config.sacn_source_name, chunk, page, len(pages) - 1)
                transport.send(
                    packet, sacn.multicast_address(sacn.DISCOVERY_UNIVERSE), SACN_PORT, state.ip, state.ifindex
                )

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route(self, state: NetworkState, frame: DmxFrame, now: float) -> None:
        routes = self._table.get((state.name, frame.protocol, frame.universe))
        if not routes:
            key = (state.name, frame.protocol, frame.universe)
            entry = self.unrouted.get(key)
            if entry is None:
                entry = self.unrouted[key] = Unrouted(state.name, frame.protocol, frame.universe, frame.source_ip)
                log.info(
                    "network '%s': %s universe %s from %s has no route",
                    state.name,
                    frame.protocol,
                    format_universe(frame.protocol, frame.universe),
                    frame.source_ip,
                )
            entry.source = frame.source_ip
            entry.counter.hit(now)
            return
        for route in routes:
            route.rx.hit(now)
            route.last_source = frame.source_ip
            for output in route.outputs:
                self._emit(output, frame, now)

    def _emit(self, output: OutputState, frame: DmxFrame, now: float) -> None:
        cfg = output.cfg
        net = output.net
        if cfg.protocol == ARTNET:
            transport = self.artnet_transport
            if transport is None:
                return
            output.seq = output.seq % 255 + 1
            packet = artnet.encode_dmx(cfg.universe, frame.data, output.seq, 0, frame.start_code)
            if cfg.mode == "broadcast":
                targets = [net.broadcast]
            elif cfg.mode == "unicast":
                targets = cfg.targets
            else:
                targets = self.nodes.targets_for(net.name, cfg.universe) or [net.broadcast]
            output.current_targets = targets
            port = ARTNET_PORT
            counter = net.artnet_tx
        else:
            transport = self.sacn_transport
            if transport is None:
                return
            output.seq = (output.seq + 1) & 0xFF
            priority = cfg.priority if cfg.priority is not None else frame.priority
            packet = sacn.encode_data(
                cfg.universe,
                frame.data,
                self._cid,
                self.config.sacn_source_name,
                priority,
                output.seq,
                frame.start_code,
            )
            if cfg.mode == "multicast":
                targets = [sacn.multicast_address(cfg.universe)]
                transport.set_multicast_ttl(net.cfg.sacn.ttl)
            else:
                targets = cfg.targets
            output.current_targets = targets
            port = SACN_PORT
            counter = net.sacn_tx
        for target in targets:
            if transport.send(packet, target, port, net.ip, net.ifindex):
                output.tx.hit(now)
                counter.hit(now)
            else:
                output.errors += 1
        net.last_tx[(cfg.protocol, cfg.universe)] = now

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def send_polls(self, now: float | None = None) -> None:
        transport = self.artnet_transport
        if transport is None:
            return
        now = time.monotonic() if now is None else now
        packet = artnet.encode_poll()
        for state in self.networks.values():
            if state.cfg.artnet.enabled and state.present:
                if transport.send(packet, state.broadcast, ARTNET_PORT, state.ip, state.ifindex):
                    state.artnet_tx.hit(now)

    async def _poll_loop(self) -> None:
        due: dict[str, float] = {name: 0.0 for name in self.networks}
        packet = artnet.encode_poll()
        while True:
            now = time.monotonic()
            transport = self.artnet_transport
            if transport is not None:
                for state in self.networks.values():
                    interval = state.cfg.artnet.poll_interval
                    if not state.cfg.artnet.enabled or interval <= 0 or not state.present:
                        continue
                    if now >= due[state.name]:
                        due[state.name] = now + interval
                        if transport.send(packet, state.broadcast, ARTNET_PORT, state.ip, state.ifindex):
                            state.artnet_tx.hit(now)
            for node in self.nodes.expire(self.config.node_timeout, now):
                log.info("network '%s': lost '%s' at %s", node.network, node.reply.short_name, node.ip)
            await asyncio.sleep(POLL_TICK)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            self._tick_counters(now)
            for key, entry in list(self.unrouted.items()):
                if now - entry.counter.last > UNROUTED_TTL:
                    del self.unrouted[key]
            if now - self._last_refresh >= INTERFACE_REFRESH:
                self.refresh_interfaces()
                self._join_groups()
            if now - self._last_discovery >= sacn.DISCOVERY_INTERVAL:
                self._last_discovery = now
                self._send_discovery(now)

    def _tick_counters(self, now: float) -> None:
        for state in self.networks.values():
            for counter in (
                state.artnet_rx,
                state.artnet_tx,
                state.sacn_rx,
                state.sacn_tx,
                state.polls_rx,
                state.replies_rx,
                state.replies_tx,
            ):
                counter.tick(now)
        for route in self.routes:
            route.rx.tick(now)
            for output in route.outputs:
                output.tx.tick(now)
        for entry in self.unrouted.values():
            entry.counter.tick(now)

    # ------------------------------------------------------------------
    # Status for the web UI
    # ------------------------------------------------------------------

    def status(self) -> dict:
        now = time.monotonic()
        networks = []
        for state in self.networks.values():
            live_rx = sorted(format_universe(p, u) for (p, u), t in state.last_rx.items() if now - t < ACTIVE_WINDOW)
            networks.append(
                {
                    "name": state.name,
                    "ip": state.ip,
                    "cidr": state.cfg.cidr,
                    "broadcast": state.broadcast,
                    "interface": state.interface,
                    "present": state.present,
                    "mac": ":".join(f"{b:02x}" for b in state.mac),
                    "artnet": {
                        "enabled": state.cfg.artnet.enabled,
                        "reply": state.cfg.artnet.reply,
                        "poll_interval": state.cfg.artnet.poll_interval,
                        "short_name": state.cfg.artnet.short_name,
                        "long_name": self.long_name(state),
                        "rx": state.artnet_rx.to_dict(now),
                        "tx": state.artnet_tx.to_dict(now),
                        "polls_rx": state.polls_rx.to_dict(now),
                        "replies_rx": state.replies_rx.to_dict(now),
                        "replies_tx": state.replies_tx.to_dict(now),
                        "ports": [
                            {"universe": artnet.format_universe(pa), "output": is_out, "input": is_in}
                            for pa, is_out, is_in in state.ports
                        ],
                        "other_ops": {artnet.opcode_name(op): n for op, n in state.other_ops.items()},
                    },
                    "sacn": {
                        "enabled": state.cfg.sacn.enabled,
                        "ttl": state.cfg.sacn.ttl,
                        "rx": state.sacn_rx.to_dict(now),
                        "tx": state.sacn_tx.to_dict(now),
                        "listening": sorted(state.joined),
                    },
                    "live_universes": live_rx,
                }
            )
        routes = []
        for route in self.routes:
            src = route.cfg.source
            routes.append(
                {
                    "name": route.cfg.name,
                    "enabled": route.cfg.enabled,
                    "from": {"network": src.network, "protocol": src.protocol, "universe": src.label},
                    "rx": route.rx.to_dict(now),
                    "last_source": route.last_source,
                    "outputs": [
                        {
                            "network": o.cfg.network,
                            "protocol": o.cfg.protocol,
                            "universe": o.cfg.label,
                            "mode": o.cfg.mode,
                            "targets": o.current_targets or o.cfg.targets,
                            "priority": o.cfg.priority,
                            "tx": o.tx.to_dict(now),
                            "errors": o.errors,
                        }
                        for o in route.outputs
                    ],
                }
            )
        unrouted = [
            {
                "network": u.network,
                "protocol": u.protocol,
                "universe": format_universe(u.protocol, u.universe),
                "source": u.source,
                "rx": u.counter.to_dict(now),
            }
            for u in sorted(self.unrouted.values(), key=lambda u: (u.network, u.protocol, u.universe))
        ]
        transports = {}
        for name, transport in (("artnet", self.artnet_transport), ("sacn", self.sacn_transport)):
            transports[name] = (
                None
                if transport is None
                else {
                    "port": transport.port,
                    "rx": transport.rx_packets,
                    "tx": transport.tx_packets,
                    "tx_errors": transport.tx_errors,
                    "last_error": transport.last_error,
                }
            )
        return {
            "version": __version__,
            "hostname": self.hostname,
            "running": self.running,
            "uptime": round(now - self.started_at) if self.started_at else 0,
            "networks": networks,
            "routes": routes,
            "nodes": self.nodes.to_list(now),
            "unrouted": unrouted,
            "dropped": dict(self.dropped),
            "transports": transports,
            "forward_sync": self.config.forward_sync,
            "forward_other": self.config.forward_other,
        }


def _parse_mac(text: str) -> bytes:
    try:
        raw = bytes(int(part, 16) for part in text.split(":"))
    except ValueError:
        return b"\x00" * 6
    return raw[:6].ljust(6, b"\x00") if len(raw) == 6 else b"\x00" * 6


def _version_word(version: str) -> int:
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0
    return ((major & 0xFF) << 8) | (minor & 0xFF)
