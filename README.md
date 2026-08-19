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
uploaded.

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
returned a non-zero `rc`.


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

Uploads are resumable: the first request probes the device for the offset it
expects, so an upload interrupted by a dropped connection continues where it
left off rather than restarting. If the image is already running on the device
the transfer is skipped entirely (`res.already_present`).

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
python3 test/test_image.py
python3 test/test_mgmt_image.py     # needs cbor2
```
