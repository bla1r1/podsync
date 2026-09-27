"""Cover-art format registry resolution per device model.

Covers which format ids each stock model exposes, how observed
on-device dimensions interact with the known definitions, and what
happens when no device can be identified for a path at all.  All lookups
are pure table reads; the only fixture is a tmp_path used as an unknown
device location.
"""

from __future__ import annotations

import pytest

from podsync.artwork.writer import pixels
from podsync.hardware.catalog.artwork import ITHMB_FORMAT_MAP, resolve_cover_art_format_definitions


class TestUnidentifiedLocation:
    def test_no_device_means_no_artwork_tables(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "podsync.hardware.selected_device_at",
            lambda _path: None,
        )

        assert pixels.get_artwork_format_definitions(str(tmp_path)) == {}
        assert pixels.get_artwork_formats(str(tmp_path)) == {}


class TestStockModelIdSets:
    @pytest.mark.parametrize(
        "family, generation, wanted_ids, probe_ids",
        [
            ("iPod Classic", "6th Gen", [1055, 1060, 1061, 1068], [1055]),
            ("iPod", "5th Gen", [1028, 1029], [1028, 1029]),
        ],
    )
    def test_stock_models_publish_exactly_their_registered_ids(
        self,
        family,
        generation,
        wanted_ids,
        probe_ids,
    ) -> None:
        table = resolve_cover_art_format_definitions(family, generation)

        assert list(table) == wanted_ids
        assert all(table[i] == ITHMB_FORMAT_MAP[i] for i in probe_ids)


class TestNanoSevenLocalEntries:
    def test_local_rows_override_their_global_counterparts(self) -> None:
        table = resolve_cover_art_format_definitions("iPod Nano", "7th Gen")

        assert table[1013].width == 50
        assert table[1015].width == 58
        assert table[1016].width == 57
        assert table[1016] != ITHMB_FORMAT_MAP[1016]

    @pytest.mark.parametrize(
        "observed, wanted_size, wanted_description, wanted_pixel",
        [
            ((57, 57), (57, 57), "Nano 7G album art small (aligned)", None),
            ((60, 60), (60, 60), "Device artwork format 1016", "RGB565_LE"),
        ],
    )
    def test_observed_measurements_take_precedence_over_the_registry(
        self,
        observed,
        wanted_size,
        wanted_description,
        wanted_pixel,
    ) -> None:
        table = resolve_cover_art_format_definitions(
            "iPod Nano",
            "7th Gen",
            observed_formats={1016: observed},
        )
        entry = table[1016]

        assert (entry.width, entry.height) == wanted_size
        assert entry.description == wanted_description
        if wanted_pixel is not None:
            assert entry.pixel_format == wanted_pixel
