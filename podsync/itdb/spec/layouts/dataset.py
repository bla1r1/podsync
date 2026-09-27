"""``mhsd`` — a dataset wrapper; its type says which list it holds."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHSD_HEADER_SIZE = 96

MHSD_FIELDS = record("mhsd", """
    dataset_type   u32  0x0C  required
""")
