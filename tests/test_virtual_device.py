"""Synthesized iPod volumes: catalog tables, seeding, identification, repair.

The catalog half checks that every published row exposes the lookup fields
the rest of the package relies on.  The volume half seeds real directories
under ``tmp_path`` and walks them through the ordinary identification path.
"""

from __future__ import annotations

import json

import podsync.hardware as deck


class TestCatalogTables:
    def test_every_catalog_row_carries_lookup_fields(self) -> None:
        entries = deck.available_virtual_ipod_models()

        assert entries
        assert all(row["model_number"] and row["serial_suffix"] for row in entries)
        assert any(row["model_number"] == "MB029" for row in entries)

    def test_published_suffix_lengths_hold_in_catalog_and_after_creation(
        self,
        tmp_path,
    ) -> None:
        wanted_lengths = {
            "MB453": 3,
            "MC525": 4,
            "MD475": 4,
            "MD773": 4,
        }

        by_number = {row["model_number"]: row for row in deck.available_virtual_ipod_models()}
        for number, width in wanted_lengths.items():
            assert len(by_number[number]["serial_suffix"]) == width

        # Creation round-trips only rows whose optional runtime support is
        # present: Nano 6th/7th-gen rows make the seeder raise RuntimeError
        # when the optional `wasmtime` interpreter is missing — an
        # environment gap that is deliberately not asserted here.
        for number in ("MB453", "MD773", "MC584"):
            out_root = tmp_path / number
            unit = deck.make_virtual_ipod(out_root, number)
            current = json.loads((out_root / "iPodInfo.json").read_text())

            assert current["serial_suffix"] == by_number[number]["serial_suffix"]
            assert unit.serial.endswith(current["serial_suffix"])


class TestSeededClassicVolume:
    def test_classic_seeding_layout_payload_and_database_self_repair(
        self,
        tmp_path,
    ) -> None:
        unit = deck.make_virtual_ipod(tmp_path, "MB029")

        assert deck.has_virtual_ipod_info(tmp_path)
        for rel in (
            "iPod_Control/Device/SysInfo",
            "iPod_Control/Device/HashInfo",
            "iPod_Control/iTunes/iTunesDB",
        ):
            assert (tmp_path / rel).is_file()
        for rel in (
            "iPod_Control/iTunes",
            "iPod_Control/Music",
            "iPod_Control/Artwork",
        ):
            assert (tmp_path / rel).is_dir()

        current = json.loads((tmp_path / "iPodInfo.json").read_text())
        assert current["model_number"] == "MB029"
        assert current["model_family"] == "iPod Classic"
        assert current["generation"] == "6th Gen"
        assert current["serial"].endswith(current["serial_suffix"])

        assert unit.model_number == "MB029"
        assert unit.serial.endswith(current["serial_suffix"])
        assert unit.firewire_id_bytes == bytes.fromhex(current["firewire_guid"])
        assert unit.checksum_type == deck.SignatureKind.HASH58
        assert unit.volume_identity_key.startswith("virtual|")

        # Removing the database file triggers self-repair on the next scan.
        missing_db = tmp_path / "iPod_Control" / "iTunes" / "iTunesDB"
        missing_db.unlink()
        rescanned = deck.identify_mounted_ipod(str(tmp_path))

        assert rescanned is not None
        assert missing_db.exists()

    def test_a_nano_volume_names_its_newer_database_file(
        self,
        tmp_path,
    ) -> None:
        deck.make_virtual_ipod(tmp_path, "MC062")
        itunes_dir = tmp_path / "iPod_Control" / "iTunes"

        assert (itunes_dir / "iTunesCDB").is_file()
        assert not (itunes_dir / "iTunesDB").exists()


class TestStandardIdentificationPath:
    def test_shuffle_identification_round_trips_every_stored_field(
        self,
        tmp_path,
    ) -> None:
        deck.make_virtual_ipod(tmp_path, "MB227")

        scanned = deck.identify_mounted_ipod(str(tmp_path))
        stored = deck.load_virtual_ipod_info(tmp_path)

        assert scanned is not None
        assert scanned.model_number == stored.model_number == "MB227"
        assert scanned.model_family == "iPod Shuffle"
        assert scanned.serial == stored.serial
        assert deck.detect_signature_kind(str(tmp_path)) == deck.SignatureKind.NONE
        assert deck.get_firewire_id(str(tmp_path)) == stored.firewire_id_bytes
