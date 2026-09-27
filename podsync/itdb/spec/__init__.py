"""The iTunesDB format itself: numbers, header layouts, time handling and playlist rules.

Nothing here touches the filesystem.  Importing the package registers every
header layout with :mod:`podsync.itdb.spec.fields`.
"""

from podsync.itdb.spec import layouts as _layouts  # noqa: F401  (registers LAYOUTS)
