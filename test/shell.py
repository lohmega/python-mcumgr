import logging
import sys
import os

import base64
import cbor2 as cbor
import utils
utils.use_repo_sources(True)

from mcumgr import smp
from mcumgr import transport_serial, transport_ble
from mcumgr.transport_serial import SMPTransportSerial
from mcumgr.transport_ble import SMPTransportBLE


def set_verbose(verbose_level):
    loggers = [transport_ble.logger, smp.logger, transport_serial.logger]

    if verbose_level <= 1:
        level = logging.WARNING
    elif verbose_level == 2:
        level = logging.INFO
    elif verbose_level >= 3:
        level = logging.DEBUG
    else:
        level = logging.WARNING

    if verbose_level >= 4:
        bleak_logger = logging.getLogger("bleak")
        loggers.append(bleak_logger)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    formatter = logging.Formatter("%(levelname)s:%(name)s:%(lineno)d: %(message)s")
    handler.setFormatter(formatter)

    for l in loggers:
        l.setLevel(level)
        l.addHandler(handler)


g_nh_seq = 66

def do_cmd_shell(transport_client, line):
    global g_nh_seq
    # must be str or cbor dumps encode diffrently 
    # (mcu side assumes UTF-8 encoded probably)
    if not isinstance(line, str):
        raise TypeError("must be str")

    req = smp.MgmtMsg()
    req.hdr.nh_op = smp.MGMT_OP.WRITE
    req.hdr.nh_group = smp.MGMT_GROUP_ID.SHELL
    req.hdr.nh_seq = g_nh_seq
    g_nh_seq += 1

    data = cbor.dumps({"argv": [line]})
    req.set_payload(data)

    transport_client.write_msg(req)
    rsp = transport_client.read_msg()

    if rsp.hdr.nh_seq != req.hdr.nh_seq:
        raise RuntimeError("bad sequence nr")

    rxd = cbor.loads(rsp.payload)

    out = rxd["o"]
    return out

def main():
    set_verbose(3)

    # TODO abstract enocde/decode!?
    req = smp.MgmtMsg()
    req.hdr.nh_op = smp.MGMT_OP.WRITE
    req.hdr.nh_group = smp.MGMT_GROUP_ID.SHELL
    req.hdr.nh_seq = 66

    # must be str or cbor dumps encode diffrently (mcu side assumes UTF-8 encoded probably)
    line = "device list"
    line = "modemct info"

    data = cbor.dumps({"argv": [line]})
    req.set_payload(data)
    #print("TXD:", hexdump(req.to_bytes()))

    print(vars(req.hdr))
    if (0):
        with SMPTransportBLE(name="hwt_lmin-0000", timeout=10) as clnt:
            clnt.write_msg(req)
            rsp = clnt.read_msg()

    else:
        with SMPTransportSerial(device="/dev/ttyUSB0", baudrate="115200", timeout=10) as clnt:
            clnt.write_msg(req)
            rsp = clnt.read_msg()

    print("RX hdr:", vars(rsp.hdr))

    if rsp.hdr.nh_seq != req.hdr.nh_seq:
        print("bad sequence nr")

    rxd = rsp.payload
    print("RX hex", rxd.hex())

    #rxd = base64.b64decode(rxd)
    print("RX d", rxd)
    rxd = cbor.loads(rsp.payload)
    print("keys", rxd.keys()) 

    rc = rxd["rc"]
    print("response rc =", rc)
    # "o" also for unknwon commands and other failures
    key = "o"
    if key not in rxd:
        print("error expected key '{}' in response data".format(key))

    out = rxd[key]
    print(out)

if __name__ == "__main__":
    main()

