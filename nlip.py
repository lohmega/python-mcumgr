from queue import Queue
from threading import Thread
from enum import IntEnum
import logging
import struct
import base64
import crcmod.predefined
import serial
import smp

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
    comment from:
    https://github.com/apache/mynewt-core/blob/master/sys/shell/src/shell_nlip.c       

    /* NLIP packets sent over serial are fragmented into frames of 127 bytes or
     * fewer. This 127-byte maximum applies to the entire frame, including header,
     * CRC, and terminating newline.
     */
    Note data format is _not_ the same on both sides! 16-bit CRC is first in recived (from Mynewt MCU)
    but expected to be last in sent (to Mynewt MCU) data.

    Common for both sides (in pseudo C code):
    '''
         putc(PKT_START1);
         putc(PKT_START2);
        
         BASE64_ENCODE_BEGIN();

         put_u16be(tot_pkt_size); // 16-bit big endian
         // i.e. if input to this python parser/unpacker/decoder
         if (MYNEWT_MCU_TX) 
            put_u16be(crc); // 16-bit crc of packet 
         BASE64_ENCODE_END();
        
         putc('\n'); // LF

         // if data do not fit in first line packet
         // it will be split into chunks (zero or more) as follow:
         while (chunk; chunk = get_next_chunk()) {
               putc(DATA_START1);
               putc(DATA_START2);

               BASE64_ENCODE_BEGIN();

               write(chunk);

               // i.e. if output from this python packer/encoder
               if (MYNEWT_MCU_RX && is_last_chunk())
                  put_u16be(crc); // 16-bit crc of packet 

               BASE64_ENCODE_END();

               putc('\n'); // LF
         }   
    ```
    """
    # max line length total including LF
    MAX_DATA_PER_LINE = 120

    def __init__(self):
        self._nlip_data = bytearray()
        self._nlip_len = None  # not None when we have header. might be 0

    def reset(self):
        self._nlip_data = bytearray()
        self._nlip_len = None  # not None when we have header. might be 0

    def _try_consume_payload(self):
        msg = "nlip len {} in header and got {}".format(
            self._nlip_len, len(self._nlip_data)
        )
        logger.debug(msg)
        if self._nlip_len > len(self._nlip_data):
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
        return self._try_consume_payload()

    def parse_line(self, line):
        # ignore empty line
        if not line:
            return None

        buf = bytearray()
        buf.extend(line)
        if len(buf) < 2:
            logger.debug("need more than 2 bytes")
            return None

        s1 = buf[0]
        s2 = buf[1]

        if s1 == NLIP_OP.PKT_START1 and s2 == NLIP_OP.PKT_START2:
            return self._parse_b64_pkt(buf[2:])

        if s1 == NLIP_OP.DATA_START1 and s2 == NLIP_OP.DATA_START2:
            return self._parse_b64_sub(buf[2:])

        # not an error as nlip packets mixed with other types of data.
        logger.debug("Ignoring unknown start sequence '%02x %02x'", s1, s2)
        self.reset()
        return None

    def _chunks(self, data, n):
        """Yield successive n-sized chunks of data"""
        for i in range(0, len(data), n):
            yield data[i : i + n]

    def _crc(self, data):
        """ aka. crc16_ccitt. crc on b64 decoded data """
        crc16 = crcmod.predefined.Crc("xmodem")
        crc16.update(data)
        return crc16.crcValue

    def _pack_b64_pkt(self, data):
        data = base64.b64encode(data)
        line = struct.pack(">BB", NLIP_OP.PKT_START1, NLIP_OP.PKT_START2) + data + b"\n"
        return line

    def _pack_b64_sub(self, data):
        data = base64.b64encode(data)
        line = (
            struct.pack(">BB", NLIP_OP.DATA_START1, NLIP_OP.DATA_START2) + data + b"\n"
        )
        return line

    def pack_lines(self, data):
        """ return list of lines. use `pack` to get a single bytes object """
        crc = self._crc(data)
        uint16be = lambda val: struct.pack(">H", val)

        payloadlen = len(data) + 2  # excluding this 16bit nlip_len

        # expects crc last. mynewt C code:
        # `os_mbuf_adj(g_nlip_mbuf, -sizeof(crc))`;
        # `os_mbuf_adj` doc says the following about second parameter:
        #  "[...]If positive, trims from the head of the mbuf, if negative,
        #  trims from the tail of the mbuf."
        pktdata = bytes().join([uint16be(payloadlen), data, uint16be(crc)])

        # b64_encoded_strlen = 4*N/3 = 0.75 * N, where N is number of bytes
        # also subtract for:
        #  - newline/LF  (1 byte)
        #  - start symbols (2 bytes)
        #  - 2 uint16 in header (4 bytes)
        #  - one extra in case RX-end is off by one and base64 padding
        chunk_size = int(0.75 * (self.MAX_DATA_PER_LINE - 8))
        chunks = self._chunks(pktdata, chunk_size)  # an iterator

        lines = []
        # first line with pkt_seq
        chunk = next(chunks)  # pop/dequeu first chunk
        line = self._pack_b64_pkt(chunk)
        lines.append(line)

        # remaining line with sub pkt (aka `esc_seq` or `DATA`)
        # for i, chunk in enumerate(chunks):
        for chunk in chunks:
            line = self._pack_b64_sub(chunk)
            lines.append(line)

        if 1:
            for line in lines:
                assert len(line) < self.MAX_DATA_PER_LINE

        return lines

    def pack(self, data):
        lines = self.pack_lines(data)
        return bytes().join(lines)


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
        self._read_cb = read_cb
        self._ser = None
        self._read_msg_q = Queue()
        self._read_thread = None

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
        lines = nlip.pack_lines(data)
        logger.debug("TX: %s", str(lines))
        data = bytes().join(lines)
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

