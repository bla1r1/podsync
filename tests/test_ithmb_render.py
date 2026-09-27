"""ithmb pixel codec round-trips and image-load safety checks.

Regroups the encode/decode codec coverage — including the Nano 7G padded
stride path and the override-driven pixel layout — together with the
decompression-bomb reporting behaviour of the byte-to-image loader.
Every image here is synthesised with Pillow alone (no numpy), and nothing
touches a device, the network, or a GUI.
"""

from __future__ import annotations

import pytest

from PIL import Image

from podsync.artwork.writer import pixels as rgb565
from podsync.artwork.writer.codecs import (
    decode_pixels_for_format,
    encode_image_for_format,
)
from podsync.hardware.catalog.artwork import resolve_cover_art_format_definitions


def _spectrum(cols: int, rows: int) -> Image.Image:
    """Row/column ramp so any row-boundary shear becomes visible."""
    plate = Image.new("RGB", (cols, rows))
    plate.putdata([
        ((x * 3) % 250, (y * 5) % 246, ((x + y) * 7) % 242)
        for y in range(rows)
        for x in range(cols)
    ])
    return plate


class TestLoaderSafety:
    def test_decompression_bomb_names_the_source_path(self, monkeypatch) -> None:
        """A Pillow safety abort surfaces the original artwork file path."""
        failure = Image.DecompressionBombError(
            "Image size (150000000 pixels) exceeds limit of 178956970 pixels, "
            "could be decompression bomb DOS attack.",
        )

        def rupture(_stream):
            raise failure

        monkeypatch.setattr(rgb565.Image, "open", rupture)

        with pytest.raises(ValueError) as raised:
            rgb565.image_from_bytes(
                b"garbage-bytes",
                source_path="/tunes/Cover Art/master.tiff",
            )

        detail = str(raised.value)
        assert "Offending image: /tunes/Cover Art/master.tiff" in detail
        assert "decompression bomb DOS attack" in detail


class TestCodecRoundTrips:
    def test_override_decode_follows_override_pixel_layout(self) -> None:
        """Decoding with format 1013's override honours its own pixel format."""
        spec = resolve_cover_art_format_definitions("iPod Nano", "7th Gen")[1013]
        plate = Image.new("RGB", (spec.width, spec.height), (250, 16, 64))

        packed = encode_image_for_format(
            plate,
            spec.format_id,
            spec.width,
            spec.height,
            fmt_override=spec,
        )
        rebuilt = decode_pixels_for_format(
            spec.format_id,
            packed.data,
            spec.width,
            spec.height,
            fmt_override=spec,
        )

        assert rebuilt is not None
        assert rebuilt.size == (spec.width, spec.height)
        red_first = rebuilt.convert("RGB").getpixel((0, 0))[0]
        assert red_first > 230

    def test_nano_7g_alt_entry_encodes_with_padded_stride(self) -> None:
        """Format 1016's 57-pixel rows are padded to a 58-pixel stride."""
        cols = 57
        rows = 57
        padded = 58
        spec = resolve_cover_art_format_definitions("iPod Nano", "7th Gen")[1016]

        plate = _spectrum(cols, rows)
        packed = encode_image_for_format(plate, 1016, cols, rows, fmt_override=spec)

        assert packed.stride_pixels == padded
        assert packed.size == padded * rows * 2
        assert len(packed.data) == padded * rows * 2

        rebuilt = decode_pixels_for_format(
            1016,
            packed.data,
            cols,
            rows,
            fmt_override=spec,
        )

        assert rebuilt is not None
        assert rebuilt.size == (cols, rows)
