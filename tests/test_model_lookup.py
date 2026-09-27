"""Model catalog naming, serial-suffix resolution, and capability tables.

Catalog facts are engine-owned, so the reference data below is stored as
compact ``key=value`` line tables and expanded by tiny parsers; each test
then drives the public lookup API against the parsed expectation.
"""

from __future__ import annotations

import pytest

from podsync import hardware as catalog
from podsync.hardware.virtual.device import _usb_pid_for_identity

_LINEAGE_ROWS = """
iPod Shuffle|1st Gen,2nd Gen,3rd Gen,4th Gen
iPod|1st Gen,2nd Gen,3rd Gen,4th Gen (mono),4th Gen (photo),4th Gen (color),5th Gen,5.5th Gen
iPod Classic|6th Gen,6.5th Gen,7th Gen
iPod Nano|1st Gen,2nd Gen,3rd Gen,4th Gen,5th Gen,6th Gen,7th Gen
iPod Mini|1st Gen,2nd Gen
"""

_RECOVERY_PID_ROWS = """
0x1220=iPod Nano/2nd Gen
0x1223=iPod/
0x1224=iPod Nano/3rd Gen
0x1225=iPod Nano/4th Gen
0x1231=iPod Nano/5th Gen
0x1232=iPod Nano/6th Gen
0x1233=iPod Shuffle/4th Gen
0x1234=iPod Nano/7th Gen
0x1240=iPod Nano/2nd Gen
0x1241=iPod Classic/6th Gen
0x1242=iPod Nano/3rd Gen
0x1243=iPod Nano/4th Gen
0x1245=iPod Classic/6.5th Gen
0x1246=iPod Nano/5th Gen
0x1247=iPod Classic/7th Gen
0x1248=iPod Nano/6th Gen
0x1249=iPod Nano/7th Gen
0x124A=iPod Nano/7th Gen
0x1255=iPod Nano/4th Gen
"""

_MODEL_NUMBER_ROWS = """
M9787=iPod|4th Gen (mono)|20GB|U2
M9585=iPod|4th Gen (photo)|40GB|White
MA079=iPod|4th Gen (color)|20GB|White
MA452=iPod|5th Gen|30GB|U2
MA664=iPod|5.5th Gen|30GB|U2
MB029=iPod Classic|6th Gen|80GB|Silver
MB562=iPod Classic|6.5th Gen|120GB|Silver
MC297=iPod Classic|7th Gen|160GB|Black
"""

_SERIAL_REFERENCE_ROWS = """
U5H=MA215
WEM=MA664
S4G=M9805
S4H=M9805
37G=MB651
72D=MC043
DCMN=MC525
DCMP=MC526
DDVX=MC688
DDVY=MC689
DDW0=MC690
DDW1=MC691
DDW2=MC692
DDW3=MC693
DDW4=MC694
DDW5=MC695
DDW6=MC696
DDW7=MC697
DDW8=MC698
DDW9=MC699
F0GD=MD475
F0GM=MD475
F0GF=MD476
F0GN=MD476
F0GG=MD477
F0GP=MD477
F0GH=MD478
F0GQ=MD478
F0GJ=MD479
F0GR=MD479
F0GK=MD480
F0GT=MD480
F0GL=MD481
F0GV=MD481
F4LN=MD744
F4LP=MD744
FJQ1=ME971
GK60=MKMV2
GK61=MKMX2
GK62=MKN02
GK63=MKN22
GK64=MKN52
GK65=MKN72
YX7=MB227
YXH=MB227
1ZK=MB520
YXJ=MB229
1ZM=MB522
YXK=MB231
1ZP=MB524
YXL=MB233
1ZR=MB526
436=MB811
3FK=MB681
437=MB813
3FL=MB683
438=MB815
3FM=MB685
439=MB817
3W6=MB779
DCMJ=MC584
DCMK=MC585
DFDM=MC749
DFDN=MC750
DFDP=MC751
F4RT=MD773
F4RV=MD774
F4RW=MD775
F4RY=MD776
F4T0=MD777
F4T1=MD778
F4VF=MD779
F4VG=MD780
FJDH=ME949
GK67=MKM72
GK68=MKM92
GK69=MKME2
GK6C=MKMG2
GK6D=MKMJ2
GK6F=MKML2
"""

_CEILING_ROWS = """
iPod Mini|2nd Gen||32
iPod|5.5th Gen|30GB|32
iPod|5.5th Gen|80GB|64
iPod Nano|6th Gen||64
iPod Classic|7th Gen||64
"""

_SUBTITLE_ROWS = """
iPod|5th Gen|0|0
iPod|5.5th Gen|0|0
iPod Classic|6th Gen|1|1
iPod Classic|6.5th Gen|1|1
iPod Classic|7th Gen|1|1
iPod Nano|3rd Gen|1|1
iPod Nano|4th Gen|1|1
iPod Nano|5th Gen|1|1
iPod Nano|6th Gen|0|0
iPod Nano|7th Gen|1|1
"""

_VIRTUAL_PID_ROWS = """
iPod Nano/3rd Gen=0x1262
iPod Nano/6th Gen=0x1266
iPod Nano/7th Gen=0x1267
iPod Shuffle/4th Gen=0x1303
"""


def _pipe_rows(raw: str) -> list[tuple[str, ...]]:
    return [
        tuple(cell.strip() for cell in line.split("|"))
        for line in raw.strip().splitlines()
    ]


def _lineage_table() -> dict[str, set[str]]:
    table: dict[str, set[str]] = {}
    for family, generations in _pipe_rows(_LINEAGE_ROWS):
        table[family] = {gen.strip() for gen in generations.split(",")}
    return table


def _recovery_table() -> dict[int, tuple[str, str]]:
    table: dict[int, tuple[str, str]] = {}
    for row in _RECOVERY_PID_ROWS.strip().splitlines():
        pid_text, target = row.split("=")
        family, generation = target.split("/")
        table[int(pid_text, 16)] = (family, generation)
    return table


def _kv_pairs(raw: str) -> dict[str, str]:
    return dict(
        row.split("=") for row in raw.strip().splitlines()
    )


RECOGNIZED_LINEAGES = _lineage_table()


class TestSerialSuffixResolution:
    def test_short_and_long_suffixes_pin_their_exact_models(self) -> None:
        assert catalog.lookup_by_serial("Q9X772613F") == (
            "MB453",
            ("iPod Nano", "3rd Gen", "8GB", "Pink"),
        )
        assert catalog.lookup_by_serial("TESTUNITF0GD") == (
            "MD475",
            ("iPod Nano", "7th Gen", "16GB", "Pink"),
        )
        assert catalog.lookup_by_serial("TESTUNITX0GD") is None
        assert catalog.lookup_by_serial("0GN") is None
        assert catalog.lookup_by_serial("MKTOPP2C7") == (
            "MB565",
            ("iPod Classic", "6.5th Gen", "120GB", "Black"),
        )

    def test_longer_suffixes_win_over_injected_shorter_keys(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setitem(catalog.SERIAL_SUFFIX_TO_MODEL, "4RT", "MB453")

        assert catalog.lookup_by_serial("ZZTOPF4RT") == (
            "MD773",
            ("iPod Shuffle", "4th Gen", "2GB", "Pink"),
        )

    def test_suffix_key_shape_matches_the_published_table(self) -> None:
        table = catalog.SERIAL_SUFFIX_TO_MODEL
        three = {key for key in table if len(key) == 3}
        four = {key for key in table if len(key) == 4}

        assert len(four) == 57
        assert all(key.isalnum() and key == key.upper() for key in table)
        assert set(map(len, table)) == {3, 4}
        assert three.isdisjoint(key[-3:] for key in four)

    def test_reference_suffixes_resolve_to_the_models_we_expect(self) -> None:
        expected = _kv_pairs(_SERIAL_REFERENCE_ROWS)

        observed = {}
        for suffix in expected:
            hit = catalog.lookup_by_serial(f"BATCH{suffix}")
            observed[suffix] = hit[0] if hit else None

        assert set(expected) <= set(catalog.SERIAL_SUFFIX_TO_MODEL)
        assert observed == expected


class TestCatalogNamingRules:
    def test_every_family_and_generation_in_both_hint_tables_is_recognized(
        self,
    ) -> None:
        for number, (family, generation, _capacity, _color) in catalog.IPOD_MODELS.items():
            assert family in RECOGNIZED_LINEAGES, number
            assert generation in RECOGNIZED_LINEAGES[family], number
            assert "Video" not in family
            assert "U2" not in family

        for pid, (family, generation) in catalog.USB_PID_TO_MODEL.items():
            assert family in RECOGNIZED_LINEAGES, hex(pid)
            if generation:
                assert generation in RECOGNIZED_LINEAGES[family], hex(pid)
            assert "Video" not in family
            assert "U2" not in family

    def test_recovery_pids_map_to_their_supported_generations(self) -> None:
        expected = _recovery_table()

        assert {
            pid: catalog.USB_PID_TO_MODEL.get(pid) for pid in expected
        } == expected
        assert catalog.IPOD_RECOVERY_USB_PIDS == frozenset(expected)

    def test_virtual_units_avoid_recovery_mode_pids(self) -> None:
        for row in _VIRTUAL_PID_ROWS.strip().splitlines():
            target, pid_text = row.split("=")
            family, generation = target.split("/")
            assert _usb_pid_for_identity(family, generation) == int(pid_text, 16)

    def test_model_number_samples_resolve_to_their_canonical_rows(self) -> None:
        for number, spec in _kv_pairs(_MODEL_NUMBER_ROWS).items():
            assert catalog.get_model_info(number) == tuple(spec.split("|"))

    def test_friendly_names_keep_family_and_finish_separate(self) -> None:
        assert catalog.get_friendly_model_name("MA664") == "iPod 5.5th Gen 30GB U2"
        assert (
            catalog.get_friendly_model_name("MC297")
            == "iPod Classic 7th Gen 160GB Black"
        )

    def test_canonicalize_normalizes_shouting_labels(self) -> None:
        for raw_family, raw_generation, family, generation in (
            ("IPOD CLASSIC", "7TH GEN", "iPod Classic", "7th Gen"),
            ("IPOD", "5.5TH GEN", "iPod", "5.5th Gen"),
        ):
            assert catalog.canonicalize_model_identity(raw_family, raw_generation) == (
                family,
                generation,
                "",
            )


class TestCapabilityTables:
    def test_every_model_row_maps_to_capabilities(self) -> None:
        for number, (family, generation, capacity, _color) in catalog.IPOD_MODELS.items():
            assert catalog.traits_for_model(
                family,
                generation,
                capacity=capacity,
                model_number=number,
            ), number

    def test_u2_finish_does_not_change_generation_capabilities(self) -> None:
        mono = catalog.traits_for_model(
            "iPod",
            "4th Gen (mono)",
            capacity="20GB",
            model_number="M9787",
        )
        chrome = catalog.traits_for_model(
            "iPod",
            "4th Gen (color)",
            capacity="20GB",
            model_number="MA127",
        )

        assert mono is not None
        assert chrome is not None
        assert mono.supports_artwork is False
        assert mono.supports_photo is False
        assert chrome.supports_artwork is True
        assert chrome.supports_photo is True

    def test_full_size_icons_follow_the_generation(self) -> None:
        phone, note = chr(0x1F4F1), chr(0x1F3B5)
        wanted = {"5th Gen": phone, "4th Gen (photo)": phone, "4th Gen (mono)": note}

        for generation, icon in wanted.items():
            assert catalog.IpodDevice(model_family="iPod", generation=generation).icon == icon

    @pytest.mark.parametrize(
        "family, generation, capacity, mib",
        [
            (row[0], row[1], row[2], int(row[3]))
            for row in _pipe_rows(_CEILING_ROWS)
        ],
    )
    def test_database_ceiling_tracks_generation_and_capacity(
        self,
        family,
        generation,
        capacity,
        mib,
    ) -> None:
        extra = {"capacity": capacity} if capacity else {}
        caps = catalog.traits_for_model(family, generation, **extra)

        assert caps is not None
        assert caps.max_database_bytes == mib << 20

    @pytest.mark.parametrize(
        "family, generation, tx3g, cea608",
        [
            (row[0], row[1], row[2] == "1", row[3] == "1")
            for row in _pipe_rows(_SUBTITLE_ROWS)
        ],
    )
    def test_subtitle_and_caption_support_per_family_generation(
        self,
        family,
        generation,
        tx3g,
        cea608,
    ) -> None:
        caps = catalog.traits_for_model(family, generation)

        assert caps is not None
        assert caps.supports_tx3g_subtitles is tx3g
        assert caps.supports_cea608_captions is cea608

    def test_classic_video_bitrate_stays_within_limit(self) -> None:
        classic = catalog.traits_for_model("iPod Classic", "7th Gen")

        assert classic is not None
        assert classic.max_video_bitrate == 2500
