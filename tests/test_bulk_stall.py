"""The bulk-only transport recovers from a STALL, and a silent device isn't asked about every page."""

import errno
import struct

from podsync.hardware.probes import libusb, scsi


class _Stall(OSError):
    def __init__(self):
        super().__init__(errno.EPIPE, "Pipe error")


class _In:
    def __init__(self, replies):
        self.replies, self.cleared = list(replies), 0

    def read(self, length, timeout=None):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def clear_halt(self):
        self.cleared += 1


class _Out:
    def write(self, data, timeout=None):
        return len(data)


def _csw(status=0):
    return struct.pack("<4sIIB", b"USBS", 2, 0, status)


def test_a_stalled_data_stage_is_cleared_and_the_status_still_read():
    ep_in = _In([_Stall(), _csw()])
    assert libusb._bulk_inquiry(None, _Out(), ep_in, 2, True, 0xC0, 255) is None
    assert ep_in.cleared == 1 and not ep_in.replies       # the status was read: in step for the next one


def test_a_reply_after_a_stall_comes_through():
    ep_in = _In([_Stall(), _csw(), b"\x00\xc0\x00\x01\xc2", _csw()])
    assert libusb._bulk_inquiry(None, _Out(), ep_in, 2, True, 0xC0, 255) is None
    assert libusb._bulk_inquiry(None, _Out(), ep_in, 3, True, 0xC0, 255) == b"\x00\xc0\x00\x01\xc2"


def test_without_a_page_list_a_silent_device_is_asked_three_times_not_sixty():
    asked = []

    def silent(evpd, page, length):
        asked.append(page)
        raise TimeoutError

    assert scsi.read_vpd_payload(silent) == b""
    assert asked == [0xC0, 0xC2, 0xC3, 0xC4]
