# mcumgr SMP Proxy/Forward transport wrapper

import cbor2 as cbor
import logging
import time
from . import smp
from .mgmt_proxy_ble import MgmtGrpProxyBle

logger = logging.getLogger(__name__)

# Proxy management group IDs
MGMT_GROUP_ID_PROXY_FWD_MGMT = 255
PROXY_FWD_MGMT_ID_FWD = 1

# CBOR keys for proxy protocol
PROXY_MGMT_KEY_MEDIA = "m"
PROXY_MGMT_KEY_ADDR = "a"
PROXY_MGMT_KEY_DATA = "d"
PROXY_MGMT_KEY_WAIT = "w"


class SmpProxyTransport:
    """
    Transport wrapper that encapsulates SMP messages in proxy forward protocol.

    This wrapper allows sending SMP messages through a proxy/dongle device that
    forwards them to a target device over a different transport (e.g., BLE).

    The proxy protocol wraps the original SMP message in a CBOR envelope with:
    - media: transport type identifier (e.g., "ble")
    - address: target device address
    - data: the complete original SMP message as a bytestring
    - wait: optional timeout for response in milliseconds
    """

    def __init__(self, base_transport, address, media="ble", timeout=5000):
        """
        Initialize proxy transport wrapper.

        Args:
            base_transport: Underlying transport to the proxy device (e.g., BleTransport)
            media: Media/transport identifier string (e.g., "ble")
            address: Target device address (uint64)
            timeout: Response timeout in milliseconds (default: 5000)
        """
        self.base_transport = base_transport
        self.media = media
        self.address = address
        self.timeout = timeout
        # The outer proxy envelope IS a message on base_transport, exactly
        # like e.g. MgmtGrpProxyBle's scan/connect control commands sharing
        # the same connection - so its nh_seq must come from
        # base_transport.next_seq() too, not an independent counter. Two
        # counters both starting at 0 would let an outer envelope reuse a
        # seq a control response is still in flight for, and the stale-seq
        # filter in read_msg() below would then let that stale response
        # through as if it were a match. The seq for messages addressed to
        # the wrapped end device is a genuinely separate conversation (it
        # only exists inside the envelope's decoded payload, invisible to
        # base_transport's own queue) and keeps its own counter.
        self._target_seq = smp.SeqCounter()
        # nh_seq of the outer proxy envelope write_msg() most recently sent,
        # so read_msg() can tell a stale/late envelope (e.g. the response to
        # a request that already timed out) from the one it is actually
        # waiting for. Both carry group=MGMT_GROUP_ID_PROXY_FWD_MGMT,
        # id=PROXY_FWD_MGMT_ID_FWD regardless of which end-device request
        # they wrap, so that check alone cannot tell them apart.
        self._last_outer_seq = None
        assert(media == "ble")
        self.ble = MgmtGrpProxyBle(base_transport)

    def next_seq(self):
        """Next nh_seq for messages addressed to the end device.

        SmpProxyTransport is itself used as a transport by the management
        groups, so it must provide this rather than inheriting the proxy
        device's counter through __getattr__.
        """
        return self._target_seq.next()

    # Overhead of wrapping a message in the proxy envelope: the outer SMP
    # header (8 bytes) plus the CBOR map around it - measured empirically at
    # ~12 bytes worst case (4 keys, "a" as a full 8-byte-wide uint for a
    # 48-bit BLE address, "d"'s length prefix at this MTU range never needing
    # more than 2 bytes since MGMT_MAX_MTU is 1024). The old estimate of 24
    # only covered part of the CBOR overhead and omitted the outer SMP header
    # entirely, so a chunk sized off it could exceed the base transport's
    # real MTU. Comfortable margin included.
    PROXY_ENVELOPE_OVERHEAD = 40

    @property
    def max_mtu(self):
        base_mtu = getattr(self.base_transport, "max_mtu", smp.MGMT_MAX_MTU)
        return max(base_mtu - self.PROXY_ENVELOPE_OVERHEAD, 32)

    def connect(self):
        if not self.base_transport.is_connected():
            raise RuntimeError("base tranport not connected - can not communicate with proxy")

        return self.ble.connect(self.address, self.timeout)

    def disconnect(self):
        return self.ble.disconnect()

    def write_msg(self, msg):
        """
        Wrap the SMP message in proxy envelope and send through base transport.

        Args:
            msg: MgmtMsg to send
        """
        # Convert original message to bytes
        original_msg_bytes = msg.to_bytes()

        # Create proxy wrapper message
        proxy_msg = smp.MgmtMsg()
        proxy_msg.hdr.nh_op = smp.MGMT_OP.WRITE
        proxy_msg.hdr.nh_group = MGMT_GROUP_ID_PROXY_FWD_MGMT
        proxy_msg.hdr.nh_id = PROXY_FWD_MGMT_ID_FWD
        proxy_msg.hdr.nh_seq = self.base_transport.next_seq()
        self._last_outer_seq = proxy_msg.hdr.nh_seq

        # Build CBOR payload with proxy envelope
        proxy_payload = {
            PROXY_MGMT_KEY_MEDIA: self.media,
            PROXY_MGMT_KEY_ADDR: self.address,
            PROXY_MGMT_KEY_WAIT: self.timeout,
            PROXY_MGMT_KEY_DATA: original_msg_bytes  # Complete SMP message as bytestring
        }

        proxy_msg.set_payload(cbor.dumps(proxy_payload))

        logger.debug(f"Wrapped SMP message for proxy: media={self.media}, addr=0x{self.address:x}, size={len(original_msg_bytes)}")

        # Send through base transport
        self.base_transport.write_msg(proxy_msg)

    def read_msg(self, timeout=None):
        """
        Read proxy response and unwrap to extract original SMP response.

        Args:
            timeout: Optional timeout override

        Returns:
            MgmtMsg: Unwrapped SMP response message

        Raises:
            SMPTransportError: If proxy response is invalid or malformed
            MgmtEndpointError: If proxy returns an error code
        """
        # Read proxy response from base transport, discarding any stale
        # envelope that does not answer the write_msg() we are pairing with -
        # bounded by the same timeout budget, the same approach
        # MgmtGrpEndpoint._read_matching() uses for the non-proxied case.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise smp.SMPTransportError(
                        "No proxy response with seq {} within {}s".format(
                            self._last_outer_seq, timeout
                        )
                    )

            msg = self.base_transport.read_msg(remaining)

            # nh_seq is drawn from base_transport.next_seq() - the same
            # counter every conversation sharing that transport uses (e.g.
            # MgmtGrpProxyBle's scan/connect control commands) - so a
            # mismatch here is conclusive regardless of group/id: it cannot
            # be the answer to the envelope we are waiting for, whether it
            # is a late FWD response to an earlier, abandoned write_msg()
            # or a late response to some other conversation entirely (a
            # timed-out scan/connect control request, say).
            if msg.hdr.nh_seq != self._last_outer_seq:
                logger.debug(
                    "discarding stale response group=%s id=%s seq=%s (want %s)",
                    msg.hdr.nh_group,
                    msg.hdr.nh_id,
                    msg.hdr.nh_seq,
                    self._last_outer_seq,
                )
                continue

            break

        # Validate this is a proxy forward response
        if msg.hdr.nh_group != MGMT_GROUP_ID_PROXY_FWD_MGMT:
            raise smp.SMPTransportError(
                f"Expected proxy forward response (group={MGMT_GROUP_ID_PROXY_FWD_MGMT}), "
                f"got group={msg.hdr.nh_group}"
            )

        if msg.hdr.nh_id != PROXY_FWD_MGMT_ID_FWD:
            raise smp.SMPTransportError(
                f"Expected proxy forward response (id={PROXY_FWD_MGMT_ID_FWD}), "
                f"got id={msg.hdr.nh_id}"
            )

        # Decode CBOR payload
        if not msg.payload:
            raise smp.SMPTransportError("Empty proxy response payload")

        proxy_data = cbor.loads(msg.payload)

        # Check for proxy-level error
        if "rc" in proxy_data:
            rc = proxy_data["rc"]
            if rc != 0:
                rsn = proxy_data.get("rsn")
                raise smp.MgmtEndpointError("Proxy forwarding failed", rc=rc, rsn=rsn)

        # Extract the wrapped SMP message data
        if PROXY_MGMT_KEY_DATA not in proxy_data:
            raise smp.SMPTransportError("No data field in proxy response (possible timeout)")

        original_msg_bytes = proxy_data[PROXY_MGMT_KEY_DATA]

        logger.debug(f"Unwrapped SMP response from proxy: size={len(original_msg_bytes)}")

        # Parse and return the original SMP message
        return smp.MgmtMsg.from_bytes(original_msg_bytes)


    def __enter__(self):
        """Context manager entry - connect to target device via proxy"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - disconnect from target device"""
        try:
            self.disconnect()
        except Exception as e:
            logger.warning(f"Error during disconnect: {e}")
        return False

    def __getattr__(self, name):
        """Delegate all other attributes/methods to base transport"""
        return getattr(self.base_transport, name)
