# mcumgr BLE Proxy management group
import logging
from . import smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint

logger = logging.getLogger(__name__)

# Group ID for BLE proxy
MGMT_GROUP_ID_SMP_PROXY_BLE = 254

# Command IDs for BLE proxy management group
SMP_PROXY_ID_BLE_STATUS = 0
SMP_PROXY_ID_BLE_CONN_CTL = 1
SMP_PROXY_ID_BLE_SCAN_RESULT = 2
SMP_PROXY_ID_BLE_SCAN_CTL = 3
SMP_PROXY_ID_BLE_SCAN_FILTER = 4
SMP_PROXY_ID_BLE_DISCONNECT = 5


class MgmtGrpProxyBle(MgmtGrpBase):
    """BLE Proxy Management Group"""

    nh_group = MGMT_GROUP_ID_SMP_PROXY_BLE

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_status = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_STATUS)
        self.mh_conn_ctl = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_CONN_CTL)
        self.mh_scan_result = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_SCAN_RESULT)
        self.mh_scan_ctl = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_SCAN_CTL)
        self.mh_scan_filter = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_SCAN_FILTER)
        self.mh_disconnect = MgmtGrpEndpoint(transport, self.nh_group, SMP_PROXY_ID_BLE_DISCONNECT)

    def scan_start(self):
        return self.mh_scan_ctl.mh_write({
            "enable": True
        })

    def scan_stop(self):
        return self.mh_scan_ctl.mh_write({
            "enable": False
        })

    def scan_result(self):

        rsp = self.mh_scan_result.mh_read()
        return rsp.get("results", [])
