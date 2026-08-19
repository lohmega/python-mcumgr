"""Unit tests for mcumgr.image - MCUboot header/TLV parsing.

Runs standalone (`python3 test/test_image.py`) or under pytest.
"""

import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from mcumgr import image


def _mk_image(hdr_size=32, body=b"\xa5" * 256, version=(1, 2, 3, 4), protected=False):
    """Build a minimal but valid MCUboot image with a correct SHA256 TLV."""
    hdr = struct.pack(
        "<IIHHIIBBHII",
        image.IMAGE_MAGIC,
        0,  # load_addr
        hdr_size,
        0,  # pad1
        len(body),
        0,  # flags
        version[0],
        version[1],
        version[2],
        version[3],
        0,  # pad2
    )
    hdr += b"\x00" * (hdr_size - len(hdr))
    data = hdr + body

    if protected:
        # one protected TLV (type 0x40), included in the hash
        ptlv = struct.pack("<BBH", 0x40, 0, 4) + b"prot"
        data += struct.pack("<HH", image.IMAGE_TLV_PROT_INFO_MAGIC, 4 + len(ptlv))
        data += ptlv

    digest = hashlib.sha256(data).digest()

    tlv = struct.pack("<BBH", image.IMAGE_TLV_SHA256, 0, len(digest)) + digest
    data += struct.pack("<HH", image.IMAGE_TLV_INFO_MAGIC, 4 + len(tlv)) + tlv
    return data, digest


def test_parse_basic():
    data, digest = _mk_image()
    info = image.ImageInfo(data)

    assert info.hdr.ih_magic == image.IMAGE_MAGIC
    assert info.hdr.magic_ok
    assert info.hdr.ih_hdr_size == 32
    assert info.hdr.ih_img_size == 256
    assert str(info.hdr.ih_ver) == "1.2.3.4"
    assert info.size == len(data)
    assert info.img_hash == digest
    assert info.calc_hash == digest
    assert info.hash_ok


def test_version_without_build_num():
    data, _ = _mk_image(version=(0, 4, 2, 0))
    assert str(image.ImageInfo(data).hdr.ih_ver) == "0.4.2"


def test_large_header():
    """Zephyr signs with a 512 byte header."""
    data, digest = _mk_image(hdr_size=512)
    info = image.ImageInfo(data)
    assert info.hdr.ih_hdr_size == 512
    assert info.hash_ok
    assert info.img_hash == digest


def test_protected_tlvs_are_hashed():
    """Protected TLVs sit before the trailer and are covered by the hash."""
    data, digest = _mk_image(protected=True)
    info = image.ImageInfo(data)

    assert info.hash_ok
    assert info.img_hash == digest
    assert [t.protected for t in info.tlvs] == [True, False]
    assert info.tlvs[0].value == b"prot"


def test_corrupt_body_fails_hash():
    data, _ = _mk_image()
    data = bytearray(data)
    data[64] ^= 0xFF  # flip a bit in the body

    info = image.ImageInfo(bytes(data))
    assert not info.hash_ok
    assert info.img_hash != info.calc_hash


def test_bad_magic_rejected():
    data, _ = _mk_image()
    data = bytearray(data)
    data[0:4] = struct.pack("<I", 0xDEADBEEF)

    try:
        image.ImageInfo(bytes(data))
    except image.ImageError as e:
        assert "0xdeadbeef" in str(e)
    else:
        raise AssertionError("expected ImageError for bad magic")


def test_too_small_rejected():
    try:
        image.ImageInfo(b"\x00" * 8)
    except image.ImageError as e:
        assert "too small" in str(e)
    else:
        raise AssertionError("expected ImageError for short file")


def test_truncated_tlv_area_rejected():
    data, _ = _mk_image()
    try:
        image.ImageInfo(data[:-8])
    except image.ImageError:
        pass
    else:
        raise AssertionError("expected ImageError for truncated TLV area")


def test_format_runs():
    data, _ = _mk_image()
    out = image.ImageInfo(data).format()
    assert "ih_magic:" in out
    assert "SHA256:       OK" in out


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
