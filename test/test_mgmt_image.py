"""Unit tests for mcumgr.mgmt_image against a mock SMP device.

Exercises the upload state machine (probe, resume, chunk sizing, stall and
error handling) without touching hardware.

Runs standalone (`python3 test/test_mgmt_image.py`) or under pytest.
Requires cbor2.
"""

import hashlib
import os
import struct
import sys
import time

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

import cbor2 as cbor

from mcumgr import image, smp
from mcumgr.mgmt_image import (
    IMG_MGMT_ID_ERASE,
    IMG_MGMT_ID_STATE,
    IMG_MGMT_ID_UPLOAD,
    UPLOAD_FIRST_CHUNK_SIZE,
    MgmtGrpImage,
)

from test_image import _mk_image


class FakeDevice:
    """Minimal SMP image-group device, driven through the transport API."""

    def __init__(self, slots=None, upload_off=None, max_mtu=256):
        self.max_mtu = max_mtu
        self.slots = slots if slots is not None else []
        # None = no upload context held
        self.upload_off = upload_off
        self.received = bytearray()
        self.total_len = None
        self.requests = []
        self.erased = []

        self._seq = smp.SeqCounter()
        self._pending_rsp = None
        self._last_id = None

    # -- transport API -------------------------------------------------------

    def next_seq(self):
        return self._seq.next()

    def write_msg(self, msg):
        req = cbor.loads(msg.payload) if msg.payload else {}
        self.requests.append((msg.hdr.nh_op, msg.hdr.nh_id, req))
        self._last_id = msg.hdr.nh_id

        if msg.hdr.nh_id == IMG_MGMT_ID_STATE:
            if msg.hdr.nh_op == smp.MGMT_OP.READ:
                rsp = {"images": self.slots, "splitStatus": 0}
            else:
                rsp = self._do_state_write(req)
        elif msg.hdr.nh_id == IMG_MGMT_ID_UPLOAD:
            rsp = self._do_upload(req)
        elif msg.hdr.nh_id == IMG_MGMT_ID_ERASE:
            self.erased.append(req.get("slot"))
            self.upload_off = None
            self.received = bytearray()
            rsp = {"rc": 0}
        else:
            rsp = {"rc": int(smp.MGMT_ERR.ENOTSUP)}

        out = smp.MgmtMsg(
            nh_op=msg.hdr.nh_op + 1,
            nh_group=msg.hdr.nh_group,
            nh_id=msg.hdr.nh_id,
            nh_seq=msg.hdr.nh_seq,
        )
        out.encode_payload(rsp)
        self._pending_rsp = out

    def read_msg(self, timeout=None):
        if self._pending_rsp is None:
            raise smp.SMPTransportError("no response pending")
        rsp, self._pending_rsp = self._pending_rsp, None
        return rsp

    # -- device behaviour ----------------------------------------------------

    def _do_state_write(self, req):
        img_hash = req.get("hash")
        confirm = req.get("confirm", False)

        if img_hash is None:
            # Matches the real device (img_mgmt_state.c): an omitted hash
            # resolves to the active slot for confirm, and is rejected for
            # test - there is no such thing as a hashless test.
            if not confirm:
                return {"rc": int(smp.MGMT_ERR.EINVAL)}
            for i, s in enumerate(self.slots):
                if s.get("active"):
                    self.slots[i] = dict(s)
                    self.slots[i]["confirmed"] = True
                    self.slots[i]["permanent"] = True
                    return {"images": self.slots, "splitStatus": 0}
            return {"rc": int(smp.MGMT_ERR.ENOENT)}

        for i, s in enumerate(self.slots):
            if s.get("hash") == img_hash:
                self.slots[i] = dict(s)
                if confirm:
                    self.slots[i]["confirmed"] = True
                    self.slots[i]["permanent"] = True
                else:
                    self.slots[i]["pending"] = True
                return {"images": self.slots, "splitStatus": 0}
        return {"rc": int(smp.MGMT_ERR.ENOENT)}

    def _do_upload(self, req):
        off = req["off"]
        data = req["data"]

        if "len" in req:
            # a request carrying len starts a fresh transfer
            self.total_len = req["len"]
            self.upload_off = 0
            self.received = bytearray()

        if self.upload_off is None:
            # no context: tell the host to start from the beginning
            return {"rc": 0, "off": 0}

        if off != self.upload_off:
            # wrong offset: just report what we actually want next
            return {"rc": 0, "off": self.upload_off}

        self.received.extend(data)
        self.upload_off = off + len(data)
        return {"rc": 0, "off": self.upload_off}


def _slot(n, img_hash, version="1.0.0", **flags):
    d = {
        "slot": n,
        "version": version,
        "hash": img_hash,
        "bootable": True,
        "pending": False,
        "confirmed": n == 0,
        "active": n == 0,
        "permanent": False,
    }
    d.update(flags)
    return d


def _img_file(tmp_path="/tmp", body_len=2000, name="test_img.bin"):
    data, digest = _mk_image(body=b"\xc3" * body_len)
    path = os.path.join(tmp_path, name)
    with open(path, "wb") as f:
        f.write(data)
    return path, data, digest


# -- state tests -------------------------------------------------------------


def test_get_state_parses_slots():
    h0 = b"\x01" * 32
    h1 = b"\x02" * 32
    dev = FakeDevice(slots=[_slot(0, h0, "1.2.3"), _slot(1, h1, "1.2.4")])
    grp = MgmtGrpImage(dev)

    state = grp.get_state()
    assert len(state.images) == 2
    assert state.slot(0).version == "1.2.3"
    assert state.slot(0).hash == h0
    assert state.slot(0).active
    assert not state.slot(1).active
    assert state.find_by_hash(h1).slot == 1
    assert state.slot(5) is None
    assert "Slot 0" in state.format()


def test_test_marks_pending():
    h1 = b"\x02" * 32
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, h1)])
    grp = MgmtGrpImage(dev)

    state = grp.test(h1)
    assert state.slot(1).pending
    assert not state.slot(1).confirmed

    op, cmd_id, req = dev.requests[-1]
    assert op == smp.MGMT_OP.WRITE and cmd_id == IMG_MGMT_ID_STATE
    assert req == {"hash": h1}  # no confirm key when testing


def test_confirm_sets_confirm_key():
    h1 = b"\x02" * 32
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, h1)])
    grp = MgmtGrpImage(dev)

    state = grp.confirm(h1)
    assert state.slot(1).confirmed

    _op, _id, req = dev.requests[-1]
    assert req == {"hash": h1, "confirm": True}


def test_confirm_without_hash_sends_no_hash():
    """confirm() with no explicit hash must not guess slot 1's hash and
    send that - it must send no hash field at all, so the device's own
    img_mgmt handler resolves it to whatever is currently active. That is
    correct both before AND after a test boot's swap; a client-side guess
    of "slot 1" is only ever correct in the former case (see
    test_confirm_without_hash_after_swap_still_confirms_active below) -
    this is the actual behavior the device's protocol supports, not a
    workaround for it.
    """
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, b"\x02" * 32)])
    grp = MgmtGrpImage(dev)

    grp.confirm()
    _op, _id, req = dev.requests[-1]
    assert "hash" not in req
    assert req["confirm"] is True
    assert dev.slots[0]["confirmed"], "must confirm the active slot (0)"


def test_confirm_without_hash_after_swap_still_confirms_active():
    """The exact scenario the old slot-1-guessing behavior got wrong: after
    a test boot, the image under test is active in slot 0 and slot 1 holds
    the fallback. confirm() with no hash must confirm slot 0 (active), not
    slot 1 (guessed)."""
    dev = FakeDevice(
        slots=[
            _slot(0, b"\x99" * 32, pending=False, confirmed=False, active=True),
            _slot(1, b"\x11" * 32, confirmed=True, active=False),
        ]
    )
    grp = MgmtGrpImage(dev)

    grp.confirm()

    assert dev.slots[0]["confirmed"], "the just-tested image (slot 0) must be confirmed"
    assert dev.slots[1]["hash"] == b"\x11" * 32  # untouched fallback


def test_hash_accepts_hex_string():
    h1 = b"\x02" * 32
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, h1)])
    grp = MgmtGrpImage(dev)

    grp.test(h1.hex())
    _op, _id, req = dev.requests[-1]
    assert req["hash"] == h1


def test_bad_hash_length_rejected():
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32)])
    grp = MgmtGrpImage(dev)
    try:
        grp.test(b"\x02" * 8)
    except ValueError as e:
        assert "32 bytes" in str(e)
    else:
        raise AssertionError("expected ValueError for short hash")


def test_erase():
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)
    grp.erase(slot=1)
    assert dev.erased == [1]


def test_error_rc_raises():
    h1 = b"\x02" * 32
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32)])  # no slot 1 -> ENOENT
    grp = MgmtGrpImage(dev)
    try:
        grp.test(h1)
    except smp.MgmtEndpointError as e:
        assert e.rc == smp.MGMT_ERR.ENOENT
    else:
        raise AssertionError("expected MgmtEndpointError")


# -- upload tests ------------------------------------------------------------


def test_upload_full_transfer():
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32)])
    grp = MgmtGrpImage(dev)

    seen = []
    res = grp.upload(path, progress_callback=lambda o, t, r: seen.append(o))

    assert bytes(dev.received) == data, "device did not receive the exact image"
    assert dev.total_len == len(data)
    assert res.off == len(data)
    assert res.size == len(data)
    assert not res.already_present
    assert seen[-1] == len(data)
    assert seen == sorted(seen), "progress must be monotonic"


def test_upload_probes_before_declaring_len():
    """First request probes at offset 32 with no len; the second starts the
    transfer with len and a small chunk."""
    path, data, _ = _img_file()
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)
    grp.upload(path)

    uploads = [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]
    _op, _id, first = uploads[0]
    assert first["off"] == 32
    assert "len" not in first

    _op, _id, second = uploads[1]
    assert second["off"] == 0
    assert second["len"] == len(data)
    assert len(second["data"]) == UPLOAD_FIRST_CHUNK_SIZE

    # after the device reports progress the chunk opens up to the MTU
    _op, _id, third = uploads[2]
    assert len(third["data"]) > UPLOAD_FIRST_CHUNK_SIZE


def test_max_chunk_reserves_overhead_even_on_a_tiny_mtu():
    """A small/un-negotiated MTU (e.g. BLE's un-negotiated default of 23)
    must not make _max_chunk return the whole MTU as data - the SMP header
    and CBOR map still need to fit inside it too. 23 is below
    UPLOAD_CHUNK_OVERHEAD, so no positive chunk size can actually make a
    request fit; the floor of 1 is the least-bad, honest answer rather
    than pretending the whole 23 bytes are available for data."""
    dev = FakeDevice(slots=[], max_mtu=23)
    grp = MgmtGrpImage(dev)

    assert grp._max_chunk == 1


def test_max_chunk_reserves_overhead_on_a_realistic_small_mtu():
    dev = FakeDevice(slots=[], max_mtu=64)
    grp = MgmtGrpImage(dev)

    assert grp._max_chunk == 64 - 32  # UPLOAD_CHUNK_OVERHEAD


def test_upload_resumes_partial():
    path, data, _ = _img_file()
    dev = FakeDevice(slots=[])
    # device already holds the first 1024 bytes of a transfer
    dev.upload_off = 1024
    dev.received = bytearray(data[:1024])
    dev.total_len = len(data)

    grp = MgmtGrpImage(dev)
    res = grp.upload(path)

    assert bytes(dev.received) == data
    assert res.resumed_off == 1024, "should report where it picked up"
    assert res.off == len(data)

    uploads = [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]
    assert all("len" not in r[2] for r in uploads), "resume must not restart"


def test_upload_skips_image_already_running():
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[_slot(0, digest), _slot(1, b"\x02" * 32)])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path)
    assert res.already_present
    assert res.off == len(data)
    assert not [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]


def test_upload_does_not_skip_on_a_different_image_num():
    """A matching hash on a different image must not cause a skip.

    On a multi-image device, image 0 happens to already run the exact bytes
    we are about to upload for image 1. Without scoping the "already
    present"/"already staged" check by image number, this looks identical to
    the image we are targeting actually being there.
    """
    path, data, digest = _img_file()
    dev = FakeDevice(
        slots=[
            _slot(0, digest, image=0),
            _slot(1, b"\x02" * 32, image=0),
            _slot(0, b"\x03" * 32, image=1),
            _slot(1, b"\x04" * 32, image=1),
        ]
    )
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, image_num=1)
    assert not res.already_present, "must not skip based on image 0's matching hash"
    assert [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD], "upload should run"


def test_upload_skips_image_already_in_slot1():
    """A matching slot 1 hash means the whole image is already staged."""
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, digest)])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path)
    assert res.already_present
    assert not [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]


def test_upload_reuploads_when_slot1_pending():
    """If it is already pending the device restarts, so we upload again."""
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, digest, pending=True)])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path)
    assert not res.already_present
    assert bytes(dev.received) == data


def test_upload_no_resume_forces_full_transfer():
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[_slot(0, digest)])  # would normally be skipped
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, resume=False)
    assert not res.already_present
    assert bytes(dev.received) == data


def test_upload_no_resume_ignores_a_stale_unrelated_context():
    """--no-resume must not pick up an unrelated in-progress upload.

    The probe-and-follow-the-device's-offset dance that makes resume work is
    unconditional in the network sense - it isn't gated by the resume flag.
    If the device happens to be sitting on a partial upload of some other
    file when a --no-resume upload starts, following that offset would
    splice the new file's bytes onto the old file's prefix and report a
    corrupted hybrid image as a complete, successful transfer.
    """
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[])
    # Simulate a stale context: some other, larger file, already 5000 bytes in.
    dev.upload_off = 5000
    dev.total_len = 99999
    dev.received = bytearray(b"\xee" * 5000)

    grp = MgmtGrpImage(dev)
    res = grp.upload(path, resume=False)

    assert res.complete
    assert bytes(dev.received) == data, "must not splice onto the stale context"
    uploads = [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]
    assert uploads[0][2]["off"] == 0
    assert uploads[0][2]["len"] == len(data)


# -- partial upload / continue ----------------------------------------------


def test_upload_zero_byte_budget_reports_a_real_offset():
    """max_bytes=0 must not stop before the very first round trip - off
    would otherwise still be the synthetic UPLOAD_PROBE_OFFSET (32), not
    a real device offset (which could be 0 or far larger), making the
    returned UploadResult.off a meaningless placeholder."""
    path, data, _ = _img_file()
    dev = FakeDevice(slots=[])  # fresh device, real offset is 0
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, max_bytes=0)

    assert not res.complete
    assert res.off == 0, "must report the device's real (probed) offset, not 32"
    uploads = [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]
    assert len(uploads) == 1, "exactly one round trip (the probe), then stop"


def test_partial_upload_then_continue():
    """max_bytes stops cleanly; a second call finishes from the device offset."""
    path, data, _ = _img_file(body_len=20000, name="test_img_big.bin")
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)

    first = grp.upload(path, max_bytes=4000)
    assert not first.complete, "should report an unfinished transfer"
    assert 0 < first.off < len(data)
    assert first.remaining == len(data) - first.off
    partial = bytes(dev.received)
    assert partial == data[: first.off]

    second = grp.upload(path)
    assert second.complete
    assert second.off == len(data)
    assert second.resumed_off == first.off, "should pick up where it stopped"
    assert bytes(dev.received) == data, "reassembled image must match the file"


def test_progress_rate_after_resume_uses_bytes_sent_this_call():
    """The reported rate must reflect bytes actually transmitted THIS
    call, not the absolute device offset - a resumed upload's offset can
    already be far into the file from an earlier call, which would
    otherwise make the very first progress callback after a resume report
    a wildly inflated rate (as if all of that had just been sent)."""
    path, data, _ = _img_file(body_len=20000, name="test_img_big.bin")
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)

    first = grp.upload(path, max_bytes=4000)
    assert not first.complete
    assert first.off > 2000, "need a large-enough offset for the bug to be obvious"

    calls = []

    import mcumgr.mgmt_image as mi

    class _FakeTime:
        """Replaces mgmt_image's own `time` name (not the real module - other
        code on the same call stack, e.g. mgmt.py's own time.monotonic() use
        for its read deadline, must be unaffected)."""

        def __init__(self, seq):
            self._seq = iter(seq)

        def monotonic(self):
            return next(self._seq)

    real_time = mi.time
    # start_t reads 0.0 once; every later read (one per progress callback)
    # reads 1.0, so elapsed is a fixed, deterministic 1.0s throughout.
    mi.time = _FakeTime([0.0] + [1.0] * 1000)
    try:
        grp.upload(
            path,
            progress_callback=lambda off, total, rate_kbps: calls.append(
                (off, total, rate_kbps)
            ),
        )
    finally:
        mi.time = real_time

    assert calls, "progress_callback was never invoked"
    first_off, _total, first_rate_kbps = calls[0]
    # The old (off/elapsed) formula would report first.off/1024 kB/s here
    # (elapsed pinned to 1.0s) - orders of magnitude more than what could
    # actually have been sent in one chunk this call.
    assert first_rate_kbps * 1024 < first.off, (
        "rate must be based on bytes sent this call, not the absolute "
        "device offset ({} implies the whole prior offset was just sent)".format(
            first_rate_kbps
        )
    )


def test_partial_upload_many_windows():
    """A whole image transferred in small windows still reassembles."""
    path, data, _ = _img_file(body_len=20000, name="test_img_big.bin")
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)

    calls = 0
    res = None
    while res is None or not res.complete:
        res = grp.upload(path, max_bytes=3000)
        calls += 1
        assert calls < 60, "should converge"

    assert bytes(dev.received) == data
    assert res.off == len(data)
    assert calls > 2, "expected several windows"


def test_max_duration_stops_early():
    path, data, _ = _img_file(body_len=20000, name="test_img_big.bin")

    class SlowDevice(FakeDevice):
        def read_msg(self, timeout=None):
            time.sleep(0.02)
            return super().read_msg(timeout)

    dev = SlowDevice(slots=[])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, max_duration=0.05)
    assert not res.complete
    assert res.off < len(data)


def test_reconnect_continues_upload():
    """A dropped link mid-transfer is rebuilt and the transfer carries on."""
    path, data, _ = _img_file()

    class FlappyDevice(FakeDevice):
        drop_at = 3
        reconnects = 0

        def read_msg(self, timeout=None):
            if self._last_id == IMG_MGMT_ID_UPLOAD:
                self.drop_at -= 1
                if self.drop_at == 0:
                    self._pending_rsp = None
                    raise smp.SMPDisconnectedError("Disconnected")
            return super().read_msg(timeout)

        def reconnect(self):
            self.reconnects += 1

    dev = FlappyDevice(slots=[])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, reconnects=3)
    assert res.complete
    assert dev.reconnects == 1, "should have rebuilt the link once"
    assert bytes(dev.received) == data


def test_disconnect_without_reconnect_budget_raises():
    path, _, _ = _img_file()

    class DroppingDevice(FakeDevice):
        def read_msg(self, timeout=None):
            if self._last_id == IMG_MGMT_ID_UPLOAD:
                self._pending_rsp = None
                raise smp.SMPDisconnectedError("Disconnected")
            return super().read_msg(timeout)

    dev = DroppingDevice(slots=[])
    grp = MgmtGrpImage(dev)
    try:
        grp.upload(path, reconnects=0)
    except smp.SMPDisconnectedError:
        pass
    else:
        raise AssertionError("expected SMPDisconnectedError")


def test_reconnect_budget_is_finite():
    path, _, _ = _img_file()

    class AlwaysDropDevice(FakeDevice):
        reconnects = 0

        def read_msg(self, timeout=None):
            if self._last_id == IMG_MGMT_ID_UPLOAD:
                self._pending_rsp = None
                raise smp.SMPDisconnectedError("Disconnected")
            return super().read_msg(timeout)

        def reconnect(self):
            self.reconnects += 1

    dev = AlwaysDropDevice(slots=[])
    grp = MgmtGrpImage(dev)
    try:
        grp.upload(path, reconnects=2)
    except smp.SMPDisconnectedError:
        assert dev.reconnects == 2
    else:
        raise AssertionError("expected SMPDisconnectedError after budget")


def test_ebusy_is_retried():
    """A transient EBUSY (busy flash rail) should be retried, not fatal."""

    class BusyOnceDevice(FakeDevice):
        busy = 2

        def _do_state_write(self, req):
            if self.busy > 0:
                self.busy -= 1
                return {"rc": int(smp.MGMT_ERR.EBUSY)}
            return super()._do_state_write(req)

    h1 = b"\x02" * 32
    dev = BusyOnceDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, h1)])
    grp = MgmtGrpImage(dev)

    import mcumgr.mgmt_image as mi
    old_delay, mi.DEFAULT_EBUSY_DELAY = mi.DEFAULT_EBUSY_DELAY, 0
    try:
        state = grp.test(h1)
    finally:
        mi.DEFAULT_EBUSY_DELAY = old_delay
    assert state.slot(1).pending


def test_persistent_ebusy_still_raises():
    class AlwaysBusyDevice(FakeDevice):
        def _do_state_write(self, req):
            return {"rc": int(smp.MGMT_ERR.EBUSY)}

    h1 = b"\x02" * 32
    dev = AlwaysBusyDevice(slots=[_slot(0, b"\x01" * 32), _slot(1, h1)])
    grp = MgmtGrpImage(dev)

    import mcumgr.mgmt_image as mi
    old_delay, mi.DEFAULT_EBUSY_DELAY = mi.DEFAULT_EBUSY_DELAY, 0
    try:
        grp.test(h1)
    except smp.MgmtEndpointError as e:
        assert e.rc == smp.MGMT_ERR.EBUSY
    else:
        raise AssertionError("expected MgmtEndpointError for persistent EBUSY")
    finally:
        mi.DEFAULT_EBUSY_DELAY = old_delay


def test_upload_image_num_key():
    path, _, _ = _img_file()
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)
    grp.upload(path, image_num=1)

    starts = [
        r[2] for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD and "len" in r[2]
    ]
    assert starts[0]["image"] == 1


def test_upload_rejects_corrupt_image():
    data, _ = _mk_image(body=b"\xc3" * 512)
    data = bytearray(data)
    data[64] ^= 0xFF
    path = "/tmp/test_img_corrupt.bin"
    with open(path, "wb") as f:
        f.write(data)

    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)
    try:
        grp.upload(path)
    except image.ImageError as e:
        assert "corrupt" in str(e)
    else:
        raise AssertionError("expected ImageError for corrupt image")


def test_upload_stall_is_detected():
    """A device stuck on one offset must not spin forever."""

    class StuckDevice(FakeDevice):
        def _do_upload(self, req):
            return {"rc": 0, "off": 64}

    path, _, _ = _img_file()
    dev = StuckDevice(slots=[])
    grp = MgmtGrpImage(dev)

    try:
        grp.upload(path, max_timeouts=2)
    except smp.MgmtEndpointError as e:
        assert "stalled" in str(e)
    else:
        raise AssertionError("expected MgmtEndpointError for a stalled upload")


def test_upload_restarts_on_an_offset_past_file_size():
    """A device offset that cannot belong to this file must not be
    followed - it is the device's leftover context from some other, larger
    upload. Blindly accepting it would exit the loop immediately (since
    off >= file_size right away) and report a bogus complete=True, having
    sent nothing of the actual image.
    """
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[])
    dev.upload_off = 99999  # far beyond this file's size
    dev.total_len = 200000
    dev.received = bytearray(b"\xee" * 99999)

    grp = MgmtGrpImage(dev)
    res = grp.upload(path, resume=True)

    assert res.complete
    assert res.off == len(data)
    assert bytes(dev.received) == data, "must restart, not report a bogus complete"


def test_upload_restarts_on_an_offset_exactly_equal_to_file_size():
    """The boundary case of the above: a device holding a DIFFERENT,
    already-complete upload of the exact same size as this file. Its
    reported offset equals file_size exactly (not beyond it), but on the
    very first response this call (before we have sent anything of our
    own) that is just as impossible to be legitimate progress on THIS
    file as an offset past it - must also force a restart, not report a
    bogus complete=True having sent nothing.
    """
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[])
    dev.upload_off = len(data)  # exactly file_size - an unrelated same-size upload
    dev.total_len = len(data)
    dev.received = bytearray(b"\xdd" * len(data))

    grp = MgmtGrpImage(dev)
    res = grp.upload(path, resume=True)

    assert res.complete
    assert res.off == len(data)
    assert bytes(dev.received) == data, "must restart, not report a bogus complete"


def test_upload_does_not_restart_its_own_completed_transfer():
    """The normal case new_off == file_size legitimately covers: our own
    upload's last chunk lands and the device correctly reports off ==
    file_size on a LATER response this call (after resumed_off is
    already set). Must not be mistaken for the stale-context case above
    and forced to restart right after finishing."""
    path, data, digest = _img_file()
    dev = FakeDevice(slots=[])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, resume=True)

    assert res.complete
    assert res.off == len(data)
    uploads = [r for r in dev.requests if r[1] == IMG_MGMT_ID_UPLOAD]
    # A restart would show a second off=0/len=... request after the first
    # transfer already reached the end - there must be exactly one.
    starts = [r for _op, _id, r in uploads if r.get("off") == 0 and "len" in r]
    assert len(starts) == 1, "must not restart a transfer that just completed"


def test_upload_error_rc_raises():
    class FailingDevice(FakeDevice):
        def _do_upload(self, req):
            return {"rc": int(smp.MGMT_ERR.ENOENT)}

    path, _, _ = _img_file()
    dev = FailingDevice(slots=[])
    grp = MgmtGrpImage(dev)

    try:
        grp.upload(path)
    except smp.MgmtEndpointError as e:
        assert e.rc == smp.MGMT_ERR.ENOENT
        assert "pending" in str(e)
    else:
        raise AssertionError("expected MgmtEndpointError")


def test_upload_retries_transport_timeout():
    class FlakyDevice(FakeDevice):
        """Drops the first two upload responses, answers state normally."""

        fails = 2

        def read_msg(self, timeout=None):
            if self._last_id == IMG_MGMT_ID_UPLOAD and self.fails > 0:
                self.fails -= 1
                self._pending_rsp = None
                raise smp.SMPTransportError("timeout")
            return super().read_msg(timeout)

    path, data, _ = _img_file()
    dev = FlakyDevice(slots=[])
    grp = MgmtGrpImage(dev)

    res = grp.upload(path, max_timeouts=3)
    assert res.off == len(data)


def test_upload_gives_up_after_max_timeouts():
    class DeadDevice(FakeDevice):
        """Answers state, then never answers an upload."""

        def read_msg(self, timeout=None):
            if self._last_id == IMG_MGMT_ID_UPLOAD:
                raise smp.SMPTransportError("timeout")
            return super().read_msg(timeout)

    path, _, _ = _img_file()
    dev = DeadDevice(slots=[])
    grp = MgmtGrpImage(dev)

    try:
        grp.upload(path, max_timeouts=2)
    except smp.SMPTransportError:
        pass
    else:
        raise AssertionError("expected SMPTransportError after max timeouts")


def test_seq_is_shared_across_endpoints():
    """Two endpoints on one transport must not hand out the same nh_seq."""
    dev = FakeDevice(slots=[_slot(0, b"\x01" * 32)])
    grp = MgmtGrpImage(dev)
    grp.get_state()
    grp.erase()
    grp.get_state()

    seqs = [dev._seq._seq]
    assert seqs[0] == 3, "expected 3 distinct sequence numbers to be consumed"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
