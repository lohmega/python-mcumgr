# mcumgr management group and endpoint classes

import cbor
from . import smp


class MgmtGrpEndpoint:
    """Represents a single endpoint (nh_group, nh_id pair)"""

    def __init__(self, transport, nh_group, nh_id):
        self.transport = transport
        self.nh_group = nh_group
        self.nh_id = nh_id
        self._seq = 0

    def _communicate(self, op, payload_data=None):
        """Execute a command with the specified operation and payload"""
        req = smp.MgmtMsg()
        req.hdr.nh_op = op
        req.hdr.nh_group = self.nh_group
        req.hdr.nh_id = self.nh_id
        req.hdr.nh_seq = self._seq
        self._seq += 1

        if payload_data:
            data = cbor.dumps(payload_data)
            req.set_payload(data)

        self.transport.write_msg(req)
        rsp = self.transport.read_msg()

        if rsp.hdr.nh_seq != req.hdr.nh_seq:
            raise RuntimeError("bad sequence nr")

        return cbor.loads(rsp.payload) if rsp.payload else {}

    def mh_read(self, payload_data=None):
        """Perform READ operation"""
        return self._communicate(smp.MGMT_OP.READ, payload_data)

    def mh_write(self, payload_data):
        """Perform WRITE operation"""
        return self._communicate(smp.MGMT_OP.WRITE, payload_data)


class MgmtGrpBase:
    """Base class for SMP management groups"""

    nh_group = None  # Subclasses must set this

    def __init__(self, transport):
        self.transport = transport
        if self.nh_group is None:
            raise NotImplementedError("Subclasses must set nh_group")
