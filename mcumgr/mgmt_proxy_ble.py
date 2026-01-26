# mcumgr BLE Proxy management group
import logging
import time
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

def _rename_key(data, from_key, to_key):

    if from_key in data:
        data[to_key] = data.pop(from_key)

    return data

def _rename_keys(datas, from_key, to_key):
    for data in datas:
        _rename_key(data, from_key, to_key)
    return datas

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

    def scan_filter_set(self, filters):
        if filters is None:
            return
        if isinstance(filters, (list, tuple)):
            pass
        else:
            filters = [filters]

        for spec in filters:
            assert "index" in spec
            assert "name" in spec or "a" in spec
            self.mh_scan_filter.mh_write(spec, check=True)

    def scan_filter_get(self):
        rsp = self.mh_scan_filter.mh_read()
        return rsp.get("filters", None)

    def scan_filter_clear(self, index=-1):
        """
        Clear scan filter(s).

        Args:
            index: Filter index to clear, or -1 to clear all filters (default: -1)

        Returns:
            dict: Response from clear operation

        Raises:
            MgmtEndpointError: If clear fails (e.g., EBUSY if scanning is active)
        """
        return self.mh_scan_filter.mh_write({"index": index}, check=True)

    def _scan_result_poll(self, result_cb=None, timeout=None, poll_interval=0.5):
        start_time = time.time()
        # Poll for results until timeout or callback requests stop
        while True:
            if timeout:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.debug("scan poll timeout")
                    return None
            # Get current scan results
            results = self.scan_result()
            results = _rename_keys(results, 'a', 'address')
            if result_cb:
                for device in results:
                    # Call callback if provided
                     if result_cb(device):
                        return results
            elif results:
                return results

            # Wait before next poll
            time.sleep(poll_interval)

    def scan(self, result_cb=None, timeout=None, poll_interval=0.5):
        """
        Perform BLE scan with optional filtering.

        Args:
            filters: List of filter dicts with keys:
                    - "index": Filter slot index (0-N)
                    - "name": Device name to filter (optional)
                    - "a": Device address to filter (optional)
                    - "name_exact": Exact name match (optional, bool)
                    - "name_icase": Case-insensitive match (optional, bool)
            result_cb: Optional callback function(device) -> bool
                      Called for each discovered device. Return True to stop scanning.
            timeout: Maximum scan duration in seconds (default: 5.0)
            poll_interval: Interval between result polls in seconds (default: 0.5)

        Returns:
            list: List of all discovered devices (if result_cb is None)
                  Each device is a dict with keys like: "a" (address), "name", "rssi", etc.

        Example:
            # Scan for devices matching a name
            devices = ble_proxy.scan(filters=[{"index": 0, "name": "MyDevice"}])

            # Scan with callback, stop when target found
            def on_device(dev):
                print(f"Found: {dev.get('name')} at 0x{dev.get('a'):x}")
                if dev.get('name') == 'TargetDevice':
                    return True  # Stop scanning
                return False  # Continue scanning

            ble_proxy.scan(result_cb=on_device, timeout=10.0)
        """

        # Start scanning
        self.scan_start()
        logger.info(f"Started BLE scan for up to {timeout}s")

        res = None
        try:
            res = self._scan_result_poll(result_cb, timeout, poll_interval)
        finally:
            # Always stop scanning
            self.scan_stop()

        return res

    def connect(self, address, wait=None):
        """
        Connect to a BLE device.

        Args:
            address: Device BLE address (uint64)
            wait: Optional timeout in milliseconds to wait for connection

        Returns:
            dict: Connection response with status information

        Raises:
            RuntimeError: If connection fails
        """
        req = {
            "connect": True,
            "a": address,
        }

        if wait:
            req["w"] = wait

        return self.mh_conn_ctl.mh_write(req, check=True)

    def disconnect(self):
        """
        Disconnect from the current BLE device.

        Returns:
            dict: Disconnect response

        Raises:
            RuntimeError: If disconnect fails
        """
        req = {
            "connect": False,
        }

        return self.mh_conn_ctl.mh_write(req, check=False)

