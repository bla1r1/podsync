# podsync

A pure-Python engine for reading and writing the databases an iPod keeps
in `iPod_Control\iTunes` (the iTunesDB family) and `iPod_Control\Artwork`
(the ArtworkDB family), plus the device-identity, hardware-discovery and
write-safety machinery around them. No iTunes, no third-party iPod tools —
everything is produced byte-for-byte the way the device itself expects.

## What it does

- Parses and writes iTunesDB (tracks, playlists, smart playlists, play
  counts, on-the-go lists) and ArtworkDB (`.ithmb` artwork chunks).
- Writes the SQLite `iTunes Library.itlp` database family iPod nano 5G-7G
  use instead of the classic iTunesDB (`podsync/itdb/sqlite/`) — write-only,
  no reader.
- Discovers connected iPods on macOS, Windows and Linux, identifies the
  model/generation from USB, SCSI VPD and on-device SysInfo evidence, and
  enforces write-safety checks before touching a device.
- On macOS, queries live SCSI VPD data through IOKit's `SCSITaskLib`
  (`podsync/hardware/probes/macos.py`) — no root, no iTunes required.

See `docs/01-overview.md` for the full architecture and scope, and
`docs/00-layout-map.md` for how the specification chapters map to the
actual module layout under `podsync/`.

## Scope

In scope: iTunesDB/ArtworkDB parsing and writing, device identity and
discovery, write-safety guards. Explicitly **out of scope** (see
`docs/01-overview.md` §5 for the full list): a GUI, podcasts, photos,
scrobbling, backups, transcoding, an updater, iPod Shuffle's `iTunesSD`,
and iPod Touch/iPhone (those use a different sync protocol entirely).

## Requirements

- Python 3, `pip install -r requirements.txt` (pytest, pycryptodome,
  pillow, pyusb, wasmtime — see that file for exact versions; nothing else
  is used by the package itself).
- The macOS USB-vendor VPD path additionally needs a libusb-1.0 library
  on the system (e.g. `brew install libusb`); it degrades gracefully to
  "unavailable" without one.
- Signing a database for iPod nano 6G/7G (HASHAB) runs a vendored
  WebAssembly module through `wasmtime`; every other device is unaffected
  if that path is never used.

## Running the tests

```bash
python -m pytest
```

## License

GPL-3.0-only — see `COPYRIGHT`.
