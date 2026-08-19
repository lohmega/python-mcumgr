"""BLE (GATT) transport for the SMP protocol.

mcumgr/newtmgr over BLE uses a single GATT service with one characteristic that
takes write-without-response for requests and notifies for responses:

    service  8D53DC1D-1DB7-4CD3-868B-8A527460AA84
    charact  DA2E7828-FBCE-4E01-AE9E-261174997C48

bleak is async and the rest of this package is synchronous, so a single daemon
thread runs an event loop for the whole process and every bleak call is
marshalled onto it.
"""

import asyncio
import logging
import queue
import time
from threading import Thread

# third party imports
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

# local imports
from mcumgr import smp

UUID_SERVICE = "8d53dc1d-1db7-4cd3-868b-8a527460aa84"
UUID_CHARACT = "da2e7828-fbce-4e01-ae9e-261174997c48"

logger = logging.getLogger(__name__)

_thread_loop = None


def _async_loop_worker(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_thread_loop():
    """The one event loop that all bleak calls in this process run on."""
    global _thread_loop
    if _thread_loop is not None:
        return _thread_loop

    _thread_loop = asyncio.new_event_loop()
    t = Thread(target=_async_loop_worker, args=(_thread_loop,), daemon=True)
    t.start()
    return _thread_loop


def _async_call(coro, timeout=None):
    """Run a coroutine on the background loop and block until it completes.

    Backend failures surface as SMPTransportError so that callers only have to
    know about this package's exceptions, not bleak's.
    """
    fut = asyncio.run_coroutine_threadsafe(coro, _get_thread_loop())
    try:
        return fut.result(timeout=timeout)
    except TimeoutError:
        fut.cancel()
        raise smp.SMPTransportError("timeout after {}s".format(timeout)) from None
    except BleakError as e:
        raise smp.SMPTransportError(str(e)) from e


def _adv_has_smp_service(adv):
    uuids = [str(u).lower() for u in (adv.service_uuids or [])]
    return UUID_SERVICE in uuids


def scan(timeout=10, smp_only=True):
    """Scan for devices. Returns a list of (BLEDevice, AdvertisementData).

    Note that a device can host the SMP service without advertising its UUID,
    so `smp_only` filters on what is advertised, not on what the device has.
    """
    found = _async_call(
        BleakScanner.discover(timeout=timeout, return_adv=True),
        timeout=timeout + 10,
    )

    devices = []
    for dev, adv in found.values():
        logger.debug(
            "address=%s name=%s rssi=%s uuids=%s",
            dev.address,
            adv.local_name,
            adv.rssi,
            adv.service_uuids,
        )
        if smp_only and not _adv_has_smp_service(adv):
            continue
        devices.append((dev, adv))

    return devices


def find_device(address=None, name=None, timeout=10):
    """Find a single device by address or advertised name."""
    if address is None and name is None:
        raise ValueError("No device identifier. Need address or name")

    if address:
        # BleakScanner can stop as soon as it sees the address, no need to
        # burn the full scan window.
        dev = _async_call(
            BleakScanner.find_device_by_address(address, timeout=timeout),
            timeout=timeout + 10,
        )
        return dev

    for dev, adv in scan(timeout=timeout, smp_only=False):
        if name in (adv.local_name, dev.name):
            return dev

    return None


class SMPTransportBLE:
    """BLE transport for SMP protocol using GATT characteristics"""

    # Bytes of ATT payload lost to the ATT opcode + handle on a write command.
    ATT_HEADER_SIZE = 3

    def __init__(
        self, address=None, name=None, timeout=10, read_cb=None, *args, **kwargs
    ):
        if address is None and name is None:
            raise ValueError("No device identifier. Need address or name")

        self._address = address
        self._name = name
        self._timeout = timeout
        self._read_cb = read_cb
        self._read_buf = bytearray()
        # plain thread queue: notifications are delivered on the event loop
        # thread and consumed by the caller's thread, and unlike the asyncio
        # queue this one actually honours a get() timeout.
        self._read_msg_q = queue.Queue()
        self._clnt = None
        self._seq = smp.SeqCounter()

    def next_seq(self):
        """Next nh_seq for this connection. One counter per transport."""
        return self._seq.next()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    @property
    def max_mtu(self):
        """Largest SMP message that fits in one write, in bytes."""
        if self._clnt is None:
            return smp.MGMT_MAX_MTU
        mtu = getattr(self._clnt, "mtu_size", None)
        if not mtu:
            return smp.MGMT_MAX_MTU
        return max(mtu - self.ATT_HEADER_SIZE, 20)

    # BlueZ can drop a device from its cache between discovery and connect, so
    # a first attempt failing with "not found" is common and not terminal.
    CONNECT_ATTEMPTS = 3
    CONNECT_BACKOFF = 2.0

    def connect(self):
        last_err = None

        for attempt in range(1, self.CONNECT_ATTEMPTS + 1):
            try:
                self._connect_once()
                return
            except smp.SMPTransportError as e:
                last_err = e
                logger.debug("connect attempt %d/%d failed: %s",
                             attempt, self.CONNECT_ATTEMPTS, e)
                if attempt < self.CONNECT_ATTEMPTS:
                    # Give BlueZ a moment to re-discover rather than hammering
                    # it with back-to-back scans.
                    time.sleep(self.CONNECT_BACKOFF)

        raise last_err

    def reconnect(self):
        """Drop the link and establish a fresh one.

        Used to carry an interrupted upload across a dropped connection.
        """
        logger.debug("reconnecting")
        try:
            self.disconnect()
        except Exception as e:
            logger.debug("ignoring error while dropping old link: %s", e)

        # A stale half-message must not be parsed onto the new connection.
        self._read_buf = bytearray()
        while True:
            try:
                self._read_msg_q.get_nowait()
            except queue.Empty:
                break

        self.connect()

    def _connect_once(self):
        dev = find_device(self._address, self._name, self._timeout)
        if not dev:
            raise smp.SMPTransportError(
                "Device not found (address={}, name={})".format(
                    self._address, self._name
                )
            )

        logger.debug("Device found %s", str(dev))

        # No explicit pair() call. bleak 0.x needed one and it never worked on
        # BlueZ anyway (issue #1); BlueZ pairs on demand when a characteristic
        # requires an encrypted link.
        self._clnt = BleakClient(
            dev,
            timeout=self._timeout,
            disconnected_callback=self._on_disconnect,
        )

        _async_call(self._clnt.connect(), timeout=self._timeout + 10)
        self._acquire_mtu()
        logger.debug("connected, mtu=%d", self.max_mtu)
        _async_call(
            self._clnt.start_notify(UUID_CHARACT, self._response_handler),
            timeout=self._timeout,
        )

    def _acquire_mtu(self):
        """Ask BlueZ for the negotiated ATT MTU.

        On BlueZ, mtu_size reports the 23 byte default (and warns) until the
        MTU has been acquired from a characteristic, which would cap upload
        chunks at 20 bytes. Other backends know it already. This uses private
        bleak API, so failure is not fatal - we just keep the default.
        """
        backend = getattr(self._clnt, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is None:
            return
        if getattr(backend, "_mtu_size", None) is not None:
            return

        try:
            _async_call(acquire(), timeout=self._timeout)
        except Exception as e:  # private API, best effort only
            logger.debug("could not acquire MTU: %s", e)

    def disconnect(self):
        if self._clnt is None:
            return
        try:
            _async_call(self._clnt.disconnect(), timeout=self._timeout + 10)
        except (BleakError, smp.SMPTransportError) as e:
            logger.warning("error during disconnect: %s", e)

    def is_connected(self):
        # bleak >= 0.10 exposes this as a property, not a coroutine.
        return self._clnt is not None and self._clnt.is_connected

    def _response_handler(self, sender, data):
        data = bytearray(data)
        logger.debug("RX: %s", data.hex())

        self._read_buf.extend(data)

        # A response can be split over several notifications, and more than one
        # response can have arrived, so drain everything that is complete.
        while True:
            try:
                msg = smp.MgmtMsg.from_bytes(self._read_buf)
            except IndexError:
                logger.debug("buffered %d bytes, need more", len(self._read_buf))
                return

            logger.debug("received msg size %d", msg.size)
            self._read_buf = self._read_buf[msg.size :]

            if self._read_cb:
                self._read_cb(self, msg)
            else:
                self._read_msg_q.put_nowait(msg)

    def _on_disconnect(self, client):
        logger.debug("disconnected")
        # Wake up anyone blocked in read_msg() instead of letting them wait out
        # the full timeout on a link that is already gone.
        self._read_msg_q.put_nowait(smp.SMPDisconnectedError("Disconnected"))

    def write(self, data):
        if hasattr(data, "__bytes__"):
            data = bytes(data)

        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)

        if not self.is_connected():
            raise smp.SMPDisconnectedError("Not connected")

        logger.debug("TX: %s", bytearray(data).hex())
        _async_call(
            self._clnt.write_gatt_char(UUID_CHARACT, data, response=False),
            timeout=self._timeout,
        )

    def write_msg(self, msg):
        self.write(msg.to_bytes())

    def read_msg(self, timeout=None):
        if self._read_cb:
            raise RuntimeError("blocking read not allowed when callback set")

        if timeout is None:
            timeout = self._timeout

        try:
            itm = self._read_msg_q.get(timeout=timeout)
        except queue.Empty:
            raise smp.SMPTransportError(
                "No response within {}s".format(timeout)
            ) from None

        # raise transport errors in the caller's thread, not the loop thread
        if isinstance(itm, Exception):
            raise itm
        return itm


# Backward compatibility alias
SMPClientBLE = SMPTransportBLE
