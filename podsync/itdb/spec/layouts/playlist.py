"""``mhyp`` — a playlist header (visible, podcast, smart, folder or category)."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHYP_HEADER_SIZE = 184

MHYP_FIELDS = record("mhyp", """
    mhod_child_count            u32  0x0C
    mhip_child_count            u32  0x10
    master_flag                 u8   0x14
    flag1                       u8   0x15
    flag2                       u8   0x16
    flag3                       u8   0x17
    timestamp                   u32  0x18  mac-time
    playlist_id                 u64  0x1C
    unk0x24                     u32  0x24
    string_mhod_child_count     u16  0x28
    playlist_kind_flags         u16  0x2A
    sort_order                  u32  0x2C
    parent_folder_playlist_id   u64  0x30
    unk0x38                     u32  0x38
    db_id_2                     u64  0x3C  since=0x44
    playlist_id_2               u64  0x44  since=0x4C
    unk0x4C                     u32  0x4C  since=0x50
    mhsd5_type                  u16  0x50  since=0x52   # category kind in dataset 5
    phase_game_flag             u16  0x52  since=0x54
    mhsd5_special_flag          u32  0x54  since=0x58
    timestamp_2                 u32  0x58  since=0x5C  mac-time
""")
