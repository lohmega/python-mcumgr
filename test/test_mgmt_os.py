"""Unit tests for mcumgr.mgmt_os - the OS management group (echo/reset/
taskstat/datetime).

Runs standalone (`python3 test/test_mgmt_os.py`) or under pytest.
Requires cbor2.
"""

import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from mcumgr import smp
from mcumgr.mgmt_os import MgmtGrpOs


class QueueTransport:
    """Transport that replays a scripted list of responses (see
    test_mgmt.py's identically-named helper)."""

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


class RaisingTransport:
    """Transport whose read_msg always raises a given exception (see
    test_mgmt.py's identically-named helper)."""

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


def _rsp(group, cmd_id, payload):
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.WRITE_RSP, nh_group=group, nh_id=cmd_id, nh_seq=0)
    m.encode_payload(payload)
    return m


def test_echo_round_trips_text():
    from mcumgr.mgmt_os import OS_MGMT_ID

    t = QueueTransport([_rsp(smp.MGMT_GROUP_ID.OS, OS_MGMT_ID.ECHO, {"r": "hi"})])
    grp = MgmtGrpOs(t)

    assert grp.echo("hi") == "hi"


def test_echo_rejects_non_str():
    grp = MgmtGrpOs(QueueTransport([]))
    try:
        grp.echo(b"not a string")
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for non-str echo text")


def test_reset_swallows_a_plain_timeout():
    """The device may reboot before it can answer - a plain timeout is not
    a failure for reset() specifically."""
    t = RaisingTransport(smp.SMPTransportError("no response"))
    grp = MgmtGrpOs(t)

    grp.reset()  # must not raise


def test_reset_does_not_swallow_a_corrupt_response():
    t = RaisingTransport(smp.SMPResponseError("garbage"))
    grp = MgmtGrpOs(t)
    try:
        grp.reset()
    except smp.SMPResponseError:
        pass
    else:
        raise AssertionError("expected SMPResponseError to propagate")


def test_reset_raises_on_an_explicit_device_rejection():
    """Regression: reset() used check=False, so a device that DID answer
    with an explicit error rc (ENOTSUP, EACCESSDENIED, ...) was reported
    as a successful reset - tolerate_no_response only covers the case
    where nothing came back at all, not an explicit rejection."""
    from mcumgr.mgmt_os import OS_MGMT_ID

    t = QueueTransport(
        [_rsp(smp.MGMT_GROUP_ID.OS, OS_MGMT_ID.RESET, {"rc": int(smp.MGMT_ERR.ENOTSUP)})]
    )
    grp = MgmtGrpOs(t)

    try:
        grp.reset()
    except smp.MgmtEndpointError as e:
        assert e.rc == int(smp.MGMT_ERR.ENOTSUP)
    else:
        raise AssertionError("expected MgmtEndpointError for an explicit rejection")


def test_taskstats_returns_tasks_dict():
    from mcumgr.mgmt_os import OS_MGMT_ID

    t = QueueTransport(
        [_rsp(smp.MGMT_GROUP_ID.OS, OS_MGMT_ID.TASKSTAT, {"tasks": {"main": {}}})]
    )
    grp = MgmtGrpOs(t)

    assert grp.taskstats() == {"main": {}}


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
