"""Unit tests for mcumgr.transport_serial - NLIP framing, CRC and reader thread.

No hardware: the pyserial `Serial` class is replaced by a fake port whose
`readline()` is driven from the test.

Runs standalone (`python3 test/test_transport_serial.py`) or under pytest.
"""

import base64
import logging
import os
import queue
import struct
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

# these tests deliberately feed in garbage; no need to log about it
logging.getLogger("mcumgr").setLevel(logging.CRITICAL)

from unittest.mock import patch

from mcumgr import smp, transport_serial
from mcumgr.transport_serial import NLIP_OP, NlipPkt, SMPTransportSerial

PKT_START = struct.pack(">BB", NLIP_OP.PKT_START1, NLIP_OP.PKT_START2)
DATA_START = struct.pack(">BB", NLIP_OP.DATA_START1, NLIP_OP.DATA_START2)


def _crc(data):
    return NlipPkt()._crc(data)


def _wire_frames(data, crc=None, chunk_size=84):
    """Build raw NLIP wire lines for `data`, independently of pack_lines()."""
    if crc is None:
        crc = _crc(data)
    pktdata = struct.pack(">H", len(data) + 2) + data + struct.pack(">H", crc)
    chunks = [pktdata[i : i + chunk_size] for i in range(0, len(pktdata), chunk_size)]

    lines = [PKT_START + base64.b64encode(chunks[0]) + b"\n"]
    for chunk in chunks[1:]:
        lines.append(DATA_START + base64.b64encode(chunk) + b"\n")
    return lines


def _parse_all(lines):
    """Feed lines to a fresh parser, return the single decoded payload."""
    nlip = NlipPkt()
    out = None
    for line in lines:
        got = nlip.parse_line(line)
        if got is not None:
            assert out is None, "more than one packet decoded"
            out = got
    return out


# -- fake serial port --------------------------------------------------------

_CLOSED = object()


class FakeSerial:
    """The subset of pyserial's Serial that transport_serial actually uses."""

    def __init__(self, port=None, baudrate=None, timeout=None, **kwargs):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.written = bytearray()
        self.flushed = 0
        self.line_on_close = None
        self._q = queue.Queue()

    def feed(self, line):
        self._q.put(line)

    def readline(self):
        # Blocks like a real port with a long timeout, until fed or closed.
        item = self._q.get(timeout=10)
        if item is _CLOSED:
            # pyserial aborts a blocked read when the port is closed
            raise OSError("attempting to use a port that is not open")
        if isinstance(item, Exception):
            # feed()ing an exception simulates a real port failure (e.g. a
            # genuine serial.SerialException for an unplugged cable),
            # distinct from the intentional-close OSError above.
            raise item
        return item

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.is_open = False
        if self.line_on_close is not None:
            self._q.put(self.line_on_close)
            self.line_on_close = None
        self._q.put(_CLOSED)


class FakeSerialFactory:
    """Stands in for `serial.Serial`, remembering every port it hands out."""

    def __init__(self):
        self.ports = []

    def __call__(self, port=None, **kwargs):
        ser = FakeSerial(port=port, **kwargs)
        self.ports.append(ser)
        return ser

    @property
    def last(self):
        return self.ports[-1]


def _mk_transport(factory, **kwargs):
    kwargs.setdefault("timeout", 1)
    return SMPTransportSerial(port="/dev/fake", **kwargs)


def _msg(seq=0, payload=None):
    m = smp.MgmtMsg(nh_op=smp.MGMT_OP.READ_RSP, nh_group=1, nh_id=2, nh_seq=seq)
    m.encode_payload(payload if payload is not None else {"rc": 0})
    return m


# -- framing -----------------------------------------------------------------


def test_pack_roundtrip_single_frame():
    data = b"hello nlip"
    lines = NlipPkt().pack_lines(data)
    assert len(lines) == 1
    assert lines[0].startswith(PKT_START)
    assert lines[0].endswith(b"\n")
    assert _parse_all(lines) == data


def test_pack_roundtrip_multi_frame():
    """A payload too big for one line is split into start + continuation."""
    data = bytes(range(256)) * 2
    lines = NlipPkt().pack_lines(data)
    assert len(lines) > 1
    assert lines[0].startswith(PKT_START)
    for line in lines[1:]:
        assert line.startswith(DATA_START)
        assert len(line) < NlipPkt.MAX_DATA_PER_LINE
    assert _parse_all(lines) == data


def test_partial_frame_yields_nothing_until_complete():
    data = bytes(range(200))
    lines = NlipPkt().pack_lines(data)
    nlip = NlipPkt()
    for line in lines[:-1]:
        assert nlip.parse_line(line) is None
    assert nlip.parse_line(lines[-1]) == data


def test_decode_handcrafted_wire_bytes():
    """Decode bytes built to the wire format, not by our own packer."""
    data = b"\x01\x02\x03\x04 raw wire payload"
    assert _parse_all(_wire_frames(data)) == data


def test_decode_handcrafted_wire_bytes_multi_frame():
    data = bytes([i % 251 for i in range(300)])
    frames = _wire_frames(data)
    assert len(frames) > 1
    assert _parse_all(frames) == data


def test_unknown_start_sequence_is_ignored():
    nlip = NlipPkt()
    assert nlip.parse_line(b"some shell output\n") is None
    # ...and the parser still works afterwards
    assert _parse_all_into(nlip, _wire_frames(b"after noise")) == b"after noise"


def _parse_all_into(nlip, lines):
    out = None
    for line in lines:
        got = nlip.parse_line(line)
        if got is not None:
            out = got
    return out


def test_empty_line_is_ignored():
    assert NlipPkt().parse_line(b"") is None


# -- CRC ---------------------------------------------------------------------


def test_good_crc_is_accepted_and_stripped():
    data = b"payload with crc"
    out = _parse_all(_wire_frames(data))
    # exactly the payload, no CRC bytes tacked on the end
    assert out == data
    assert len(out) == len(data)


def test_bad_crc_is_rejected():
    """Regression: a corrupted frame used to be accepted silently because the
    CRC was neither checked nor stripped - MgmtMsg.from_bytes just sliced it
    off by nh_len."""
    data = b"payload with crc"
    bad = (_crc(data) ^ 0xFFFF) & 0xFFFF
    frames = _wire_frames(data, crc=bad)

    nlip = NlipPkt()
    try:
        for line in frames:
            nlip.parse_line(line)
    except smp.SMPTransportError as e:
        assert "crc" in str(e).lower()
    else:
        raise AssertionError("expected SMPTransportError on crc mismatch")


def test_corrupt_body_is_rejected():
    """Same bug, from the other side: body damaged, CRC trailer intact."""
    data = bytearray(b"payload with crc")
    crc = _crc(bytes(data))
    data[0] ^= 0xFF
    frames = _wire_frames(bytes(data), crc=crc)

    try:
        _parse_all(frames)
    except smp.SMPTransportError:
        pass
    else:
        raise AssertionError("expected SMPTransportError for corrupted body")


def test_too_short_for_crc_is_not_a_packet():
    """A length that cannot hold the CRC trailer yields no message."""
    nlip = NlipPkt()
    line = PKT_START + base64.b64encode(struct.pack(">H", 1) + b"\x00") + b"\n"
    assert nlip.parse_line(line) is None


# -- reader thread -----------------------------------------------------------


def test_reader_delivers_message():
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        try:
            factory.last.feed(NlipPkt().pack(_msg(seq=3).to_bytes()))
            out = t.read_msg(timeout=5)
            assert out.hdr.nh_seq == 3
            assert out.decode_payload() == {"rc": 0}
        finally:
            t.disconnect()


def test_reader_survives_a_corrupt_frame():
    """One bad frame must not kill the reader thread or wedge the stream."""
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        thread = t._read_thread
        try:
            good = _msg(seq=9, payload={"rc": 0, "ok": True})
            bad_crc = (_crc(b"garbage") ^ 0xA5A5) & 0xFFFF
            for line in _wire_frames(b"garbage", crc=bad_crc):
                factory.last.feed(line)
            # a truncated continuation frame right after, still mid-stream
            factory.last.feed(DATA_START + base64.b64encode(b"\x01\x02") + b"\n")
            for line in NlipPkt().pack_lines(good.to_bytes()):
                factory.last.feed(line)

            # the bad frames surface as errors to the caller...
            try:
                t.read_msg(timeout=5)
            except smp.SMPTransportError as e:
                assert "crc" in str(e).lower()
            else:
                raise AssertionError("expected the corrupt frame to be reported")

            # ...and the reader is still alive and delivers the next message,
            # whatever the desynced frames in between produced.
            out = None
            for _ in range(5):
                try:
                    out = t.read_msg(timeout=5)
                except Exception:
                    continue
                break
            assert thread.is_alive(), "reader thread died on a corrupt frame"
            assert out is not None, "good message never arrived after a bad one"
            assert out.hdr.nh_seq == 9
            assert out.decode_payload()["ok"] is True
        finally:
            t.disconnect()


def test_port_failure_is_reported_as_disconnected():
    """readline() only raises when the port itself is broken (a plain
    timeout returns an empty/partial read here, not an exception) - that
    must surface as SMPDisconnectedError specifically, not a raw
    SerialException/OSError, since mgmt_image.upload()'s reconnect
    handling only triggers on that type. Queueing the raw exception meant
    a real serial disconnect fell through to the plain-timeout retry path
    instead and never used the reconnects budget at all."""
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        try:
            import serial as pyserial

            factory.last.feed(pyserial.SerialException("device disconnected"))

            try:
                t.read_msg(timeout=5)
            except smp.SMPDisconnectedError:
                pass
            else:
                raise AssertionError("expected SMPDisconnectedError")
        finally:
            t.disconnect()


def test_parser_errors_are_wrapped_as_response_error():
    """Any frame-parsing failure (bad base64, an out-of-place continuation
    frame, a truncated header, ...) means a frame WAS received - not "no
    response" - and must surface as SMPResponseError specifically, so a
    caller can tell it apart from a genuine timeout (see mgmt.py's
    tolerate_no_response). A raw binascii.Error/AssertionError/struct.error
    reaching read_msg() would also escape main()'s exception chain as an
    uncaught traceback instead of the documented transport-error exit."""
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        try:
            # A DATA_START continuation frame with no preceding PKT_START -
            # desyncs the parser (previously a bare AssertionError).
            factory.last.feed(DATA_START + base64.b64encode(b"\x01\x02") + b"\n")

            try:
                t.read_msg(timeout=5)
            except smp.SMPResponseError:
                pass
            else:
                raise AssertionError("expected SMPResponseError")
        finally:
            t.disconnect()


def test_disconnect_stops_the_reader_thread():
    """Regression (reconnect race): disconnect() must not return while the old
    reader thread can still push onto the shared queue."""
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        thread = t._read_thread
        assert thread.is_alive()

        # A line that arrives exactly as the port is being closed. It belongs
        # to the connection going away and must never reach the queue.
        factory.last.line_on_close = NlipPkt().pack(_msg(seq=1).to_bytes())

        t.disconnect()

        assert not thread.is_alive(), "reader thread outlived disconnect()"
        assert t._read_thread is None
        assert t._read_msg_q.empty(), "stale line leaked onto the shared queue"


def test_reconnect_starts_a_fresh_reader_and_drains_the_queue():
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        old_thread = t._read_thread
        old_port = factory.last

        # queue up a leftover from the old link
        old_port.feed(NlipPkt().pack(_msg(seq=1).to_bytes()))
        t._read_msg_q.get(timeout=5)  # make sure the reader ran at least once
        old_port.feed(NlipPkt().pack(_msg(seq=2).to_bytes()))

        t.reconnect()
        try:
            assert not old_thread.is_alive()
            assert t._read_thread is not None and t._read_thread.is_alive()
            assert t._read_thread is not old_thread
            assert len(factory.ports) == 2
            assert factory.last is not old_port
            assert t._read_msg_q.empty(), "stale message survived reconnect()"

            # the new link works
            factory.last.feed(NlipPkt().pack(_msg(seq=7).to_bytes()))
            assert t.read_msg(timeout=5).hdr.nh_seq == 7
        finally:
            t.disconnect()


def test_write_emits_nlip_frames():
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        try:
            msg = _msg(seq=5)
            t.write_msg(msg)
            written = bytes(factory.last.written)
            assert written.startswith(PKT_START)
            assert factory.last.flushed == 1

            lines = [ln + b"\n" for ln in written.split(b"\n") if ln]
            assert _parse_all(lines) == msg.to_bytes()
        finally:
            t.disconnect()


def test_read_msg_times_out():
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        t = _mk_transport(factory)
        t.connect()
        try:
            t.read_msg(timeout=0.1)
        except smp.SMPTransportError as e:
            assert "No response" in str(e)
        else:
            raise AssertionError("expected a timeout")
        finally:
            t.disconnect()


def test_no_port_raises():
    try:
        SMPTransportSerial()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a port")


def test_device_is_an_alias_for_port():
    t = SMPTransportSerial(device="/dev/fake")
    assert t._port == "/dev/fake"


def test_context_manager_connects_and_disconnects():
    factory = FakeSerialFactory()
    with patch.object(transport_serial.serial, "Serial", factory):
        with _mk_transport(factory) as t:
            assert t.is_connected()
            thread = t._read_thread
        assert not t.is_connected()
        assert not thread.is_alive()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("{} passed".format(len(tests)))


if __name__ == "__main__":
    main()
