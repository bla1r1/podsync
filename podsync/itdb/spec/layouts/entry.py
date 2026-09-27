"""``mhip`` — one entry of a playlist (a track reference, or a podcast group header)."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHIP_HEADER_SIZE = 76

MHIP_FIELDS = record("mhip", """
    child_count                   u32  0x0C
    podcast_group_flag            u16  0x10
    unk0x12                       u16  0x12
    group_id                      u32  0x14
    track_id                      u32  0x18  required
    timestamp                     u32  0x1C  mac-time
    group_link                    u32  0x20
    unk0x24_group_persistent_id   u64  0x24
    track_persistent_id           u64  0x2C  since=0x34
    mhip_persistent_id            u64  0x3C  since=0x44
""")
