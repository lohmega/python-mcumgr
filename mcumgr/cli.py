import logging
import argparse
import sys
import cbor2 as cbor

from mcumgr import smp
from mcumgr import transport_serial, transport_ble
from mcumgr.transport_serial import SMPTransportSerial
from mcumgr.transport_ble import SMPTransportBLE

logger = logging.getLogger(__name__)

_str_to_mgmt_op = { str(v.name).lower() : v for v in smp.MGMT_OP }
_str_to_mgmt_id = { str(v.name).lower() : v for v in smp.Mynewt.OS_MGMT_ID }

parser = argparse.ArgumentParser(
        description="MCUMGR microcontroller unit managarer tool")


parser.add_argument(
    #-l, --loglevel
    "--verbose",
    "-v",
    default=0,
    action="count",
    help="Verbose output (-vvv for more verbosity)",
)

parser.add_argument(
    "--version",
    action="store_true",
    default=False,
    help="Show version info and exit"
)

parser.add_argument("--transport", "--conntype",
    dest="transport",
    choices=["serial", "ble", "nlip"],
    type=str,
    help="transport method. Bluetooth LE, (ble) serial nlip",
)

parser.add_argument(
    #"--interface-dev",
    "--port",
    "--hci", # <--- newtmgr compat
    type=str,
    required=False,
    dest="interface",
    default=None,
    help="transport interface device. serial port or BLE HCI device",
)


parser.add_argument(
    "--interactive",
    action="store_true",
    default=False,
    help="Run in interactive mode"
)

# TODO implement these
""" 
parser.add_argument('--mtu',
    type=int,
    default=None,
    help="Maximum Transmission Unit (MTU) largest packet or frame size. Auto negotiated for BLE transport"
)

parser.add_argument('--hci', 
        type=str,
        help=argparse.SUPPRESS,
        help="newtmgr compat. HCI index for the controller on Linux machine"
)
"""

parser.add_argument(
    "--baud",
    type=int,
    default=115200,
    help="serial port baudrate",
)

parser.add_argument(
    "--ble-name",
    type=str,
    help="BLE device name",
)

parser.add_argument(
    "--timeout",
    type=float,
    default=10,
    help="timeout in seconds",
)

parser.add_argument('commands', nargs=argparse.REMAINDER)

def _set_verbose(verbose_level):
    loggers = [logger, transport_ble.logger, smp.logger, transport_serial.logger]

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

def _do_cmd_shell(transport_client, line):
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


def run_interactive_shell(transport):

    req = smp.MgmtMsg()
    #req.hdr.nh_op = args.mgmt_op
    #req.hdr.nh_id = args.mgmt_id

    req.hdr.nh_op = smp.MGMT_OP.WRITE
    req.hdr.nh_group = smp.MGMT_GROUP_ID.SHELL

    with transport:
        line = b"device list" #input("shell:")
        #data = cbor.dumps({"d": line })
        req.set_payload(line)
        print("TX", vars(req.hdr))
        print(req.to_bytes())
        transport.write_msg(req)
        rsp = transport.read_msg()
        print("RX", (rsp.hdr))

        print(cbor.loads(rsp.payload))


def _print_smp_mgmt_msg(tag, msg):

        print("----", tag, "----") 
        print("   header:", vars(msg.hdr))
        print("   payload:", msg.payload.hex())
        print("   payload:", cbor.loads(msg.payload))

def _smp_forward(transport, req):
    req = _smp_fwd_send_wrap(req)
    with transport:
        # write request
        transport.write_msg(req)
        # write response
        rsp = transport.read_msg()
        _print_smp_mgmt_msg("wr_rsp", rsp)
        """
        # read request
        req = _smp_req_fwd_recv()
        transport.write_msg(req)
        # read response
        rsp = transport.read_msg()
        _print_smp_mgmt_msg("rd_rsp", rsp)
        """


def _mk_echo_req(msg="hello", fwd=True):

    req = smp.MgmtMsg()
    req.hdr.nh_group = smp.MGMT_GROUP_ID.OS
    req.hdr.nh_op = smp.MGMT_OP.WRITE
    req.hdr.nh_id = smp.Mynewt.OS_MGMT_ID.ECHO

    req.encode_payload({"d": msg })
    return req

SMP_FORWARD_ID_STATE=0
SMP_FORWARD_ID_SEND=1
SMP_FORWARD_ID_RECV=2
MGMT_GROUP_ID_FORWARD=255
def _mk_image_state_req():
    req = smp.MgmtMsg()
    req.hdr.nh_group=0x1
    req.hdr.nh_op=0x0
    req.hdr.nh_id=0x0

    req.encode_payload({"m": "m" })
    return req

def _smp_fwd_send_wrap(fwd_req, media='ble', addr=0):
    req = smp.MgmtMsg()
    #req.hdr.nh_op = args.mgmt_op
    #req.hdr.nh_id = args.mgmt_id
    req.hdr.nh_group = MGMT_GROUP_ID_FORWARD
    req.hdr.nh_op = smp.MGMT_OP.WRITE
    req.hdr.nh_id = SMP_FORWARD_ID_SEND

    org_data = fwd_req.to_bytes()
    #print(org_data)
    payload = {
            "m" : media,
            "a" : addr,
            "d": org_data,
            "r": 500
    }
    data = cbor.dumps(payload)
    req.set_payload(data)
    return req

def _smp_req_fwd_recv(media='ble', addr=0):
    req = smp.MgmtMsg()
    #req.hdr.nh_op = args.mgmt_op
    #req.hdr.nh_id = args.mgmt_id
    req.hdr.nh_group = MGMT_GROUP_ID_FORWARD
    req.hdr.nh_op = smp.MGMT_OP.READ
    req.hdr.nh_id = SMP_FORWARD_ID_SEND

    payload = {
            "m" : media,
            "a" : addr
    }
    data = cbor.dumps(payload)
    req.set_payload(data)
    return req

def main():
    args = parser.parse_args()
    _set_verbose(args.verbose)
    logger.debug("args={}".format(args))

    if args.version:
        print("version:", "<unkwown>") # TODO
        exit(0)


    if args.transport == "ble":
        transport = SMPTransportBLE(name=args.ble_name, timeout=args.timeout)
    elif args.transport in ["serial", "nlip"]:
        transport = SMPTransportSerial(
                            device=args.interface,
                            baudrate=args.baud,
                            timeout=args.timeout)
    else:
        raise argparse.ArgumentTypeError("Unknown transport")

    #req = _mk_echo_req()
    req = _mk_image_state_req()
    _smp_forward(transport, req)

    #run_interactive_shell(transport)


if __name__ == "__main__":
    main()
