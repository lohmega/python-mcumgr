import asyncio
import signal
import logging
import ble
import smp
import sys

def cancel_tasks():
    # Cancel all task to ensure all connections closed.  Otherwise devices
    # can be tied to "zombie connections" and not visible on next scan/connect.
    for task in asyncio.Task.all_tasks():
        if task is asyncio.tasks.Task.current_task():
            continue
        task.cancel()

def signal_handler(signo):
    cancel_tasks()


async def run(address):
    req = smp.MgmtMsg()
    req.hdr.nh_id = smp.Mynewt.OS_MGMT_ID.ECHO
    req.payload = "hello"
    async with ble.SMPClientBLE(name="hwt_lmin-0000") as clnt:
        await clnt.write_msg(req)
        rsp = await clnt.read_msg()
    print(rsp.payload)



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
    try:
        address =  sys.argv[1]
    except IndexError:
        #address = "C1:F2:56:DE:2A:1A"
        address = "27:52:05:AA:A5:C5" # hwt_lmin-0000
    loop = asyncio.get_event_loop()
    # signal.SIGHUP unix only
    for signo in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(signo, signal_handler, signo)

    loop.run_until_complete(run(address))


if __name__ == "__main__":
    main()
