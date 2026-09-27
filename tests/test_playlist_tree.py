"""Folder topology: reconciliation, writer round trips, and builder output.

Playlist rows carry a kind flag bit, a parent-folder reference, and an
implicit membership rule that mirrors their children. These cases drive the
pure reconciler, the MHLP/MHYP writers, and the playlist builder across
synthesized in-process payloads.
"""

from __future__ import annotations

from podsync.itdb.reader.walker import parse_chunk
from podsync.itdb.spec.codes import MEDIA_TYPE_PODCAST
from podsync.itdb.spec.playlists.tree import normalize_playlist_tree
from podsync.itdb.spec.playlists.kinds import playlist_kind_flags
from podsync.itdb.writer.track import TrackRecord
from podsync.itdb.writer.lists import write_mhlp_with_playlists
from podsync.itdb.writer.smart_rules import SmartRule
from podsync.itdb.writer.playlist import PlaylistRecord, write_mhyp, write_playlist
from podsync.library.playlists import assemble_playlists

FOLDER_KIND = 0x0100


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------


def _row(
    pid,
    title,
    *,
    kind=None,
    parent=None,
    dangling=None,
    master=False,
    podcast=None,
    tracks=None,
):
    """Build one reconciler input row without repeating its field keys."""
    row = {"playlist_id": pid, "title": title}
    if master:
        row["master_flag"] = 1
    if kind is not None:
        row["playlist_kind_flags"] = kind
    if parent is not None:
        row["parent_folder_playlist_id"] = parent
    if dangling is not None:
        row["unk0x30_playlist_ref"] = dangling
    if podcast is not None:
        row["podcast_flag"] = podcast
    if tracks is not None:
        row["items"] = [{"db_track_id": tid} for tid in tracks]
    return row


def _ids(entries):
    return [entry["playlist_id"] for entry in entries]


def _track_refs(*wanted):
    return [{"db_track_id": tid} for tid in wanted]


def _owner_rule(pid):
    """Membership row a reconciled folder publishes for one of its children."""
    return {
        "field_id": 0x28,
        "action_id": 1,
        "from_value": pid,
        "from_units": 1,
        "to_value": pid,
        "to_units": 1,
    }


def _child_rule(pid):
    """Full mhod-51 membership entry a folder writer emits per child."""
    return {
        "field_id": 0x28,
        "action_id": 1,
        "data_length": 68,
        "from_value": pid,
        "from_date": 0,
        "from_units": 1,
        "to_value": pid,
        "to_date": 0,
        "to_units": 1,
        "unk052": 0,
        "unk056": 0,
        "unk060": 0,
        "unk064": 0,
        "unk068": 0,
    }


def _owner_values(row):
    return [rule["from_value"] for rule in row["smart_playlist_rules"]["rules"]]


def _child_track_ids(row):
    return [kid["data"]["track_id"] for kid in row["mhip_children"]]


def _owner_payload(row):
    return next(
        kid["data"]["data"]
        for kid in row["mhod_children"]
        if kid["data"]["mhod_type"] == 51
    )


def _write_list(playlists):
    return parse_chunk(write_mhlp_with_playlists([], playlists, db_id_2=91), 0)


def _sample_tracks():
    lead = TrackRecord(
        title="Number",
        location=":iPod_Control:Music:F01:NUM.mp3",
        track_id=7,
        db_track_id=601,
    )
    trailer = TrackRecord(
        title="Broadcast",
        location=":iPod_Control:Music:F01:BRD.mp3",
        track_id=8,
        db_track_id=602,
        media_type=MEDIA_TYPE_PODCAST,
        podcast_flag=1,
    )
    return lead, trailer


# ---------------------------------------------------------------------------
# Writer-level round trips over playlist lists
# ---------------------------------------------------------------------------


class TestPlaylistListWriting:
    def test_nested_shelves_produce_recursive_aggregates(self):
        stack = [
            PlaylistRecord(
                name="Shell",
                playlist_id=30,
                playlist_kind_flags=FOLDER_KIND,
                track_ids=[996, 502],
            ),
            PlaylistRecord(
                name="Pocket",
                playlist_id=40,
                playlist_kind_flags=FOLDER_KIND,
                parent_folder_playlist_id=30,
                track_ids=[997, 501],
            ),
            PlaylistRecord(
                name="Token",
                playlist_id=41,
                parent_folder_playlist_id=40,
                track_ids=[501, 502],
            ),
        ]

        parsed, chunk_type = _write_list(stack)
        entries = [kid["data"] for kid in parsed["data"]][1:]

        assert chunk_type == "mhlp"
        assert _ids(entries) == [30, 40, 41]
        shell, pocket, leaf = entries
        assert [e["parent_folder_playlist_id"] for e in (shell, pocket, leaf)] == [
            0,
            30,
            40,
        ]
        assert _child_track_ids(shell) == [502, 501]
        assert _child_track_ids(pocket) == [501, 502]

        shell_rules = _owner_payload(shell)
        pocket_rules = _owner_payload(pocket)

        assert (shell_rules["conjunction"], shell_rules["unk004"]) == (1, 0x00010001)
        assert shell_rules["rules"] == [_child_rule(40)]
        assert (pocket_rules["conjunction"], pocket_rules["unk004"]) == (1, 0x00010001)
        assert pocket_rules["rules"] == [_child_rule(41)]

        # Reconciliation runs on clones: the caller's inputs stay untouched.
        assert [item.track_ids for item in stack[:2]] == [[996, 502], [997, 501]]

    def test_writer_orders_shelves_in_place_and_keeps_inputs_pristine(self):
        shelf = PlaylistRecord(
            name="Bins",
            playlist_id=30,
            playlist_kind_flags=FOLDER_KIND,
            track_ids=[602, 999, 601],
        )
        stack = [
            shelf,
            PlaylistRecord(
                name="Alpha",
                playlist_id=31,
                parent_folder_playlist_id=30,
                track_ids=[601],
            ),
            PlaylistRecord(name="Singles", playlist_id=40, track_ids=[700]),
            PlaylistRecord(
                name="Beta",
                playlist_id=32,
                parent_folder_playlist_id=30,
                track_ids=[601, 602],
            ),
        ]

        parsed, chunk_type = _write_list(stack)
        entries = [kid["data"] for kid in parsed["data"]]
        shelf_entry = entries[1]
        shelf_rules = _owner_payload(shelf_entry)

        assert chunk_type == "mhlp"
        assert _ids(entries[1:]) == [30, 31, 32, 40]
        assert _child_track_ids(shelf_entry) == [602, 601]
        assert shelf_rules["conjunction"] == 1
        assert [
            (
                rule["from_value"],
                rule["to_value"],
                rule["from_units"],
                rule["to_units"],
            )
            for rule in shelf_rules["rules"]
        ] == [(31, 31, 1, 1), (32, 32, 1, 1)]
        assert shelf.track_ids == [602, 999, 601]


# ---------------------------------------------------------------------------
# Playlist builder
# ---------------------------------------------------------------------------


class TestBuilderOutput:
    def test_builder_keeps_topology_and_child_derived_membership(self):
        lead, trailer = _sample_tracks()

        _head, _head_id, built, *_tail = assemble_playlists(
            [{"track_id": 7, "db_track_id": 601}, {"track_id": 8, "db_track_id": 602}],
            [
                _row(2, "Device", master=True),
                _row(30, "Bins", kind=FOLDER_KIND, tracks=[999]),
                _row(31, "Alpha", parent=30, tracks=[601]),
            ],
            [],
            [],
            [lead, trailer],
        )

        shelf, alpha = built
        assert (shelf.name, alpha.name) == ("Bins", "Alpha")
        assert shelf.is_folder is True
        assert shelf.is_podcast is False
        assert shelf.track_ids == [601]
        assert alpha.parent_folder_playlist_id == 30
        assert shelf.smart_rules is not None
        assert shelf.smart_rules.conjunction == "OR"
        assert all(
            isinstance(rule, SmartRule)
            for rule in shelf.smart_rules.rules
        )
        assert [
            rule.from_value
            for rule in shelf.smart_rules.rules
            if isinstance(rule, SmartRule)
        ] == [31]

    def test_builder_leaves_a_childless_shelf_with_no_tracks(self):
        lead, *_unused = _sample_tracks()

        _head, _head_id, built, *_tail = assemble_playlists(
            [{"track_id": 7, "db_track_id": 601}],
            [
                _row(2, "Device", master=True),
                _row(30, "Empty Bins", kind=FOLDER_KIND, tracks=[]),
            ],
            [],
            [],
            [lead],
        )

        assert len(built) == 1
        assert built[0].is_folder is True
        assert built[0].track_ids == []


# ---------------------------------------------------------------------------
# Reconciliation of playlist rows
# ---------------------------------------------------------------------------


class TestReconcilerTopology:
    def test_shelves_emit_before_their_descendants(self):
        emitted = normalize_playlist_tree([
            _row(2, "Device", master=True),
            _row(30, "Bins", kind=FOLDER_KIND),
            _row(31, "Alpha", parent=30),
            _row(40, "Singles"),
            _row(32, "Beta", parent=30),
        ])

        assert _ids(emitted) == [2, 30, 31, 32, 40]

    def test_shelf_items_become_the_deduplicated_union_of_children(self):
        emitted = normalize_playlist_tree([
            _row(30, "Bins", kind=FOLDER_KIND, tracks=[503, 999, 501]),
            _row(31, "Alpha", parent=30, tracks=[501, 502]),
            _row(32, "Beta", parent=30, tracks=[502, 503]),
        ])

        shelf = emitted[0]
        # Only entries the children also hold survive, in child traversal order
        # after the shelf's own surviving picks.
        assert shelf["items"] == _track_refs(503, 501, 502)
        assert shelf["mhip_child_count"] == 3
        assert shelf["smart_playlist_data"]["check_rules"] is True
        assert shelf["smart_playlist_rules"] == {
            "conjunction": "OR",
            "unk004": 0x00010001,
            "rules": [_owner_rule(31), _owner_rule(32)],
        }

    def test_dangling_and_mutual_parent_links_both_get_cleared(self):
        stranded = normalize_playlist_tree([
            _row(34, "Stray", podcast=0, dangling=555),
        ])

        # A reference to a playlist that is not a shelf cannot survive.
        assert stranded == [{
            "playlist_id": 34,
            "title": "Stray",
            "podcast_flag": 0,
            "playlist_kind_flags": 0,
            "is_folder": False,
            "is_podcast": False,
            "unk0x30_playlist_ref": 0,
            "parent_folder_playlist_id": 0,
        }]

        looped = normalize_playlist_tree([
            _row(30, "Left", kind=FOLDER_KIND, parent=40),
            _row(40, "Right", kind=FOLDER_KIND, parent=30),
        ])

        assert [row["parent_folder_playlist_id"] for row in looped] == [0, 0]

    def test_parent_links_chain_across_nested_shelves(self):
        emitted = normalize_playlist_tree([
            _row(30, "Shell", kind=FOLDER_KIND),
            _row(40, "Pocket", kind=FOLDER_KIND, parent=30),
            _row(41, "Token", parent=40),
        ])

        shell, pocket, leaf = emitted
        assert shell["parent_folder_playlist_id"] == 0
        assert pocket["parent_folder_playlist_id"] == 30
        assert leaf["parent_folder_playlist_id"] == 40
        assert _owner_values(shell) == [40]
        assert _owner_values(pocket) == [41]


# ---------------------------------------------------------------------------
# Single-chunk round trips and flag encoding
# ---------------------------------------------------------------------------


class TestFolderFlagEncoding:
    def test_explicit_markers_merge_into_the_stored_kind_word(self):
        marker_cases = [
            ({"is_folder": True}, 0x0100),
            ({"is_podcast": True}, 0x0001),
            (
                {
                    "playlist_kind_flags": FOLDER_KIND,
                    "is_folder": False,
                    "is_podcast": True,
                },
                0x0101,
            ),
        ]

        for markers, expected in marker_cases:
            assert playlist_kind_flags(markers) == expected

    def test_playlist_info_exposes_shelf_kind_without_becoming_a_podcast(self):
        current = PlaylistRecord(
            name="Collected",
            playlist_id=0x5E2,
            playlist_kind_flags=FOLDER_KIND,
            parent_folder_playlist_id=0x5E1,
        )

        assert current.kind_flags == 0x0100
        assert current.podcast_flag == 0x0100
        assert current.is_folder is True
        assert current.is_podcast is False

        parsed, _chunk_type = parse_chunk(write_playlist(current), 0)
        assert parsed["data"]["playlist_kind_flags"] == 0x0100
        assert parsed["data"]["parent_folder_playlist_id"] == 0x5E1

    def test_written_mhyp_keeps_folder_word_and_parent_reference(self):
        """A written shelf MHYP must read back with both aliases intact."""
        blob = write_mhyp(
            "Archive",
            [21, 32],
            playlist_id=0xB77,
            playlist_kind_flags=FOLDER_KIND,
            parent_folder_playlist_id=0xC03,
        )

        parsed, chunk_type = parse_chunk(blob, 0)
        entry = parsed["data"]

        assert chunk_type == "mhyp"
        assert entry["playlist_kind_flags"] == 0x0100
        assert entry["podcast_flag"] == 0x0100
        assert entry["is_folder"] is True
        assert entry["is_podcast"] is False
        assert entry["parent_folder_playlist_id"] == 0xC03
        assert entry["unk0x30_playlist_ref"] == 0xC03
