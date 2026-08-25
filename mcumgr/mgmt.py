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
            except smp.SMPResponseError:
                # A response DID arrive but failed to validate - a real
                # transport integrity problem, not the benign "device
                # rebooted before it could answer" case this flag exists
                # for. Must not be silently swallowed the same way.
                raise
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

        if rsp.payload:
            try:
                response = cbor.loads(rsp.payload)
            except cbor.CBORDecodeError as e:
                # A response WAS received, its CBOR is just broken - a real
                # transport integrity problem, matching SMPResponseError's
                # contract (see smp.py). Leaving this as a raw decoder
                # exception meant no caller in this codebase's exception
                # chain (including main()'s) ever caught it, so a single
                # corrupted response produced a traceback instead of the
                # documented transport-error exit.
                raise smp.SMPResponseError(
                    "Malformed CBOR in response: {}".format(e)
                ) from e
            if not isinstance(response, dict):
                # Valid CBOR, just not a map - an SMP response body is
                # always one. The membership/`.get()` calls below assume a
                # dict; a list or bare scalar decoding successfully would
                # otherwise reach them and raise a raw TypeError/
                # AttributeError instead of the documented transport error.
                raise smp.SMPResponseError(
                    "Response payload is not a CBOR map: {!r}".format(response)
                )
        else:
            response = {}

        # Check for error code in response if requested. Modern SMP v2
        # group-error responses (smp_add_cmd_err() device-side - used by
        # e.g. an app-level MGMT_EVT_OP_CMD_RECV hook rejecting a command
        # via some means other than a plain nonzero handler rc) put the
        # error under "err": {"group":.., "rc":..} instead of a top-level
        # "rc" - the ordinary case for a handler's own nonzero return
        # (image erase/confirm/upload/os reset all go through this path in
        # every real device tested against this branch) still uses the
        # legacy top-level "rc" this always checked, so this is additive.
        if check:
            rc = None
            rsn = None
            if "rc" in response:
                rc = response["rc"]
                rsn = response.get("rsn")
            elif isinstance(response.get("err"), dict) and "rc" in response["err"]:
                err = response["err"]
                rc = err["rc"]
                rsn = "group {}".format(err["group"]) if "group" in err else None
            if rc:
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
