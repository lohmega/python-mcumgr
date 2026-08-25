python-mcumgr
=============

Python implementation of the mcumgr protocol(s). Used by Apache Mynewt, Zephyr
and others.

Transports: BLE (via bleak) and serial/NLIP (via pyserial).


Install
=======
```
python3 -m pip install git+https://git@github.com/lohmega/python-mcumgr.git
```
(if private repo, replace `git+https` with `git+ssh`)

To run from a checkout without installing, use the `./mcumgr.sh` wrapper, which
puts the repo on `PYTHONPATH`.


Command line
============

```
mcumgr [--transport ble|serial] [--ble-name NAME | --ble-addr ADDR]
       [--port DEV] [--baud N] [--timeout SEC] [-v] <group> <command>
```

Image management:

```
mcumgr --ble-name sem-bb image state           # slots, versions, hashes, flags
mcumgr --ble-name sem-bb image upload fw.bin   # upload to the secondary slot
mcumgr --ble-name sem-bb image test [<hash>]   # boot once, revert unless confirmed
mcumgr --ble-name sem-bb image confirm [<hash>]# confirm permanently
mcumgr --ble-name sem-bb image erase           # erase slot 1
mcumgr image dump fw.bin                       # parse a local image, no device
```

`test` and `confirm` default to the hash of slot 1, i.e. the image you just
uploaded. After a test boot the device has already swapped: the image under
test is the *active* one in slot 0 and slot 1 holds what it would revert to, so
pass the hash explicitly when confirming a test boot.

A full update is `upload` -> `test` -> reset -> `confirm`. Between the reset and
the `confirm` the device is running the new image unconfirmed; if it never gets
confirmed the next reset reverts to the previous one.

OS management:

```
mcumgr --ble-name sem-bb os echo "hello"
mcumgr --ble-name sem-bb os reset
mcumgr --ble-name sem-bb os taskstat
```

Serial/NLIP works the same way:

```
mcumgr --transport serial --port /dev/ttyACM0 image state
```

Exit codes: 0 success, 1 usage/image error, 2 transport error, 3 device
returned a non-zero `rc`, 4 upload stopped on a budget and can be continued.

Some devices are only connectable in short windows - a power-managed node may
advertise steadily but accept a connection only occasionally, and BlueZ reports
that as `le-connection-abort-by-local` or a disconnect during service
discovery. The transport already retries connecting a few times; for such
devices, retry the whole command in a loop rather than raising the timeout.


Partial uploads over an intermittent link
=========================================

If the link is only up in short windows, cap each attempt and rerun. The device
keeps the offset, so the next run continues rather than restarting:

```sh
# transfer at most 60kB (or 20s) per attempt, loop until done
until mcumgr --ble-addr F4:65:25:1E:D7:B6 image upload --max-bytes 60000 fw.bin
do
    sleep 5     # exit code 4 = more to send
done
```

Each run reports `upload_off`, `upload_size`, `resumed_off` and `complete`, so a
supervising script can track progress across attempts.

To ride out drops within a single run instead, let it rebuild the link itself:

```sh
mcumgr --ble-addr F4:65:25:1E:D7:B6 image upload --reconnects 5 fw.bin
```

From the library, `upload()` takes `max_bytes`, `max_duration` and `reconnects`,
and the result carries `complete`, `remaining` and `percent`:

```python
res = img.upload("fw.bin", max_bytes=60_000)
while not res.complete:
    res = img.upload("fw.bin", max_bytes=60_000)
```


Library
=======

```python
from mcumgr.transport_ble import SMPTransportBLE
from mcumgr.mgmt_image import MgmtGrpImage

with SMPTransportBLE(name="sem-bb", timeout=30) as transport:
    img = MgmtGrpImage(transport)

    state = img.get_state()
    for slot in state.images:
        print(slot.slot, slot.version, slot.hash.hex(), slot.flags)

    res = img.upload(
        "fw.bin",
        progress_callback=lambda off, total, kbps: print(off, "/", total),
    )
    if not res.already_present:
        img.test()          # or img.confirm()
```

Uploads ask the device where to start rather than assuming: the first request
probes for the offset it expects, so a device holding a partial upload
continues from there instead of restarting.

The transfer is skipped entirely when the image is already on the device,
either running in slot 0 or fully staged in slot 1; `res.already_present` is
then True and `res.already_in_slot` says which. Pass `resume=False` to force a
full transfer regardless.

Image files can also be inspected without a device:

```python
from mcumgr import image

info = image.image_info("fw.bin")
print(info.hdr.ih_ver, info.size, info.calc_hash.hex(), info.hash_ok)
```


Tests
=====

The image parser and the upload state machine are covered by tests that need no
hardware:

```
./test/run.sh                       # needs cbor2
```

The image parser is checked against real signed firmware; the endpoint layer
and the upload state machine run against mock transports covering sequencing,
stale replies, the probe, multi-window transfers, reconnect, skip, stall,
retry and error paths.
