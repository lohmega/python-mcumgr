"""Command line front end for the mcumgr/newtmgr SMP protocol.

    mcumgr --transport ble --ble-name sem-bb image state
    mcumgr --transport serial --port /dev/ttyACM0 image upload fw.bin
    mcumgr image dump fw.bin          # offline, no device needed
"""

import argparse
import logging
import sys

from mcumgr import image, smp
from mcumgr.__version__ import __version__

logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_TRANSPORT_ERROR = 2
EXIT_RESPONSE_ERROR = 3
# upload stopped cleanly on a byte/time budget and can be continued
EXIT_INCOMPLETE = 4


def _set_verbose(verbose_level):
    from mcumgr import mgmt_image

    loggers = [logger, smp.logger, mgmt_image.logger]

    if verbose_level >= 3:
        level = logging.DEBUG
    elif verbose_level == 2:
        level = logging.INFO
    else:
        level = logging.WARNING

    if verbose_level >= 4:
        loggers.append(logging.getLogger("bleak"))

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(levelname)s:%(name)s:%(lineno)d: %(message)s")
    )

    # transport loggers only if the module is importable
    for modname in ("transport_ble", "transport_serial"):
        try:
            mod = __import__("mcumgr." + modname, fromlist=["logger"])
        except ImportError:
            continue
        loggers.append(mod.logger)

    for l in loggers:
        l.setLevel(level)
        l.addHandler(handler)


def _mk_transport(args):
    if args.transport == "ble":
        from mcumgr.transport_ble import SMPTransportBLE

        if not (args.ble_name or args.ble_addr):
            raise argparse.ArgumentTypeError(
                "ble transport needs --ble-name or --ble-addr"
            )
        return SMPTransportBLE(
            address=args.ble_addr, name=args.ble_name, timeout=args.timeout
        )

    if args.transport in ("serial", "nlip"):
        from mcumgr.transport_serial import SMPTransportSerial

        if not args.port:
            raise argparse.ArgumentTypeError("serial transport needs --port")
        return SMPTransportSerial(
            port=args.port, baudrate=args.baud, timeout=args.timeout
        )

    raise argparse.ArgumentTypeError("Unknown transport '{}'".format(args.transport))


def _progress(off, total, rate_kbps):
    pct = 100.0 * off / total if total else 0.0
    sys.stderr.write("\rUploading: {:5.1f}% done ({:6.1f}kB/s)".format(pct, rate_kbps))
    sys.stderr.flush()


# -- commands ----------------------------------------------------------------


def do_image_dump(args):
    """Offline: parse and print an image file. No device involved."""
    print(image.image_info(args.file).format())
    return EXIT_SUCCESS


def do_image_state(args, grp):
    print(grp.get_state(timeout=args.timeout).format())
    return EXIT_SUCCESS


def do_image_upload(args, grp):
    info = image.image_info(args.file)
    logger.info("uploading %s v%s (%d bytes)", args.file, info.hdr.ih_ver, info.size)

    cb = None if args.verbose else _progress
    res = grp.upload(
        args.file,
        image_num=args.image_num,
        progress_callback=cb,
        timeout=args.timeout,
        resume=not args.no_resume,
        max_bytes=args.max_bytes,
        max_duration=args.max_seconds,
        reconnects=args.reconnects,
    )

    if cb:
        sys.stderr.write("\n")

    if res.already_present:
        if res.already_in_slot == 0:
            print("Image '{}' already running in device".format(args.file))
        else:
            print(
                "Image '{}' already uploaded to slot {}, nothing to do".format(
                    args.file, res.already_in_slot
                )
            )
        print("hash: {}".format(info.calc_hash.hex()))
        return EXIT_SUCCESS

    print(
        "upload_off={} upload_size={} resumed_off={} complete={}".format(
            res.off, res.size, res.resumed_off, 1 if res.complete else 0
        )
    )
    print("hash: {}".format(info.calc_hash.hex()))

    if not res.complete:
        print(
            "incomplete: {} of {} bytes ({:.1f}%), {} remaining - run the same "
            "command again to continue".format(
                res.off, res.size, res.percent, res.remaining
            ),
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE

    return EXIT_SUCCESS


def _check_hash_arg(hash_arg, default_desc):
    """Validate a CLI-supplied image hash before it reaches the device.

    Returns None (unchanged) or raises a ValueError with a message fit to
    print directly - malformed hex/length is a user error, not a crash.
    """
    if hash_arg is None:
        print("Assuming {}".format(default_desc), file=sys.stderr)
        return None
    try:
        raw = bytes.fromhex(hash_arg)
    except ValueError:
        raise ValueError("hash must be hex, got '{}'".format(hash_arg)) from None
    if len(raw) != image.IMAGE_HASH_LEN:
        raise ValueError(
            "hash must be {} bytes ({} hex chars), got {}".format(
                image.IMAGE_HASH_LEN, image.IMAGE_HASH_LEN * 2, len(raw)
            )
        )
    return hash_arg


def do_image_test(args, grp):
    args.hash = _check_hash_arg(args.hash, "hash of image slot 1")
    print(
        grp.test(args.hash, timeout=args.timeout, image_num=args.image_num).format()
    )
    return EXIT_SUCCESS


def do_image_confirm(args, grp):
    # Unlike test(), an omitted hash here is not a client-side guess - the
    # device natively resolves it to "confirm whatever is currently active",
    # which is correct both before and after a test boot's swap.
    args.hash = _check_hash_arg(args.hash, "the currently active image")
    print(
        grp.confirm(
            args.hash, timeout=args.timeout, image_num=args.image_num
        ).format()
    )
    return EXIT_SUCCESS


def do_image_erase(args, grp):
    sys.stderr.write("Erasing... (can take more than 10s)\n")
    grp.erase(slot=args.slot, timeout=max(args.timeout, 15.0))
    print("Erased slot {}".format(args.slot))
    return EXIT_SUCCESS


def do_os_echo(args, grp):
    print(grp.echo(args.text, timeout=args.timeout))
    return EXIT_SUCCESS


def do_os_reset(args, grp):
    grp.reset(timeout=args.timeout)
    print("Reset sent")
    return EXIT_SUCCESS


def do_os_taskstat(args, grp):
    for name, stats in grp.taskstats(timeout=args.timeout).items():
        print("{}: {}".format(name, stats))
    return EXIT_SUCCESS


# -- argument parsing --------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="mcumgr", description="MCU manager (mcumgr/newtmgr SMP) tool"
    )

    p.add_argument(
        "--verbose",
        "-v",
        default=0,
        action="count",
        help="Verbose output (-vvv for more verbosity)",
    )
    p.add_argument("--version", action="store_true", help="Show version and exit")
    p.add_argument(
        "--transport",
        "--conntype",
        dest="transport",
        choices=["ble", "serial", "nlip"],
        default="ble",
        help="transport method (default: ble)",
    )
    p.add_argument("--ble-name", type=str, help="BLE device name")
    p.add_argument("--ble-addr", type=str, help="BLE device address")
    p.add_argument(
        "--port", "--interface", dest="port", type=str, help="serial port device"
    )
    p.add_argument("--baud", type=int, default=115200, help="serial port baudrate")
    p.add_argument(
        "--timeout", type=float, default=10.0, help="timeout in seconds (default: 10)"
    )

    sub = p.add_subparsers(dest="group")

    # -- image
    p_img = sub.add_parser("image", help="firmware image management")
    img_sub = p_img.add_subparsers(dest="cmd")

    s = img_sub.add_parser("state", help="show image state of each slot")
    s.set_defaults(_func=do_image_state)

    s = img_sub.add_parser("upload", help="upload a firmware image")
    s.add_argument("file", help="MCUboot image file")
    s.add_argument(
        "--image-num",
        type=int,
        default=None,
        help="image number for multi-image devices",
    )
    s.add_argument(
        "--no-resume",
        action="store_true",
        help="do not inspect device state first; always upload from scratch",
    )
    s.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="stop cleanly after about this many bytes; rerun to continue "
        "(exit code 4 while incomplete)",
    )
    s.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="stop cleanly after about this many seconds; rerun to continue",
    )
    s.add_argument(
        "--reconnects",
        type=int,
        default=0,
        help="reconnect and carry on this many times if the link drops",
    )
    s.set_defaults(_func=do_image_upload)

    s = img_sub.add_parser("test", help="mark image pending (boot once)")
    s.add_argument("hash", nargs="?", default=None, help="image hash (default: slot 1)")
    s.add_argument(
        "--image-num",
        type=int,
        default=None,
        help="image number for multi-image devices, used only to pick the "
        "default slot-1 hash when no hash is given",
    )
    s.set_defaults(_func=do_image_test)

    s = img_sub.add_parser("confirm", help="confirm image permanently")
    s.add_argument(
        "hash",
        nargs="?",
        default=None,
        help="image hash (default: whatever is currently active)",
    )
    s.add_argument(
        "--image-num",
        type=int,
        default=None,
        help="unused when no hash is given - the device resolves that case "
        "to its own active image, not a client-picked one",
    )
    s.set_defaults(_func=do_image_confirm)

    s = img_sub.add_parser("erase", help="erase a slot")
    s.add_argument("--slot", type=int, default=1, help="slot to erase (default: 1)")
    s.set_defaults(_func=do_image_erase)

    s = img_sub.add_parser("dump", help="print image file info (offline)")
    s.add_argument("file", help="MCUboot image file")
    s.set_defaults(_func=do_image_dump, _offline=True)

    # -- os
    p_os = sub.add_parser("os", help="OS management")
    os_sub = p_os.add_subparsers(dest="cmd")

    s = os_sub.add_parser("echo", help="echo a string via the device")
    s.add_argument("text", nargs="?", default="hello")
    s.set_defaults(_func=do_os_echo)

    s = os_sub.add_parser("reset", help="reboot the device")
    s.set_defaults(_func=do_os_reset)

    s = os_sub.add_parser("taskstat", help="show task statistics")
    s.set_defaults(_func=do_os_taskstat)

    args = p.parse_args(argv)
    return p, args


def main(argv=None):
    parser, args = parse_args(argv)

    _set_verbose(args.verbose)

    if args.version:
        print(__version__)
        return EXIT_SUCCESS

    func = getattr(args, "_func", None)
    if func is None:
        parser.print_help()
        return EXIT_USER_ERROR

    # `image dump` reads a local file, it needs no device
    if getattr(args, "_offline", False):
        try:
            return func(args)
        except (image.ImageError, OSError) as e:
            print("ERR: {}".format(e), file=sys.stderr)
            return EXIT_USER_ERROR

    from mcumgr.mgmt_image import MgmtGrpImage
    from mcumgr.mgmt_os import MgmtGrpOs

    grp_cls = {"image": MgmtGrpImage, "os": MgmtGrpOs}[args.group]

    try:
        transport = _mk_transport(args)
    except argparse.ArgumentTypeError as e:
        print("ERR: {}".format(e), file=sys.stderr)
        return EXIT_USER_ERROR

    try:
        with transport:
            return func(args, grp_cls(transport))
    except smp.MgmtEndpointError as e:
        print("\nERR: {}".format(e), file=sys.stderr)
        return EXIT_RESPONSE_ERROR
    except smp.SMPTransportError as e:
        print("\nERR: transport: {}".format(e), file=sys.stderr)
        return EXIT_TRANSPORT_ERROR
    except OSError as e:
        # serial.SerialException is an OSError - e.g. port busy or missing
        print("\nERR: transport: {}".format(e), file=sys.stderr)
        return EXIT_TRANSPORT_ERROR
    except image.ImageError as e:
        print("\nERR: {}".format(e), file=sys.stderr)
        return EXIT_USER_ERROR
    except ValueError as e:
        # malformed CLI-supplied value (e.g. a bad --hash), not a bug
        print("\nERR: {}".format(e), file=sys.stderr)
        return EXIT_USER_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_USER_ERROR


if __name__ == "__main__":
    sys.exit(main())
