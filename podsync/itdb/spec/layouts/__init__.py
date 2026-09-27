"""Header layouts of the iTunesDB records, registered with the field codec on import."""

from __future__ import annotations

from podsync.itdb.spec.fields import LAYOUTS
from podsync.itdb.spec.layouts import album, artist, dataset, entry, header, playlist, strings, track

LAYOUTS.update({
    "mhbd": header.MHBD_FIELDS,
    "mhsd": dataset.MHSD_FIELDS,
    "mhit": track.MHIT_FIELDS,
    "mhia": album.MHIA_FIELDS,
    "mhii": artist.MHII_FIELDS,
    "mhip": entry.MHIP_FIELDS,
    "mhyp": playlist.MHYP_FIELDS,
    "mhod": strings.MHOD_FIELDS,
})
