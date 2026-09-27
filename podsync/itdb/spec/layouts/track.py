"""``mhit`` — one track.  The header grew with every iTunes generation."""

from __future__ import annotations

from podsync.itdb.spec.layouts._table import record

MHIT_HEADER_SIZE = 0x270

# (last database version using the size, header size), oldest first.
_HEADER_SIZES = ((0x12, 0x9C), (0x19, 0x148), (0x2D, 0x1F8))


def mhit_header_size_for_version(db_version: int) -> int:
    """The MHIT header length iTunes writes for a given database version."""
    return next((size for newest, size in _HEADER_SIZES if db_version <= newest), MHIT_HEADER_SIZE)


# Fields from 0x9C on only exist in headers long enough to contain them.
MHIT_FIELDS = record("mhit", """
    child_count              u32      0x0C
    track_id                 u32      0x10   required
    visible                  u32      0x14   default=1
    filetype                 u32      0x18                # big-endian fourcc stored LE
    is_vbr                   u8       0x1C
    is_mp3                   u8       0x1D
    compilation_flag         u8       0x1E
    rating                   u8       0x1F   via=rating   # stars × 20
    last_modified            u32      0x20   mac-time
    size                     u32      0x24
    length                   u32      0x28                # milliseconds
    track_number             u32      0x2C
    total_tracks             u32      0x30
    year                     u32      0x34
    bitrate                  u32      0x38
    sample_rate              u32      0x3C   via=fixed16-hz
    volume                   i32      0x40   via=volume
    start_time               u32      0x44
    stop_time                u32      0x48
    sound_check              u32      0x4C
    play_count               u32      0x50
    pending_play_count       u32      0x54
    last_played              u32      0x58   mac-time
    disc_number              u32      0x5C
    total_discs              u32      0x60
    user_id                  u32      0x64
    date_added               u32      0x68   mac-time
    bookmark_time            u32      0x6C
    db_track_id              u64      0x70   required
    checked_flag             u8       0x78                # 0 = checked
    app_rating               u8       0x79
    bpm                      u16      0x7A
    artwork_count            u16      0x7C
    audio_format_flag        u16      0x7E   default=0xFFFF
    artwork_size             u32      0x80
    unk0x84                  u32      0x84
    sample_rate_float        f32      0x88
    date_released            u32      0x8C   mac-time
    mpeg_audio_type          u16      0x90
    explicit_flag            u8       0x92
    purchased_aac_flag       u8       0x93
    unk0x94                  u32      0x94
    genius_category_id       u32      0x98
    # ── extended area ──
    skip_count               u32      0x9C
    last_skipped             u32      0xA0   mac-time
    has_artwork              u8       0xA4
    skip_when_shuffling      u8       0xA5
    remember_position        u8       0xA6
    podcast_now_playing      u8       0xA7
    db_track_id_2            u64      0xA8
    has_lyrics_flag          u8       0xB0
    is_movie                 u8       0xB1
    unplayed_mark            u8       0xB2
    unk0xB3                  u8       0xB3
    unk0xB4                  u32      0xB4
    pregap                   u32      0xB8
    sample_count             u64      0xBC
    unk0xC4                  u32      0xC4
    postgap                  u32      0xC8
    encoder                  u32      0xCC
    media_type               u32      0xD0   default=1
    season_number            u32      0xD4
    episode_number           u32      0xD8
    date_added_to_itunes     u32      0xDC   mac-time
    store_track_id           u32      0xE0
    store_encoder_version    u32      0xE4
    store_artist_id          u32      0xE8
    unk0xEC                  u32      0xEC
    store_album_id           u32      0xF0
    store_content_flag       u32      0xF4
    gapless_payload_bytes    u32      0xF8
    unk0xFC                  u32      0xFC
    gapless_track_flag       u16      0x100
    gapless_album_flag       u16      0x102
    hash_0x104               bytes20  0x104
    unk0x118                 u32      0x118
    unk0x11C                 u32      0x11C
    album_id                 u32      0x120
    library_db_link          u64      0x124
    size_2                   u32      0x12C
    unk0x130                 u32      0x130
    sort_mhod_indicators     bytes8   0x134
    unk0x154                 u32      0x154
    artwork_link             u32      0x160
    unk0x164                 u32      0x164
    unk0x168                 u32      0x168  default=1
    unk0x173                 u8       0x173
    movie_flag_2             u8       0x194
    purchased_aac_flag_2     u8       0x195
    unk0x197                 u8       0x197
    unk0x1A0                 u32      0x1A0
    store_track_id_2         u64      0x1B0
    store_encoder_version_2  u64      0x1B8
    store_artist_id_2        u64      0x1C0
    unk0xEC_2                u64      0x1C8
    store_album_id_2         u64      0x1D0
    store_content_flag_2     u64      0x1D8
    artist_link              u32      0x1E0
    unk0x1EC                 u32      0x1EC
    composer_id              u32      0x1F4
    unk0x1F8                 u32      0x1F8
    unk0x20C                 u32      0x20C
    unk0x229                 u8       0x229
    unk0x22B                 u8       0x22B
""", extended_from=0x9C)
