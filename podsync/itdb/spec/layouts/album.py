"""``mhia`` — one album of the album list (dataset 4)."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHIA_HEADER_SIZE = 88

MHIA_FIELDS = record("mhia", """
    child_count              u32  0x0C
    album_id                 u32  0x10  required
    sql_id                   u64  0x14
    platform_flag            u16  0x1C  default=2
    album_compilation_flag   u16  0x1E
    album_track_db_id        u64  0x20  since=0x28   # representative track
    album_rating             u8   0x28
    unk0x29_rating_flag      u8   0x29
    season_number            u32  0x2C
""")
