# mcumgr management group and endpoint classes

import logging
import time

import cbor2 as cbor

from . import smp

logger = logging.getLogger(__name__)


class MgmtGrpEndpoint:
    """Represents a single endpoint (nh_group, nh_id pair)"""

    def __init__(self, transport, nh_group, nh_id):
        self.transport = transport
        self.nh_group = nh_group
        self.nh_id = nh_id

    def _communicate(
        self, op, data=None, check=False, timeout=None, tolerate_no_response=False
    ):
        """Execute a command with the specified operation and payload

        Args:
            op: Operation type (READ or WRITE)
            data: Optional data to send in request
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc' field
            tolerate_no_response: If True, a timeout while waiting for the
                response (after the request was sent successfully) is not an
                error - returns {} instead. A failure to send the request at
                all still raises. For commands like reset, where the device
                may act before it can answer.

        Returns:
            dict: Decoded CBOR response payload

        Raises:
            MgmtEndpointError: If sequence number mismatch or if check=True and rc != 0
        """
        req = smp.MgmtMsg()
        req.hdr.nh_op = op
        req.hdr.nh_group = self.nh_group
        req.hdr.nh_id = self.nh_id
        req.hdr.nh_seq = self.transport.next_seq()

        if data is not None:
            # `data={}` must still encode an explicit empty CBOR map - `if
            # data:` would treat it the same as omitting the argument and
            # send no payload at all, a different wire message.
            cbor_data = cbor.dumps(data)
            req.set_payload(cbor_data)

        self.transport.write_msg(req)

        if tolerate_no_response:
            try:
                rsp = self._read_matching(req, timeout)
            except smp.SMPTransportError:
                logger.info("no response after write, treating as success")
                return {}
        else:
            rsp = self._read_matching(req, timeout)

        if rsp.hdr.nh_group != req.hdr.nh_group:
            raise smp.MgmtEndpointError(
                "Group mismatch: sent {}, got {}".format(
                    req.hdr.nh_group, rsp.hdr.nh_group
                )
            )

        if rsp.hdr.nh_id != req.hdr.nh_id:
            raise smp.MgmtEndpointError(
                "Command id mismatch: sent {}, got {}".format(
                    req.hdr.nh_id, rsp.hdr.nh_id
                )
            )

        response = cbor.loads(rsp.payload) if rsp.payload else {}

        # Check for error code in response if requested
        if check and "rc" in response:
            rc = response["rc"]
            if rc != 0:
                rsn = response.get("rsn")
                raise smp.MgmtEndpointError("SMP command failed", rc=rc, rsn=rsn)

        return response

    def _read_matching(self, req, timeout):
        """Read until the response for `req` arrives.

        A request that timed out and got re-sent leaves its late response in
        flight. Failing the next command on that stale reply would turn one
        slow response into a cascade of errors, so responses carrying an
        older sequence number are discarded and we keep waiting - bounded by
        the same timeout budget, so a device that only ever answers with the
        wrong sequence still fails rather than hanging.
        """
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + timeout

        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise smp.SMPTransportError(
                        "No response with seq {} within {}s".format(
                            req.hdr.nh_seq, timeout
                        )
                    )

            rsp = self.transport.read_msg(timeout=remaining)

            if rsp.hdr.nh_seq == req.hdr.nh_seq:
                return rsp

            logger.debug(
                "discarding stale response seq=%d (waiting for %d)",
                rsp.hdr.nh_seq,
                req.hdr.nh_seq,
            )

    def mh_read(self, data=None, check=False, timeout=None):
        """Perform READ operation

        Args:
            data: Optional data to send
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc'
        """
        return self._communicate(smp.MGMT_OP.READ, data, check=check, timeout=timeout)

    def mh_write(self, data=None, check=False, timeout=None, tolerate_no_response=False):
        """Perform WRITE operation

        Args:
            data: data to send
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc'
            tolerate_no_response: see _communicate()
        """
        return self._communicate(
            smp.MGMT_OP.WRITE,
            data,
            check=check,
            timeout=timeout,
            tolerate_no_response=tolerate_no_response,
        )


class MgmtGrpBase:
    """Base class for SMP management groups"""

    nh_group = None  # Subclasses must set this

    def __init__(self, transport):
        self.transport = transport
        if self.nh_group is None:
            raise NotImplementedError("Subclasses must set nh_group")
