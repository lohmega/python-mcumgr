import asyncio
import signal
import logging
import ble
import nlip
import smp
import sys
import cbor

def cancel_tasks():
    # Cancel all task to ensure all connections closed.  Otherwise devices
    # can be tied to "zombie connections" and not visible on next scan/connect.
    for task in asyncio.Task.all_tasks():
        if task is asyncio.tasks.Task.current_task():
            continue
        task.cancel()


def signal_handler(signo):
    cancel_tasks()


async def run():
    # TODO abstract enocde/decode!?
    req = smp.MgmtMsg()
    req.hdr.nh_op = smp.MGMT_OP.READ
    req.hdr.nh_id = smp.Mynewt.OS_MGMT_ID.ECHO
    data = cbor.dumps({"d": "hello" })
    req.set_payload(data)
    if (0):
        async with ble.SMPClientBLE(name="hwt_lmin-0000", timeout=10) as clnt:
            await clnt.write_msg(req)
            rsp = await clnt.read_msg()
    else:
        async with nlip.SMPClientNlip(device="/dev/ttyUSB0", baudrate="1000000", timeout=10) as clnt:
            await clnt.write_msg(req)
            rsp = await clnt.read_msg()

    print(vars(rsp.hdr))
    print(rsp.payload.hex())
    print(cbor.loads(rsp.payload))


def set_verbose(verbose_level):
    loggers = [ble.logger, smp.logger]

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
    loop = asyncio.get_event_loop()
    # signal.SIGHUP unix only
    for signo in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(signo, signal_handler, signo)

    loop.run_until_complete(run())


if __name__ == "__main__":
    main()

