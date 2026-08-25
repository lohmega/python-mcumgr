"""Python implementation of the mcumgr / newtmgr SMP protocol(s).

Transports are imported lazily so that installing only the serial extra (or
only the BLE extra) does not make `import mcumgr` fail on a missing bleak or
pyserial.
"""

from mcumgr.__version__ import __version__

__all__ = [
    "__version__",
    "smp",
    "image",
    "mgmt",
    "mgmt_os",
    "mgmt_image",
    "SMPTransportBLE",
    "SMPTransportSerial",
]


def __getattr__(name):
    # PEP 562 module level __getattr__ - defer the optional third party imports
    # (bleak, pyserial) until a transport is actually asked for.
    if name == "SMPTransportBLE":
        from mcumgr.transport_ble import SMPTransportBLE

        return SMPTransportBLE

    if name == "SMPTransportSerial":
        from mcumgr.transport_serial import SMPTransportSerial

        return SMPTransportSerial

    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))
