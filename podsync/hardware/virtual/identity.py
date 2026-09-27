"""A volume identity for virtual iPods that survives remounts but not replacement.

A virtual iPod lives in an ordinary directory, so the host volume's identity
would match every virtual iPod on that disk.  The identity is narrowed to the
directory (device + inode) and pinned to the marker file's inode, change time
and size, so recreating the virtual iPod invalidates earlier write locks.
"""

from __future__ import annotations

import os
import stat
from dataclasses import replace

from podsync.hardware.safety.fsprofile import VolumeIdentity, VolumeProfile

__all__ = ["VIRTUAL_IPOD_INFO_FILENAME", "virtual_ipod_profile"]

VIRTUAL_IPOD_INFO_FILENAME = "iPodInfo.json"


def virtual_ipod_profile(host_profile: VolumeProfile, ipod_path) -> VolumeProfile:
    """*host_profile* re-rooted at the virtual iPod; unchanged when there is no marker file."""
    try:
        root = os.path.realpath(ipod_path)
        root_st = os.stat(root)
        marker_st = os.stat(os.path.join(root, VIRTUAL_IPOD_INFO_FILENAME))
    except OSError:
        return host_profile
    if not stat.S_ISREG(marker_st.st_mode):
        return host_profile
    pin = (marker_st.st_dev, marker_st.st_ino, marker_st.st_ctime_ns, marker_st.st_size)
    identity = VolumeIdentity(
        operating_system="virtual",
        device_id=str(root_st.st_dev),
        volume_id=str(root_st.st_ino),
        mount_instance=":".join(str(part) for part in pin),
    )
    return replace(
        host_profile,
        mount_path=root,
        filesystem_type=host_profile.filesystem_type or "virtual",
        mount_source=host_profile.mount_source or root,
        inspection_path=root,
        identity=identity,
    )
