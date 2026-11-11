import asyncio
import json

import pytest

from artbridge import artnet, netinfo, sacn
from artbridge.config import parse_config
from artbridge.discovery import NodeTable
from artbridge.engine import ACTIVE_WINDOW, Engine

CONSOLE_IP = "2.0.0.10"
STAGE_IP = "10.20.30.5"
ADDRESSES = [
    netinfo.Address("eth0", 4, CONSOLE_IP, 8, "aa:bb:cc:dd:ee:01"),
    netinfo.Address("eth1", 5, STAGE_IP, 24, "aa:bb:cc:dd:ee:02"),
]


class FakeTransport:
    def __init__(self, port, handler, **kwargs):
        self.port = port
        self.handler = handler
        self.sent = []
        self.joined = []
        self.rx_packets = 0
        self.tx_packets = 0
        self.tx_errors = 0
        self.last_error = ""
        self.ttl = None
        self.opened = False
        self.closed = False

    def open(self, loop=None):
        self.opened = True

    def close(self):
        self.closed = True

    def send(self, data, ip, port, src_ip="", ifindex=0):
        self.sent.append((ip, port, src_ip, ifindex, data))
        self.tx_packets += 1
        return True

    def join_multicast(self, group, local_ip, ifindex=0):
        self.joined.append((group, local_ip, ifindex))
        return True

    def leave_multicast(self, group, local_ip, ifindex=0):
        pass

    def set_multicast_ttl(self, ttl):
        self.ttl = ttl

    def take(self):
        sent, self.sent = self.sent, []
        return sent


def make_engine(routes, *, console=None, stage=None, **extra):
    raw = {
        "networks": {
            "console": console or {"cidr": f"{CONSOLE_IP}/8", "artnet": {"enabled": True}, "sacn": {"enabled": True}},
            "stage": stage or {"cidr": f"{STAGE_IP}/24", "artnet": {"enabled": True}, "sacn": {"enabled": True}},
        },
        "routes": routes,
    }
    raw.update(extra)
    engine = Engine(
        parse_config(raw), transport_factory=FakeTransport, address_source=lambda: ADDRESSES, hostname="box"
    )
    engine.refresh_interfaces()
    engine.artnet_transport = FakeTransport(6454, engine._on_artnet)
    engine.sacn_transport = FakeTransport(5568, engine._on_sacn)
    return engine


def console_dmx(engine, universe, data=b"\x01\x02\x03\x04", src="2.0.0.1", seq=0):
    pkt = artnet.encode_dmx(artnet.parse_universe(universe), data, sequence=seq)
    engine._on_artnet(pkt, src, 6454, "2.255.255.255", CONSOLE_IP, 4)


def stage_dmx(engine, universe, data=b"\x09\x08", src="10.20.30.50"):
    pkt = artnet.encode_dmx(artnet.parse_universe(universe), data)
    engine._on_artnet(pkt, src, 6454, "10.20.30.255", STAGE_IP, 5)


def node_reply(engine, ip, universes, bind_index=1, name="Node"):
    pas = [artnet.parse_universe(u) for u in universes]
    net, sub, _ = artnet.split_port_address(pas[0])
    reply = artnet.ArtPollReply(
        ip=ip,
        net_switch=net,
        sub_switch=sub,
        short_name=name,
        num_ports=len(pas),
        port_types=bytes([artnet.PORT_TYPE_OUTPUT] * len(pas)).ljust(4, b"\x00"),
        sw_out=bytes(pa & 0x0F for pa in pas).ljust(4, b"\x00"),
        bind_index=bind_index,
    )
    engine._on_artnet(artnet.encode_poll_reply(reply), ip, 6454, "10.20.30.255", STAGE_IP, 5)


SIMPLE = [
    {
        "name": "c2s",
        "from": {"network": "console", "universes": "0.0.0-0.0.3"},
        "to": [{"network": "stage", "universes": "0.0.0-0.0.3"}],
    }
]


def test_broadcast_forwarding_with_fresh_sequence():
    engine = make_engine(SIMPLE)
    console_dmx(engine, "0.0.1", seq=77)
    console_dmx(engine, "0.0.1", seq=78)
    sent = engine.artnet_transport.take()
    assert [(ip, port, src, ifindex) for ip, port, src, ifindex, _ in sent] == [("10.20.30.255", 6454, STAGE_IP, 5)] * 2
    first, second = (artnet.decode_dmx(s[4]) for s in sent)
    assert first.universe == 1 and first.data == b"\x01\x02\x03\x04"
    assert (first.sequence, second.sequence) == (1, 2)
    route = engine._table[("console", "artnet", 1)][0]
    assert route.rx.total == 2 and route.last_source == "2.0.0.1"


def test_sequence_wraps_from_255_to_1():
    engine = make_engine(SIMPLE)
    output = engine._table[("console", "artnet", 0)][0].outputs[0]
    output.seq = 255
    console_dmx(engine, "0.0.0")
    assert artnet.decode_dmx(engine.artnet_transport.take()[0][4]).sequence == 1


def test_remap_and_unicast_targets():
    engine = make_engine(
        [
            {
                "from": {"network": "console", "universe": "0.0.4"},
                "to": [
                    {
                        "network": "stage",
                        "universe": "1.2.3",
                        "mode": "unicast",
                        "targets": ["10.20.30.50", "10.20.30.51"],
                    }
                ],
            }
        ]
    )
    console_dmx(engine, "0.0.4")
    sent = engine.artnet_transport.take()
    assert [s[0] for s in sent] == ["10.20.30.50", "10.20.30.51"]
    assert all(artnet.format_universe(artnet.decode_dmx(s[4]).universe) == "1.2.3" for s in sent)


def test_own_packets_are_ignored():
    engine = make_engine(SIMPLE)
    pkt = artnet.encode_dmx(1, b"\x01\x02")
    # our own broadcast looping back on the stage side
    engine._on_artnet(pkt, STAGE_IP, 6454, "10.20.30.255", STAGE_IP, 5)
    engine._on_artnet(pkt, CONSOLE_IP, 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.artnet_transport.take() == []
    assert engine.networks["stage"].artnet_rx.total == 0


def test_never_forwards_back_to_source_network_without_route():
    engine = make_engine(SIMPLE)
    stage_dmx(engine, "0.0.1")
    assert engine.artnet_transport.take() == []
    assert ("stage", "artnet", 1) in engine.unrouted
    assert engine.unrouted[("stage", "artnet", 1)].source == "10.20.30.50"


def test_same_network_remap_is_allowed_and_loop_safe():
    engine = make_engine(
        [{"from": {"network": "console", "universe": "0.0.0"}, "to": [{"network": "console", "universe": "0.0.5"}]}]
    )
    console_dmx(engine, "0.0.0")
    sent = engine.artnet_transport.take()
    assert len(sent) == 1 and artnet.decode_dmx(sent[0][4]).universe == 5
    # the looped-back copy of our own output must not be forwarded again
    engine._on_artnet(sent[0][4], CONSOLE_IP, 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.artnet_transport.take() == []


def test_auto_mode_uses_discovered_nodes_then_falls_back():
    engine = make_engine(
        [
            {
                "from": {"network": "console", "universe": "0.0.2"},
                "to": [{"network": "stage", "universe": "0.0.2", "mode": "auto"}],
            }
        ]
    )
    console_dmx(engine, "0.0.2")
    assert [s[0] for s in engine.artnet_transport.take()] == ["10.20.30.255"]
    node_reply(engine, "10.20.30.50", ["0.0.2", "0.0.3"])
    node_reply(engine, "10.20.30.51", ["0.0.2"])
    node_reply(engine, "10.20.30.52", ["0.0.9"])
    console_dmx(engine, "0.0.2")
    assert sorted(s[0] for s in engine.artnet_transport.take()) == ["10.20.30.50", "10.20.30.51"]
    assert len(engine.nodes) == 3
    engine.nodes.expire(0.0)
    console_dmx(engine, "0.0.2")
    assert [s[0] for s in engine.artnet_transport.take()] == ["10.20.30.255"]


def test_poll_reply_describes_ports():
    routes = SIMPLE + [
        {"from": {"network": "console", "universe": "1.2.3"}, "to": [{"network": "stage", "universe": "1.2.3"}]},
        {"from": {"network": "stage", "universe": "0.0.7"}, "to": [{"network": "console", "universe": "0.0.8"}]},
    ]
    engine = make_engine(routes)
    console_dmx(engine, "0.0.1")
    engine.artnet_transport.take()
    engine._on_artnet(artnet.encode_poll(), "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    sent = engine.artnet_transport.take()
    assert [s[0] for s in sent] == ["2.255.255.255"] * 3
    replies = [artnet.decode_poll_reply(s[4]) for s in sent]
    assert [r.bind_index for r in replies] == [1, 2, 3]
    assert all(r.ip == CONSOLE_IP and r.bind_ip == CONSOLE_IP for r in replies)
    # group 1: net 0 sub 0, four ports 0..3 (console sends to us = our outputs)
    assert replies[0].output_universes() == [0, 1, 2, 3]
    assert replies[0].input_universes() == []
    assert replies[0].good_output[1] & artnet.GOOD_OUTPUT_DATA_TRANSMITTED  # 0.0.1 just received data
    assert not replies[0].good_output[0] & artnet.GOOD_OUTPUT_DATA_TRANSMITTED
    # group 2: 0.0.8 which we send onto the console network = input port
    assert replies[1].input_universes() == [8] and replies[1].output_universes() == []
    # group 3: net 1 sub 2
    assert replies[2].output_universes() == [artnet.port_address(1, 2, 3)]
    assert replies[2].short_name == "ArtBridge"
    assert replies[2].long_name == "ArtBridge on box (console)"
    assert replies[2].mac_str == "aa:bb:cc:dd:ee:01"
    assert replies[2].status2 & artnet.STATUS2_PORT_ADDRESS_15BIT
    assert replies[2].status2 & artnet.STATUS2_SACN_CAPABLE
    assert engine.networks["console"].polls_rx.total == 1
    assert engine.networks["console"].replies_tx.total == 3


def test_poll_reply_unicast_mode_and_targeted_poll():
    engine = make_engine(SIMPLE, console={"cidr": f"{CONSOLE_IP}/8", "artnet": {"reply": "unicast"}})
    engine._on_artnet(artnet.encode_poll(), "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    sent = engine.artnet_transport.take()
    assert len(sent) == 1 and sent[0][0] == "2.0.0.1"
    out_of_range = artnet.encode_poll(flags=artnet.POLL_FLAG_TARGETED) + bytes([0x10, 0x00, 0x0F, 0x00])
    engine._on_artnet(out_of_range, "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.artnet_transport.take() == []
    in_range = artnet.encode_poll(flags=artnet.POLL_FLAG_TARGETED) + bytes([0x00, 0x02, 0x00, 0x01])
    engine._on_artnet(in_range, "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    reply = artnet.decode_poll_reply(engine.artnet_transport.take()[0][4])
    assert reply.output_universes() == [1, 2]


def test_node_with_no_ports_still_answers_polls():
    engine = make_engine([])
    engine._on_artnet(artnet.encode_poll(), "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    reply = artnet.decode_poll_reply(engine.artnet_transport.take()[0][4])
    assert reply.num_ports == 0 and reply.ip == CONSOLE_IP


def test_disabled_route_is_inert():
    routes = [
        {
            "enabled": False,
            "from": {"network": "console", "universe": "0.0.0"},
            "to": [{"network": "stage", "universe": "0.0.0"}],
        }
    ]
    engine = make_engine(routes)
    console_dmx(engine, "0.0.0")
    assert engine.artnet_transport.take() == []
    assert engine.networks["console"].ports == []
    assert len(engine.routes) == 1 and not engine._table


def test_sync_and_other_opcodes():
    engine = make_engine(SIMPLE)
    engine._on_artnet(artnet.encode_sync(), "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    sent = engine.artnet_transport.take()
    assert [(s[0], s[2]) for s in sent] == [("10.20.30.255", STAGE_IP)]
    assert artnet.opcode(sent[0][4]) == artnet.OP_SYNC
    timecode = (
        b"Art-Net\x00"
        + bytes([artnet.OP_TIME_CODE & 0xFF, artnet.OP_TIME_CODE >> 8])
        + b"\x00\x0e\x00\x00\x01\x02\x03\x04\x00"
    )
    engine._on_artnet(timecode, "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.artnet_transport.take() == []
    assert engine.networks["console"].other_ops[artnet.OP_TIME_CODE] == 1

    engine = make_engine(SIMPLE, forward_sync=False, forward_other=True)
    engine._on_artnet(artnet.encode_sync(), "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.artnet_transport.take() == []
    engine._on_artnet(timecode, "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert len(engine.artnet_transport.take()) == 1


def test_artnet_to_sacn_and_back():
    routes = [
        {
            "from": {"network": "console", "universe": "0.0.5"},
            "to": [{"network": "stage", "protocol": "sacn", "universe": 6, "priority": 120}],
        },
        {
            "from": {"network": "stage", "protocol": "sacn", "universe": 9},
            "to": [{"network": "console", "protocol": "artnet", "universe": "0.0.9"}],
        },
    ]
    engine = make_engine(routes, stage={"cidr": f"{STAGE_IP}/24", "sacn": {"enabled": True, "ttl": 4}})
    console_dmx(engine, "0.0.5", data=b"\x10\x20\x30\x40")
    sent = engine.sacn_transport.take()
    assert [(s[0], s[1], s[2], s[3]) for s in sent] == [("239.255.0.6", 5568, STAGE_IP, 5)]
    pkt = sacn.decode_data(sent[0][4])
    assert pkt.universe == 6 and pkt.data == b"\x10\x20\x30\x40" and pkt.priority == 120 and pkt.sequence == 1
    assert pkt.source_name == "ArtBridge" and pkt.cid == engine.config.cid_bytes
    assert engine.sacn_transport.ttl == 4

    incoming = sacn.encode_data(9, b"\x05\x06", b"\x01" * 16, "desk", priority=90, sequence=3)
    engine._on_sacn(incoming, "10.20.30.77", 5568, "239.255.0.9", STAGE_IP, 5)
    sent = engine.artnet_transport.take()
    assert [s[0] for s in sent] == ["2.255.255.255"]
    dmx = artnet.decode_dmx(sent[0][4])
    assert dmx.universe == 9 and dmx.data == b"\x05\x06"
    # preview data must never reach fixtures
    preview = sacn.encode_data(9, b"\x05\x06", b"\x01" * 16, "desk", options=sacn.OPT_PREVIEW)
    engine._on_sacn(preview, "10.20.30.77", 5568, "239.255.0.9", STAGE_IP, 5)
    assert engine.artnet_transport.take() == []
    # multicast groups wanted on the stage network
    assert engine.networks["stage"].wanted_groups == {9}
    engine._join_groups()
    assert engine.sacn_transport.joined == [("239.255.0.9", STAGE_IP, 5)]


def test_sacn_discovery_lists_outputs():
    routes = [
        {
            "from": {"network": "console", "universe": "0.0.0"},
            "to": [
                {"network": "stage", "protocol": "sacn", "universe": 12},
                {"network": "stage", "protocol": "sacn", "universe": 3},
            ],
        }
    ]
    engine = make_engine(routes)
    engine._send_discovery(0.0)
    sent = engine.sacn_transport.take()
    assert [(s[0], s[2]) for s in sent] == [("239.255.250.214", STAGE_IP)]
    assert sent[0][4][-4:] == b"\x00\x03\x00\x0c"


def test_classification_fallbacks():
    engine = make_engine(SIMPLE)
    # unknown local address but unique interface
    assert engine._classify("2.0.0.1", "255.255.255.255", "", 4).name == "console"
    # nothing known about the interface: directed broadcast decides
    assert engine._classify("9.9.9.9", "10.20.30.255", "", 0).name == "stage"
    # otherwise the source subnet decides
    assert engine._classify("10.20.30.7", "255.255.255.255", "", 0).name == "stage"
    assert engine._classify("192.168.1.1", "255.255.255.255", "", 99) is None
    pkt = artnet.encode_dmx(0, b"\x01\x02")
    engine._on_artnet(pkt, "192.168.1.1", 6454, "255.255.255.255", "", 99)
    assert engine.dropped == {"unknown network": 1}
    engine._on_artnet(b"junk", "2.0.0.1", 6454, "2.255.255.255", CONSOLE_IP, 4)
    assert engine.dropped["not art-net"] == 1


def test_missing_interface_is_reported_and_sends_skip_polls():
    engine = Engine(
        parse_config({"networks": {"a": {"cidr": "2.0.0.10/8"}, "b": {"cidr": "10.20.30.5/24"}}, "routes": SIMPLE[:0]}),
        transport_factory=FakeTransport,
        address_source=lambda: ADDRESSES[:1],
        hostname="box",
    )
    engine.refresh_interfaces()
    assert engine.networks["a"].present and not engine.networks["b"].present
    engine.artnet_transport = FakeTransport(6454, engine._on_artnet)
    engine.send_polls()
    assert [s[0] for s in engine.artnet_transport.take()] == ["2.255.255.255"]
    status = engine.status()
    assert [n["present"] for n in status["networks"]] == [True, False]


def test_status_is_json_serialisable():
    engine = make_engine(SIMPLE)
    console_dmx(engine, "0.0.0")
    node_reply(engine, "10.20.30.50", ["0.0.0"])
    stage_dmx(engine, "0.0.9")
    engine._tick_counters(ACTIVE_WINDOW)
    text = json.dumps(engine.status())
    data = json.loads(text)
    assert data["routes"][0]["outputs"][0]["targets"] == ["10.20.30.255"]
    assert data["nodes"][0]["ip"] == "10.20.30.50"
    assert data["unrouted"][0]["universe"] == "0.0.9"
    assert data["networks"][0]["artnet"]["accepts"] == ["0.0.0-0.0.3"]
    assert data["networks"][0]["artnet"]["sends"] == []


def test_start_and_stop_with_fake_transports():
    async def main():
        engine = Engine(
            parse_config({"networks": {"a": {"cidr": "2.0.0.10/8"}}, "routes": []}),
            transport_factory=FakeTransport,
            address_source=lambda: ADDRESSES,
            hostname="box",
        )
        await engine.start()
        assert engine.running and engine.artnet_transport.opened and engine.sacn_transport is None
        # announce() broadcast an ArtPollReply at startup
        assert artnet.opcode(engine.artnet_transport.sent[0][4]) == artnet.OP_POLL_REPLY
        await asyncio.sleep(0.6)  # one poll tick
        assert any(artnet.opcode(s[4]) == artnet.OP_POLL for s in engine.artnet_transport.sent)
        transport = engine.artnet_transport
        await engine.stop()
        assert transport.closed and not engine.running

    asyncio.run(main())


def test_node_table():
    table = NodeTable()
    reply = artnet.ArtPollReply(
        ip="10.0.0.5",
        short_name="n",
        num_ports=2,
        port_types=bytes([0x80, 0x40, 0, 0]),
        sw_out=bytes([1, 0, 0, 0]),
        sw_in=bytes([0, 2, 0, 0]),
    )
    node = table.update("stage", "10.0.0.5", reply, now=10.0)
    assert node.output_universes == [1] and node.input_universes == [2]
    assert table.targets_for("stage", 1) == ["10.0.0.5"]
    assert table.targets_for("stage", 2) == []
    table.update("stage", "10.0.0.5", reply, now=12.0)
    assert len(table) == 1 and node.replies == 2
    assert table.expire(5.0, now=16.0) == []
    assert [n.ip for n in table.expire(5.0, now=18.0)] == ["10.0.0.5"]
    assert table.targets_for("stage", 1) == []
    assert table.to_list(0.0) == []


@pytest.mark.parametrize("universe", ["0.0.0", "7.15.15"])
def test_frames_keep_full_512_slots(universe):
    engine = make_engine(
        [{"from": {"network": "console", "universe": universe}, "to": [{"network": "stage", "universe": universe}]}]
    )
    data = bytes(range(256)) * 2
    console_dmx(engine, universe, data=data)
    assert artnet.decode_dmx(engine.artnet_transport.take()[0][4]).data == data


# -- big universe counts ---------------------------------------------------

BIG = [
    {
        "name": "Rig",
        "from": {"network": "console", "protocol": "artnet", "universes": "0.0.0-1.7.15"},
        "to": [
            {"network": "stage", "protocol": "artnet", "universe": "0.0.0", "mode": "broadcast"},
            {"network": "stage", "protocol": "sacn", "universe": 1},
        ],
    }
]


def test_384_universes_forward_one_to_one():
    engine = make_engine(BIG)
    assert len(engine.routes) == 384
    for u in range(384):
        console_dmx(engine, artnet.format_universe(u), data=bytes([u & 0xFF, 7]))
    art = engine.artnet_transport.take()
    acn = engine.sacn_transport.take()
    assert len(art) == 384 and len(acn) == 384
    got = sorted(artnet.decode_dmx(p[4]).universe for p in art)
    assert got == list(range(384))
    assert sorted(sacn.decode_data(p[4]).universe for p in acn) == list(range(1, 385))


def test_384_universes_are_announced_in_poll_replies():
    engine = make_engine(BIG)
    replies = [artnet.decode_poll_reply(p) for p in engine.build_poll_replies(engine.networks["console"], None, 0.0)]
    assert len(replies) == 96
    assert [r.bind_index for r in replies] == list(range(1, 97))
    ports = sorted(u for r in replies for u in r.output_universes())
    assert ports == list(range(384))


def test_status_shows_a_range_route_as_one_entry():
    engine = make_engine(BIG)
    for u in range(380):
        console_dmx(engine, artnet.format_universe(u))
    status = engine.status()
    assert len(status["routes"]) == 1
    route = status["routes"][0]
    assert route["name"] == "Rig" and route["count"] == 384 and route["live"] == 380
    assert route["from"]["universe"] == "0.0.0-1.7.15"
    assert route["missing"] == ["1.7.12-1.7.15"]
    art_out, acn_out = route["outputs"]
    assert art_out["universe"] == "0.0.0-1.7.15" and art_out["tx"]["total"] == 380
    assert art_out["targets"] == ["10.20.30.255"]
    assert acn_out["universe"] == "1-384" and acn_out["targets"] == []
    console = status["networks"][0]
    assert console["artnet"]["accepts"] == ["0.0.0-1.7.15"]
    assert console["live_universes"] == ["0.0.0-1.7.11"]
    assert len(json.dumps(status)) < 20000


def test_universe_ranges():
    from artbridge.engine import universe_ranges

    assert universe_ranges("artnet", [5, 0, 1, 2, 16]) == ["0.0.0-0.0.2", "0.0.5", "0.1.0"]
    assert universe_ranges("sacn", [1, 2, 3, 10]) == ["1-3", "10"]
    assert universe_ranges("sacn", []) == []


def test_two_networks_on_one_interface_are_logged(caplog):
    addresses = [
        netinfo.Address("eth0", 4, CONSOLE_IP, 8, "aa:bb:cc:dd:ee:01"),
        netinfo.Address("eth0", 4, STAGE_IP, 24, "aa:bb:cc:dd:ee:01"),
    ]
    engine = Engine(
        parse_config({"networks": {"a": {"cidr": f"{CONSOLE_IP}/8"}, "b": {"cidr": f"{STAGE_IP}/24"}}, "routes": []}),
        transport_factory=FakeTransport,
        address_source=lambda: addresses,
        hostname="box",
    )
    with caplog.at_level("WARNING"):
        engine.refresh_interfaces()
    assert "'a', 'b' are on the same interface" in caplog.text
