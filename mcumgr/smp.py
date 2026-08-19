# mcumgr SMP (Simple Management Protocol) (previosly or based on NMP)
# see https://github.com/apache/mynewt-mcumgr  for details.
#   mynewt-mcumgr/protocol.md
#   mynewt-mcumgrmgmt/inlcude/mgmt.h

from enum import Enum, IntEnum
import struct
import cbor2 as cbor
import logging

logger = logging.getLogger(__name__)
# MTU for newtmgr responses
MGMT_MAX_MTU = 1024


def _enum2str(enumclass, val):
    """
    enumclass - a Enum class, either instance or class
    """
    try:
        return enumclass(val).name
    except ValueError:
        return "{}.<unknown {}>".format(enumclass.__name__, val)


# Exception Classes
class SMPError(Exception):
    """Base exception for all SMP-related errors"""
    pass


class SMPTransportError(SMPError):
    """Transport-level errors (connection, communication failures)"""
    pass


class SMPDisconnectedError(SMPTransportError):
    """The link went away.

    Distinct from a plain timeout: a timeout is worth retrying on the same
    connection, a dead link is not - retrying just burns the retry budget
    against a transport that cannot recover without reconnecting.
    """


class MgmtEndpointError(SMPError):
    """Management endpoint command errors with rc codes"""

    def __init__(self, message, rc=None, rsn=None):
        """
        Initialize management endpoint error.

        Args:
            message: Error message
            rc: Error code from response (optional)
            rsn: Reason string from response (optional)
        """
        super().__init__(message)
        self.rc = rc
        self.rsn = rsn
        self.error_name = None

        # Get symbolic name for error code if available
        if rc is not None:
            # Import locally to avoid circular dependency
            # MGMT_ERR will be defined later in this file
            try:
                self.error_name = MGMT_ERR.int_to_str(rc)
            except:
                self.error_name = f"UNKNOWN({rc})"

    def __str__(self):
        """Format error with all available context"""
        parts = [super().__str__()]

        if self.rc is not None:
            if self.error_name:
                parts.append(f"rc={self.rc} ({self.error_name})")
            else:
                parts.append(f"rc={self.rc}")

        if self.rsn:
            parts.append(f"reason: {self.rsn}")

        return " | ".join(parts)


class SeqCounter:
    """Sequence number generator shared by every endpoint on one transport.

    `nh_seq` is a single uint8 field in the SMP header and the device echoes it
    back verbatim, so it is what lets a caller match a response to its request.
    Giving each management endpoint its own counter (as an earlier version did)
    means two endpoints on the same connection both start at 0 and hand out
    colliding sequence numbers. libmcumgr threads one `uint8_t *seq` through a
    session for the same reason - keep exactly one of these per transport.
    """

    def __init__(self, start=0):
        self._seq = start & 0xFF

    def next(self):
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq


class MGMT_OP(IntEnum):
    """ Opcodes; encoded in first byte of header. """

    # fmt: off
    READ          = 0
    READ_RSP      = 1
    WRITE         = 2
    WRITE_RSP     = 3
    # fmt: on

    @staticmethod
    def int_to_str(val):
        """Convert opcode integer to string name"""
        return _enum2str(MGMT_OP, val)


class MGMT_GROUP_ID(IntEnum):
    """ The first 64 groups are reserved for system level mcumgr commands.
     Per-user commands are then defined after group 64.
    """

    # fmt: off
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
    # fmt: on

    @staticmethod
    def int_to_str(val):
        """Convert group ID integer to string name"""
        return _enum2str(MGMT_GROUP_ID, val)


class MGMT_ERR(IntEnum):
    """ mcumgr error codes """

    # fmt: off
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
    EBUSY        = 10      #/* Resource busy. */
    EACCESSDENIED = 11     #/* Access denied. */
    EPERUSER     = 256
    # fmt: on

    @staticmethod
    def int_to_str(val):
        """Convert error code integer to string name"""
        return _enum2str(MGMT_ERR, val)


class MGMT_EVT_OP(IntEnum):
    """ MGMT event opcodes."""

    # fmt: off
    CMD_RECV         =  0x01
    CMD_STATUS       =  0x02
    CMD_DONE         =  0x03
    # fmt: on

    @staticmethod
    def int_to_str(val):
        """Convert event opcode integer to string name"""
        return _enum2str(MGMT_EVT_OP, val)


class Mynewt:
    class OS_MGMT_ID(IntEnum):
        """ Command IDs for Mynewt OS management group. """

        # fmt: off
        ECHO           = 0
        CONS_ECHO_CTRL = 1
        TASKSTAT       = 2
        MPSTAT         = 3
        DATETIME_STR   = 4
        RESET          = 5
        # fmt: on

        @staticmethod
        def int_to_str(val):
            """Convert OS management ID integer to string name"""
            return _enum2str(Mynewt.OS_MGMT_ID, val)

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

    def __init__(self, nh_op=0, nh_flags=0, nh_len=0, nh_group=0, nh_seq=0, nh_id=0):
        # fmt: off
        self.nh_op    = nh_op & 0x03
        self.nh_flags = nh_flags
        self.nh_len   = nh_len
        self.nh_group = nh_group
        self.nh_seq   = nh_seq
        self.nh_id    = nh_id
        # fmt: on


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
    """
    MgmtMsg base class that only operates on bytes payload
    """
    def __init__(self, hdr=None, payload=None, **kwargs):
        # Both defaults must be built per instance. They used to be
        # `hdr=MgmtHdr(), payload=bytearray()`, which Python evaluates once at
        # definition time, so every MgmtMsg built without an explicit header
        # shared ONE MgmtHdr: setting nh_seq on a new message silently
        # rewrote the header of every other message still being held.
        self.hdr = hdr if hdr is not None else MgmtHdr()
        self.payload = None
        # note that nh_len excluded here
        for nh in ["nh_op", "nh_flags", "nh_group", "nh_seq", "nh_id"]:
            if nh in kwargs:
                setattr(self.hdr, nh, kwargs.get(nh))

        self.set_payload(payload)

    @property
    def size(self):
        hdr_size = MgmtHdr.BYTE_SIZE if self.hdr else 0
        payload_size = len(self.payload) if self.payload else 0
        return hdr_size + payload_size

    def encode_payload(self, data_dict):
        data = cbor.dumps(data_dict)
        self.set_payload(data)

    def decode_payload(self):
        return cbor.loads(self.payload)

    def set_payload(self, obj):
        if obj is None:
            self.payload = bytearray()
        elif isinstance(obj, (bytes, bytearray)):
            self.payload = obj
        elif isinstance(obj, str):
            self.payload = obj.encode()
        elif isinstance(obj, (list, tuple)):
            self.payload = bytearray(obj)
        else:
            raise ValueError("Invalid payload type")
        self.hdr.nh_len = len(self.payload)

    def to_bytes(self):
        return self.hdr.to_bytes() + self.payload

    @classmethod
    def from_bytes(cls, data):
        hdr_size = MgmtHdr.BYTE_SIZE
        if len(data) < hdr_size:
            raise IndexError("Size is less than header")

        hdr = MgmtHdr.from_bytes(data[0:hdr_size])
        if (len(data) - hdr_size) < hdr.nh_len:
            raise IndexError("Size is less than header nh_len")

        payload = data[hdr_size : hdr_size + hdr.nh_len]
        return MgmtMsg(hdr, payload)

