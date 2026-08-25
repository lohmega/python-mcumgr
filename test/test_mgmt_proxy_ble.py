"""Unit tests for mcumgr.mgmt_proxy_ble - the BLE scan/connect proxy group.

Runs standalone (`python3 test/test_mgmt_proxy_ble.py`) or under pytest.
"""

import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from mcumgr import smp
from mcumgr.mgmt_proxy_ble import MgmtGrpProxyBle


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


class ScriptedProxy(MgmtGrpProxyBle):
    """Bypasses the SMP layer entirely - feeds scan_result() a scripted
    sequence of poll batches, and records scan_start()/scan_stop() calls."""

    def __init__(self, batches):
        # Deliberately not calling super().__init__(): no real transport is
        # needed to exercise _scan_result_poll()/scan()'s own control flow.
        self._batches = list(batches)
        self.started = 0
        self.stopped = 0

    def scan_start(self):
        self.started += 1

    def scan_stop(self):
        self.stopped += 1

    def scan_result(self):
        return self._batches.pop(0) if self._batches else []


def test_scan_without_callback_collects_the_full_timeout_window():
    """Without a result_cb there is no "stop early" signal - scan() must
    keep polling for the whole timeout and return everything seen, not
    return after the very first non-empty poll batch."""
    batches = [
        [],
        [{"address": 1}],
        [{"address": 2}],
        [],
    ]
    proxy = ScriptedProxy(batches)

    result = proxy.scan(timeout=0.2, poll_interval=0.03)

    assert {d["address"] for d in result} == {1, 2}, "must keep collecting across polls"
    assert proxy.started == 1
    assert proxy.stopped == 1


def test_scan_without_callback_deduplicates_repeat_sightings():
    """scan_result() commonly reports the same device again on a later
    poll (a cumulative results buffer, not consume-once) - without a
    result_cb there is nothing else filtering that out, so the same
    address must not be appended once per poll it happens to still be
    visible in."""
    batches = [
        [{"address": 1}],
        [{"address": 1}, {"address": 2}],  # 1 seen again, plus a new one
        [{"address": 1}, {"address": 2}],  # both seen again
        [],
    ]
    proxy = ScriptedProxy(batches)

    result = proxy.scan(timeout=0.2, poll_interval=0.03)

    addrs = [d["address"] for d in result]
    assert sorted(addrs) == [1, 2], "each address must appear exactly once"


def test_scan_without_callback_returns_empty_list_on_timeout():
    proxy = ScriptedProxy([[], [], []])

    result = proxy.scan(timeout=0.1, poll_interval=0.03)

    assert result == []


def test_scan_with_callback_stops_as_soon_as_it_signals_stop():
    batches = [[{"address": 1}], [{"address": 2}]]
    proxy = ScriptedProxy(batches)

    seen = []

    def cb(candidate):
        seen.append(candidate["address"])
        return True  # stop scanning immediately

    result = proxy.scan(result_cb=cb, timeout=5.0, poll_interval=0.01)

    assert seen == [1]
    assert [d["address"] for d in result] == [1]
    assert proxy._batches == [[{"address": 2}]], "must not have polled again"


def test_scan_start_propagates_a_device_error():
    """EBUSY/ENOTSUP etc rejecting scan startup must surface immediately,
    not get silently ignored until scan() times out polling for results
    that were never going to arrive because scanning never started."""
    from mcumgr.mgmt_proxy_ble import MGMT_GROUP_ID_SMP_PROXY_BLE, SMP_PROXY_ID_BLE_SCAN_CTL

    rsp = smp.MgmtMsg(
        nh_op=smp.MGMT_OP.WRITE_RSP,
        nh_group=MGMT_GROUP_ID_SMP_PROXY_BLE,
        nh_id=SMP_PROXY_ID_BLE_SCAN_CTL,
        nh_seq=0,
    )
    rsp.encode_payload({"rc": int(smp.MGMT_ERR.EBUSY)})

    proxy = MgmtGrpProxyBle(QueueTransport([rsp]))

    try:
        proxy.scan_start()
    except smp.MgmtEndpointError as e:
        assert e.rc == int(smp.MGMT_ERR.EBUSY)
    else:
        raise AssertionError("expected MgmtEndpointError for EBUSY")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
