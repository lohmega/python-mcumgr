import sys, argparse, tty, termios
from queue import Queue
from threading import Thread
import re
import time
import json
import struct
import base64
import serial
import serial.threaded
import crcmod.predefined

import asyncio
import threading
from serial.tools.list_ports import comports
from socket import *
from queue import Queue

import logging
import platform
import smp
from enum import Enum, IntEnum

logger = logging.getLogger(__name__)

class NLIP_OP(IntEnum):
    """ Opcodes; first two bytes in nlip-line. """

    # fmt: off
    PKT_START1    = 6
    PKT_START2    = 9
    DATA_START1   = 4
    DATA_START2   = 20
    # fmt: on

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
            self._queue.get(), self._loop
        ).result()

    def aput_nowait(self, item):
        self._queue.put_nowait(item)

    async def aput(self, item):
        await self._queue.put(item)

    async def aget(self, timeout=None):
        return await self._queue.get()

class HwtSerial(serial.threaded.LineReader):
    """Represents the serial connection to a board used in the test"""
    def __init__(self):
        super(HwtSerial, self).__init__()
        self.rx_cb = None
        self.connected = False

    def connection_made(self, transport):
        super(HwtSerial, self).connection_made(transport)
        self.connected = True

    def handle_line(self, line):
        if self.rx_cb != None:
            self.rx_cb(line)


class SMPClientNlip:

    MAX_DATA_PER_LINE = 120

    def __init__(
            self, device=None, baudrate=115200, timeout=10, read_cb=None, *args, **kwargs
    ):
        if device is None:
            raise ValueError("No device identifier. Need address or name")

        self._device = device
        self._baudrate = baudrate
        self._timeout = timeout
        self._read_cb = read_cb
        self._read_buf = bytearray()
        self._read_msg_q = _Queue()
        self._clnt = None
        self._comm_ser = None

    def _set_disconnected_callback(self, cb):
        try:
            self._clnt.set_disconnected_callback(cb)
        # not in all backend (yet). will work without it but might hang forever
        except NotImplementedError:
            #logger.debug("set_disconnected_callback not supported")
            pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    def nlip_clear(self):
        self._nlip_data = bytearray()
        self._nlip_len = 0
        self._nlip_hdr = None

    def nlip_process(self, b64_data):
        data = base64.b64decode(b64_data);
        print(data.hex())
        if self._nlip_len == 0:
            self._nlip_len, = struct.unpack('>H', data[0:2])
            self._nlip_data += data[2:]
        else:
            self._nlip_data += data

        if (self._nlip_hdr == None and len(self._nlip_data) > 10):
            self._nlip_hdr = smp.MgmtHdr.from_bytes(self._nlip_data[2:10])

        if (self._nlip_len == len(self._nlip_data)):
            try:
                msg = smp.MgmtMsg.from_bytes(self._nlip_data)
            except IndexError as e:
                logger.debug("received %d bytes. %s", len(self._read_buf), str(e))

            if self._read_cb:
                self._read_cb(self, msg)
            else:
                self._read_msg_q.put_nowait(msg)

    def received_line(self, line):
        if (len(line) > 2):
            if (ord(line[0]) == NLIP_OP.PKT_START1 and
                ord(line[1]) == NLIP_OP.PKT_START2):
                self.nlip_clear()
                self.nlip_process(line[2:])
            elif (ord(line[0]) == NLIP_OP.DATA_START1 and
                ord(line[1]) == NLIP_OP.DATA_START2):
                self.nlip_process(line[2:])
            else:
                print("line: {}".format(line))
        else:
            print("line: {}".format(line))

    async def connect(self):
        for p in sorted(comports()):
            if self._device == p.device:
                try:
                    print("#   Opening {}".format(p.device))
                    self.comm_ser = serial.Serial(p.device, baudrate=self._baudrate, timeout=self._timeout)
                except serial.serialutil.SerialException:
                    print('# Could not open ' + p.device)
                    raise ValueError("Could not connect to port")
                break

        self._clnt = serial.threaded.ReaderThread(self.comm_ser, HwtSerial)

        self.linereader = self._clnt.__enter__()
        self.linereader.rx_cb = self.received_line

        return 0
        # self._set_disconnected_callback(self._on_disconnect)
        #await self._clnt.connect(timeout=self._timeout)
        #await self._clnt.start_notify(UUID_CHARACT, self._response_handler)

    async def disconnect(self):
        # self._set_disconnected_callback(None)
        #await self._clnt.disconnect()
        return 0

    def _on_disconnect(self, client, _x=None):
        raise RuntimeError("Disconnected")

    async def is_connected(self):
        while self.linereader.connected == False:
            time.sleep(0.1);
        return self.linereader.connected

    async def write(self, data):
        if hasattr(data, "__bytes__"):
            data = bytes(data)

        if not isinstance(data, bytearray):
            data = bytearray(data)  # some backend(s) might require this

        if not await self.is_connected():
            raise RuntimeError("Not connected")

        # Formal NLIP packet(s)
        crc16 = crcmod.predefined.Crc('xmodem')
        crc16.update(data)
        data = struct.pack('>H', len(data)+2) + data + struct.pack('>H', crc16.crcValue)
        if (len(base64.b64encode(data)) < self.MAX_DATA_PER_LINE):
            txdata = struct.pack('>BB', NLIP_OP.PKT_START1, NLIP_OP.PKT_START2) + base64.b64encode(data) + b'\n';
            logger.debug("TX: %s", txdata.hex())
            print("# TX: {}".format(txdata.hex()))
            self.comm_ser.write(txdata)
        else:
            offset = 0
            sub_data = data[0:int(0.75*self.MAX_DATA_PER_LINE)]
            txdata = struct.pack('>BB', NLIP_OP.PKT_START1, NLIP_OP.PKT_START2) + base64.b64encode(sub_data) + b'\n';
            self.comm_ser.write(txdata)
            while offset < len(data):
                to_copy = len(data) - offset
                if (to_copy > int(0.75*self.MAX_DATA_PER_LINE)): to_copy = int(0.75*self.MAX_DATA_PER_LINE)
                sub_data = data[offset:(offset+to_copy)]
                txdata = struct.pack('>BB', NLIP_OP.DATA_START1, NLIP_OP.DATA_START2) + base64.b64encode(sub_data) + b'\n';
                self.comm_ser.write(txdata)
                offset += to_copy

        self.comm_ser.flush()
        # Why do we need this sleep here?
        time.sleep(0.5);

    async def write_msg(self, msg):
        await self.write(msg.to_bytes())

    async def read_msg(self, timeout=None):
        if self._read_cb:
            raise RuntimeError("blocking read not allowed when callback set")

        return await self._read_msg_q.aget(timeout=timeout)
