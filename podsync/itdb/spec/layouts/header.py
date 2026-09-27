"""``mhbd`` — the database header, including where the signatures live."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHBD_HEADER_SIZE = 0xF4

MHBD_OFFSET_DB_ID = 0x18
MHBD_OFFSET_HASHING_SCHEME = 0x30
MHBD_OFFSET_UNK_0x32 = 0x32
MHBD_OFFSET_HASH58 = 0x58
MHBD_OFFSET_HASH72 = 0x72
MHBD_OFFSET_HASHAB = 0xAB

MHBD_FIELDS = record("mhbd", """
    compressed            u32      0x0C  default=1
    version               u32      0x10  required
    child_count           u32      0x14
    db_id                 u64      0x18  required
    platform              u16      0x20  default=2      # 1 = Mac, 2 = Windows
    unk0x22               u16      0x22
    db_id_2               u64      0x24
    unk0x2c               u32      0x2C
    hashing_scheme        u16      0x30
    unk0x32               bytes20  0x32
    language              bytes2   0x46  default=b"en"
    db_persistent_id      u64      0x48
    unk0x50               u32      0x50  default=1
    unk0x54               u32      0x54  default=15
    hash58                bytes20  0x58
    timezone_offset       i32      0x6C
    hash_type_indicator   u16      0x70
    hash72                bytes46  0x72
    audio_language        u16      0xA0  since=0xA2
    subtitle_language     u16      0xA2  since=0xA4
    unk0xa4               u16      0xA4  since=0xA6
    unk0xa6               u16      0xA6  since=0xA8
    cdb_flag              u16      0xA8  since=0xAA
    hashab                bytes57  0xAB  since=0xE4
""")
