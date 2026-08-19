"""MCUboot image header/TLV parsing.

Port of libmcumgr's `src/image_util.c` + `include/mcumgr/image.h`.

Knowing the image layout is what makes a sane upload possible: the SHA256 in
the image trailer is the same hash the device reports for each slot, so it is
how you tell "already running", "already uploaded and pending" and "partially
uploaded, resume" apart before sending a single byte.

All image structures are little endian.
"""

import hashlib
import struct
from collections import namedtuple

IMAGE_MAGIC = 0x96F3B83D
IMAGE_MAGIC_V1 = 0x96F3B83C
IMAGE_MAGIC_NONE = 0xFFFFFFFF

IMAGE_TLV_INFO_MAGIC = 0x6907
# Protected TLVs (MCUboot >= 1.6). Covered by the image hash, unlike the
# regular trailer TLVs. libmcumgr does not handle these and rejects such an
# image; we parse them so signed/encrypted images hash correctly.
IMAGE_TLV_PROT_INFO_MAGIC = 0x6908

IMAGE_HEADER_SIZE = 32
IMAGE_HASH_LEN = 32

# Image header flags
IMAGE_F_PIC = 0x00000001
IMAGE_F_ENCRYPTED = 0x00000004
IMAGE_F_NON_BOOTABLE = 0x00000010

# Image trailer TLV types
IMAGE_TLV_KEYHASH = 0x01
IMAGE_TLV_SHA256 = 0x10
IMAGE_TLV_RSA2048_PSS = 0x20
IMAGE_TLV_ECDSA224 = 0x21
IMAGE_TLV_ECDSA256 = 0x22
IMAGE_TLV_RSA3072_PSS = 0x23
IMAGE_TLV_ED25519 = 0x24
IMAGE_TLV_ENC_RSA2048 = 0x30
IMAGE_TLV_ENC_KW128 = 0x31

_TLV_TYPE_NAMES = {
    IMAGE_TLV_KEYHASH: "KEYHASH",
    IMAGE_TLV_SHA256: "SHA256",
    IMAGE_TLV_RSA2048_PSS: "RSA2048_PSS",
    IMAGE_TLV_ECDSA224: "ECDSA224",
    IMAGE_TLV_ECDSA256: "ECDSA256",
    IMAGE_TLV_RSA3072_PSS: "RSA3072_PSS",
    IMAGE_TLV_ED25519: "ED25519",
    IMAGE_TLV_ENC_RSA2048: "ENC_RSA2048",
    IMAGE_TLV_ENC_KW128: "ENC_KW128",
}


def tlv_type_str(it_type):
    return _TLV_TYPE_NAMES.get(it_type, "0x{:02x}".format(it_type))


class ImageError(Exception):
    """Raised when a file is not a usable MCUboot image."""


ImageTlv = namedtuple("ImageTlv", ["it_type", "value", "protected"])


class ImageVersion(namedtuple("ImageVersion", "major minor revision build_num")):
    __slots__ = ()

    def __str__(self):
        s = "{}.{}.{}".format(self.major, self.minor, self.revision)
        if self.build_num:
            s += ".{}".format(self.build_num)
        return s


class ImageHeader:
    """struct image_header - 32 bytes, little endian."""

    _STRUCT_FMT = "<IIHHIIBBHII"
    BYTE_SIZE = IMAGE_HEADER_SIZE

    def __init__(
        self,
        ih_magic=0,
        ih_load_addr=0,
        ih_hdr_size=0,
        _pad1=0,
        ih_img_size=0,
        ih_flags=0,
        iv_major=0,
        iv_minor=0,
        iv_revision=0,
        iv_build_num=0,
        _pad2=0,
    ):
        self.ih_magic = ih_magic
        self.ih_load_addr = ih_load_addr
        self.ih_hdr_size = ih_hdr_size
        self.ih_img_size = ih_img_size
        self.ih_flags = ih_flags
        self.ih_ver = ImageVersion(iv_major, iv_minor, iv_revision, iv_build_num)

    @classmethod
    def from_bytes(cls, data):
        if len(data) < cls.BYTE_SIZE:
            raise ImageError(
                "Image is too small: {} bytes < {}".format(len(data), cls.BYTE_SIZE)
            )
        return cls(*struct.unpack(cls._STRUCT_FMT, data[: cls.BYTE_SIZE]))

    @property
    def magic_ok(self):
        return self.ih_magic in (IMAGE_MAGIC, IMAGE_MAGIC_V1)

    @property
    def magic_str(self):
        if self.ih_magic == IMAGE_MAGIC:
            return "v2 ok"
        if self.ih_magic == IMAGE_MAGIC_V1:
            return "v1 ok"
        return "err"

    @property
    def bootable(self):
        return not (self.ih_flags & IMAGE_F_NON_BOOTABLE)

    @property
    def encrypted(self):
        return bool(self.ih_flags & IMAGE_F_ENCRYPTED)


class ImageInfo:
    """A parsed MCUboot image."""

    def __init__(self, data, name=None):
        self.name = name
        self.size = len(data)
        self.hdr = ImageHeader.from_bytes(data)

        if not self.hdr.magic_ok:
            raise ImageError(
                "Not an MCUboot image, ih_magic=0x{:08x}".format(self.hdr.ih_magic)
            )

        self.tlvs = []
        self._parse_tlvs(data)

        # The SHA256 TLV covers the header, the image body and any protected
        # TLV area - i.e. everything up to the unprotected trailer.
        self.calc_hash = hashlib.sha256(data[: self._hash_len]).digest()

    @property
    def img_hash(self):
        """SHA256 from the image trailer, or None if the image has no such TLV."""
        for tlv in self.tlvs:
            if tlv.it_type == IMAGE_TLV_SHA256:
                return tlv.value
        return None

    @property
    def hash_ok(self):
        """True if the trailer hash matches the hash computed over the file."""
        img_hash = self.img_hash
        return img_hash is not None and img_hash == self.calc_hash

    def _parse_tlv_area(self, data, offset, protected):
        """Parse one tlv_info block. Returns the offset just past it."""
        info = data[offset : offset + 4]
        if len(info) < 4:
            raise ImageError("Truncated TLV info at offset {}".format(offset))

        it_magic, it_tlv_tot = struct.unpack("<HH", info)
        expect = IMAGE_TLV_PROT_INFO_MAGIC if protected else IMAGE_TLV_INFO_MAGIC
        if it_magic != expect:
            return None, it_magic

        end = offset + it_tlv_tot
        if end > len(data):
            raise ImageError(
                "TLV area runs past end of image ({} > {})".format(end, len(data))
            )

        pos = offset + 4
        while pos + 4 <= end:
            it_type, _pad, it_len = struct.unpack("<BBH", data[pos : pos + 4])
            pos += 4
            value = bytes(data[pos : pos + it_len])
            if len(value) < it_len:
                raise ImageError("Truncated TLV type=0x{:02x}".format(it_type))
            self.tlvs.append(ImageTlv(it_type, value, protected))
            pos += it_len

        return end, it_magic

    def _parse_tlvs(self, data):
        offset = self.hdr.ih_hdr_size + self.hdr.ih_img_size
        if offset > len(data):
            raise ImageError(
                "Image body runs past end of file ({} > {})".format(offset, len(data))
            )

        # Optional protected TLV area first - it is included in the hash.
        end, magic = self._parse_tlv_area(data, offset, protected=True)
        if end is not None:
            offset = end

        # Everything hashed: header + body + protected TLVs.
        self._hash_len = offset

        end, magic = self._parse_tlv_area(data, offset, protected=False)
        if end is None:
            raise ImageError("Invalid TLV info it_magic=0x{:04x}".format(magic))

    @classmethod
    def from_file(cls, filename):
        with open(filename, "rb") as f:
            data = f.read()
        return cls(data, name=filename)

    def format(self):
        """Multi-line human readable dump, like `mcumgr image dump`."""
        hdr = self.hdr
        lines = [
            "file_size:    {}".format(self.size),
            "ih_magic:     0x{:08x} {}".format(hdr.ih_magic, hdr.magic_str),
            "ih_load_addr: 0x{:08x}".format(hdr.ih_load_addr),
            "ih_hdr_size:  {}".format(hdr.ih_hdr_size),
            "ih_img_size:  {}".format(hdr.ih_img_size),
            "ih_flags:     0x{:08x}{}".format(
                hdr.ih_flags, " encrypted" if hdr.encrypted else ""
            ),
            "ih_ver:       {}".format(hdr.ih_ver),
        ]

        for tlv in self.tlvs:
            lines.append(
                "TLV_{:<9} {}{}".format(
                    tlv_type_str(tlv.it_type) + ":",
                    tlv.value.hex(),
                    " (protected)" if tlv.protected else "",
                )
            )

        if self.img_hash is None:
            lines.append("SHA256:       <no hash TLV in image>")
        elif self.hash_ok:
            lines.append("SHA256:       OK")
        else:
            lines.append("SHA256:       ERR (calc: {})".format(self.calc_hash.hex()))

        return "\n".join(lines)


def image_info(filename):
    """Parse an MCUboot image file. Raises ImageError if it is not one."""
    return ImageInfo.from_file(filename)


def image_dump_info(filename):
    print(image_info(filename).format())
