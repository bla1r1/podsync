# podsync layout map

The chapters in this folder describe `podsync` with its original module
layout and identifiers.  The implementation keeps the package name but uses
its own layout (`itdb`, `artwork`, `hardware`, `library`), its own names and
snake_case record keys.  This page maps the specification onto the code;
behaviour is as the chapters describe unless listed under *Additions* below.

## Packages

| Spec | Implementation |
|---|---|
| `podsync.itunesdb_shared` | `podsync.itdb.spec` |
| `podsync.itunesdb_parser` | `podsync.itdb.reader` |
| `podsync.itunesdb_writer` | `podsync.itdb.writer` |
| `podsync.sqlitedb_writer` | `podsync.itdb.sqlite` |
| `podsync.artworkdb_shared` | `podsync.artwork.spec` |
| `podsync.artworkdb_parser` | `podsync.artwork.reader` |
| `podsync.artworkdb_writer` | `podsync.artwork.writer` |
| `podsync.device` | `podsync.hardware` (with `catalog`, `safety`, `probes`, `linux`, `discovery`, `virtual`, `diagnostics`) |
| `podsync.sync` | `podsync.library` |

## Modules

| Spec module | Implementation module |
|---|---|
| `podsync.artworkdb_parser` | `podsync.artwork.reader` |
| `podsync.artworkdb_parser.chunk_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.constants` | `podsync.artwork.spec.format` (merged) |
| `podsync.artworkdb_parser.mhfd_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.mhii_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.mhli_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.mhni_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.mhod_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.mhsd_parser` | `podsync.artwork.reader.records` (merged) |
| `podsync.artworkdb_parser.parser` | `podsync.artwork.reader` (merged) |
| `podsync.artworkdb_shared` | `podsync.artwork.spec` |
| `podsync.artworkdb_shared.binary` | `podsync.artwork.spec.format` (merged) |
| `podsync.artworkdb_shared.constants` | `podsync.artwork.spec.format` (merged) |
| `podsync.artworkdb_shared.ithmb_paths` | `podsync.artwork.spec.files` (merged) |
| `podsync.artworkdb_shared.mhlf` | `podsync.artwork.spec.files` (merged) |
| `podsync.artworkdb_shared.mhni` | `podsync.artwork.spec.renditions` (merged) |
| `podsync.artworkdb_shared.mhod` | `podsync.artwork.spec.format` (merged) |
| `podsync.artworkdb_writer` | `podsync.artwork.writer` |
| `podsync.artworkdb_writer.art_extractor` | `podsync.artwork.writer.covers` |
| `podsync.artworkdb_writer.artwork_types` | `podsync.artwork.writer.records` |
| `podsync.artworkdb_writer.artwork_writer` | `podsync.artwork.writer.pipeline` |
| `podsync.artworkdb_writer.artworkdb_chunks` | `podsync.artwork.writer.chunks` |
| `podsync.artworkdb_writer.ithmb_codecs` | `podsync.artwork.writer.codecs` |
| `podsync.artworkdb_writer.rgb565` | `podsync.artwork.writer.pixels` |
| `podsync.device` | `podsync.hardware` |
| `podsync.device.artwork` | `podsync.hardware.catalog.artwork` |
| `podsync.device.artwork_presets` | `podsync.hardware.catalog.artwork_presets` |
| `podsync.device.bootstrap` | `podsync.hardware.bootstrap` |
| `podsync.device.capabilities` | `podsync.hardware.catalog.capabilities` |
| `podsync.device.checksum` | `podsync.hardware.catalog.checksum` |
| `podsync.device.diagnostic_log` | `podsync.hardware.diagnostics.log` |
| `podsync.device.dump` | `podsync.hardware.diagnostics.dump` |
| `podsync.device.durability` | `podsync.hardware.safety.durable` |
| `podsync.device.eject` | `podsync.hardware.eject` |
| `podsync.device.filesystem` | `podsync.hardware.safety.fstype` |
| `podsync.device.filesystem_profile` | `podsync.hardware.safety.fsprofile` |
| `podsync.device.info` | `podsync.hardware.current`, `podsync.hardware.enrich` |
| `podsync.device.linux_identity` | `podsync.hardware.linux.identity` |
| `podsync.device.linux_integration` | `podsync.hardware.linux.udev` |
| `podsync.device.lookup` | `podsync.hardware.catalog.lookup` |
| `podsync.device.metadata_write` | `podsync.hardware.safety.sysinfo_write` |
| `podsync.device.models` | `podsync.hardware.catalog.models` |
| `podsync.device.path_safety` | `podsync.hardware.safety.paths` |
| `podsync.device.recovery` | `podsync.hardware.linux.recovery` |
| `podsync.device.scanner` | `podsync.hardware.discovery.scan`, `podsync.hardware.discovery.resolve`, `podsync.hardware.discovery.windows`, `podsync.hardware.discovery.macos`, `podsync.hardware.discovery.master_title` |
| `podsync.device.storage_safety` | `podsync.hardware.safety.limits` |
| `podsync.device.sysinfo` | `podsync.hardware.catalog.sysinfo` |
| `podsync.device.usb_backend` | `podsync.hardware.probes.backend` |
| `podsync.device.virtual` | `podsync.hardware.virtual.device` |
| `podsync.device.virtual_identity` | `podsync.hardware.virtual.identity` |
| `podsync.device.vpd_iokit` | `podsync.hardware.probes.macos` |
| `podsync.device.vpd_libusb` | `podsync.hardware.probes.libusb` |
| `podsync.device.vpd_linux` | `podsync.hardware.probes.linux`, `podsync.hardware.probes.scsi` |
| `podsync.device.vpd_usb_control` | `podsync.hardware.probes.usb_control` |
| `podsync.device.vpd_windows` | `podsync.hardware.probes.windows` |
| `podsync.device.write_guard` | `podsync.hardware.safety.guard` |
| `podsync.device.write_readiness` | `podsync.hardware.safety.readiness` |
| `podsync.itunesdb_parser` | `podsync.itdb.reader` |
| `podsync.itunesdb_parser._parsing` | `podsync.itdb.reader.primitives` |
| `podsync.itunesdb_parser.artwork_links` | `podsync.itdb.reader.cover_links` |
| `podsync.itunesdb_parser.byte_walk` | `podsync.itdb.reader.byte_index` |
| `podsync.itunesdb_parser.chunk_parser` | `podsync.itdb.reader.walker` |
| `podsync.itunesdb_parser.exceptions` | `podsync.itdb.reader.errors` |
| `podsync.itunesdb_parser.forensics` | `podsync.itdb.reader.diagnose` |
| `podsync.itunesdb_parser.ipod_library` | `podsync.itdb.reader.library` |
| `podsync.itunesdb_parser.mhbd_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhia_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhii_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhip_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhit_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhod_parser` | `podsync.itdb.reader.chunks.strings` |
| `podsync.itunesdb_parser.mhsd_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.mhyp_parser` | `podsync.itdb.reader.chunks.records` (merged) |
| `podsync.itunesdb_parser.otg` | `podsync.itdb.reader.onthego` |
| `podsync.itunesdb_parser.parser` | `podsync.itdb.reader.entry` |
| `podsync.itunesdb_parser.playcounts` | `podsync.itdb.reader.play_stats` |
| `podsync.itunesdb_shared` | `podsync.itdb.spec` |
| `podsync.itunesdb_shared.album_identity` | `podsync.itdb.spec.albums` |
| `podsync.itunesdb_shared.constants` | `podsync.itdb.spec.codes` |
| `podsync.itunesdb_shared.device_time` | `podsync.itdb.spec.clock` |
| `podsync.itunesdb_shared.extraction` | `podsync.itdb.spec.flatten` |
| `podsync.itunesdb_shared.field_base` | `podsync.itdb.spec.fields` |
| `podsync.itunesdb_shared.mhbd_defs` | `podsync.itdb.spec.layouts.header` |
| `podsync.itunesdb_shared.mhia_defs` | `podsync.itdb.spec.layouts.album` |
| `podsync.itunesdb_shared.mhii_defs` | `podsync.itdb.spec.layouts.artist` |
| `podsync.itunesdb_shared.mhip_defs` | `podsync.itdb.spec.layouts.entry` |
| `podsync.itunesdb_shared.mhit_defs` | `podsync.itdb.spec.layouts.track` |
| `podsync.itunesdb_shared.mhod_defs` | `podsync.itdb.spec.layouts.strings` |
| `podsync.itunesdb_shared.mhsd_defs` | `podsync.itdb.spec.layouts.dataset` |
| `podsync.itunesdb_shared.mhyp_defs` | `podsync.itdb.spec.layouts.playlist` |
| `podsync.itunesdb_shared.playlist_hierarchy` | `podsync.itdb.spec.playlists.tree` |
| `podsync.itunesdb_shared.playlist_kinds` | `podsync.itdb.spec.playlists.kinds` |
| `podsync.itunesdb_shared.playlist_lifecycle` | `podsync.itdb.spec.playlists.lifecycle` |
| `podsync.itunesdb_shared.playlist_properties` | `podsync.itdb.spec.playlists.properties` |
| `podsync.itunesdb_writer` | `podsync.itdb.writer` |
| `podsync.itunesdb_writer.hash58` | `podsync.itdb.writer.signing.hmac58` |
| `podsync.itunesdb_writer.hash72` | `podsync.itdb.writer.signing.aes72` |
| `podsync.itunesdb_writer.hashab` | `podsync.itdb.writer.signing.ab` |
| `podsync.itunesdb_writer.mhbd_writer` | `podsync.itdb.writer.database` |
| `podsync.itunesdb_writer.mhip_writer` | `podsync.itdb.writer.playlist` (merged) |
| `podsync.itunesdb_writer.mhit_writer` | `podsync.itdb.writer.track` |
| `podsync.itunesdb_writer.mhla_writer` | `podsync.itdb.writer.lists` (merged) |
| `podsync.itunesdb_writer.mhli_writer` | `podsync.itdb.writer.lists` (merged) |
| `podsync.itunesdb_writer.mhlp_writer` | `podsync.itdb.writer.lists` (merged) |
| `podsync.itunesdb_writer.mhlt_writer` | `podsync.itdb.writer.lists` (merged) |
| `podsync.itunesdb_writer.mhod52_writer` | `podsync.itdb.writer.sort_index` |
| `podsync.itunesdb_writer.mhod_spl_writer` | `podsync.itdb.writer.smart_rules` |
| `podsync.itunesdb_writer.mhod_writer` | `podsync.itdb.writer.strings` |
| `podsync.itunesdb_writer.mhsd_writer` | `podsync.itdb.writer.lists` (merged) |
| `podsync.itunesdb_writer.mhyp_writer` | `podsync.itdb.writer.playlist` |
| `podsync.sync` | `podsync.library` |
| `podsync.sync._db_io` | `podsync.library.database` |
| `podsync.sync._playlist_builder` | `podsync.library.playlists` |
| `podsync.sync._track_conversion` | `podsync.library.tracks` |
| `podsync.sync.ipod_track_paths` | `podsync.library.media_paths` |
| `podsync.sync.path_identity` | `podsync.library.paths` |
| `podsync.sync.spl_evaluator` | `podsync.library.smart` |

## Identifiers

Public names and the private names that tests patch.  Other private helpers and local
variables named in the chapters were restructured freely and are not listed.

| Spec name | Implementation name |
|---|---|
| `_build_artwork_id_to_song_id` | `_songs_by_image` |
| `_build_song_to_artwork_id` | `_images_by_song` |
| `_compute_itunesdb_sha1` | `signature_digest` |
| `_database_media_path_key` | `_media_identity_key` |
| `_db_io` | `library_db` |
| `_estimate_capacity` | `estimate_capacity` |
| `_f32` | `f32` |
| `_hash_generate` | `hash72_signature` |
| `_i32` | `i32` |
| `_int_or_default` | `_to_int` |
| `_parse_macos_ioreg_bsd_serials` | `parse_ioreg_bsd_serials` |
| `_parse_mhod51` | `decode_rule_list` |
| `_raw` | `raw_bytes` |
| `_read_master_playlist_title` | `read_master_playlist_title` |
| `_resolve_model` | `resolve_model` |
| `_section` | `stamp_section` |
| `_u16` | `u16` |
| `_u32` | `u32` |
| `_U32_MAX` | `U32_MAX` |
| `_u64` | `u64` |
| `_u8` | `u8` |
| `aac` | `m4a` |
| `alac` | `m4a` |
| `artwork_links` | `cover_links` |
| `build_and_evaluate_playlists` | `assemble_playlists` |
| `byte_walk` | `byte_index` |
| `ByteWalkChunkCache` | `ByteIndexCache` |
| `ByteWalkChunkIndexEntry` | `ByteIndexEntry` |
| `CachedIpodMusicPathResolver` | `MusicFolderCache` |
| `capabilities_for_family_gen` | `traits_for_model` |
| `capture_database_generation` | `snapshot_database_state` |
| `ChecksumType` | `SignatureKind` |
| `chunk_type_map` | `DATASET_RESULT_KEYS` |
| `clamp_rating` | `bounded_rating` |
| `coerce_int` | `as_int` |
| `create_virtual_ipod` | `make_virtual_ipod` |
| `current_device_time_context` | `active_device_clock` |
| `DatabaseVerificationError` | `ReadbackError` |
| `decode_raw_blob` | `blob_from_cache` |
| `delete_playcounts_files` | `clear_device_play_state` |
| `detect_checksum_type` | `detect_signature_kind` |
| `detect_filesystem_type` | `detect_volume_format` |
| `DeviceBusyError` | `IpodBusyError` |
| `DeviceCapabilities` | `ModelTraits` |
| `DeviceInfo` | `IpodDevice` |
| `DeviceMetadataWriteSession` | `SysInfoWriteSession` |
| `DeviceTimeContext` | `DeviceClock` |
| `DeviceWriteGuard` | `WriteLock` |
| `DeviceWriteSafetyError` | `UnsafeWriteError` |
| `durability` | `durable` |
| `durable_publish_new` | `safe_publish` |
| `durable_replace` | `safe_replace` |
| `durable_unlink` | `safe_unlink` |
| `effective_max_file_size_bytes` | `max_file_bytes` |
| `eject_ipod` | `eject_device` |
| `eval_rule` | `rule_matches` |
| `existing_ipod_track_file_path` | `find_media_file` |
| `expected_ipod_track_file_path` | `expected_media_path` |
| `ExternalDatabaseChangeError` | `DatabaseChangedElsewhereError` |
| `extract_datasets` | `split_datasets` |
| `extract_mhod_strings` | `collect_strings` |
| `extract_playlist_extras` | `collect_playlist_extras` |
| `extract_playlist_item_extras` | `collect_entry_extras` |
| `extract_track_extras` | `collect_track_extras` |
| `FIELD_REGISTRY` | `LAYOUTS` |
| `FieldDef` | `FieldSpec` |
| `FileSizeLimitError` | `FileTooLargeError` |
| `filesystem` | `fstype` |
| `filesystem_profile` | `fsprofile` |
| `FilesystemProfile` | `VolumeProfile` |
| `FilesystemRevalidation` | `VolumeRecheck` |
| `filetype_to_string` | `fourcc_text` |
| `fixed_to_sample_rate` | `fixed16_to_hz` |
| `flush_filesystem` | `flush_volume` |
| `GENERIC_HEADER_SIZE` | `CHUNK_HEADER_SIZE` |
| `GENERIC_HEADER_STRUCT` | `CHUNK_HEADER_FORMAT` |
| `get_current_device` | `selected_device` |
| `get_current_device_for_path` | `selected_device_at` |
| `get_fields` | `layout_for` |
| `get_version_name` | `itunes_version_label` |
| `guarded_device_metadata_session` | `sysinfo_write_session` |
| `hashing_scheme` | `itunes` |
| `hydrate_track_artwork_refs` | `attach_cover_refs` |
| `identifier_readable_map` | `CHUNK_LABELS` |
| `identify_ipod_at_path` | `identify_mounted_ipod` |
| `identify_via_vpd` | `identify_by_vpd` |
| `info` | `current` |
| `inspect_device_write_readiness` | `check_write_ready` |
| `inspect_filesystem_profile` | `profile_volume` |
| `InvalidFieldValueError` | `BadFieldValueError` |
| `ipod_filetype_for_extension` | `format_for_extension` |
| `ipod_location_from_file_path` | `location_for_media_path` |
| `itdb_write_filename` | `database_filename_for_write` |
| `linux_filesystem_recovery_plan` | `linux_repair_plan` |
| `linux_identity_setup_needed` | `udev_rule_needed` |
| `linux_integration` | `udev` |
| `LinuxMountDetails` | `LinuxMountFacts` |
| `load_otg_playlists` | `load_onthego_playlists` |
| `MacTimestampOutOfRangeError` | `MacTimeRangeError` |
| `merge_playcounts` | `apply_play_stats` |
| `metadata_write` | `sysinfo_write` |
| `mhbd_writer` | `db_writer` |
| `mhod_type_map` | `MHOD_FIELD_KEYS` |
| `MissingRequiredFieldError` | `MissingFieldError` |
| `mov` | `m4v` |
| `parse_itunesdb` | `read_itdb` |
| `parse_mhod` | `parse_data_object` |
| `parse_playcounts` | `read_play_stats` |
| `PlayCountEntry` | `PlayStatsEntry` |
| `playcounts` | `play_stats` |
| `PlaylistInfo` | `PlaylistRecord` |
| `PlaylistItemMeta` | `EntryMeta` |
| `prefs_from_parsed` | `prefs_from_row` |
| `probe_linux_identity` | `linux_device_identity` |
| `read_device_time_context` | `load_device_clock` |
| `read_existing_database` | `load_device_library` |
| `read_field` | `decode_field` |
| `read_fields` | `decode_fields` |
| `reconcile_playlist_hierarchy` | `normalize_playlist_tree` |
| `require_file_size_supported` | `ensure_file_fits` |
| `resolve_device_path` | `safe_device_path` |
| `resolve_itdb_path` | `locate_database` |
| `revalidate_device_write_readiness` | `recheck_write_ready` |
| `revalidate_filesystem_profile` | `recheck_volume` |
| `rgb565` | `pixels` |
| `RuleGroup` | `NestedRules` |
| `rules_from_parsed` | `rules_from_row` |
| `sample_rate_to_fixed` | `hz_to_fixed16` |
| `scan_for_ipods` | `find_ipods` |
| `scanner` | `scan` |
| `serial` | `udev_scsi_id` |
| `set_current_device` | `select_device` |
| `SmartPlaylistPrefs` | `SmartPrefs` |
| `SmartPlaylistRule` | `SmartRule` |
| `SmartPlaylistRules` | `SmartRuleSet` |
| `sort_trackinfos_by_order` | `order_track_ids` |
| `sort_tracks_by_order` | `order_rows` |
| `spl_update` | `evaluate_smart_playlist` |
| `spl_update_all` | `evaluate_all_smart_playlists` |
| `spl_update_from_parsed` | `evaluate_parsed_smart_playlist` |
| `stable_path_key` | `path_key` |
| `status` | `padding` |
| `storage_safety` | `limits` |
| `strip_article` | `without_article` |
| `timezone_changed_since_database` | `zone_changed_since_write` |
| `track_dict_to_info` | `record_from_row` |
| `TrackInfo` | `TrackRecord` |
| `trackinfo_to_eval_dict` | `rule_view_of` |
| `UnsafeDevicePathError` | `PathEscapeError` |
| `use_device_time_context` | `device_clock_scope` |
| `validate_rating` | `check_rating` |
| `validate_volume` | `check_volume` |
| `verify_written_database` | `verify_saved_library` |
| `version_map` | `ITUNES_VERSIONS` |
| `volume_lock_key` | `lock_key_for` |
| `vpd_libusb` | `libusb_probe` |
| `vpd_linux` | `linux_probe` |
| `write_database` | `save_device_library` |
| `write_field` | `encode_field` |
| `write_fields` | `encode_fields` |
| `write_generic_header` | `pack_chunk_header` |
| `write_guard` | `guard_mod` |
| `write_itunesdb` | `write_itdb` |
| `write_list_chunk` | `list_chunk_bytes` |
| `write_list_header` | `list_header_bytes` |
| `write_readiness` | `readiness` |
| `WriteError` | `FieldWriteError` |

## Record keys

Parsed rows use snake_case keys instead of the spec's display labels.

| Spec key | Implementation key |
|---|---|
| `Title` | `title` |
| `Location` | `location` |
| `Album` | `album` |
| `Artist` | `artist` |
| `Genre` | `genre` |
| `Filetype` | `kind` |
| `Comment` | `comment` |
| `Category` | `category` |
| `Lyrics` | `lyrics` |
| `Composer` | `composer` |
| `Grouping` | `grouping` |
| `Description Text` | `description` |
| `Podcast Enclosure URL` | `enclosure_url` |
| `Podcast RSS URL` | `feed_url` |
| `Chapter Data` | `chapter_blob` |
| `Subtitle` | `subtitle` |
| `Show` | `show` |
| `Episode` | `episode` |
| `TV Network` | `network` |
| `Album Artist` | `album_artist` |
| `Sort Artist` | `sort_artist` |
| `Track Keywords` | `keywords` |
| `Show Locale` | `show_locale` |
| `iTunes Store Asset Info` | `store_asset_info` |
| `Sort Title` | `sort_title` |
| `Sort Album` | `sort_album` |
| `Sort Album Artist` | `sort_album_artist` |
| `Sort Composer` | `sort_composer` |
| `Sort Show` | `sort_show` |
| `Unknown for Video Track` | `video_unknown` |
| `Unknown (33)` | `unknown_33` |
| `Unknown (34)` | `unknown_34` |
| `Unknown (35)` | `unknown_35` |
| `Unknown (36)` | `unknown_36` |
| `Content Provider` | `content_provider` |
| `Unknown (38)` | `unknown_38` |
| `Copyright` | `copyright` |
| `Unknown (40)` | `unknown_40` |
| `Unknown (41)` | `unknown_41` |
| `Encoding Quality Descriptor` | `encoding_quality` |
| `Purchase Account` | `purchase_account` |
| `Purchaser Name` | `purchaser_name` |
| `Smart Playlist Data` | `smart_prefs_record` |
| `Smart Playlist Rules` | `smart_rules_record` |
| `Library Playlist Index` | `library_index` |
| `Library Playlist Jump Table` | `library_jump_table` |
| `Playlist Property Plist` | `playlist_property_plist` |
| `Column Size or Playlist Order` | `playlist_order` |
| `Playlist Settings (binary)` | `playlist_settings_blob` |
| `Album (Used by Album Item)` | `album_name` |
| `Artist (Used by Album Item)` | `album_artist_name` |
| `Sort Artist (Used by Album Item)` | `album_sort_artist` |
| `Podcast URL (Used by Album Item)` | `album_feed_url` |
| `Show (Used by Album Item)` | `album_show` |
| `Artist (Used by Artist Item)` | `artist_name` |
| `Source Path` | `source_path` |
| `Source Relative Path` | `source_relative_path` |
| `Sort Name` | `sort_title` |
| `Purchased` | `purchased` |
| `play_count_1` | `play_count` |
| `play_count_2` | `pending_play_count` |
| `sample_rate_1` | `sample_rate` |
| `sample_rate_2` | `sample_rate_float` |
| `use_podcast_now_playing_flag` | `podcast_now_playing` |
| `not_played_flag` | `unplayed_mark` |
| `gapless_audio_payload_size` | `gapless_payload_bytes` |
| `artwork_id_ref` | `artwork_link` |
| `artist_id_ref` | `artist_link` |
| `db_id_2_ref` | `library_db_link` |
| `group_id_ref` | `group_link` |
| `vbr_flag` | `is_vbr` |
| `mp3_flag` | `is_mp3` |
| `movie_flag` | `is_movie` |
| `lyrics_flag` | `has_lyrics_flag` |

## Additions beyond the specification

* `podsync.hardware.probes.windows.storage_identity` — `IOCTL_STORAGE_QUERY_PROPERTY` plus a
  SetupAPI/cfgmgr32 walk of the PnP tree (chapter 06 §9.4 step 1–2); `usb_ids_from_registry` is the
  registry PID fallback of step 3.
* Live SCSI VPD on Windows: `identify_by_vpd` accepts a drive-letter mount path on Windows, and the
  scanner/enrich use it (the spec skips VPD on Windows).
* `parse_sysinfo_extended` repairs `<key>` elements that iPod 5G/5.5G firmware places inside
  `<array>` (`ParsedSysInfoExtended.repaired`), so image formats survive instead of falling back to
  the regex scan.
* Windows eject calls `CM_Request_Device_EjectW` directly instead of compiling a helper class in
  PowerShell.

