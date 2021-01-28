import logging
import argparse
import sys
from mcumgr import smp, ble, nlip

logger = logging.getLogger(__name__)

_str_to_mgmt_op = { str(v.name).lower() : v for v in smp.MGMT_OP }
_str_to_mgmt_id = { str(v.name).lower() : v for v in smp.Mynewt.OS_MGMT_ID }


def do_nlip(device=None, baud=None, **kwargs):
    pass # TODO

def do_ble(name=None, **kwargs):
    pass # TODO


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

def parse_args():
    base = argparse.ArgumentParser( description='MCUMGR tool')

    base.add_argument(
        "--verbose", 
        "-v", 
        default=0, 
        action="count", 
        help="Verbose output (-vvv for more verbosity)",
    )

    base.add_argument(
        "--version",
        action="store_true",
        help="Show version info and exit"
    )


    # help for common args only visible with `<positional> -h`
    common = argparse.ArgumentParser(add_help=False)
    
    common.add_argument(
        "--mgmt-op",
        type=lambda x : _str_to_mgmt_op[x],
        choices=list(_str_to_mgmt_op.keys()),
        required=True,
        help="mgmt op",
    )
    common.add_argument(
        "--mgmt-id",
        type=lambda x : _str_to_mgmt_id[x],
        choices=list(_str_to_mgmt_id.keys()),
        required=True,
        help="mgmt id as defined by Mynewt",
    )
    
    subparsers = base.add_subparsers()

    # NLIP
    p_nlip = subparsers.add_parser(
        "nlip",
        parents=[common],
        description="nlip serial transport"
    )
    p_nlip.set_defaults(_actionfunc=do_nlip)

    p_nlip.add_argument(
        "--device",
        type=str,
        required=True,
        help="serial port/device",
    )
    p_nlip.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="baudrate",
    )


    p_ble = subparsers.add_parser(
        "ble",
        parents=[common],
        description="Bluetooth LE transport"
    )
    p_nlip.set_defaults(_actionfunc=do_ble)

    p_ble.add_argument(
        "--name",
        type=str,
        help="BLE device name",
    )
    """
    group = p_nlip.add_mutually_exclusive_group()
    group.add_argument("mgmt_id", default="read", nargs="?")

    """
    p_nlip.add_argument("data", nargs='*')
    args = base.parse_args()

    if "verbose" not in args:
        args.verbose = 0

    return vars(args)

def main():
    args = parse_args()
    print(args)

    verbose_level = args["verbose"]
    set_verbose(verbose_level)
    logger.debug("args={}".format(args))

    if args.get("version"):
        print_versions()
        exit(0)

    actionfunc = args.get("_actionfunc")
    if not actionfunc:
        return
    actionfunc(**args)



if __name__ == "__main__":
    main()
