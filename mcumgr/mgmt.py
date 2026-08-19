# mcumgr management group and endpoint classes

import cbor2 as cbor
from . import smp


class MgmtGrpEndpoint:
    """Represents a single endpoint (nh_group, nh_id pair)"""

    def __init__(self, transport, nh_group, nh_id):
        self.transport = transport
        self.nh_group = nh_group
        self.nh_id = nh_id

    def _communicate(self, op, data=None, check=False, timeout=None):
        """Execute a command with the specified operation and payload

        Args:
            op: Operation type (READ or WRITE)
            data: Optional data to send in request
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc' field

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

        if data:
            cbor_data = cbor.dumps(data)
            req.set_payload(cbor_data)

        self.transport.write_msg(req)
        rsp = self.transport.read_msg(timeout=timeout)

        if rsp.hdr.nh_seq != req.hdr.nh_seq:
            raise smp.MgmtEndpointError(
                "Sequence number mismatch: sent {}, got {}".format(
                    req.hdr.nh_seq, rsp.hdr.nh_seq
                )
            )

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

    def mh_read(self, data=None, check=False, timeout=None):
        """Perform READ operation

        Args:
            data: Optional data to send
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc'
        """
        return self._communicate(smp.MGMT_OP.READ, data, check=check, timeout=timeout)

    def mh_write(self, data=None, check=False, timeout=None):
        """Perform WRITE operation

        Args:
            data: data to send
            check: If True, raise MgmtEndpointError if response contains non-zero 'rc'
        """
        return self._communicate(smp.MGMT_OP.WRITE, data, check=check, timeout=timeout)


class MgmtGrpBase:
    """Base class for SMP management groups"""

    nh_group = None  # Subclasses must set this

    def __init__(self, transport):
        self.transport = transport
        if self.nh_group is None:
            raise NotImplementedError("Subclasses must set nh_group")
