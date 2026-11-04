"""Table of Art-Net devices seen on each network (from their ArtPollReply)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import artnet
from .artnet import ArtPollReply


@dataclass
class Node:
    network: str
    ip: str
    bind_index: int
    reply: ArtPollReply
    first_seen: float
    last_seen: float
    replies: int = 1
    output_universes: list[int] = field(default_factory=list)
    input_universes: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.network, self.ip, self.bind_index)

    def to_dict(self, now: float) -> dict:
        r = self.reply
        return {
            "network": self.network,
            "ip": self.ip,
            "bind_index": self.bind_index,
            "short_name": r.short_name,
            "long_name": r.long_name,
            "style": r.style_name,
            "mac": r.mac_str,
            "node_report": r.node_report,
            "firmware": f"{r.version >> 8}.{r.version & 0xFF}",
            "outputs": [artnet.format_universe(u) for u in self.output_universes],
            "inputs": [artnet.format_universe(u) for u in self.input_universes],
            "age": round(now - self.last_seen, 1),
            "replies": self.replies,
        }


class NodeTable:
    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str, int], Node] = {}
        self._by_output: dict[tuple[str, int], list[str]] = {}
        self._dirty = False

    def __len__(self) -> int:
        return len(self._nodes)

    def update(self, network: str, ip: str, reply: ArtPollReply, now: float | None = None) -> Node:
        now = time.monotonic() if now is None else now
        key = (network, ip, reply.bind_index)
        node = self._nodes.get(key)
        outputs = reply.output_universes()
        inputs = reply.input_universes()
        if node is None:
            node = Node(network, ip, reply.bind_index, reply, now, now, 1, outputs, inputs)
            self._nodes[key] = node
            self._dirty = True
        else:
            if node.output_universes != outputs or node.input_universes != inputs:
                self._dirty = True
            node.reply = reply
            node.last_seen = now
            node.replies += 1
            node.output_universes = outputs
            node.input_universes = inputs
        return node

    def expire(self, timeout: float, now: float | None = None) -> list[Node]:
        now = time.monotonic() if now is None else now
        gone = [n for n in self._nodes.values() if now - n.last_seen > timeout]
        for node in gone:
            del self._nodes[node.key]
        if gone:
            self._dirty = True
        return gone

    def _rebuild(self) -> None:
        index: dict[tuple[str, int], list[str]] = {}
        for node in self._nodes.values():
            for universe in node.output_universes:
                ips = index.setdefault((node.network, universe), [])
                if node.ip not in ips:
                    ips.append(node.ip)
        self._by_output = index
        self._dirty = False

    def targets_for(self, network: str, universe: int) -> list[str]:
        """IPs of nodes on `network` that output DMX for `universe`."""
        if self._dirty:
            self._rebuild()
        return self._by_output.get((network, universe), [])

    def nodes(self, network: str | None = None) -> list[Node]:
        nodes = [n for n in self._nodes.values() if network is None or n.network == network]
        return sorted(nodes, key=lambda n: (n.network, tuple(int(p) for p in n.ip.split(".")), n.bind_index))

    def to_list(self, now: float | None = None) -> list[dict]:
        now = time.monotonic() if now is None else now
        return [n.to_dict(now) for n in self.nodes()]
