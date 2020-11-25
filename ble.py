import asyncio
import logging
import platform
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import smp
from queue import Queue

# mcumgr or newtmgr can be used over BLE with the following GATT service and
# characteristic UUIDs to connect to a SMP server running on the target device:
UUID_SERVICE = "8D53DC1D-1DB7-4CD3-868B-8A527460AA84"
UUID_CHARACT = "DA2E7828-FBCE-4E01-AE9E-261174997C48"

# The "SMP" GATT service consists of one write no-rsp characteristic for SMP
# requests: a single-byte characteristic that can only accepts
# write-without-response commands. The contents of each write command contains
# an SMP request.

logger = logging.getLogger(__name__)

from subprocess import Popen, run, PIPE
import time


class _Queue:
    """ Queue shared between asynchronous and synchronous code"""
    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

    def put_nowait(self, item):
        self._loop.call_soon(self._queue.put_nowait, item)
        # self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def put(self, item):
        asyncio.run_coroutine_threadsafe(self._queue.put(item), self._loop).result()

    def get(self):
        return asyncio.run_coroutine_threadsafe(
            self._queue.get(item), self._loop
        ).result()

    def aput_nowait(self, item):
        self._queue.put_nowait(item)

    async def aput(self, item):
        await self._queue.put(item)

    async def aget(self, timeout=None):
        return await self._queue.get()


if platform.system() == "Linux":

    def bluetoothctl(dev):
        props = dev.details["props"]
        if not props:
            logger.warning("No props")
            return

        logger.debug(str(props))

        p = Popen("bluetoothctl", stdin=PIPE, stdout=PIPE, stderr=PIPE)

        trusted = props.get("Trusted", False)
        if not trusted:
            cmd = "trust {}\n".format(dev.address)
            p.stdin.write(cmd.encode())
            time.sleep(1)

        paired = props.get("Paired", False)
        if not paired:
            cmd = "pair {}\n".format(dev.address)
            p.stdin.write(cmd.encode())
            time.sleep(1)

        if 1:
            cmd = "quit\n"
            p.stdin.write(cmd.encode())
            time.sleep(1)

        p.communicate()
        logger.debug(
            "bluethoothctl stdout:'%s', stderr:'%s'", str(p.stdout), str(p.stderr)
        )


async def scan(address=None, name=None, timeout=10):
    devices = []
    scanner = BleakScanner()
    candidates = await scanner.discover(timeout=timeout)
    suuid = UUID_SERVICE
    for d in candidates:
        logger.debug(
            "address={}. details={}, metadata={}".format(
                d.address, d.details, d.metadata
            )
        )

        if not "uuids" in d.metadata:
            continue

        if address and address != d.address:
            continue

        advertised = d.metadata["uuids"]
        if not suuid.lower() in advertised and not suuid.upper() in advertised:
            continue

        devices.append(d)

    return devices


async def find_device(address=None, name=None, timeout=10):
    scanner = BleakScanner()

    logger.debug("connecting...")
    candidates = await scanner.discover(timeout=timeout)

    for d in candidates:
        logger.debug(
            "address={}. details={}, metadata={}".format(
                d.address, d.details, d.metadata
            )
        )
        if name and name == d.name:
            return d

        if address and address == d.address:
            return d

    return None


class SMPClientBLE:
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
        self._read_msg_q = _Queue()
        self._clnt = None

    def _set_disconnected_callback(self, cb):
        try:
            self._clnt.set_disconnected_callback(cb)
        # not in all backend (yet). will work without it but might hang forever
        except NotImplementedError:
            logger.debug("set_disconnected_callback not supported")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def connect(self):
        dev = await find_device(self._address, self._name, self._timeout)
        if not dev:
            raise RuntimeError("Device not found")

        logger.debug("Device found %s", str(dev))

        self._clnt = BleakClient(dev, timeout=self._timeout)

        try:
            paired = self._clnt.pair()
            if not paired:
                logger.warning("not paired")
        except NotImplementedError as e:
            if platform.system() == "Darwin":
                pass # pairing is automagic in MacOS
            else:
                raise e # probably old bleak version

        # self._set_disconnected_callback(self._on_disconnect)
        await self._clnt.connect(timeout=self._timeout)
        await self._clnt.start_notify(UUID_CHARACT, self._response_handler)

    async def disconnect(self):
        # self._set_disconnected_callback(None)
        await self._clnt.disconnect()

    def _response_handler(self, sender, data):
        if not isinstance(data, bytearray):
            data = bytearray(data)  # some BLE backend(s) might require this

        logger.debug("RX: %s", data.hex())
        if self._read_cb:
            self._read_cb(self, data)

        self._read_buf.extend(data)
        try:
            msg = smp.MgmtMsg.from_bytes(self._read_buf)
        except IndexError as e:
            logger.debug("received %d bytes. %s", len(self._read_buf), str(e))
            return

        logger.debug("received msg size %d", msg.size)
        # keep data that is not part of the msg
        self._read_buf = self._read_buf[msg.size :]

        if self._read_cb:
            self._read_cb(self, msg)
        else:
            self._read_msg_q.put_nowait(msg)

    def _on_disconnect(self, client, _x=None):
        raise RuntimeError("Disconnected")

    async def write(self, data):
        if hasattr(data, "__bytes__"):
            data = bytes(data)

        if not isinstance(data, bytearray):
            data = bytearray(data)  # some BLE backend(s) might require this

        if not await self._clnt.is_connected():
            raise RuntimeError("Not connected")
        logger.debug("TX: %s", data.hex())
        await self._clnt.write_gatt_char(UUID_CHARACT, data, response=False)

    async def write_msg(self, msg):
        await self.write(msg.to_bytes())

    async def read_msg(self, timeout=None):
        if self._read_cb:
            raise RuntimeError("blocking read not allowed when callback set")

        return await self._read_msg_q.aget(timeout=timeout)

