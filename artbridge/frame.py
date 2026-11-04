"""The protocol-neutral DMX frame that flows through the router."""

from __future__ import annotations

from dataclasses import dataclass

ARTNET = "artnet"
SACN = "sacn"
PROTOCOLS = (ARTNET, SACN)


@dataclass(slots=True)
class DmxFrame:
    network: str  # name of the network the frame arrived on
    protocol: str  # ARTNET or SACN
    universe: int  # protocol native universe id
    data: bytes  # DMX slots, without the start code (1..512 bytes)
    start_code: int = 0
    priority: int = 100  # sACN priority; 100 when the frame came from Art-Net
    sequence: int = 0
    source_ip: str = ""
