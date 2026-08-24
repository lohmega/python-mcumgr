"""Deprecated import path - kept for backward compatibility.

Use `mcumgr.transport_serial` (or `mcumgr.SMPTransportSerial`) instead. This
module will be removed in a future release.
"""

from mcumgr.transport_serial import SMPClientNlip, SMPTransportSerial  # noqa: F401
