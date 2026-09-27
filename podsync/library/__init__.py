"""The library layer: device database in, writer-ready records out, and back again.

Import the concrete modules; this package deliberately re-exports nothing:

* :mod:`podsync.library.database` — load, save, verify, post-commit cleanup
* :mod:`podsync.library.tracks` — parsed rows ⇄ :class:`TrackRecord`
* :mod:`podsync.library.playlists` — playlist assembly and sort orders
* :mod:`podsync.library.smart` — smart-playlist rule engine
* :mod:`podsync.library.media_paths` — database locations ⇄ files on the device
* :mod:`podsync.library.paths` — host path identity and integer coercion
"""
