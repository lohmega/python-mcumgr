
import signal
import logging
import ble
import nlip
import smp
import sys
import cbor



def set_verbose(verbose_level):
    loggers = [ble.logger, smp.logger, nlip.logger]

    if verbose_level <= 1:
        level = logging.WARNING
    if verbose_level == 2:
        level = logging.INFO
    elif verbose_level >= 3:
        level = logging.DEBUG

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



def main():
    set_verbose(3)

    # TODO abstract enocde/decode!?
    req = smp.MgmtMsg()
    req.hdr.nh_op = smp.MGMT_OP.READ
    req.hdr.nh_id = smp.Mynewt.OS_MGMT_ID.ECHO
    data = cbor.dumps({"d": "hello" })
    req.set_payload(data)
    
    if (0):
        with ble.SMPClientBLE(name="hwt_lmin-0000", timeout=10) as clnt:
            clnt.write_msg(req)
            rsp = clnt.read_msg()

    else:
        with nlip.SMPClientNlip(device="/dev/ttyUSB0", baudrate="115200", timeout=10) as clnt:
            clnt.write_msg(req)
            rsp = clnt.read_msg()

    print(vars(rsp.hdr))
    print(rsp.payload.hex())
    print(cbor.loads(rsp.payload))

if __name__ == "__main__":
    main()

