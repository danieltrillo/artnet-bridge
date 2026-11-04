import pytest

from artbridge import artnet


def test_port_address_roundtrip():
    pa = artnet.port_address(1, 2, 3)
    assert pa == (1 << 8) | (2 << 4) | 3
    assert artnet.split_port_address(pa) == (1, 2, 3)
    assert artnet.format_universe(pa) == "1.2.3"


def test_parse_universe_forms():
    assert artnet.parse_universe("0.0.1") == 1
    assert artnet.parse_universe(17) == 17
    assert artnet.parse_universe("17") == 17
    assert artnet.parse_universe("0.1.0") == 16
    assert artnet.parse_universe(" 1.0.0 ") == 256
    for bad in ("0.1", 0.1, "x", -1, 40000, "0.16.0", "128.0.0", True, None, "1.2.3.4"):
        with pytest.raises(ValueError):
            artnet.parse_universe(bad)


def test_dmx_roundtrip():
    data = bytes(range(256)) * 2
    pkt = artnet.encode_dmx(0x0123, data, sequence=9, physical=2)
    assert len(pkt) == 18 + 512
    assert artnet.opcode(pkt) == artnet.OP_DMX
    assert pkt[10:12] == b"\x00\x0e"  # protocol version 14
    dmx = artnet.decode_dmx(pkt)
    assert dmx.universe == 0x0123
    assert dmx.data == data
    assert dmx.sequence == 9
    assert dmx.physical == 2
    assert dmx.start_code == 0


def test_dmx_pads_to_even_length():
    assert artnet.decode_dmx(artnet.encode_dmx(0, b"\x01\x02\x03")).data == b"\x01\x02\x03\x00"
    assert artnet.decode_dmx(artnet.encode_dmx(0, b"\x01")).data == b"\x01\x00"
    assert len(artnet.decode_dmx(artnet.encode_dmx(0, bytes(600))).data) == 512


def test_nzs_carries_start_code():
    pkt = artnet.encode_dmx(5, b"\x10\x20", start_code=0x17)
    assert artnet.opcode(pkt) == artnet.OP_NZS
    dmx = artnet.decode_dmx(pkt)
    assert dmx.start_code == 0x17
    assert dmx.data == b"\x10\x20"
    assert dmx.universe == 5


def test_dmx_rejects_garbage():
    assert artnet.decode_dmx(b"Art-Net\x00" + b"\x00" * 5) is None
    assert artnet.decode_dmx(b"nope") is None
    assert artnet.decode_dmx(artnet.encode_sync()) is None
    assert artnet.opcode(b"short") is None
    assert artnet.opcode(b"Art-Net\x00\x00\x50") == artnet.OP_DMX
    # zero length claims no data
    pkt = bytearray(artnet.encode_dmx(0, b"\x01\x02"))
    pkt[16] = pkt[17] = 0
    assert artnet.decode_dmx(bytes(pkt)) is None


def test_poll_roundtrip():
    pkt = artnet.encode_poll()
    assert len(pkt) == 14
    assert artnet.opcode(pkt) == artnet.OP_POLL
    poll = artnet.decode_poll(pkt)
    assert poll.flags == artnet.POLL_FLAG_REPLY_ON_CHANGE
    assert poll.target_top is None
    assert poll.wants(12345)


def test_targeted_poll():
    pkt = artnet.encode_poll(flags=artnet.POLL_FLAG_TARGETED) + bytes([0x00, 0x10, 0x00, 0x02])
    poll = artnet.decode_poll(pkt)
    assert (poll.target_bottom, poll.target_top) == (2, 16)
    assert poll.wants(5)
    assert not poll.wants(1)
    assert not poll.wants(17)


def test_poll_reply_roundtrip():
    reply = artnet.ArtPollReply(
        ip="2.0.0.10",
        version=0x0102,
        net_switch=1,
        sub_switch=2,
        oem=0x00FF,
        status1=0xE0,
        esta_man=0x7FF0,
        short_name="Shorty",
        long_name="Long name here",
        node_report="#0001 [0001] ok",
        num_ports=2,
        port_types=bytes([0x80, 0x40, 0, 0]),
        sw_in=bytes([0, 5, 0, 0]),
        sw_out=bytes([3, 0, 0, 0]),
        style=artnet.STYLE_NODE,
        mac=bytes.fromhex("aabbccddeeff"),
        bind_ip="2.0.0.10",
        bind_index=1,
        status2=0x09,
    )
    pkt = artnet.encode_poll_reply(reply)
    assert len(pkt) == artnet.REPLY_LENGTH == 239
    assert pkt[14:16] == b"\x36\x19"  # port 0x1936, low byte first
    assert pkt[10:14] == bytes([2, 0, 0, 10])
    back = artnet.decode_poll_reply(pkt)
    assert back.ip == "2.0.0.10"
    assert back.port == artnet.ARTNET_PORT
    assert back.version == 0x0102
    assert back.short_name == "Shorty"
    assert back.long_name == "Long name here"
    assert back.node_report == "#0001 [0001] ok"
    assert back.esta_man == 0x7FF0
    assert back.oem == 0x00FF
    assert back.status1 == 0xE0
    assert back.output_universes() == [artnet.port_address(1, 2, 3)]
    assert back.input_universes() == [artnet.port_address(1, 2, 5)]
    assert back.mac_str == "aa:bb:cc:dd:ee:ff"
    assert back.bind_ip == "2.0.0.10"
    assert back.bind_index == 1
    assert back.status2 == 0x09
    assert back.style_name == "Node"


def test_poll_reply_short_packet_from_old_node():
    reply = artnet.ArtPollReply(ip="10.0.0.5", short_name="old", num_ports=1, port_types=bytes([0x80, 0, 0, 0]))
    pkt = artnet.encode_poll_reply(reply)[:207]
    back = artnet.decode_poll_reply(pkt)
    assert back.ip == "10.0.0.5"
    assert back.short_name == "old"
    assert back.output_universes() == [0]
    assert back.bind_index == 0
    assert artnet.decode_poll_reply(pkt[:20]) is None


def test_poll_reply_num_ports_zero_still_lists_ports():
    reply = artnet.ArtPollReply(
        ip="10.0.0.5", num_ports=0, port_types=bytes([0x80, 0x80, 0, 0]), sw_out=bytes([1, 2, 0, 0])
    )
    back = artnet.decode_poll_reply(artnet.encode_poll_reply(reply))
    assert back.output_universes() == [1, 2]


def test_strings_truncate_with_terminator():
    reply = artnet.ArtPollReply(ip="1.2.3.4", short_name="x" * 40, long_name="y" * 100)
    back = artnet.decode_poll_reply(artnet.encode_poll_reply(reply))
    assert back.short_name == "x" * 17
    assert back.long_name == "y" * 63


def test_sync_packet():
    pkt = artnet.encode_sync()
    assert artnet.opcode(pkt) == artnet.OP_SYNC
    assert len(pkt) == 14


def test_node_report_format():
    assert artnet.node_report(artnet.RC_POWER_OK, 12, "fine") == "#0001 [0012] fine"
    assert artnet.opcode_name(artnet.OP_DMX) == "ArtDmx"
    assert artnet.opcode_name(0x1234) == "Op0x1234"
