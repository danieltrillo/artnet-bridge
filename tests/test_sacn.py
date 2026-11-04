import struct

import pytest

from artbridge import sacn

CID = bytes(range(16))


def test_data_roundtrip():
    data = bytes(range(256)) * 2
    pkt = sacn.encode_data(123, data, CID, "My source", priority=150, sequence=42)
    assert len(pkt) == 638
    assert sacn.is_sacn(pkt)
    assert sacn.root_vector(pkt) == sacn.VECTOR_ROOT_E131_DATA
    # flags and length fields
    assert struct.unpack_from(">H", pkt, 16)[0] == 0x7000 | 622
    assert struct.unpack_from(">H", pkt, 38)[0] == 0x7000 | 600
    assert struct.unpack_from(">H", pkt, 115)[0] == 0x7000 | 523
    assert struct.unpack_from(">H", pkt, 123)[0] == 513
    d = sacn.decode_data(pkt)
    assert d.universe == 123
    assert d.data == data
    assert d.priority == 150
    assert d.sequence == 42
    assert d.source_name == "My source"
    assert d.cid == CID
    assert d.start_code == 0
    assert not d.preview and not d.terminated


def test_short_data_and_start_code():
    pkt = sacn.encode_data(1, b"\x01\x02\x03", CID, "s", start_code=0x17)
    assert len(pkt) == 129
    d = sacn.decode_data(pkt)
    assert d.data == b"\x01\x02\x03"
    assert d.start_code == 0x17


def test_priority_is_clamped():
    d = sacn.decode_data(sacn.encode_data(1, b"\x00", CID, "s", priority=250))
    assert d.priority == 200


def test_options_flags():
    d = sacn.decode_data(sacn.encode_data(1, b"\x00", CID, "s", options=sacn.OPT_PREVIEW))
    assert d.preview and not d.terminated
    d = sacn.decode_data(sacn.encode_data(1, b"\x00", CID, "s", options=sacn.OPT_TERMINATED))
    assert d.terminated


def test_multicast_address():
    assert sacn.multicast_address(1) == "239.255.0.1"
    assert sacn.multicast_address(256) == "239.255.1.0"
    assert sacn.multicast_address(sacn.DISCOVERY_UNIVERSE) == "239.255.250.214"


def test_discovery_packet():
    pkt = sacn.encode_discovery(CID, "src", [3, 1, 2])
    assert len(pkt) == 126
    assert sacn.root_vector(pkt) == sacn.VECTOR_ROOT_E131_EXTENDED
    assert sacn.decode_data(pkt) is None
    assert pkt[-6:] == b"\x00\x01\x00\x02\x00\x03"
    assert struct.unpack_from(">H", pkt, 112)[0] == 0x7000 | 14


def test_rejects_garbage():
    assert sacn.decode_data(b"garbage") is None
    assert not sacn.is_sacn(b"\x00\x10" + b"\x00" * 40)
    bad = bytearray(sacn.encode_data(1, b"\x00", CID, "s"))
    bad[117] = 0x03  # wrong DMP vector
    assert sacn.decode_data(bytes(bad)) is None


def test_parse_universe():
    assert sacn.parse_universe(1) == 1
    assert sacn.parse_universe("42") == 42
    for bad in (0, 64000, "x", True, 1.5, None):
        with pytest.raises(ValueError):
            sacn.parse_universe(bad)
