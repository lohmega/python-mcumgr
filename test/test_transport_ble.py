"""Unit tests for mcumgr.transport_ble without a BLE adapter.

bleak needs real hardware for anything end to end, so BleakClient and
BleakScanner are replaced by fakes. What is exercised here is our own logic:
device lookup with its scan fallback, connect teardown on failure, the stale
disconnect filter and notification reassembly.

Runs standalone (`python3 test/test_transport_ble.py`) or under pytest.
Requires bleak (imported by the module under test) and cbor2.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

# these tests deliberately feed in garbage; no need to log about it
logging.getLogger("mcumgr").setLevel(logging.CRITICAL)

from unittest.mock import patch

from mcumgr import smp, transport_ble
from mcumgr.transport_ble import UUID_CHARACT, UUID_SERVICE, SMPTransportBLE


class FakeDev:
    """Stand-in for bleak's BLEDevice."""

    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="smpdev"):
        self.address = address
        self.name = name

    def __str__(self):
        return "{} ({})".format(self.address, self.name)


class FakeAdv:
    def __init__(self, local_name=None, service_uuids=None, rssi=-50):
        self.local_name = local_name
        self.service_uuids = service_uuids
        self.rssi = rssi


class FakeClient:
    """The subset of BleakClient that transport_ble actually uses."""

    # transport_ble pokes at _backend._acquire_mtu; None keeps it a no-op.
    _backend = None

    def __init__(self, dev, timeout=None, disconnected_callback=None,
                 connect_error=None, notify_error=None, mtu_size=None):
        self.dev = dev
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.connect_error = connect_error
        self.notify_error = notify_error
        self.mtu_size = mtu_size
        self.is_connected = False
        self.notify_calls = []
        self.disconnect_calls = 0
        self.writes = []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    async def start_notify(self, uuid, handler):
        if self.notify_error:
            raise self.notify_error
        self.notify_calls.append((uuid, handler))

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data), response))


class FakeClientFactory:
    """Stands in for the BleakClient class, remembering every client made."""

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.clients = []

    def __call__(self, dev, **kwargs):
        kwargs.update(self.client_kwargs)
        clnt = FakeClient(dev, **kwargs)
        self.clients.append(clnt)
        return clnt

    @property
    def last(self):
        return self.clients[-1]


class FakeScanner:
    """Only find_device_by_address() is called directly on BleakScanner."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    async def _find(self, address, timeout=None):
        self.calls.append((address, timeout))
        return self.result

    def find_device_by_address(self, address, timeout=None):
        return self._find(address, timeout=timeout)


class FakeScan:
    """Stands in for transport_ble.scan()."""

    def __init__(self, result=None):
        self.result = result if result is not None else []
        self.calls = []

    def __call__(self, timeout=10, smp_only=True):
        self.calls.append((timeout, smp_only))
        return self.result


def _msg(seq=0, payload=None):
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.READ_RSP, nh_group=1, nh_id=2, nh_seq=seq)
    m.encode_payload(payload if payload is not None else {"rc": 0})
    return m


# -- device lookup -----------------------------------------------------------


def test_address_lookup_hit_skips_the_full_scan():
    dev = FakeDev()
    scanner = FakeScanner(result=dev)
    full_scan = FakeScan(result=[(FakeDev(), FakeAdv())])

    with patch.object(transport_ble, "BleakScanner", scanner), \
            patch.object(transport_ble, "scan", full_scan):
        got = transport_ble.find_device(address=dev.address, timeout=1)

    assert got is dev
    assert len(scanner.calls) == 1
    assert full_scan.calls == [], "full scan must not run when the fast path hits"


def test_address_lookup_miss_falls_back_to_full_scan():
    """Regression: BlueZ returns None from find_device_by_address for devices a
    full scan sees perfectly well."""
    wanted = FakeDev(address="11:22:33:44:55:66")
    other = FakeDev(address="00:00:00:00:00:00")
    scanner = FakeScanner(result=None)
    full_scan = FakeScan(result=[(other, FakeAdv()), (wanted, FakeAdv())])

    with patch.object(transport_ble, "BleakScanner", scanner), \
            patch.object(transport_ble, "scan", full_scan):
        got = transport_ble.find_device(address="11:22:33:44:55:66", timeout=1)

    assert got is wanted
    assert len(full_scan.calls) == 1, "fallback scan should have run once"
    # the fallback must not filter on the advertised service uuid
    assert full_scan.calls[0][1] is False


def test_address_lookup_is_case_insensitive_in_the_fallback():
    dev = FakeDev(address="aa:bb:cc:dd:ee:ff")
    scanner = FakeScanner(result=None)
    full_scan = FakeScan(result=[(dev, FakeAdv())])

    with patch.object(transport_ble, "BleakScanner", scanner), \
            patch.object(transport_ble, "scan", full_scan):
        got = transport_ble.find_device(address="AA:BB:CC:DD:EE:FF", timeout=1)

    assert got is dev


def test_address_not_found_anywhere_returns_none():
    scanner = FakeScanner(result=None)
    full_scan = FakeScan(result=[(FakeDev(address="00:00:00:00:00:01"), FakeAdv())])

    with patch.object(transport_ble, "BleakScanner", scanner), \
            patch.object(transport_ble, "scan", full_scan):
        got = transport_ble.find_device(address="99:99:99:99:99:99", timeout=1)

    assert got is None
    assert len(full_scan.calls) == 1


def test_name_lookup_uses_the_full_scan_only():
    dev = FakeDev(address="11:22:33:44:55:66", name=None)
    scanner = FakeScanner(result=FakeDev())
    full_scan = FakeScan(result=[(dev, FakeAdv(local_name="mydev"))])

    with patch.object(transport_ble, "BleakScanner", scanner), \
            patch.object(transport_ble, "scan", full_scan):
        got = transport_ble.find_device(name="mydev", timeout=1)

    assert got is dev
    assert scanner.calls == [], "no address lookup when only a name is given"


def test_find_device_needs_an_identifier():
    try:
        transport_ble.find_device()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without address or name")


def test_adv_has_smp_service():
    assert transport_ble._adv_has_smp_service(FakeAdv(service_uuids=[UUID_SERVICE]))
    assert transport_ble._adv_has_smp_service(
        FakeAdv(service_uuids=[UUID_SERVICE.upper()])
    )
    assert not transport_ble._adv_has_smp_service(FakeAdv(service_uuids=None))
    assert not transport_ble._adv_has_smp_service(FakeAdv(service_uuids=["1234"]))


# -- connect / teardown ------------------------------------------------------


def _mk_transport(**kwargs):
    kwargs.setdefault("timeout", 1)
    t = SMPTransportBLE(address="AA:BB:CC:DD:EE:FF", **kwargs)
    # no point retrying against a fake that fails deterministically
    t.CONNECT_ATTEMPTS = 1
    t.CONNECT_BACKOFF = 0
    return t


def test_connect_subscribes_to_notifications():
    dev = FakeDev()
    factory = FakeClientFactory()
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.connect()

    clnt = factory.last
    assert t._clnt is clnt
    assert clnt.is_connected
    assert len(clnt.notify_calls) == 1
    assert clnt.notify_calls[0][0] == UUID_CHARACT
    assert clnt.notify_calls[0][1] == t._response_handler
    assert clnt.disconnect_calls == 0


def test_failed_start_notify_does_not_leak_the_connection():
    """Regression: start_notify failing left a live GATT link behind, and
    connect() then retried with a brand new client."""
    dev = FakeDev()
    factory = FakeClientFactory(notify_error=RuntimeError("no such characteristic"))

    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        try:
            t.connect()
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the start_notify failure to propagate")

    clnt = factory.last
    assert clnt.disconnect_calls == 1, "connection leaked after start_notify failed"
    assert not clnt.is_connected
    assert t._clnt is None, "failed client must not stay current"


def test_connect_retries_and_reports_the_last_error():
    dev = FakeDev()
    factory = FakeClientFactory(connect_error=smp.SMPTransportError("le-connect fail"))

    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.CONNECT_ATTEMPTS = 3
        try:
            t.connect()
        except smp.SMPTransportError as e:
            assert "le-connect fail" in str(e)
        else:
            raise AssertionError("expected SMPTransportError")

    assert len(factory.clients) == 3
    # every failed attempt cleans up after itself
    assert all(c.disconnect_calls == 1 for c in factory.clients)


def test_device_not_found_raises_transport_error():
    with patch.object(transport_ble, "find_device", lambda *a, **k: None):
        t = _mk_transport()
        try:
            t.connect()
        except smp.SMPTransportError as e:
            assert "not found" in str(e).lower()
        else:
            raise AssertionError("expected SMPTransportError")


def test_disconnect_clears_the_current_client():
    dev = FakeDev()
    factory = FakeClientFactory()
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.connect()
        clnt = factory.last
        t.disconnect()

    assert clnt.disconnect_calls == 1
    assert t._clnt is None
    assert not t.is_connected()
    t.disconnect()  # idempotent
    assert clnt.disconnect_calls == 1


def test_reconnect_uses_a_new_client_and_drops_stale_state():
    dev = FakeDev()
    factory = FakeClientFactory()
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.connect()
        old = factory.last

        # leftovers from the link that is about to go away
        t._read_buf.extend(b"\x01\x02\x03")
        t._read_msg_q.put_nowait(_msg(seq=1))

        t.reconnect()

    assert old.disconnect_calls == 1
    assert len(factory.clients) == 2
    assert t._clnt is factory.last and t._clnt is not old
    assert bytes(t._read_buf) == b"", "stale half-message survived reconnect()"
    assert t._read_msg_q.empty(), "stale message survived reconnect()"


# -- disconnect callback -----------------------------------------------------


def test_on_disconnect_from_stale_client_is_ignored():
    """Regression: a late disconnect signal from an old client used to inject a
    bogus error into the queue of a healthy new connection."""
    t = _mk_transport()
    client_a = object()
    client_b = object()
    t._clnt = client_a

    t._on_disconnect(client_b)
    assert t._read_msg_q.empty(), "stale disconnect leaked onto the queue"

    t._on_disconnect(client_a)
    assert not t._read_msg_q.empty(), "disconnect of the current client was dropped"
    itm = t._read_msg_q.get_nowait()
    assert isinstance(itm, smp.SMPDisconnectedError)


def test_on_disconnect_after_teardown_is_ignored():
    """_teardown() makes the client non-current first, so its own disconnect
    callback cannot reach the queue."""
    dev = FakeDev()
    factory = FakeClientFactory()
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.connect()
        clnt = factory.last
        t.disconnect()

    # BlueZ delivers the signal for the client we just tore down
    clnt.disconnected_callback(clnt)
    assert t._read_msg_q.empty()


def test_disconnect_error_wakes_a_blocked_reader():
    t = _mk_transport()
    clnt = object()
    t._clnt = clnt
    t._on_disconnect(clnt)
    try:
        t.read_msg(timeout=1)
    except smp.SMPDisconnectedError:
        pass
    else:
        raise AssertionError("expected SMPDisconnectedError")


# -- notifications / io ------------------------------------------------------


def test_response_handler_delivers_a_message():
    t = _mk_transport()
    msg = _msg(seq=4, payload={"rc": 0, "ok": True})
    t._response_handler(None, msg.to_bytes())

    out = t.read_msg(timeout=1)
    assert out.hdr.nh_seq == 4
    assert out.decode_payload()["ok"] is True


def test_response_handler_reassembles_split_notifications():
    t = _mk_transport()
    raw = _msg(seq=5).to_bytes()
    t._response_handler(None, raw[:4])
    assert t._read_msg_q.empty()
    t._response_handler(None, raw[4:])
    assert t.read_msg(timeout=1).hdr.nh_seq == 5


def test_response_handler_drains_several_messages_in_one_notification():
    t = _mk_transport()
    raw = _msg(seq=1).to_bytes() + _msg(seq=2).to_bytes()
    t._response_handler(None, raw)
    assert t.read_msg(timeout=1).hdr.nh_seq == 1
    assert t.read_msg(timeout=1).hdr.nh_seq == 2
    assert bytes(t._read_buf) == b""


def test_response_handler_bounds_the_rx_buffer():
    """Garbage that never parses must not wedge every later read."""
    t = _mk_transport()
    # 0xff length bytes: always claims more payload than has arrived
    junk = b"\x00\x00\xff\xff\x00\x00\x00\x00" * 64
    for _ in range(2 * (t.MAX_RX_BUFFER // len(junk) + 1)):
        t._response_handler(None, junk)
        assert len(t._read_buf) <= t.MAX_RX_BUFFER + len(junk)
    assert t._read_msg_q.empty()

    msg = _msg(seq=6)
    t._response_handler(None, msg.to_bytes())
    assert t.read_msg(timeout=1).hdr.nh_seq == 6


def test_write_when_disconnected_raises():
    t = _mk_transport()
    try:
        t.write(b"\x00")
    except smp.SMPDisconnectedError:
        pass
    else:
        raise AssertionError("expected SMPDisconnectedError")


def test_write_msg_goes_out_without_response():
    dev = FakeDev()
    factory = FakeClientFactory()
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t = _mk_transport()
        t.connect()
        msg = _msg(seq=8)
        t.write_msg(msg)

    uuid, data, response = factory.last.writes[0]
    assert uuid == UUID_CHARACT
    assert data == msg.to_bytes()
    assert response is False


def test_max_mtu_defaults_and_follows_the_link():
    t = _mk_transport()
    assert t.max_mtu == smp.MGMT_MAX_MTU

    dev = FakeDev()
    factory = FakeClientFactory(mtu_size=247)
    with patch.object(transport_ble, "find_device", lambda *a, **k: dev), \
            patch.object(transport_ble, "BleakClient", factory):
        t.connect()
        assert t.max_mtu == 247 - SMPTransportBLE.ATT_HEADER_SIZE
        # a useless MTU never drops below the smallest usable write
        factory.last.mtu_size = 5
        assert t.max_mtu == 20
        factory.last.mtu_size = 0
        assert t.max_mtu == smp.MGMT_MAX_MTU


def test_read_msg_times_out():
    t = _mk_transport()
    try:
        t.read_msg(timeout=0.1)
    except smp.SMPTransportError as e:
        assert "No response" in str(e)
    else:
        raise AssertionError("expected a timeout")


def test_needs_an_identifier():
    try:
        SMPTransportBLE()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without address or name")


def test_seq_counter_is_per_transport():
    t = _mk_transport()
    assert [t.next_seq() for _ in range(3)] == [0, 1, 2]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
