# mcumgr OS management group

from . import smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint


class MgmtGrpOs(MgmtGrpBase):
    """OS Management Group"""

    nh_group = smp.MGMT_GROUP_ID.OS

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_echo = MgmtGrpEndpoint(transport, self.nh_group, 0)
        self.mh_taskstats = MgmtGrpEndpoint(transport, self.nh_group, 2)
