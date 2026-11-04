import asyncio
import socket

import pytest

from artbridge.transport import UdpTransport


@pytest.mark.skipif(not hasattr(socket, "IP_PKTINFO"), reason="needs Linux IP_PKTINFO")
def test_loopback_delivery_reports_local_address():
    async def main():
        loop = asyncio.get_running_loop()
        got = loop.create_future()

        def handler(data, src_ip, src_port, dst_ip, local_ip, ifindex):
            if not got.done():
                got.set_result((data, src_ip, dst_ip, local_ip, ifindex))

        receiver = UdpTransport(0, handler, bind_ip="127.0.0.1")
        receiver.open(loop)
        sender = UdpTransport(0, lambda *a: None, bind_ip="127.0.0.1")
        sender.open(loop)
        try:
            assert receiver.bound_port > 0
            assert sender.send(b"hello", "127.0.0.1", receiver.bound_port, "127.0.0.1")
            data, src_ip, dst_ip, local_ip, ifindex = await asyncio.wait_for(got, 3)
            assert data == b"hello"
            assert src_ip == "127.0.0.1"
            assert dst_ip == "127.0.0.1"
            assert local_ip == "127.0.0.1"
            assert ifindex == socket.if_nametoindex("lo")
            assert receiver.rx_packets == 1 and sender.tx_packets == 1
        finally:
            receiver.close()
            sender.close()
        assert not receiver.is_open

    asyncio.run(main())


def test_send_error_is_counted_not_raised():
    async def main():
        loop = asyncio.get_running_loop()
        t = UdpTransport(0, lambda *a: None, bind_ip="127.0.0.1")
        t.open(loop)
        try:
            ok = t.send(b"x", "127.0.0.1", 9, "203.0.113.9")  # source address not on this host
            assert ok is False
            assert t.tx_errors == 1
            assert t.last_error
        finally:
            t.close()

    asyncio.run(main())


def test_send_on_closed_transport_is_harmless():
    t = UdpTransport(0, lambda *a: None)
    assert t.send(b"x", "127.0.0.1", 9) is False
    t.close()
    assert t.bound_port == 0
