# mcumgr OS management group

from enum import Enum, IntEnum
from . import smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint

class OS_MGMT_ID(IntEnum):
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
        try:
            return enumclass(val).name
        except ValueError:
            return "{}.<unknown {}>".format(enumclass.__name__, val)

class MgmtGrpOs(MgmtGrpBase):
    """OS Management Group"""

    nh_group = smp.MGMT_GROUP_ID.OS

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_echo = MgmtGrpEndpoint(transport, self.nh_group, 0)
        self.mh_taskstats = MgmtGrpEndpoint(transport, self.nh_group, 2)
