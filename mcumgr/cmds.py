from . import smp
from .mgmt_os import OS_MGMT_ID


class MgmtGroupCmd():
    def __init__(self, transport, nh_group):
        self.transport = transport
        self.nh_group = nh_group


def _get_rsp_rc(rsp):
    d = rsp.decode_payload()
    if 'rc' in d:
        return d['rc']
    return None


def cmd(transport, req, string):

    req.encode_payload({"d": string })

    transport.write_msg(req)
    rsp = transport.read_msg()
    if rsp.hdr.nh_group != req.hdr.nh_group:
        raise Exception()

    if rsp.hdr.nh_id != req.hdr.nh_id:
        raise Exception()

    if req.hdr.nh_op == smp.MGMT_OP.WRITE:
        rc = _get_rsp_rc(rsp)
        if rc != 0:
            raise Exception("write response rc={}".format(rc) )

    return rsp

def os_echo(transport, string):

    req = smp.MgmtMsg()
    req.hdr.nh_group = smp.MGMT_GROUP_ID.OS
    req.hdr.nh_op = smp.MGMT_OP.READ
    req.hdr.nh_id = OS_MGMT_ID.ECHO

    req.encode_payload({"d": string })

    transport.write_msg(req)
    rsp = transport.read_msg()
    if rsp.hdr.nh_group != req.hdr.nh_group:
        raise Exception()
    if rsp.hdr.nh_id != req.hdr.nh_id:
        raise Exception()

    print(vars(rsp.hdr))
    print(rsp.payload.hex())
    print(rsp.decode_payload())

