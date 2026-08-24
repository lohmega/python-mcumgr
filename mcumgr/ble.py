"""Deprecated import path - kept for backward compatibility.

Use `mcumgr.transport_ble` (or `mcumgr.SMPTransportBLE`) instead. This module
will be removed in a future release.
"""

from mcumgr.transport_ble import SMPClientBLE, SMPTransportBLE  # noqa: F401
