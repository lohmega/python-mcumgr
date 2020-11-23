
# mcumgr SMP (Simple Management Protocol) (previosly or based on NMP)
# see https://github.com/apache/mynewt-mcumgr  for details.
#   mynewt-mcumgr/protocol.md 
#   mynewt-mcumgrmgmt/inlcude/mgmt.h

from enum import Enum, IntEnum
import struct
import logging

logger = logging.getLogger(__name__)
# MTU for newtmgr responses 
MGMT_MAX_MTU = 1024

class MGMT_OP(IntEnum):
    """ Opcodes; encoded in first byte of header. """
    READ          = 0
    READ_RSP      = 1
    WRITE         = 2
    WRITE_RSP     = 3

class MGMT_GROUP_ID(IntEnum):
    """ The first 64 groups are reserved for system level mcumgr commands.
     Per-user commands are then defined after group 64.
    """
    OS      = 0
    IMAGE   = 1
    STAT    = 2
    CONFIG  = 3
    LOG     = 4
    CRASH   = 5
    SPLIT   = 6
    RUN     = 7
    FS      = 8
    SHELL   = 9
    PERUSER = 64


class MGMT_ERR(IntEnum):
    """ mcumgr error codes """

    EOK          = 0
    EUNKNOWN     = 1
    ENOMEM       = 2
    EINVAL       = 3
    ETIMEOUT     = 4
    ENOENT       = 5
    EBADSTATE    = 6       #/* Current state disallows command. */
    EMSGSIZE     = 7       #/* Response too large. */
    ENOTSUP      = 8       #/* Command not supported. */
    ECORRUPT     = 9       #/* Corrupt */
    EPERUSER     = 256




class MGMT_EVT_OP(IntEnum):
    """ MGMT event opcodes."""
    CMD_RECV         =  0x01
    CMD_STATUS       =  0x02
    CMD_DONE         =  0x03

class Mynewt:
    class OS_MGMT_ID(IntEnum):
        """ Command IDs for Mynewt OS management group. """
        ECHO           = 0
        CONS_ECHO_CTRL = 1
        TASKSTAT       = 2
        MPSTAT         = 3
        DATETIME_STR   = 4
        RESET          = 5

    """
    #define OS_MGMT_TASK_NAME_LEN       32

    struct os_mgmt_task_info {
        uint8_t oti_prio;
        uint8_t oti_taskid;
        uint8_t oti_state;
        uint16_t oti_stkusage;
        uint16_t oti_stksize;
        uint32_t oti_cswcnt;
        uint32_t oti_runtime;
        uint32_t oti_last_checkin;
        uint32_t oti_next_checkin;

        char oti_name[OS_MGMT_TASK_NAME_LEN];
    };
    """

class MgmtHdr:
    """
    struct mgmt_hdr {
        uint8_t  nh_op;             /* MGMT_OP_[...] */
        uint8_t  nh_flags;          /* Reserved for future flags */
        uint16_t nh_len;            /* Length of the payload */
        uint16_t nh_group;          /* MGMT_GROUP_ID_[...] */
        uint8_t  nh_seq;            /* Sequence number */
        uint8_t  nh_id;             /* Message ID within group */
    };
    """

    BYTE_SIZE = 8

    @property
    def size(self):
        """ only instances have size """
        return 8

    def __init__(self,
            nh_op=0,
            nh_flags=0,
            nh_len=0,
            nh_group=0,
            nh_seq=0,
            nh_id=0
        ):

        self.nh_op = nh_op & 0x03
        self.nh_flags = nh_flags
        self.nh_len = nh_len
        self.nh_group = nh_group
        self.nh_seq = nh_seq
        self.nh_id = nh_id

    # B = uint8, H = uint16, > = big endian
    _STRUCT_FMT = ">BBHHBB"

    def __bytes__(self):
        return self.to_bytes()

    def to_bytes(self):
        data = struct.pack(
            self._STRUCT_FMT,
            self.nh_op,
            self.nh_flags,
            self.nh_len,
            self.nh_group,
            self.nh_seq,
            self.nh_id,
        )
        return data

    @classmethod
    def from_bytes(cls, data):
        r = struct.unpack(cls._STRUCT_FMT, data)
        return MgmtHdr(*r)


class MgmtMsg:
    def __init__(self, hdr=MgmtHdr(), payload=bytearray()):
        self.hdr = hdr
        self.payload = payload

    @property
    def size(self):
        hdr_size = MgmtHdr.BYTE_SIZE if self.hdr else 0
        payload_size =  len(self.payload) if self.payload else 0
        return hdr_size + payload_size

    def to_bytes(self):
        hdr = self.hdr.to_bytes()

        # this should probably be in a setter
        if isinstance(self.payload, (bytes, bytearray)):
            payload = self.payload
        elif isinstance(self.payload, str):
            payload = self.payload.encode()
        elif isinstance(self.payload, (list, tuple)):
            payload = bytearray(self.payload)
        else:
            raise ValueError("Invalid payload type")
        return hdr + payload

    @classmethod
    def from_bytes(cls, data):
        hdr_size = MgmtHdr.BYTE_SIZE
        if len(data) < hdr_size:
            raise IndexError("Size is less then header")

        hdr = MgmtHdr.from_bytes(data[0:hdr_size])
        if (len(data) - hdr_size) < hdr.nh_len:
            raise IndexError("Size is less then header nh_len")

        payload = data[hdr_size: hdr_size+hdr.nh_len]
        return MgmtMsg(hdr, payload)


