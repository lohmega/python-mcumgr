# mcumgr OS management group

from enum import IntEnum

from . import smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint


class OS_MGMT_ID(IntEnum):
    # fmt: off
    ECHO           = 0
    CONS_ECHO_CTRL = 1
    TASKSTAT       = 2
    MPSTAT         = 3
    DATETIME_STR   = 4
    RESET          = 5
    # fmt: on

    @staticmethod
    def int_to_str(val):
        try:
            return OS_MGMT_ID(val).name
        except ValueError:
            return "{}.<unknown {}>".format(OS_MGMT_ID.__name__, val)


class MgmtGrpOs(MgmtGrpBase):
    """OS Management Group"""

    nh_group = smp.MGMT_GROUP_ID.OS

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_echo = MgmtGrpEndpoint(transport, self.nh_group, OS_MGMT_ID.ECHO)
        self.mh_taskstats = MgmtGrpEndpoint(
            transport, self.nh_group, OS_MGMT_ID.TASKSTAT
        )
        self.mh_reset = MgmtGrpEndpoint(transport, self.nh_group, OS_MGMT_ID.RESET)
        self.mh_datetime = MgmtGrpEndpoint(
            transport, self.nh_group, OS_MGMT_ID.DATETIME_STR
        )

    def echo(self, text, timeout=None):
        """Round trip a string through the device. Returns the echoed string."""
        if not isinstance(text, str):
            raise TypeError("echo text must be str")
        rsp = self.mh_echo.mh_write({"d": text}, check=True, timeout=timeout)
        return rsp.get("r", "")

    def reset(self, timeout=None):
        """Reboot the device.

        The device may reset before answering, so a timeout while waiting for
        the response is not necessarily a failure - but a failure to send the
        request at all still is. check=True so an explicit rejection (e.g.
        ENOTSUP, EACCESSDENIED) is not silently treated as a successful
        reset just because tolerate_no_response covers the no-answer case.
        """
        return self.mh_reset.mh_write(
            timeout=timeout, check=True, tolerate_no_response=True
        )

    def taskstats(self, timeout=None):
        rsp = self.mh_taskstats.mh_read(check=True, timeout=timeout)
        return rsp.get("tasks", {})

    def datetime(self, timeout=None):
        rsp = self.mh_datetime.mh_read(check=True, timeout=timeout)
        return rsp.get("datetime")
