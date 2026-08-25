"""mcumgr Image management group (MGMT_GROUP_ID.IMAGE = 1).

Follows libmcumgr's `src/image_mgmt.c` and the upload state machine in
`cli/mcumgr/cmd_image.c`.
"""

import logging
import time
from collections import namedtuple

from . import image, smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint
from .smp import MgmtEndpointError

logger = logging.getLogger(__name__)

# Image Management Command IDs
IMG_MGMT_ID_STATE = 0
IMG_MGMT_ID_UPLOAD = 1
IMG_MGMT_ID_FILE = 2
IMG_MGMT_ID_CORELIST = 3
IMG_MGMT_ID_CORELOAD = 4
IMG_MGMT_ID_ERASE = 5

# The first upload request carries "len" (and possibly "sha"), so it must stay
# small - the link MTU has not been probed yet at that point. Once the device
# has replied with a non-zero offset we switch to max_mtu minus this same
# margin, which covers the SMP header plus the CBOR map around the data.
UPLOAD_FIRST_CHUNK_SIZE = 32
UPLOAD_CHUNK_OVERHEAD = 32

# Where a resume probe starts. Sending a chunk at this offset *without* "len"
# makes the device answer with the offset it actually expects, which is how a
# partial upload is picked up again. See cmd_image.c.
UPLOAD_PROBE_OFFSET = 32

DEFAULT_TIMEOUT = 5.0
DEFAULT_ERASE_TIMEOUT = 15.0
DEFAULT_MAX_TIMEOUTS = 3

# A device can answer EBUSY to a slot-mutating command because the flash it
# needs is momentarily unavailable - a power-managed external flash rail that
# has to be woken, or another subsystem holding the slot. Those are meant to be
# retried; a persistent EBUSY means something really does own the slot.
DEFAULT_EBUSY_RETRIES = 3
DEFAULT_EBUSY_DELAY = 2.0


class ImageSlot(
    namedtuple(
        "ImageSlot",
        "image slot version hash bootable pending confirmed active permanent",
    )
):
    """One entry of the `images` array in an image state response."""

    __slots__ = ()

    @classmethod
    def from_dict(cls, d):
        return cls(
            image=d.get("image", 0),
            slot=d.get("slot", 0),
            version=d.get("version", ""),
            hash=d.get("hash", b""),
            bootable=bool(d.get("bootable", False)),
            pending=bool(d.get("pending", False)),
            confirmed=bool(d.get("confirmed", False)),
            active=bool(d.get("active", False)),
            permanent=bool(d.get("permanent", False)),
        )

    @property
    def flags(self):
        names = ("bootable", "pending", "confirmed", "active", "permanent")
        return [n for n in names if getattr(self, n)]

    def format(self):
        return "  Slot {}: v:{} {}{}".format(
            self.slot,
            self.version,
            self.hash.hex() if self.hash else "",
            "".join(" " + f for f in self.flags),
        )


class ImageState:
    """Decoded response of an image state read."""

    def __init__(self, response):
        self.response = response
        self.images = [
            ImageSlot.from_dict(d) for d in response.get("images", [])
        ]
        self.split_status = response.get("splitStatus", 0)

    def slot(self, n, image_num=0):
        """Slot n of image `image_num`, or None if the device did not report it.

        Multi-image devices report one slot-0/slot-1 pair per image; without
        filtering by `image` a lookup can silently match a different image's
        slot that happens to come first in the response.
        """
        for img in self.images:
            if img.slot == n and img.image == (image_num or 0):
                return img
        return None

    def find_by_hash(self, img_hash):
        for img in self.images:
            if img.hash and img.hash == img_hash:
                return img
        return None

    def format(self):
        lines = ["Image state:"]
        lines.extend(img.format() for img in self.images)
        return "\n".join(lines)


class UploadResult(
    namedtuple(
        "UploadResult",
        "off size resumed_off already_present already_in_slot complete",
    )
):
    """Outcome of an upload.

    `off` is the device-reported next offset, i.e. how many bytes it now
    holds. `complete` is False when the transfer stopped early because a
    byte/time budget ran out - call upload() again to carry on from there.
    `resumed_off` is where this call started once the device had its say,
    which is non-zero when it picked up a partial upload.
    """

    __slots__ = ()

    @property
    def remaining(self):
        return max(self.size - self.off, 0)

    @property
    def percent(self):
        return 100.0 * self.off / self.size if self.size else 100.0


UploadResult.__new__.__defaults__ = (None, True)


class MgmtGrpImage(MgmtGrpBase):
    """Image Management Group (MGMT_GROUP_ID.IMAGE = 1)

    Firmware image management: read image state, upload an image, erase a
    slot, and mark an image test (pending) or confirmed.
    """

    nh_group = smp.MGMT_GROUP_ID.IMAGE

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_state = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_STATE)
        self.mh_upload = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_UPLOAD)
        self.mh_erase = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_ERASE)

    # -- state ---------------------------------------------------------------

    def get_state(self, timeout=DEFAULT_TIMEOUT):
        """Read the image state.

        Returns an ImageState. Raises MgmtEndpointError on a non-zero rc.
        """
        rsp = self.mh_state.mh_read({"m": "m"}, check=True, timeout=timeout)
        return ImageState(rsp)

    def set_state(
        self,
        img_hash=None,
        confirm=False,
        timeout=DEFAULT_TIMEOUT,
        retries=DEFAULT_EBUSY_RETRIES,
        image_num=None,
    ):
        """Mark an image pending (test) or confirmed.

        Args:
            img_hash: 32 byte image hash. If None:
                      - confirm=True: no hash is sent at all. The device's
                        own img_mgmt handler resolves an absent hash to
                        "confirm whatever is currently active" - correct
                        both before and after a test boot's swap, unlike
                        guessing a hash client-side ever could be (see
                        below). This is a real, intentional feature of the
                        wire protocol, not a fallback: the device rejects a
                        hashless test() outright (IMG_MGMT_ERR_INVALID_HASH),
                        precisely because "confirm active" is unambiguous
                        but "test active" is not - so only confirm() gets
                        this treatment.
                      - confirm=False (test): the hash of slot 1 is read
                        from the device and used - i.e. "the image I just
                        uploaded", which is what the mcumgr CLI does. A
                        hashless test() is invalid per the protocol, so
                        there is no equivalent to omit here.
            confirm:  False marks the image pending (boots once, reverts
                      unless confirmed). True confirms it permanently.
            image_num: which image's slot 1 to default img_hash from, on a
                      multi-image device, for confirm=False only. Only used
                      when img_hash is None.

        Returns the ImageState reported after the write.
        """
        if img_hash is None and not confirm:
            state = self.get_state(timeout=timeout)
            slot1 = state.slot(1, image_num)
            if slot1 is None or not slot1.hash:
                raise MgmtEndpointError("No image in slot 1 to mark")
            img_hash = slot1.hash
            logger.debug("using slot 1 hash %s", img_hash.hex())

        data = {}
        if img_hash is not None:
            data["hash"] = self._coerce_hash(img_hash)
        if confirm:
            data["confirm"] = True

        rsp = self._retry_ebusy(
            lambda: self.mh_state.mh_write(data, check=True, timeout=timeout),
            retries,
        )
        return ImageState(rsp)

    def test(self, img_hash=None, timeout=DEFAULT_TIMEOUT, image_num=None):
        """Mark an image pending: boot it once, revert unless confirmed."""
        return self.set_state(
            img_hash, confirm=False, timeout=timeout, image_num=image_num
        )

    def confirm(self, img_hash=None, timeout=DEFAULT_TIMEOUT, image_num=None):
        """Confirm an image permanently.

        See set_state() on why you usually want to pass img_hash explicitly
        when confirming after a test boot.
        """
        return self.set_state(
            img_hash, confirm=True, timeout=timeout, image_num=image_num
        )

    @staticmethod
    def _coerce_hash(img_hash):
        if isinstance(img_hash, str):
            img_hash = bytes.fromhex(img_hash)
        if not isinstance(img_hash, (bytes, bytearray)):
            raise TypeError("hash must be bytes or a hex string")
        if len(img_hash) != image.IMAGE_HASH_LEN:
            raise ValueError(
                "hash must be {} bytes, got {}".format(
                    image.IMAGE_HASH_LEN, len(img_hash)
                )
            )
        return bytes(img_hash)

    # -- erase ---------------------------------------------------------------

    def erase(self, slot=1, timeout=DEFAULT_ERASE_TIMEOUT, retries=DEFAULT_EBUSY_RETRIES):
        """Erase a slot. Can take >10s, hence the longer default timeout."""
        return self._retry_ebusy(
            lambda: self.mh_erase.mh_write({"slot": slot}, check=True, timeout=timeout),
            retries,
        )

    @staticmethod
    def _retry_ebusy(fn, retries):
        for attempt in range(retries + 1):
            try:
                return fn()
            except MgmtEndpointError as e:
                if e.rc != smp.MGMT_ERR.EBUSY or attempt == retries:
                    raise
                logger.info(
                    "device busy (attempt %d/%d), retrying in %.0fs",
                    attempt + 1,
                    retries,
                    DEFAULT_EBUSY_DELAY,
                )
                time.sleep(DEFAULT_EBUSY_DELAY)

    # -- upload --------------------------------------------------------------

    @property
    def _max_chunk(self):
        max_mtu = getattr(self.transport, "max_mtu", smp.MGMT_MAX_MTU)
        # Always reserve the SMP header + CBOR map overhead, even on a small
        # or un-negotiated MTU (e.g. BLE's un-negotiated default of 23) -
        # returning the whole MTU as data there produced a request bigger
        # than the link could carry, guaranteed to fail on write. A floor of
        # 1 keeps this from going to zero/negative on a degenerate MTU
        # instead of silently never making progress.
        return max(max_mtu - UPLOAD_CHUNK_OVERHEAD, 1)

    def upload(
        self,
        file_path,
        image_num=None,
        progress_callback=None,
        timeout=DEFAULT_TIMEOUT,
        max_timeouts=DEFAULT_MAX_TIMEOUTS,
        verify=True,
        resume=True,
        max_bytes=None,
        max_duration=None,
        reconnects=0,
    ):
        """Upload a firmware image to the device's secondary slot.

        The offset is driven entirely by the device: every response carries the
        offset it expects next, which is what makes an interrupted upload
        resumable. The first request is deliberately sent at
        UPLOAD_PROBE_OFFSET with no "len" key, so a device holding a partial
        upload answers with its real offset instead of restarting from zero.

        Args:
            file_path: path to the MCUboot image
            image_num: image number for multi-image devices (omitted if None)
            progress_callback: called as f(offset, total_size, rate_kbps)
            timeout: per-request timeout in seconds
            max_timeouts: consecutive timeouts tolerated before giving up
            verify: parse the image and refuse to upload a corrupt one
            resume: check device state first to skip or resume work
            max_bytes: stop cleanly after transferring about this many bytes
            max_duration: stop cleanly after about this many seconds
            reconnects: how many times to reconnect and carry on if the link
                        drops mid-transfer (needs transport.reconnect())

        max_bytes/max_duration exist for links that are only up in short
        windows: stop while the link is still good, then call upload() again
        later and the device says where to continue from. The result's
        `complete` field says whether the image is fully transferred.

        Returns an UploadResult.
        """
        info = image.image_info(file_path)
        if verify and not info.hash_ok:
            raise image.ImageError(
                "Image '{}' is corrupt, hash fail".format(file_path)
            )

        with open(file_path, "rb") as f:
            data = f.read()

        file_size = len(data)
        resumed_off = None
        chunk_size = self._max_chunk

        if resume:
            off = UPLOAD_PROBE_OFFSET
            skip, off, in_slot = self._plan_upload(info, off, timeout, image_num)
            if skip:
                return UploadResult(
                    off=file_size,
                    size=file_size,
                    resumed_off=file_size,
                    already_present=True,
                    already_in_slot=in_slot,
                    complete=True,
                )
        else:
            # Do not probe the device's offset at all: any context it holds
            # (its own stale partial upload of a different file, say) is
            # irrelevant to a caller that explicitly asked to always start
            # from scratch. off=0 with "len" below forces the device to
            # discard whatever it had and start a fresh transfer.
            off = 0
            # This off=0/"len" request is the very first one sent on this
            # upload - same situation UPLOAD_FIRST_CHUNK_SIZE exists for on
            # the probe path: keep it small since the link has not proven it
            # can carry a full-size write yet. Clamped to _max_chunk too - on
            # a small/un-negotiated MTU even 32 bytes of data can be too much
            # once wrapped.
            chunk_size = min(UPLOAD_FIRST_CHUNK_SIZE, self._max_chunk)

        num_timeouts = 0
        start_t = time.monotonic()
        stalled = 0
        reconnects_left = reconnects
        sent = 0
        stopped_early = False

        while off < file_size:
            # A budget of 0 (or already-elapsed max_duration) must not stop
            # the call before the very first round trip - off is still the
            # synthetic UPLOAD_PROBE_OFFSET at that point, not a real device
            # offset, and reporting it as the result's `off` would be a
            # meaningless placeholder rather than reality (the device's real
            # offset could be 0, or far larger from earlier sessions).
            # resumed_off is only set once a genuine response has updated
            # off, so gate the budget checks on that.
            if resumed_off is not None:
                if max_bytes is not None and sent >= max_bytes:
                    logger.info("byte budget reached at offset %d/%d", off, file_size)
                    stopped_early = True
                    break

                if max_duration is not None and (time.monotonic() - start_t) >= max_duration:
                    logger.info("time budget reached at offset %d/%d", off, file_size)
                    stopped_early = True
                    break

            chunk = data[off : off + chunk_size]
            if not chunk:
                break

            payload = {"off": off, "data": chunk}
            if off == 0:
                # Only the request that actually starts the transfer declares
                # the total length.
                payload["len"] = file_size
                if image_num is not None:
                    payload["image"] = image_num

            try:
                rsp = self.mh_upload.mh_write(payload, timeout=timeout)
            except smp.SMPDisconnectedError:
                # Retrying a write on a dead link cannot succeed; the link has
                # to be rebuilt first. Whether the device still has our offset
                # is up to its firmware - we re-send the same request after
                # reconnecting and let it correct us.
                if reconnects_left <= 0:
                    logger.info(
                        "link lost at offset %d/%d; reconnect and upload again "
                        "to continue",
                        off,
                        file_size,
                    )
                    raise

                reconnects_left -= 1
                logger.warning(
                    "link lost at offset %d/%d, reconnecting (%d left)",
                    off,
                    file_size,
                    reconnects_left,
                )
                if not self._reconnect_transport():
                    raise
                # The new link may have negotiated a smaller MTU than the one
                # chunk_size was computed for, and the chunk about to be
                # re-sent is built before any response refreshes it. Clamp it
                # now; growing again is left to the next response so that a
                # deliberately small first chunk stays small.
                chunk_size = min(chunk_size, self._max_chunk)
                continue
            except smp.SMPTransportError as e:
                num_timeouts += 1
                logger.warning("upload timeout %d/%d: %s", num_timeouts, max_timeouts, e)
                if num_timeouts > max_timeouts:
                    raise
                continue

            num_timeouts = 0

            rc = rsp.get("rc", 0)
            if rc:
                rsn = rsp.get("rsn")
                if rc == smp.MGMT_ERR.ENOENT:
                    rsn = rsn or "image pending? try erasing slot 1 first"
                raise MgmtEndpointError(
                    "Upload failed at offset {}".format(off), rc=rc, rsn=rsn
                )

            if "off" not in rsp:
                raise MgmtEndpointError(
                    "Upload response at offset {} has no 'off' field".format(off)
                )

            new_off = rsp["off"]
            if new_off > file_size:
                # Cannot be a legitimate resume point for THIS file - it is
                # the device's own leftover context from some other, larger
                # upload. Blindly continuing from here would either splice
                # new bytes onto an unrelated prefix, or (since off would
                # already be >= file_size) exit the loop immediately and
                # report a bogus `complete=True` having sent nothing at all.
                # Force a clean restart instead.
                logger.warning(
                    "device offset %d exceeds this image's size %d - "
                    "forcing a restart from 0",
                    new_off,
                    file_size,
                )
                new_off = 0
            if resumed_off is None:
                resumed_off = new_off
                if new_off not in (0, off):
                    logger.info("device resumed upload at offset %d", new_off)

            # The device answering 0 means it has no upload context, so the
            # next request is the real first one: keep it small and carry
            # "len". Otherwise open up to the full MTU. Clamped to
            # _max_chunk - on a small/un-negotiated MTU even
            # UPLOAD_FIRST_CHUNK_SIZE bytes of data can be too much once
            # wrapped in the SMP header and CBOR map.
            chunk_size = (
                min(UPLOAD_FIRST_CHUNK_SIZE, self._max_chunk)
                if new_off == 0
                else self._max_chunk
            )

            if new_off <= off and not (off == UPLOAD_PROBE_OFFSET and new_off == 0):
                # No forward progress. Tolerate a couple of these (a device may
                # legitimately re-request an offset) but do not spin forever,
                # which is what an earlier version of this loop could do.
                stalled += 1
                if stalled > max_timeouts:
                    raise MgmtEndpointError(
                        "Upload stalled at offset {} (device keeps asking for {})".format(
                            off, new_off
                        )
                    )
            else:
                stalled = 0

            sent += len(chunk)
            off = new_off

            if progress_callback:
                elapsed = time.monotonic() - start_t
                # `sent` (bytes actually transmitted THIS call), not `off`
                # (the absolute device offset) - a resumed upload's off can
                # already be far into the file from earlier calls/sessions,
                # which `off / elapsed` would count as if it had all just
                # been sent in the last few seconds, wildly overstating the
                # rate on the very first callback after a resume.
                rate_kbps = (sent / elapsed / 1024) if elapsed > 0 else 0
                progress_callback(off, file_size, rate_kbps)

        return UploadResult(
            off=off,
            size=file_size,
            resumed_off=resumed_off if resumed_off is not None else 0,
            already_present=False,
            already_in_slot=None,
            complete=(off >= file_size) and not stopped_early,
        )

    def _reconnect_transport(self):
        """Rebuild the link. Returns False if the transport cannot do it."""
        reconnect = getattr(self.transport, "reconnect", None)
        if reconnect is None:
            logger.warning("transport has no reconnect(), cannot continue")
            return False

        try:
            reconnect()
        except Exception as e:
            logger.warning("reconnect failed: %s", e)
            return False
        return True

    def _plan_upload(self, info, off, timeout, image_num=None):
        """Check device state before uploading.

        Returns (skip, start_offset, slot_it_is_already_in).
        """
        state = self.get_state(timeout=timeout)

        slot0 = state.slot(0, image_num)
        if slot0 is not None and slot0.hash == info.calc_hash:
            logger.info("image already running in device")
            return True, off, 0

        slot1 = state.slot(1, image_num)
        if slot1 is not None and slot1.hash == info.calc_hash:
            if slot1.pending:
                # Already staged and marked for boot - a fresh upload would
                # have to start over anyway.
                logger.info("image already in device, and pending")
                return False, 0, None

            # The slot hash is read from the image trailer, which is the last
            # thing written, so a matching hash means the whole image is
            # already there. libmcumgr re-uploads anyway; there is no point,
            # and over BLE it costs minutes. Mark it test/confirm instead.
            logger.info("image already uploaded to slot 1, skipping transfer")
            return True, off, 1

        return False, off, None
