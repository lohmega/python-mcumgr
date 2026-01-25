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
import os

import utils
utils.use_repo_sources(True)

from mcumgr import nlip, smp
from mcumgr.smp_proxy import SmpProxyTransport
from mcumgr.mgmt_proxy_ble import MgmtGrpProxyBle


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


def main():
    parser = argparse.ArgumentParser(description="SMP BLE Proxy Test")
    parser.add_argument("--port",
                        default='/dev/serial/by-id/usb-ZEPHYR_SMP_Dongle_*-if02',
                        help="Serial port for proxy device, supports glob patterns (default: %(default)s)")
    parser.add_argument("--ble-name", required=True, help="BLE device name to scan for and connect to")
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

    # Connect to proxy device via NLIP
    with nlip.SMPClientNlip(device=port, baudrate=args.baudrate, timeout=args.timeout) as base_transport:
        logger.info("Connected to proxy device")

        # Create BLE proxy management interface
        # Note: We use the base transport directly (not wrapped) for BLE proxy control commands
        # because these commands are directed AT the proxy device itself, not forwarded through it
        ble_proxy = MgmtGrpProxyBle(base_transport)

        # Set scan filter to the target BLE device name
        logger.info(f"Setting scan filter to: {args.ble_name}")
        filter_rsp = ble_proxy.mh_scan_filter.mh_write({
            "index": 0,
            "name": args.ble_name
        })
        logger.debug(f"Scan filter response: {filter_rsp}")

        filter_rsp = ble_proxy.mh_scan_filter.mh_read()
        logger.debug(f"Scan filter read: {filter_rsp}")

        # Start BLE scan
        logger.info("Starting BLE scan...")
        scan_ctl_rsp = ble_proxy.mh_scan_ctl.mh_write({
            "enable": True
        })
        logger.info(f"Scan control response: {scan_ctl_rsp}")

        # Read scan results
        logger.info("Reading scan results...")
        scan_result_rsp = ble_proxy.mh_scan_result.mh_read()
        logger.info(f"Scan results: {scan_result_rsp}")

        # Extract device address from scan results
        if "devices" in scan_result_rsp and len(scan_result_rsp["devices"]) > 0:
            target_device = scan_result_rsp["devices"][0]
            target_addr = target_device.get("a")  # "a" is the address key
            logger.info(f"Found target device: {target_device}")
            logger.info(f"Target address: 0x{target_addr:x}")

            # Connect to the target device
            logger.info(f"Connecting to target device at 0x{target_addr:x}...")
            conn_rsp = ble_proxy.mh_conn_ctl.mh_write({
                "connect": True,
                "a": target_addr,
                "w": 5000  # 5 second timeout
            })
            logger.info(f"Connection response: {conn_rsp}")

            # Check connection status
            if conn_rsp.get("connected"):
                logger.info("Successfully connected to target device!")
                logger.info(f"Connection details: {conn_rsp}")
            else:
                logger.error("Failed to connect to target device")
                return 1

        else:
            logger.error(f"No devices found matching name '{args.ble_name}'")
            return 1

        # Stop scanning
        logger.info("Stopping BLE scan...")
        ble_proxy.mh_scan_ctl.mh_write({"enable": False})

        logger.info("Test completed successfully")
        return 0


if __name__ == "__main__":
    sys.exit(main())
