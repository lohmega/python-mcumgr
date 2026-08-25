"""Unit tests for mcumgr.mgmt - endpoint framing, sequencing, stale replies.

Runs standalone (`python3 test/test_mgmt.py`) or under pytest. Requires cbor2.
"""

import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

import cbor2 as cbor

from mcumgr import smp
from mcumgr.mgmt import MgmtGrpEndpoint


class QueueTransport:
    """Transport that replays a scripted list of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self._seq = smp.SeqCounter()

    def next_seq(self):
        return self._seq.next()

    def write_msg(self, msg):
        self.sent.append(msg)

    def read_msg(self, timeout=None):
        if not self._responses:
            raise smp.SMPTransportError("no more responses")
        return self._responses.pop(0)


def _rsp(seq, group=0, cmd_id=0, payload=None, op=smp.MGMT_OP.READ_RSP):
    m = smp.MgmtMsg(nh_op=op, nh_group=group, nh_id=cmd_id, nh_seq=seq)
    m.encode_payload(payload if payload is not None else {"rc": 0})
    return m


def test_request_is_framed_correctly():
    t = QueueTransport([_rsp(0, group=1, cmd_id=2)])
    ep = MgmtGrpEndpoint(t, 1, 2)
    ep.mh_read({"m": "m"})

    req = t.sent[0]
    assert req.hdr.nh_op == smp.MGMT_OP.READ
    assert req.hdr.nh_group == 1
    assert req.hdr.nh_id == 2
    assert req.hdr.nh_seq == 0
    assert cbor.loads(req.payload) == {"m": "m"}
    assert req.hdr.nh_len == len(req.payload)


def test_empty_dict_payload_is_still_encoded():
    """data={} must send an explicit empty CBOR map, not nothing.

    `if data:` treats an empty dict as falsy - indistinguishable from data
    being omitted entirely - which sends no payload at all instead of the
    explicit `{}` the caller asked for.
    """
    t = QueueTransport([_rsp(0, group=1, cmd_id=2)])
    ep = MgmtGrpEndpoint(t, 1, 2)
    ep.mh_write(data={})

    assert t.sent[0].payload == cbor.dumps({})


def test_omitted_payload_sends_no_body():
    t = QueueTransport([_rsp(0, group=1, cmd_id=2)])
    ep = MgmtGrpEndpoint(t, 1, 2)
    ep.mh_write()

    assert not t.sent[0].payload


class RaisingTransport:
    """Transport whose read_msg always raises a given exception."""

    def __init__(self, exc):
        self._exc = exc
        self.sent = []
        self._seq = smp.SeqCounter()

    def next_seq(self):
        return self._seq.next()

    def write_msg(self, msg):
        self.sent.append(msg)

    def read_msg(self, timeout=None):
        raise self._exc


def test_tolerate_no_response_swallows_a_plain_timeout():
    t = RaisingTransport(smp.SMPTransportError("no response within 5s"))
    ep = MgmtGrpEndpoint(t, 1, 2)

    rsp = ep.mh_write(tolerate_no_response=True)
    assert rsp == {}


def test_tolerate_no_response_does_not_swallow_a_corrupt_response():
    """A response that arrived but failed validation (e.g. NLIP CRC
    mismatch) is a real transport integrity problem, not a benign
    "device rebooted before answering" - must still raise."""
    t = RaisingTransport(smp.SMPResponseError("nlip crc mismatch"))
    ep = MgmtGrpEndpoint(t, 1, 2)

    try:
        ep.mh_write(tolerate_no_response=True)
    except smp.SMPResponseError:
        pass
    else:
        raise AssertionError("expected SMPResponseError to propagate")


def test_stale_response_is_skipped():
    """A late reply to a previous request must not fail the current one."""
    t = QueueTransport(
        [
            _rsp(200, group=1, cmd_id=0),  # stale, from an earlier request
            _rsp(0, group=1, cmd_id=0, payload={"rc": 0, "ok": True}),
        ]
    )
    ep = MgmtGrpEndpoint(t, 1, 0)
    out = ep.mh_read()
    assert out["ok"] is True


def test_several_stale_responses_skipped():
    t = QueueTransport(
        [
            _rsp(7, group=1, cmd_id=0),
            _rsp(8, group=1, cmd_id=0),
            _rsp(9, group=1, cmd_id=0),
            _rsp(0, group=1, cmd_id=0, payload={"rc": 0, "ok": True}),
        ]
    )
    ep = MgmtGrpEndpoint(t, 1, 0)
    assert ep.mh_read()["ok"] is True


def test_group_mismatch_raises():
    t = QueueTransport([_rsp(0, group=9, cmd_id=0)])
    ep = MgmtGrpEndpoint(t, 1, 0)
    try:
        ep.mh_read()
    except smp.MgmtEndpointError as e:
        assert "Group mismatch" in str(e)
    else:
        raise AssertionError("expected group mismatch")


def test_id_mismatch_raises():
    t = QueueTransport([_rsp(0, group=1, cmd_id=5)])
    ep = MgmtGrpEndpoint(t, 1, 0)
    try:
        ep.mh_read()
    except smp.MgmtEndpointError as e:
        assert "Command id mismatch" in str(e)
    else:
        raise AssertionError("expected id mismatch")


def test_check_raises_on_rc():
    t = QueueTransport([_rsp(0, group=1, cmd_id=0, payload={"rc": 3, "rsn": "bad"})])
    ep = MgmtGrpEndpoint(t, 1, 0)
    try:
        ep.mh_write({"x": 1}, check=True)
    except smp.MgmtEndpointError as e:
        assert e.rc == 3
        assert e.rsn == "bad"
        assert "EINVAL" in str(e)
    else:
        raise AssertionError("expected MgmtEndpointError")


def test_rc_zero_is_not_an_error():
    t = QueueTransport([_rsp(0, group=1, cmd_id=0, payload={"rc": 0})])
    ep = MgmtGrpEndpoint(t, 1, 0)
    assert ep.mh_write({"x": 1}, check=True) == {"rc": 0}


def test_endpoints_share_the_transport_sequence():
    """Two endpoints must not hand out colliding sequence numbers."""
    t = QueueTransport(
        [_rsp(0, group=1, cmd_id=0), _rsp(1, group=1, cmd_id=5), _rsp(2, group=1, cmd_id=0)]
    )
    a = MgmtGrpEndpoint(t, 1, 0)
    b = MgmtGrpEndpoint(t, 1, 5)

    a.mh_read()
    b.mh_read()
    a.mh_read()

    assert [m.hdr.nh_seq for m in t.sent] == [0, 1, 2]


def test_messages_do_not_share_a_header():
    """Regression: MgmtMsg used a mutable default header shared by all
    instances, so building a new message rewrote older ones."""
    a = smp.MgmtMsg(nh_seq=1, nh_group=1, nh_id=2)
    b = smp.MgmtMsg(nh_seq=2, nh_group=3, nh_id=4)

    assert a.hdr is not b.hdr
    assert (a.hdr.nh_seq, a.hdr.nh_group, a.hdr.nh_id) == (1, 1, 2)
    assert (b.hdr.nh_seq, b.hdr.nh_group, b.hdr.nh_id) == (2, 3, 4)


def test_messages_do_not_share_a_payload():
    a = smp.MgmtMsg()
    b = smp.MgmtMsg()
    a.set_payload(b"hello")

    assert bytes(b.payload) == b""
    assert b.hdr.nh_len == 0


def test_roundtrip_bytes():
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.WRITE, nh_group=1, nh_id=1, nh_seq=42)
    m.encode_payload({"off": 7})
    raw = m.to_bytes()

    out = smp.MgmtMsg.from_bytes(raw)
    assert out.hdr.nh_op == smp.MGMT_OP.WRITE
    assert out.hdr.nh_group == 1
    assert out.hdr.nh_id == 1
    assert out.hdr.nh_seq == 42
    assert out.decode_payload() == {"off": 7}


def test_from_bytes_needs_full_payload():
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.WRITE, nh_group=1, nh_id=1, nh_seq=1)
    m.encode_payload({"off": 7})
    raw = m.to_bytes()

    for n in (0, 4, 7, len(raw) - 1):
        try:
            smp.MgmtMsg.from_bytes(raw[:n])
        except IndexError:
            pass
        else:
            raise AssertionError("expected IndexError for %d bytes" % n)


def test_from_bytes_ignores_trailing_data():
    """Several responses can arrive in one read; extra bytes are not ours."""
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.WRITE, nh_group=1, nh_id=1, nh_seq=1)
    m.encode_payload({"off": 7})
    raw = m.to_bytes()

    out = smp.MgmtMsg.from_bytes(raw + b"\xde\xad\xbe\xef")
    assert out.size == len(raw)
    assert out.decode_payload() == {"off": 7}


def test_empty_payload_response():
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.WRITE_RSP, nh_group=1, nh_id=0, nh_seq=0)
    t = QueueTransport([m])
    ep = MgmtGrpEndpoint(t, 1, 0)
    assert ep.mh_write() == {}


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
