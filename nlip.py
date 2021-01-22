from queue import Queue
from threading import Thread
import logging
import struct
import base64
import serial
import crcmod.predefined
import smp
from enum import IntEnum

logger = logging.getLogger(__name__)


class NLIP_OP(IntEnum):
    """ Opcodes; first two bytes in nlip-line. """

    # fmt: off
    PKT_START1    = 6
    PKT_START2    = 9
    DATA_START1   = 4
    DATA_START2   = 20
    # fmt: on


class NlipPkt:

    # TODO handle crc in unpack
    """ NLIP packet parser .
        
        data format is someting like this in pseudo c:
        unclear where the crc is?

           uint8_t start1; // = PKT_START1
           uint8_t start2; // = PKT_START2
           nlip_base64_encoded_pkt_data {
                uint16_t len; // total size on packet (incl crc?)
                char payload[X]; //  N > 0 && N < (MAX_DATA_PER_LINE -2)?
           };
           uint8_t eol; // == '\n' LF

           // if data do not fit in above line, 
           // it will be split into chunks (zero or more) as follow:
           struct nlip_sub_data {
                uint8_t start1; // = DATA_START1
                uint8_t start2; // = DATA_START2
                char bas64_enoded_payload[X]; //  N > 0 && N < (MAX_DATA_PER_LINE -2)?
                uint8_t eol; // == '\n' LF
           } sub_data[N]; // N >= 0

           uint16_t crc; // crc of packet 
           uint8_t eol; // == '\n' LF
        }
    """
    MAX_DATA_PER_LINE = 120

    def __init__(self):
        self._buf = bytearray()
        self._nlip_data = bytearray()
        self._nlip_len = None  # not None when we have header. might be 0

    def reset(self):
        self._buf = bytearray()
        self._nlip_data = bytearray()
        self._nlip_len = None  # not None when we have header. might be 0

    def _try_consume_payload(self):
        msg = "nlip len {} in header and got {}".format(
            self._nlip_len, len(self._nlip_data)
        )
        logger.debug(msg)
        if self._nlip_len < len(self._nlip_data):
            return None

        payload = self._nlip_data
        self.reset()
        return payload

    def _parse_b64_pkt(self, b64data):

        assert self._nlip_len is None
        data = base64.b64decode(b64data)
        u16data = data[0:2]
        if len(u16data) < 2:
            logger.warning("missing nlip pkt len in '%s'", u16data.hex())
            return None

        (nlip_len,) = struct.unpack(">H", u16data)
        logger.debug("nlip_pkt_len=%d", nlip_len)
        self._nlip_len = nlip_len

        self._nlip_data.extend(data[2:])
        return self._try_consume_payload()

    def _parse_b64_sub(self, b64data):
        assert self._nlip_len is not None
        data = base64.b64decode(b64data)
        self._nlip_data.extend(data)

    def parse_line(self, line):
        # ignore empty line
        if not line:
            return None

        self._buf.extend(line)
        if len(self._buf) < 2:
            logger.debug("need more then 2 bytes")
            return None

        s1 = self._buf[0]
        s2 = self._buf[1]

        if s1 == NLIP_OP.PKT_START1 and s2 == NLIP_OP.PKT_START2:
            return self._parse_b64_pkt(self._buf[2:])

        if s1 == NLIP_OP.DATA_START1 and s2 == NLIP_OP.DATA_START2:
            return self._parse_b64_sub(self._buf[2:])

        # TODO is an error? ignore for now
        msg = "Ignoring unknown start sequence '{:02x} {:02x}'".format(s1, s2)
        logger.warning(msg)
        self.reset()
        # raise ValueError(msg)
        return None

    def _chunks(self, data, n):
        """Yield successive n-sized chunks of data"""
        for i in range(0, len(data), n):
            yield data[i : i + n]

    def _crc(self, data):
        """ crc on b64 decoded data """
        crc16 = crcmod.predefined.Crc("xmodem")
        crc16.update(data)
        return crc16.crcValue

    def _pack_pkt_line(self, data):
        line = struct.pack(">BB", NLIP_OP.PKT_START1, NLIP_OP.PKT_START2) + data + b"\n"
        return line

    def _pack_sub_line(self, data):
        line = (
            struct.pack(">BB", NLIP_OP.DATA_START1, NLIP_OP.DATA_START2) + data + b"\n"
        )
        return line

    def pack(self, data):
        crc = self._crc(data)

        totlen = len(data) + 2
        pktdata = struct.pack(">H", totlen) + data + struct.pack(">H", crc)
        b64data = base64.b64encode(pktdata)

        if len(b64data) < self.MAX_DATA_PER_LINE:
            return self._pack_pkt_line(b64data)

        n = int(0.75 * self.MAX_DATA_PER_LINE)
        chunks = self._chunks(b64data, n)

        ba = bytearray()
        for i, chunk in enumerate(chunks):
            if i == 0:
                # first line with pkt_seq
                line = self._pack_pkt_line(chunk)
            else:
                # remaining with sub pkt esc_seq
                line = self._pack_sub_line(chunk)
            ba.extend(line)

        return ba


class SMPClientNlip:

    MAX_DATA_PER_LINE = 120

    def __init__(
        self, device=None, baudrate=115200, timeout=10, read_cb=None, *args, **kwargs
    ):
        """ warning: if param read_cb is provided it will be called from reader thread. 
        safer to use blocking read with a timeout to consume incomming messages.
        """
        if device is None:
            raise ValueError("No device identifier. Need address or name")

        self._device = device
        self._baudrate = baudrate
        self._timeout = timeout
        # self._read_buf = bytearray()
        self._read_cb = read_cb
        self._ser = None
        self._read_msg_q = Queue()
        self._read_thread = None
        # self._clnt = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def _read_thread_worker(self, ser, msg_queue, read_cb):
        logger.debug("reader thread started")

        if msg_queue and read_cb:
            logger.warn("cant have both msg_queue and read_cb")

        nlip = NlipPkt()
        while ser.is_open:
            line = ser.readline()
            rxline = bytearray(line)
            logger.debug("RX: %s", rxline.hex())
            msg = None
            try:
                pkt = nlip.parse_line(rxline)
                if pkt is not None:
                    msg = smp.MgmtMsg.from_bytes(pkt)
            except KeyboardInterrupt as e:
                msg = e
                break
            except Exception as e:
                msg = e
            # should be None or some exception
            logger.debug("read item: %s", str(msg))
            if msg:
                if read_cb:
                    read_cb(msg)
                else:
                    msg_queue.put_nowait(msg)
                # parser.reset()

        logger.debug("reader thread stopped")

    def connect(self):
        self._ser = serial.Serial(
            self._device, baudrate=self._baudrate, timeout=self._timeout
        )
        self._read_thread = Thread(
            target=self._read_thread_worker,
            args=(self._ser, self._read_msg_q, self._read_cb),
            daemon=True,
        )
        self._read_thread.start()
        return 0

    def disconnect(self):
        logger.debug("closing serial port")
        self._ser.close()
        return 0

    def is_connected(self):
        return self._ser.is_open

    def write(self, data):
        """ nlip pack and write """
        if hasattr(data, "__bytes__"):
            data = bytes(data)

        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)

        nlip = NlipPkt()
        data = nlip.pack(data)
        logger.debug("TX: %s", data.hex())
        self._ser.write(data)
        self._ser.flush()

    def write_msg(self, msg):
        """ pack smp msg and write """
        self.write(msg.to_bytes())

    def read_msg(self, timeout=None):
        if self._read_cb:
            raise RuntimeError("blocking read not allowed when callback set")
        itm = self._read_msg_q.get(timeout=timeout)
        # raise error in main/caller thread instead of reader thread
        if isinstance(itm, Exception):
            raise itm
        return itm

