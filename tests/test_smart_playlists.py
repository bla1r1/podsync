"""Smart-playlist rule payloads, nested groups, and host evaluation.

Covers the binary SLst/SPLPref writers and parsers, recursive rule-group
containers, host-side rule evaluation (including date/time semantics), and the
track conversion step that feeds the evaluator. Fixtures are synthesized in
process; nothing touches the network, a device, or a GUI.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest

from podsync.itdb.writer.smart_rules import (
    NestedRules,
    SmartPrefs,
    SmartRule,
    SmartRuleSet,
    prefs_from_row,
    rules_from_row,
    write_mhod50,
    write_mhod51,
)
from podsync.library.smart import rule_matches, evaluate_smart_playlist
from podsync.itdb.reader.chunks.strings import decode_rule_list, parse_data_object
from podsync.itdb.spec.clock import DeviceClock
from podsync.itdb.spec.fields import MAC_EPOCH_OFFSET
from podsync.itdb.spec.layouts.strings import MHOD_HEADER_SIZE
from podsync.itdb.spec.smart_fields import (
    SPL_AUTHORABLE_FIELD_IDS,
    SPL_DATE_IDENTIFIER,
    SPL_FIELD_MAP,
    SPL_FIELD_TYPE_MAP,
    SPL_HOST_BINARY_AND_FIELD_KEYS,
    SPL_HOST_BOOLEAN_FIELD_KEYS,
    SPL_HOST_DATE_FIELD_KEYS,
    SPL_HOST_EVALUABLE_FIELD_IDS,
    SPL_HOST_INT_FIELD_KEYS,
    SPL_HOST_STRING_FIELD_KEYS,
    SPLFT_BINARY_AND,
    SPLFT_BOOLEAN,
    SPLFT_INT,
    SPLFT_STRING,
)
from podsync.itdb.reader.diagnose import (
    forensic_json_document,
    reconstruct_byte_walk,
)
from podsync.itdb.writer.database import write_mhbd
from podsync.itdb.writer.track import TrackRecord
from podsync.itdb.writer.playlist import PlaylistRecord
from podsync.library.tracks import rule_view_of

PREF_SNAPSHOT = {
    "live_update": 1,
    "check_rules": 1,
    "check_limits": 1,
    "limit_type": 5,
    "limit_sort": 0x10,
    "limit_value": 40,
    "match_checked_only": 1,
    "reverse_sort": 1,
}


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------


def _group_frame(*entries: bytes, tie: int = 0, root: int = 0x00010001) -> bytes:
    """Assemble one SLst record wrapping the given big-endian rule entries."""
    return b"".join((
        b"SLst",
        struct.pack(">III", root, len(entries), tie),
        bytes(120),
        b"".join(entries),
    ))


def _masked_entry(
    nested: bytes,
    *,
    field: int = 0,
    act: int = 1,
    marker: int = 0x01000000,
    stamp: bytes = bytes(40),
) -> bytes:
    """Hand-build a rule entry that embeds a nested SLst payload."""
    assert len(stamp) == 40
    return b"".join((
        struct.pack(">III", field, act, marker),
        stamp,
        struct.pack(">I", len(nested)),
        nested,
    ))


def _mhod51_envelope(body: bytes) -> bytes:
    """Wrap an SLst body in an MHOD type-51 chunk header."""
    whole = 24 + len(body)
    return struct.pack("<4sIIIII", b"mhod", 24, whole, 51, 0, 0) + body


def _walk_nested(chunk: dict):
    """Depth-first walk over every nested chunk of a forensic document."""
    yield chunk
    for slot in chunk["bytes"]:
        if "chunk" in slot:
            yield from _walk_nested(slot["chunk"])


def _text_rule(field, action, phrase):
    return SmartRule(
        field_id=field,
        action_id=action,
        string_value=phrase,
    )


def _count_rule(field, action, amount):
    return SmartRule(field_id=field, action_id=action, from_value=amount)


def _eval_track(tid, *, artist=None, title=None, genre=None, plays=0):
    row = {"track_id": tid, "play_count": plays}
    if artist is not None:
        row["artist"] = artist
    if title is not None:
        row["title"] = title
    if genre is not None:
        row["genre"] = genre
    return row


# ---------------------------------------------------------------------------
# Preference blob and forensic byte walk
# ---------------------------------------------------------------------------


class TestPreferenceBlob:
    def test_mhod50_preferences_round_trip_through_observed_body_size(self):
        prefs = prefs_from_row(dict(PREF_SNAPSHOT))

        blob = write_mhod50(prefs)
        parsed = parse_data_object(blob, 0, 24, len(blob))["data"]["data"]

        assert len(blob) == 96
        assert struct.unpack_from("<I", blob, 8)[0] == 96
        assert parsed == PREF_SNAPSHOT


class TestForensicByteWalk:
    def test_byte_walk_labels_group_marker_and_nested_container_magic(self, tmp_path):
        ledger = SmartRuleSet(
            rules=[NestedRules(group=SmartRuleSet(conjunction="OR"))],
        )
        dump = tmp_path / "LibraryDump"
        dump.write_bytes(
            write_mhbd(
                [],
                playlists_type2=[],
                playlists_type3=[],
                playlists_type5=[
                    PlaylistRecord(
                        name="Sectioned",
                        smart_prefs=SmartPrefs(),
                        smart_rules=ledger,
                    ),
                ],
            ),
        )

        report = forensic_json_document(dump)
        assert reconstruct_byte_walk(report) == dump.read_bytes()

        payload_chunk = next(
            chunk
            for chunk in _walk_nested(report["file"])
            if chunk["chunk"] == "mhod"
            and any(entry.get("value") == 51 for entry in chunk["bytes"])
        )
        marker_entry = next(
            entry
            for entry in payload_chunk["bytes"]
            if entry.get("field") == "group_marker"
        )
        magic_offsets = [
            entry
            for entry in payload_chunk["bytes"]
            if entry.get("field") == "slst_magic"
        ]

        assert marker_entry == {
            "at": "0x00A8",
            "byte_length": 4,
            "field": "group_marker",
            "value": 0x01000000,
            "encoding": "u32be",
            "hex": "01 00 00 00",
            "status": "known",
        }
        assert [entry["at"] for entry in magic_offsets] == ["0x0018", "0x00D8"]


# ---------------------------------------------------------------------------
# Evaluation semantics
# ---------------------------------------------------------------------------


class TestHostEvaluation:
    def test_and_root_of_groups_recurses_as_a_boolean_expression(self):
        """An AND root of two groups behaves like (artistOrTitle) AND (genreAndPlays)."""
        ledger = SmartRuleSet(
            conjunction="AND",
            rules=[
                NestedRules(
                    group=SmartRuleSet(
                        conjunction="OR",
                        rules=[
                            _text_rule(0x04, 0x01000002, "Lena Marsh"),
                            _text_rule(0x02, 0x01000002, "Lena Marsh"),
                        ],
                    ),
                ),
                NestedRules(
                    group=SmartRuleSet(
                        conjunction="AND",
                        rules=[
                            _text_rule(0x08, 0x03000002, "Yuletide"),
                            _count_rule(0x16, 0x00000010, 35),
                        ],
                    ),
                ),
            ],
        )
        rows = [
            _eval_track(11, artist="Lena Marsh", genre="Ambient", plays=41),
            _eval_track(12, artist="Lena Marsh", genre="Yuletide", plays=41),
            _eval_track(13, title="Lena Marsh Live", genre="Ambient", plays=38),
            _eval_track(14, artist="Other Person", genre="Ambient", plays=55),
        ]

        assert evaluate_smart_playlist(SmartPrefs(), ledger, rows) == [11, 13]

    def test_groups_without_rules_follow_boolean_identity(self):
        """Combining nothing yields False for OR and True for AND."""
        rows = [{"track_id": 21}, {"track_id": 22}]

        empty_any = SmartRuleSet(
            conjunction="AND",
            rules=[NestedRules(group=SmartRuleSet(conjunction="OR"))],
        )
        empty_all = SmartRuleSet(
            conjunction="AND",
            rules=[NestedRules(group=SmartRuleSet(conjunction="AND"))],
        )

        assert evaluate_smart_playlist(SmartPrefs(), empty_any, rows) == []
        assert evaluate_smart_playlist(SmartPrefs(), empty_all, rows) == [21, 22]

    def test_track_conversion_covers_every_host_rule_key(self):
        """The evaluator-facing dict must expose every field the host can score."""
        sample = TrackRecord(
            title="Broadcast",
            location=":iPod_Control:Music:F02:BRDCST.m4v",
            db_track_id=77,
            last_modified=1_650_000_000,
            artwork_count=2,
            mhii_link=64,
            purchased_aac_flag=1,
            sort_show="Sample Show",
        )

        mapped = rule_view_of(sample)

        needed = (
            set(SPL_HOST_STRING_FIELD_KEYS.values())
            | set(SPL_HOST_INT_FIELD_KEYS.values())
            | set(SPL_HOST_DATE_FIELD_KEYS.values())
            | set(SPL_HOST_BOOLEAN_FIELD_KEYS.values())
            | set(SPL_HOST_BINARY_AND_FIELD_KEYS.values())
        )
        assert needed <= mapped.keys()
        assert mapped["last_modified"] == 1_650_000_000
        assert mapped["has_artwork"] is True
        assert mapped["artwork_link"] == 64
        assert mapped["purchased_flag"] == 1
        assert mapped["sort_show"] == "Sample Show"
        assert mapped["location_kind"] == 1

        assert rule_matches(
            SmartRule(
                field_id=0x0A,
                action_id=0x00000001,
                from_value=MAC_EPOCH_OFFSET + 1_650_000_000,
            ),
            mapped,
        )
        assert rule_matches(
            SmartRule(field_id=0x25, action_id=0x00000001),
            mapped,
        )
        assert rule_matches(
            SmartRule(field_id=0x29, action_id=0x00000001),
            mapped,
        )
        assert rule_matches(
            SmartRule(
                field_id=0x53,
                action_id=0x01000001,
                string_value="Sample Show",
            ),
            mapped,
        )
        assert rule_matches(
            SmartRule(field_id=0x85, action_id=0x00000400, from_value=1),
            mapped,
        )

    def test_absolute_date_rule_is_decoded_with_the_given_device_clock(self):
        """The same Mac timestamp lands on different instants per clock context."""
        gate = SmartRule(
            field_id=0x17,
            action_id=0x00000001,
            # 2026-05-05 16:30:00 as an iPod-local Mac timestamp.
            from_value=3_860_843_400,
        )
        sample = {
            "last_played": int(
                datetime(2026, 5, 5, 14, 30, 0, tzinfo=UTC).timestamp()
            ),
        }

        assert rule_matches(
            gate,
            sample,
            time_context=DeviceClock.fixed_offset(7200),
        )
        assert not rule_matches(
            gate,
            sample,
            time_context=DeviceClock.fixed_offset(-18000),
        )

    def test_relative_date_actions_treat_the_marker_as_a_sentinel_not_an_instant(self):
        """The marker must never be decoded into a real timestamp during scoring."""
        for field_id, track_key in ((0x17, "last_played"), (0x45, "last_skipped")):
            assert rule_matches(
                SmartRule(
                    field_id=field_id,
                    action_id=0x02000200,
                    from_value=SPL_DATE_IDENTIFIER,
                    from_date=-3,
                    from_units=3600,
                    to_value=SPL_DATE_IDENTIFIER,
                    to_units=1,
                ),
                {track_key: 0},
            )


# ---------------------------------------------------------------------------
# Field catalog and host capability sets
# ---------------------------------------------------------------------------


class TestFieldCatalog:
    def test_catalog_labels_types_and_host_capability_sets(self):
        """Catalog metadata for later firmware agrees with the host evaluator."""
        label_expectations = {
            0x1D: "Checked",
            0x25: "Album Artwork",
            0x59: "Video Rating",
            0x85: "Location",
            0x86: "Cloud Status",
            0x9A: "Favorite / Suggest Less",
            0x9C: "Album Favorite / Suggest Less",
            0x9F: "Work",
            0xA0: "Movement Name",
            0xA1: "Movement Number",
        }
        type_expectations = {
            0x1D: SPLFT_BOOLEAN,
            0x25: SPLFT_BOOLEAN,
            0x1F: SPLFT_BOOLEAN,
            0x29: SPLFT_BOOLEAN,
            0x59: SPLFT_STRING,
            0x85: SPLFT_BINARY_AND,
            0x3C: SPLFT_INT,
            0x86: SPLFT_INT,
            0x9A: SPLFT_INT,
            0x9C: SPLFT_INT,
            0x9F: SPLFT_STRING,
            0xA0: SPLFT_STRING,
            0xA1: SPLFT_INT,
        }

        assert {
            field_id: SPL_FIELD_MAP[field_id] for field_id in label_expectations
        } == label_expectations
        assert {
            field_id
            for field_id, field_type in SPL_FIELD_TYPE_MAP.items()
            if field_type == SPLFT_BOOLEAN
        } == {0x1D, 0x25, 0x1F, 0x29}
        assert {
            field_id: SPL_FIELD_TYPE_MAP[field_id] for field_id in type_expectations
        } == type_expectations

        # Anything writable into a rule must also be computable during a sync.
        assert SPL_AUTHORABLE_FIELD_IDS <= SPL_HOST_EVALUABLE_FIELD_IDS
        assert SPL_AUTHORABLE_FIELD_IDS
        assert SPL_AUTHORABLE_FIELD_IDS - SPL_HOST_EVALUABLE_FIELD_IDS == set()


# ---------------------------------------------------------------------------
# Nested rule groups: byte layout, decoding, re-encoding
# ---------------------------------------------------------------------------


class TestGroupEncoding:
    def test_writer_refuses_a_header_tail_that_is_not_forty_bytes(self):
        ledger = SmartRuleSet(
            rules=[NestedRules(header_bytes=bytes(39))],
        )

        with pytest.raises(ValueError, match="exactly 40 bytes"):
            write_mhod51(ledger)

    def test_wrapper_layout_is_byte_exact_and_reparses_identically(self):
        stamp = bytes(range(40, 80))
        ledger = SmartRuleSet(
            rules=[
                NestedRules(
                    header_bytes=stamp,
                    group=SmartRuleSet(conjunction="OR"),
                ),
            ],
        )

        blob = write_mhod51(ledger)
        root_at = 24
        wrap_at = root_at + 136
        inner_at = wrap_at + 56

        assert struct.unpack_from(">I", blob, root_at + 4)[0] == 0x00010001
        assert struct.unpack_from(">II", blob, wrap_at) == (0, 1)
        assert struct.unpack_from(">I", blob, wrap_at + 8)[0] == 0x01000000
        assert blob[wrap_at + 12:wrap_at + 52] == stamp
        assert struct.unpack_from(">I", blob, wrap_at + 52)[0] == 136
        assert blob[inner_at:inner_at + 4] == b"SLst"

        reparsed = parse_data_object(blob, 0, 24, len(blob))["data"]["data"]

        assert write_mhod51(rules_from_row(reparsed)) == blob

    def test_truncated_slst_bodies_decode_to_empty_or_opaque(self):
        stub = b"SLst" + struct.pack(">III", 0x00010001, 0, 0)

        bare_blob = _mhod51_envelope(stub)
        bare = parse_data_object(bare_blob, 0, 24, len(bare_blob))
        assert bare["data"]["data"] == {}

        coated_blob = _mhod51_envelope(_group_frame(_masked_entry(stub)))
        coated = parse_data_object(coated_blob, 0, 24, len(coated_blob))[
            "data"
        ]["data"]["rules"][0]

        assert "group" not in coated
        assert coated["raw_data"] == stub

    def test_group_discriminator_needs_field_action_and_marker_all_aligned(self):
        """Flawing any one of the three identifying words keeps it an ordinary rule."""
        sealed = _group_frame(tie=1)
        flawed = (
            _masked_entry(sealed, field=5),
            _masked_entry(sealed, act=7),
            _masked_entry(sealed, marker=0),
        )

        decoded = [
            parse_data_object(_mhod51_envelope(_group_frame(piece)), 0, 24, 24 + 136 + len(piece))[
                "data"
            ]["data"]["rules"][0]
            for piece in flawed
        ]

        assert all("group" not in rule for rule in decoded)

    def test_wrapped_slst_decodes_as_a_group_with_opaque_header_tail(self):
        stamp = bytes(range(40, 80))
        sealed = _group_frame(tie=1)
        blob = _mhod51_envelope(_group_frame(_masked_entry(sealed, stamp=stamp)))

        decoded = parse_data_object(blob, 0, 24, len(blob))
        outer = decoded["data"]["data"]
        coated = outer["rules"][0]

        assert outer["unk004"] == 0x00010001
        assert outer["conjunction"] == 0
        assert coated == {
            "field_id": 0,
            "action_id": 1,
            "header_bytes": stamp,
            "group_marker": 0x01000000,
            "data_length": len(sealed),
            "group": {
                "unk004": 0x00010001,
                "rule_count": 0,
                "conjunction": 1,
                "rules": [],
            },
        }


# ---------------------------------------------------------------------------
# Rule payload writing and re-parsing
# ---------------------------------------------------------------------------


class TestRulePayloadWriters:
    def test_empty_string_payload_does_not_swallow_the_next_rule(self):
        """A zero-length string rule occupies exactly its header, nothing more."""
        probe = write_mhod51(
            rules_from_row(
                {
                    "conjunction": "AND",
                    "rules": [
                        {
                            "field_id": 0xA0,
                            "action_id": 0x01000004,
                            "string_value": "",
                        },
                        {
                            "field_id": 0x23,
                            "action_id": 0x00000100,
                            "from_value": 110,
                            "from_units": 1,
                            "to_value": 180,
                            "to_units": 1,
                        },
                    ],
                }
            )
        )

        decoded = decode_rule_list(probe, MHOD_HEADER_SIZE, len(probe) - MHOD_HEADER_SIZE)

        assert decoded["rules"][0] == {
            "field_id": 0xA0,
            "action_id": 0x01000004,
            "data_length": 0,
            "string_value": "",
        }
        assert decoded["rules"][1]["from_value"] == 110

    def test_legacy_relative_date_variants_normalize_to_the_marker(self):
        """Older payloads disagree about the amount field; the writer repairs them."""
        probe = write_mhod51(
            rules_from_row(
                {
                    "conjunction": "OR",
                    "rules": [
                        {
                            # Negative count parked in from_value.
                            "field_id": 0x17,
                            "action_id": 0x00000200,
                            "from_value": -1,
                            "from_date": -1,
                            "from_units": 86400,
                        },
                        {
                            # Same idea encoded as an unsigned 64-bit -1.
                            "field_id": 0x17,
                            "action_id": 0x02000200,
                            "from_value": 0xFFFFFFFFFFFFFFFF,
                            "from_date": -1,
                            "from_units": 86400,
                        },
                        {
                            # Whole seconds count that is a multiple of the unit.
                            "field_id": 0x45,
                            "action_id": 0x00000200,
                            "from_value": 172800,
                            "from_date": 0,
                            "from_units": 86400,
                        },
                    ],
                }
            )
        )

        decoded = decode_rule_list(probe, MHOD_HEADER_SIZE, len(probe) - MHOD_HEADER_SIZE)

        assert [(rule["from_value"], rule["from_date"]) for rule in decoded["rules"]] == [
            (SPL_DATE_IDENTIFIER, -1),
            (SPL_DATE_IDENTIFIER, -1),
            (SPL_DATE_IDENTIFIER, -2),
        ]

    def test_relative_date_rules_carry_the_marker_in_both_value_slots(self):
        """Relative windows are rewritten so firmware can spot the rule format."""
        probe = write_mhod51(
            rules_from_row(
                {
                    "conjunction": "AND",
                    "rules": [
                        {
                            "field_id": 0x19,
                            "action_id": 0x00000001,
                            "from_value": 85,
                        },
                        {
                            "field_id": 0x17,
                            "action_id": 0x02000200,
                            "from_date": -1,
                            "from_units": 86400,
                        },
                        {
                            "field_id": 0x45,
                            "action_id": 0x00000200,
                            "from_date": -1,
                            "from_units": 86400,
                        },
                    ],
                }
            )
        )

        decoded = decode_rule_list(probe, MHOD_HEADER_SIZE, len(probe) - MHOD_HEADER_SIZE)

        # Plain numeric rules pass through untouched.
        assert decoded["conjunction"] == 0
        assert decoded["rules"][0]["from_value"] == 85
        # Date fields with a relative action get the marker on each side while the
        # window itself stays in from_date/from_units.
        assert [
            (
                rule["field_id"],
                rule["from_value"],
                rule["from_date"],
                rule["to_value"],
                rule["to_units"],
            )
            for rule in decoded["rules"][1:]
        ] == [
            (0x17, SPL_DATE_IDENTIFIER, -1, SPL_DATE_IDENTIFIER, 1),
            (0x45, SPL_DATE_IDENTIFIER, -1, SPL_DATE_IDENTIFIER, 1),
        ]
