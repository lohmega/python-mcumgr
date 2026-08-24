"""Unit tests for mcumgr.smp_proxy - the proxy-forward transport wrapper.

Runs standalone (`python3 test/test_smp_proxy.py`) or under pytest.
Requires cbor2.
"""

import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

import cbor2 as cbor

from mcumgr.smp_proxy import SmpProxyTransport


class FakeBaseTransport:
    """Just enough for SmpProxyTransport's __init__/max_mtu to work."""

    def __init__(self, max_mtu):
        self.max_mtu = max_mtu

    def is_connected(self):
        return True


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


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
