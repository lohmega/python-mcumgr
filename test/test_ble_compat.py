"""Unit tests for mcumgr.ble - the deprecated backward-compat import path.

Runs standalone (`python3 test/test_ble_compat.py`) or under pytest.
Requires bleak (imported transitively) and cbor2.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import patch

import mcumgr.ble as ble


class FakeDev:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="smpdev"):
        self.address = address
        self.name = name


class FakeAdv:
    def __init__(self, local_name=None):
        self.local_name = local_name


def test_scan_defaults_to_smp_only():
    """The pre-rename scan() always filtered to the SMP service UUID,
    regardless of whether address/name were given - that must not
    regress just because the new transport_ble.scan() makes it optional."""
    seen_kwargs = {}

    def fake_scan(timeout, smp_only):
        seen_kwargs["timeout"] = timeout
        seen_kwargs["smp_only"] = smp_only
        return [(FakeDev(), FakeAdv())]

    with patch.object(ble._transport_ble, "scan", fake_scan):
        asyncio.run(ble.scan(timeout=1))

    assert seen_kwargs["smp_only"] is True


def test_scan_filters_by_name():
    devices = [
        (FakeDev(address="AA:AA", name="sem-bb"), FakeAdv(local_name="sem-bb")),
        (FakeDev(address="BB:BB", name="other"), FakeAdv(local_name="other")),
    ]

    with patch.object(ble._transport_ble, "scan", lambda **kw: devices):
        result = asyncio.run(ble.scan(name="sem-bb", timeout=1))

    assert [d.address for d in result] == ["AA:AA"]


def test_scan_filters_by_address():
    devices = [
        (FakeDev(address="AA:AA"), FakeAdv()),
        (FakeDev(address="BB:BB"), FakeAdv()),
    ]

    with patch.object(ble._transport_ble, "scan", lambda **kw: devices):
        result = asyncio.run(ble.scan(address="BB:BB", timeout=1))

    assert [d.address for d in result] == ["BB:BB"]


def test_scan_returns_bare_device_list():
    """Old contract: a list of devices, not (device, advertisement) pairs."""
    devices = [(FakeDev(), FakeAdv())]

    with patch.object(ble._transport_ble, "scan", lambda **kw: devices):
        result = asyncio.run(ble.scan(timeout=1))

    assert len(result) == 1
    assert isinstance(result[0], FakeDev)


def test_find_device_is_the_same_function():
    """find_device's signature/contract was already sync and unchanged
    across the rename, so it is a plain re-export - unlike scan()."""
    from mcumgr.transport_ble import find_device as new_find_device

    assert ble.find_device is new_find_device


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
