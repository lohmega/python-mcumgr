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


    def scan_start(self):
        # check=True: if the proxy rejects this (EBUSY, ENOTSUP, ...) the
        # caller needs to know now, not after scan()'s full timeout has
        # elapsed polling for results that were never going to arrive
        # because scanning was never actually started.
        return self.mh_scan_ctl.mh_write({
            "enable": True
        }, check=True)

    def scan_stop(self):
        return self.mh_scan_ctl.mh_write({
            "enable": False
        })

    def scan_result(self, timeout=None):
        rsp = self.mh_scan_result.mh_read(timeout=timeout)
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

    def _scan_result_poll(self, result_cb=None, timeout=5.0, poll_interval=0.5):
        start_time = time.time()
        # Poll for results until timeout or the callback requests an early
        # stop. Without a callback there is no "stop" signal at all - every
        # candidate is kept, so the old `if ret: return ret` returned after
        # the very first candidate ever seen, contradicting the documented
        # "list of all discovered devices" contract by only ever reporting
        # whatever happened to be visible in the first poll cycle.
        ret = []
        seen_addrs = set()
        while True:
            remaining = None
            if timeout:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    logger.debug("scan poll timeout")
                    return ret

            # Get current scan results. Bounded by whatever is left of the
            # overall scan timeout - mh_read()'s own default otherwise falls
            # back to the transport's unrelated default timeout, which could
            # make one poll block far longer than the scan timeout the
            # caller actually asked for (e.g. a 0.2s scan blocking for a
            # 10s transport default while the deadline check above only
            # runs between polls, not during one).
            candidates = self.scan_result(timeout=remaining)
            candidates = _rename_keys(candidates, 'a', 'address')

            for candidate in candidates:
                keep = True
                if result_cb:
                    keep = result_cb(candidate)

                if not keep:
                    continue

                # scan_result() commonly reports the same device again on a
                # later poll (a cumulative results buffer, not a
                # consume-once one) - without a callback there is nothing
                # else filtering repeats out, so the same address could
                # otherwise be appended on every single poll for the whole
                # timeout window. The callback path does not need this: it
                # already returns as soon as its first kept candidate
                # arrives (see below), so duplicates across separate polls
                # can never accumulate there regardless.
                addr = candidate.get("address")
                if addr is not None:
                    if addr in seen_addrs:
                        continue
                    seen_addrs.add(addr)

                ret.append(candidate)

            if result_cb and ret:
                # A callback signaled "stop scanning" (kept a candidate).
                # With no callback, keep polling for the full timeout.
                return ret

            # Wait before next poll
            time.sleep(poll_interval)

    def scan(self, result_cb=None, timeout=5.0, poll_interval=0.5):
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
            timeout: Maximum scan duration in seconds, or None to scan
                     forever until result_cb finds something (default: 5.0)
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
        Connect the proxy to a BLE device.
        """

        req = {
            "connect": True,
            "a": address,
        }

        # req["w"] tells the proxy device itself how long (ms) to keep
        # trying the connection - the local read must be willing to wait at
        # least that long too, or we give up and raise a local timeout
        # while the proxy is still legitimately attempting a connection
        # that (from its perspective) hasn't failed yet. A few seconds of
        # margin on top for the proxy's own round-trip/processing overhead.
        local_timeout = None
        if wait:
            req["w"] = wait
            local_timeout = wait / 1000.0 + 2.0

        logger.info(f"Connecting to at 0x{address:x}...")
        rsp = self.mh_conn_ctl.mh_write(req, check=True, timeout=local_timeout)
        if not rsp.get("connected", None):
            raise smp.SMPTransportError("Failed to connect {}".format(rsp))
        return rsp


    def disconnect(self):
        req = {
            "connect": False,
        }

        return self.mh_conn_ctl.mh_write(req, check=False)
