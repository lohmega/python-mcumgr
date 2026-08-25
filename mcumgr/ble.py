"""Deprecated import path - kept for backward compatibility.

Use `mcumgr.transport_ble` (or `mcumgr.SMPTransportBLE`) instead. This module
will be removed in a future release.
"""

import asyncio

from mcumgr.transport_ble import (  # noqa: F401
    UUID_CHARACT,
    UUID_SERVICE,
    SMPClientBLE,
    SMPTransportBLE,
    find_device,
)
from mcumgr import transport_ble as _transport_ble


async def scan(address=None, name=None, timeout=10):
    """Deprecated. Matches the pre-rename async scan()'s contract:
    an awaitable filtering by address/name, returning a bare list of
    devices (not (device, advertisement) pairs).

    `mcumgr.transport_ble.scan()` replaced this - it is synchronous, filters
    by advertised SMP service UUID rather than address/name, and returns
    (device, advertisement) pairs. This wrapper is scan()'s old shape built
    on top of that, not a re-export - the two are not interchangeable.
    """
    devices = await asyncio.to_thread(
        _transport_ble.scan, timeout=timeout, smp_only=False
    )

    result = []
    for dev, adv in devices:
        if address and address != dev.address:
            continue
        if name and name not in (adv.local_name, dev.name):
            continue
        result.append(dev)
    return result
