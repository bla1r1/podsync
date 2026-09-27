"""``mhii`` — one artist of the artist list (dataset 8).

Not to be confused with ArtworkDB's ``mhii`` (an image item), which shares the tag.
"""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHII_HEADER_SIZE = 80

MHII_FIELDS = record("mhii", """
    child_count     u32  0x0C
    artist_id       u32  0x10  required
    sql_id          u64  0x14
    platform_flag   u32  0x1C  default=2
""")
