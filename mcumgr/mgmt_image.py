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

    def slot(self, n):
        """Slot n, or None if the device did not report it."""
        for img in self.images:
            if img.slot == n:
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


UploadResult = namedtuple("UploadResult", "off size resumed_off already_present")


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

    def set_state(self, img_hash=None, confirm=False, timeout=DEFAULT_TIMEOUT):
        """Mark an image pending (test) or confirmed.

        Args:
            img_hash: 32 byte image hash. If None, the hash of slot 1 is read
                      from the device and used - i.e. "the image I just
                      uploaded", which is what the mcumgr CLI does.
            confirm:  False marks the image pending (boots once, reverts
                      unless confirmed). True confirms it permanently.

        Returns the ImageState reported after the write.
        """
        if img_hash is None:
            state = self.get_state(timeout=timeout)
            slot1 = state.slot(1)
            if slot1 is None or not slot1.hash:
                raise MgmtEndpointError("No image in slot 1 to mark")
            img_hash = slot1.hash
            logger.debug("using slot 1 hash %s", img_hash.hex())

        img_hash = self._coerce_hash(img_hash)

        data = {"hash": img_hash}
        if confirm:
            data["confirm"] = True

        rsp = self.mh_state.mh_write(data, check=True, timeout=timeout)
        return ImageState(rsp)

    def test(self, img_hash=None, timeout=DEFAULT_TIMEOUT):
        """Mark an image pending: boot it once, revert unless confirmed."""
        return self.set_state(img_hash, confirm=False, timeout=timeout)

    def confirm(self, img_hash=None, timeout=DEFAULT_TIMEOUT):
        """Confirm an image permanently."""
        return self.set_state(img_hash, confirm=True, timeout=timeout)

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

    def erase(self, slot=1, timeout=DEFAULT_ERASE_TIMEOUT):
        """Erase a slot. Can take >10s, hence the longer default timeout."""
        return self.mh_erase.mh_write({"slot": slot}, check=True, timeout=timeout)

    # -- upload --------------------------------------------------------------

    @property
    def _max_chunk(self):
        max_mtu = getattr(self.transport, "max_mtu", smp.MGMT_MAX_MTU)
        if max_mtu >= 64:
            return max_mtu - UPLOAD_CHUNK_OVERHEAD
        return max_mtu

    def upload(
        self,
        file_path,
        image_num=None,
        progress_callback=None,
        timeout=DEFAULT_TIMEOUT,
        max_timeouts=DEFAULT_MAX_TIMEOUTS,
        verify=True,
        resume=True,
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
        off = UPLOAD_PROBE_OFFSET
        resumed_off = None
        chunk_size = self._max_chunk

        if resume:
            skip, off = self._plan_upload(info, off, timeout)
            if skip:
                return UploadResult(
                    off=file_size,
                    size=file_size,
                    resumed_off=file_size,
                    already_present=True,
                )

        num_timeouts = 0
        start_t = time.monotonic()
        stalled = 0

        while off < file_size:
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
            if resumed_off is None:
                resumed_off = new_off
                if new_off not in (0, off):
                    logger.info("device resumed upload at offset %d", new_off)

            # The device answering 0 means it has no upload context, so the
            # next request is the real first one: keep it small and carry
            # "len". Otherwise open up to the full MTU.
            chunk_size = UPLOAD_FIRST_CHUNK_SIZE if new_off == 0 else self._max_chunk

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

            off = new_off

            if progress_callback:
                elapsed = time.monotonic() - start_t
                rate_kbps = (off / elapsed / 1024) if elapsed > 0 else 0
                progress_callback(off, file_size, rate_kbps)

        return UploadResult(
            off=off,
            size=file_size,
            resumed_off=resumed_off if resumed_off is not None else 0,
            already_present=False,
        )

    def _plan_upload(self, info, off, timeout):
        """Check device state before uploading.

        Returns (skip, start_offset).
        """
        state = self.get_state(timeout=timeout)

        slot0 = state.slot(0)
        if slot0 is not None and slot0.hash == info.calc_hash:
            logger.info("image already running in device")
            return True, off

        slot1 = state.slot(1)
        if slot1 is not None and slot1.hash == info.calc_hash:
            if slot1.pending:
                # Already staged and marked for boot - a fresh upload would
                # have to start over anyway.
                logger.info("image already in device, and pending")
                return False, 0
            logger.info("partial or unmarked image already in device, resuming")
            return False, UPLOAD_PROBE_OFFSET

        return False, off
