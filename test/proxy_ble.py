#!/usr/bin/env python3
"""
Test script for SMP BLE Proxy functionality.

This script connects to a proxy/dongle device over serial (NLIP) and uses it
to scan for and connect to BLE devices.
"""

import argparse
import glob
import logging
import sys

import utils
from pprint import pprint
utils.use_repo_sources(True)

from mcumgr.transport_serial import SMPTransportSerial
from mcumgr.smp_proxy import SmpProxyTransport
from mcumgr.mgmt_proxy_ble import MgmtGrpProxyBle
from mcumgr.mgmt_image import MgmtGrpImage


logger = logging.getLogger(__name__)

def setup_logging(verbose_level):
    """Configure logging based on verbosity level"""
    if verbose_level <= 1:
        level = logging.WARNING
    elif verbose_level == 2:
        level = logging.INFO
    else:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(lineno)d: %(message)s",
        stream=sys.stderr
    )

def scan(base_transport, scan_filters=[], timeout=8):
    assert base_transport.is_connected()

    # Create BLE proxy management interface
    # Note: We use the base transport directly (not wrapped) for BLE proxy control commands
    # because these commands are directed AT the proxy device itself, not forwarded through it
    ble = MgmtGrpProxyBle(base_transport)

    # Ensure scanning is stopped before setting filters (prevents EBUSY error)
    ble.scan_stop()

    # Set scan filter to the target BLE device name
    ble.scan_filter_set(scan_filters)

    # Scan for the target device
    logger.info("Starting BLE scan...")

    def on_scan_result(result):
        return True  # Stop scanning

    results = ble.scan(result_cb=on_scan_result, timeout=timeout)

    # Extract device address from scan results
    if not results:
        logger.error("No devices found matching filters")
        return None

    return results

def proy_fwd_transport(base_transport, address):
    assert base_transport.is_connected()

    return SmpProxyTransport(base_transport, address=address, media="ble")


def main():
    parser = argparse.ArgumentParser(description="SMP BLE Proxy Test")
    parser.add_argument("--port",
                        default='/dev/serial/by-id/usb-ZEPHYR_SMP_Dongle_*-if02',
                        help="Serial port for proxy device, supports glob patterns (default: %(default)s)")
    parser.add_argument("--ble-name", 
                        default="sem-lb", 
                        help="BLE device name to scan for and connect to")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity")
    parser.add_argument("--baudrate", default="115200", help="Serial baudrate (default: 115200)")
    parser.add_argument("--timeout", type=int, default=10, help="Timeout in seconds (default: 10)")

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Resolve glob pattern for port
    port_pattern = args.port
    matched_ports = glob.glob(port_pattern)

    if not matched_ports:
        logger.error(f"No serial port found matching pattern: {port_pattern}")
        return 1

    if len(matched_ports) > 1:
        logger.warning(f"Multiple ports match pattern '{port_pattern}': {matched_ports}")
        logger.info(f"Using first match: {matched_ports[0]}")

    port = matched_ports[0]
    logger.info(f"Connecting to proxy device on {port}")
    # transport to communicate with the proxy itself
    scan_filters = { "index": 0, "name": args.ble_name }

    base_tp = SMPTransportSerial(port=port, baudrate=args.baudrate, timeout=args.timeout)
    with base_tp:
        res = scan(base_tp, scan_filters, timeout=args.timeout)

        print("scan result:")
        pprint(res)
        if not res:
            return
        address=res[0].get("address", None)

        fwd_tp = SmpProxyTransport(base_tp, address=address, media="ble")
        with fwd_tp:
            img = MgmtGrpImage(fwd_tp)
            pprint(img.get_state())




if __name__ == "__main__":
    sys.exit(main())
