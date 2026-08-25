"""Unit tests for mcumgr.smp_proxy - the proxy-forward transport wrapper.

Runs standalone (`python3 test/test_smp_proxy.py`) or under pytest.
Requires cbor2.
"""

import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

import cbor2 as cbor

from mcumgr import smp
from mcumgr.smp_proxy import (
    MGMT_GROUP_ID_PROXY_FWD_MGMT,
    PROXY_FWD_MGMT_ID_FWD,
    SmpProxyTransport,
)


class FakeBaseTransport:
    """Just enough for SmpProxyTransport's __init__/max_mtu to work."""

    def __init__(self, max_mtu):
        self.max_mtu = max_mtu
        self._seq = smp.SeqCounter()

    def is_connected(self):
        return True

    def next_seq(self):
        return self._seq.next()


def _envelope(seq, inner_msg):
    """Build a raw outer proxy-forward envelope, as the base transport
    would deliver it to SmpProxyTransport.read_msg()."""
    m = smp.MgmtMsg(
        nh_op=smp.MGMT_OP.WRITE_RSP,
        nh_group=MGMT_GROUP_ID_PROXY_FWD_MGMT,
        nh_id=PROXY_FWD_MGMT_ID_FWD,
        nh_seq=seq,
    )
    m.encode_payload({"d": inner_msg.to_bytes()})
    return m


class ScriptedBaseTransport(FakeBaseTransport):
    """Replays a scripted list of raw outer envelopes to read_msg()."""

    def __init__(self, envelopes, max_mtu=256):
        super().__init__(max_mtu)
        self._envelopes = list(envelopes)
        self.sent = []

    def write_msg(self, msg):
        self.sent.append(msg)

    def read_msg(self, timeout=None):
        return self._envelopes.pop(0)


def _worst_case_envelope_size(inner_len, address=0xFFFFFFFFFFFF, wait_ms=5000):
    """The actual bytes-on-the-wire an outer proxied message costs.

    Mirrors SmpProxyTransport.write_msg()'s envelope exactly, so this is the
    ground truth max_mtu's reservation has to cover, not a guess.
    """
    body = cbor.dumps(
        {"m": "ble", "a": address, "w": wait_ms, "d": b"\x00" * inner_len}
    )
    return 8 + len(body)  # 8 = outer SMP header


def test_max_mtu_reserves_the_real_envelope_overhead():
    """A chunk sized to max_mtu must fit within the base transport's MTU
    once wrapped in the proxy envelope, for a worst-case (48-bit) address."""
    # 64 and below is already a degenerate case elsewhere in this codebase
    # (see MgmtGrpImage._max_chunk's own `if max_mtu >= 64` floor) - not a
    # realistic transport MTU, so it is not held to this invariant.
    for base_mtu in (128, 256, 512, 1024):
        proxy = SmpProxyTransport(FakeBaseTransport(base_mtu), address=1)
        inner_mtu = proxy.max_mtu
        wire_size = _worst_case_envelope_size(inner_mtu)
        assert wire_size <= base_mtu, (
            "base_mtu={} inner_mtu={} wraps to {} bytes on the wire, "
            "exceeding the base transport's MTU".format(
                base_mtu, inner_mtu, wire_size
            )
        )


def test_max_mtu_has_a_floor():
    proxy = SmpProxyTransport(FakeBaseTransport(max_mtu=8), address=1)
    assert proxy.max_mtu >= 32


def test_max_mtu_falls_back_when_base_transport_has_none():
    class NoMtuTransport:
        def is_connected(self):
            return True

    proxy = SmpProxyTransport(NoMtuTransport(), address=1)
    assert proxy.max_mtu > 0


def test_read_msg_discards_a_stale_outer_envelope():
    """A late response to an earlier, already-abandoned write_msg() must
    not be mistaken for the answer to the current one - both carry the
    same group/id (PROXY_FWD_MGMT_ID_FWD) regardless of which end-device
    request they actually wrap, so only the outer seq tells them apart."""
    stale_inner = smp.MgmtMsg(nh_op=0, nh_group=9, nh_id=9, nh_seq=9)
    stale_inner.encode_payload({"stale": True})

    real_inner = smp.MgmtMsg(nh_op=0, nh_group=9, nh_id=9, nh_seq=9)
    real_inner.encode_payload({"real": True})

    base = ScriptedBaseTransport(
        [
            _envelope(seq=99, inner_msg=stale_inner),  # a late reply to
            # some earlier, already-timed-out write_msg() call
            _envelope(seq=0, inner_msg=real_inner),  # the actual answer
        ]
    )
    proxy = SmpProxyTransport(base, address=1)

    proxy.write_msg(smp.MgmtMsg(nh_op=0, nh_group=9, nh_id=9, nh_seq=0))
    rsp = proxy.read_msg(timeout=1)

    assert rsp.decode_payload() == {"real": True}


def test_outer_seq_is_shared_with_the_base_transport():
    """The outer envelope IS a message on base_transport, exactly like
    e.g. MgmtGrpProxyBle's scan/connect control commands sharing the same
    connection - it must draw from that transport's own counter, not an
    independent one that could reissue a seq a control response is still
    in flight for."""
    base = ScriptedBaseTransport([], max_mtu=256)
    # Simulate prior, unrelated control-plane traffic on this connection
    # (e.g. MgmtGrpProxyBle.scan_start()) having already advanced the
    # shared counter past 0.
    base.next_seq()
    base.next_seq()
    proxy = SmpProxyTransport(base, address=1)

    proxy.write_msg(smp.MgmtMsg(nh_op=0, nh_group=9, nh_id=9, nh_seq=0))

    assert base.sent[0].hdr.nh_seq == 2, "must continue the shared counter, not restart at 0"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
