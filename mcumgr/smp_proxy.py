# mcumgr SMP Proxy/Forward transport wrapper

import cbor
import logging
from . import smp

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

    def __init__(self, base_transport, media, address, timeout=5000):
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
        self._seq = 0

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
        proxy_msg.hdr.nh_seq = self._seq
        self._seq += 1

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
        # Read proxy response from base transport
        msg = self.base_transport.read_msg(timeout)

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

    def __getattr__(self, name):
        """Delegate all other attributes/methods to base transport"""
        return getattr(self.base_transport, name)
