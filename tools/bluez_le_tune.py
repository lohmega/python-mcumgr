#!/usr/bin/env python3
"""Tune a BlueZ adapter so SMP-over-BLE survives Zephyr peripherals.

Two independent fixes, both applied through the kernel mgmt socket (works
even with SecureBoot lockdown, which blocks the debugfs knobs under
/sys/kernel/debug/bluetooth/):

1. Deselect the LE 2M and LE Coded PHYs, so the adapter never proposes a
   PHY update away from 1M. Intel adapters (e.g. AX201) upgrade to 2M
   right after MTU exchange; some peripherals accept the update and then
   go completely silent until the supervision timeout kills the link
   ("failed to discover services, device disconnected" from bleak, btmon
   shows Disconnect Reason: Connection Timeout (0x08) right after
   "LE PHY Update Complete: LE 2M"). Seen on Lohmega protractor_r2
   (lohmega-zephyr#206); nRF52-dongle centrals never request 2M, which is
   why proxied paths worked while the builtin adapter failed.

2. Load per-device connection parameters with a generous supervision
   timeout (default 8 s). BlueZ's default is 420 ms, which a peripheral
   that stalls its BLE stack around slow bus work (radio SPI, e-ink
   refresh, flash erase) can miss even at 1M.

Both settings are runtime-only: they do not survive an adapter power
cycle or bluetoothd restart, so re-run after reboot.

Usage (root):

    sudo ./bluez_le_tune.py                          # PHY fix only
    sudo ./bluez_le_tune.py C0:01:F0:00:8B:52 ...    # + conn params
    sudo ./bluez_le_tune.py --index 1 --timeout-ms 8000 --public ADDR

Verify with `btmgmt phy` (Selected phys must not list LE2M/LECODED) and
`btmon` during a connect (Supervision timeout in LE Connection Complete).
"""

import argparse
import ctypes
import os
import struct
import sys

AF_BLUETOOTH = 31
SOCK_RAW = 3
SOCK_CLOEXEC = 0o2000000
BTPROTO_HCI = 1
HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3

MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_OP_LOAD_CONN_PARAM = 0x0035
MGMT_OP_GET_PHY = 0x0044
MGMT_OP_SET_PHY = 0x0045

# PHY bits per mgmt-api.txt (Get/Set PHY Configuration)
PHY_LE_2M_TX = 1 << 11
PHY_LE_2M_RX = 1 << 12
PHY_LE_CODED_TX = 1 << 13
PHY_LE_CODED_RX = 1 << 14
PHY_DROP = PHY_LE_2M_TX | PHY_LE_2M_RX | PHY_LE_CODED_TX | PHY_LE_CODED_RX

# mgmt address types
ADDR_LE_PUBLIC = 1
ADDR_LE_RANDOM = 2


def mgmt_open():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI)
    if fd < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    sa = struct.pack("<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
    if libc.bind(fd, sa, len(sa)) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return fd


def mgmt_cmd(fd, opcode, index, payload=b""):
    """Send one mgmt command, return (status, response payload)."""
    os.write(fd, struct.pack("<HHH", opcode, index, len(payload)) + payload)
    while True:
        resp = os.read(fd, 1024)
        ev, _idx, plen = struct.unpack("<HHH", resp[:6])
        body = resp[6 : 6 + plen]
        if ev in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS):
            op, status = struct.unpack("<HB", body[:3])
            if op == opcode:
                return status, body[3:]
        # anything else is an unsolicited event - keep reading


def bdaddr(addr):
    b = bytes(int(x, 16) for x in reversed(addr.split(":")))
    if len(b) != 6:
        raise ValueError("bad BD address: %s" % addr)
    return b


def fix_phys(fd, index):
    status, body = mgmt_cmd(fd, MGMT_OP_GET_PHY, index)
    if status != 0:
        raise RuntimeError("Get PHY Configuration failed, status %d" % status)
    supported, configurable, selected = struct.unpack("<III", body[:12])
    wanted = selected & ~PHY_DROP
    if wanted == selected:
        print("phys: LE 2M/Coded already deselected (0x%04x)" % selected)
        return
    status, _ = mgmt_cmd(fd, MGMT_OP_SET_PHY, index, struct.pack("<I", wanted))
    if status != 0:
        raise RuntimeError("Set PHY Configuration failed, status %d" % status)
    print("phys: 0x%04x -> 0x%04x (dropped LE 2M/Coded)" % (selected, wanted))


def load_conn_params(fd, index, addrs, addr_type, timeout_ms):
    # struct mgmt_conn_param: bdaddr(6) type(1) min(2) max(2) latency(2) timeout(2)
    # intervals in 1.25 ms units (24..40 = 30..50 ms), timeout in 10 ms units
    timeout = timeout_ms // 10
    params = b"".join(
        bdaddr(a) + bytes([addr_type]) + struct.pack("<HHHH", 24, 40, 0, timeout)
        for a in addrs
    )
    payload = struct.pack("<H", len(addrs)) + params
    status, _ = mgmt_cmd(fd, MGMT_OP_LOAD_CONN_PARAM, index, payload)
    if status != 0:
        raise RuntimeError("Load Connection Parameters failed, status %d" % status)
    print(
        "conn params: %d device(s), interval 30-50 ms, supervision %d ms"
        % (len(addrs), timeout * 10)
    )


def main():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], epilog="Run as root."
    )
    p.add_argument("addrs", nargs="*", help="peer BD addresses for conn params")
    p.add_argument("--index", type=int, default=0, help="adapter index (hci0=0)")
    p.add_argument(
        "--timeout-ms", type=int, default=8000, help="supervision timeout (ms)"
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="peers use public addresses (default: LE random/static)",
    )
    args = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("must run as root (mgmt socket)")

    fd = mgmt_open()
    fix_phys(fd, args.index)
    if args.addrs:
        load_conn_params(
            fd,
            args.index,
            args.addrs,
            ADDR_LE_PUBLIC if args.public else ADDR_LE_RANDOM,
            args.timeout_ms,
        )
    os.close(fd)


if __name__ == "__main__":
    main()
