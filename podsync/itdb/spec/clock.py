"""Device clocks: turning iPod-local Mac timestamps into real instants and back.

The iPod stores times as *wall-clock* seconds since 1904-01-01 in whatever zone
the device is set to.  A :class:`DeviceClock` carries that zone.  The zone is
discovered from ``iPod_Control/Device/Preferences`` (layout depends on the
firmware generation), falling back to the offset stamped in the database
header, then UTC.
"""

from __future__ import annotations

import logging
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

MAC_EPOCH_OFFSET = 2_082_844_800  # seconds from 1904-01-01 to 1970-01-01
MAC_EPOCH = datetime(1904, 1, 1)
MAC_U32_MAX = 0xFFFF_FFFF
_DAY = 86_400
_HOUR = 3_600


class MacTimeRangeError(ValueError):
    """A UTC instant cannot be represented as a u32 device-local Mac timestamp."""


# POSIX-style names some firmware tables use, mapped to IANA zones.
_ZONE_ALIASES: dict[str, str] = {
    "PST8PDT": "America/Los_Angeles",
    "MST7MDT": "America/Denver",
    "CST6CDT": "America/Chicago",
    "EST5EDT": "America/New_York",
    "EET": "Europe/Athens",
}


@dataclass(frozen=True, slots=True)
class DeviceClock:
    """The zone an iPod's wall-clock timestamps are expressed in."""

    timezone: tzinfo
    name: str
    source: str  # "utc", "database_header" or "device_preferences"
    city_id: int | None = None

    @classmethod
    def utc(cls) -> DeviceClock:
        return cls(UTC, "UTC", "utc")

    @classmethod
    def fixed_offset(cls, seconds: int, *, source: str = "database_header") -> DeviceClock:
        seconds = int(seconds)
        if abs(seconds) > _DAY:
            raise ValueError(f"invalid UTC offset: {seconds}")
        return cls(timezone(timedelta(seconds=seconds)), f"UTC{seconds:+d}", source)

    @classmethod
    def from_timezone_name(
        cls, timezone_name: str, *, source: str = "device_preferences", city_id: int | None = None,
    ) -> DeviceClock:
        canonical = _ZONE_ALIASES.get(timezone_name, timezone_name)
        try:
            zone = ZoneInfo(canonical)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown device timezone {timezone_name!r}") from exc
        return cls(zone, canonical, source, city_id)

    def offset_at_unix(self, unix_timestamp: int) -> int:
        """UTC offset (seconds) the zone had at a given instant — DST aware."""
        offset = datetime.fromtimestamp(int(unix_timestamp), UTC).astimezone(self.timezone).utcoffset()
        return int(offset.total_seconds()) if offset is not None else 0

    def mac_to_unix(self, mac_timestamp: int) -> int:
        """Device wall-clock Mac seconds → Unix time.  Zero/negative means "never" → 0."""
        mac = int(mac_timestamp or 0)
        if mac <= 0:
            return 0
        wall = MAC_EPOCH + timedelta(seconds=mac)
        return int(wall.replace(tzinfo=self.timezone).timestamp())

    def unix_to_mac(self, unix_timestamp: int) -> int:
        """Unix time → device wall-clock Mac seconds; raises when it does not fit in u32."""
        unix = int(unix_timestamp or 0)
        if unix <= 0:
            return 0
        problem = (
            f"unix time {unix} cannot be stored as a device-local Mac timestamp "
            f"in {self.name} (device local time ends at 2040-02-06 06:28:15)"
        )
        try:
            wall = datetime.fromtimestamp(unix, UTC).astimezone(self.timezone).replace(tzinfo=None)
            mac = int((wall - MAC_EPOCH).total_seconds())
        except (OverflowError, OSError, ValueError) as exc:
            raise MacTimeRangeError(problem) from exc
        if not 0 < mac <= MAC_U32_MAX:
            raise MacTimeRangeError(problem)
        return mac


# ── the clock in effect for the current context ─────────────────────

_current: ContextVar[DeviceClock | None] = ContextVar("podsync_device_clock", default=None)


def active_device_clock() -> DeviceClock:
    """The clock set by :func:`device_clock_scope`, or UTC outside any scope."""
    return _current.get() or DeviceClock.utc()


def active_device_time_context() -> DeviceClock | None:
    """The explicitly scoped clock, or ``None`` when no scope is active."""
    return _current.get()


@contextmanager
def device_clock_scope(context: DeviceClock) -> Iterator[DeviceClock]:
    """Make *context* the active clock for the duration of the ``with`` block."""
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


# ── Preferences file decoding ───────────────────────────────────────

# City codes of the late-generation Preferences file, grouped by the zone they map to.
_CITIES_BY_ZONE: dict[str, tuple[int, ...]] = {
    "PST8PDT": (0x01, 0x05, 0x07, 0x08, 0x09, 0x0A, 0x18),
    "America/Chicago": (0x02,),
    "Pacific/Honolulu": (0x03,),
    "America/Anchorage": (0x04,),
    "America/Los_Angeles": (0x06,),
    "America/Vancouver": (0x0B,),
    "MST7MDT": (0x0C, 0x0F),
    "America/Denver": (0x0D,),
    "America/Phoenix": (0x0E,),
    "CST6CDT": (0x10, 0x11, 0x14, 0x16, 0x1A, 0xC7),
    "America/Guatemala": (0x12,),
    "America/Managua": (0x13,),
    "America/Mexico_City": (0x15,),
    "America/Regina": (0x17,),
    "America/El_Salvador": (0x19,),
    "America/Tegucigalpa": (0x1B,),
    "America/Winnipeg": (0x1C,),
    "EST5EDT": (0x1D, 0x1F, 0x20, 0x24, 0x27, 0x2A, 0x2C, 0x30),
    "America/Bogota": (0x1E,),
    "America/Detroit": (0x21,),
    "America/Havana": (0x22,),
    "America/Indiana/Indianapolis": (0x23,),
    "America/Lima": (0x25,),
    "Europe/London": (0x26, 0x48, 0x4C, 0x4F),
    "America/Montreal": (0x28,),
    "America/New_York": (0x29,),
    "America/Panama": (0x2B,),
    "America/Port-au-Prince": (0x2D,),
    "America/Guayaquil": (0x2E,),
    "America/Toronto": (0x2F,),
    "America/Asuncion": (0x31,),
    "America/Caracas": (0x32,),
    "America/Guyana": (0x33,),
    "America/Halifax": (0x34,),
    "America/La_Paz": (0x35,),
    "America/Argentina/San_Juan": (0x36,),
    "America/Santiago": (0x37,),
    "America/Santo_Domingo": (0x38,),
    "America/St_Johns": (0x39,),
    "America/Sao_Paulo": (0x3A, 0x42),
    "America/Argentina/Buenos_Aires": (0x3B,),
    "America/Cayenne": (0x3C,),
    "America/Montevideo": (0x3D,),
    "America/Godthab": (0x3E,),
    "America/Paramaribo": (0x3F,),
    "America/Recife": (0x40,),
    "Africa/Casablanca": (0x41, 0x68),
    "Atlantic/South_Georgia": (0x43,),
    "Atlantic/Azores": (0x44,),
    "Europe/Dublin": (0x45, 0x4B),
    "Africa/Accra": (0x46,),
    "Africa/Bamako": (0x47,),
    "Africa/Conakry": (0x49,),
    "Africa/Dakar": (0x4A,),
    "Africa/Freetown": (0x4D,),
    "Europe/Lisbon": (0x4E,),
    "Africa/Monrovia": (0x50,),
    "Africa/Nouakchott": (0x51,),
    "Africa/Ouagadougou": (0x52,),
    "Atlantic/Reykjavik": (0x53,),
    "Africa/Algiers": (0x54,),
    "Europe/Amsterdam": (0x55,),
    "Africa/Bangui": (0x56,),
    "Europe/Belgrade": (0x57,),
    "Europe/Berlin": (0x58, 0x63),
    "Europe/Brussels": (0x59,),
    "Europe/Budapest": (0x5A,),
    "Europe/Copenhagen": (0x5B,),
    "Africa/Douala": (0x5C,),
    "Europe/Paris": (0x5D, 0x60, 0x66, 0x6F),
    "Africa/Kinshasa": (0x5E,),
    "Africa/Lagos": (0x5F,),
    "Africa/Luanda": (0x61,),
    "Europe/Madrid": (0x62,),
    "Africa/Ndjamena": (0x64,),
    "Europe/Oslo": (0x65,),
    "Europe/Prague": (0x67,),
    "Europe/Rome": (0x69,),
    "Europe/Stockholm": (0x6A,),
    "Africa/Tripoli": (0x6B,),
    "Africa/Tunis": (0x6C,),
    "Europe/Vienna": (0x6D,),
    "Europe/Warsaw": (0x6E,),
    "Europe/Zurich": (0x70,),
    "Asia/Amman": (0x71,),
    "EET": (0x72,),
    "Asia/Beirut": (0x73,),
    "Europe/Bucharest": (0x74,),
    "Africa/Cairo": (0x75,),
    "Africa/Johannesburg": (0x76,),
    "Africa/Harare": (0x77,),
    "Europe/Helsinki": (0x78,),
    "Europe/Istanbul": (0x79,),
    "Asia/Jerusalem": (0x7A,),
    "Africa/Khartoum": (0x7B,),
    "Europe/Kiev": (0x7C,),
    "Africa/Lusaka": (0x7D,),
    "Africa/Maputo": (0x7E,),
    "Europe/Sofia": (0x7F,),
    "Africa/Addis_Ababa": (0x80,),
    "Indian/Antananarivo": (0x81,),
    "Africa/Asmara": (0x82,),
    "Asia/Baghdad": (0x83,),
    "Asia/Damascus": (0x84,),
    "Africa/Dar_es_Salaam": (0x85,),
    "Africa/Djibouti": (0x86,),
    "Asia/Qatar": (0x87,),
    "Africa/Kampala": (0x88,),
    "Asia/Bahrain": (0x89,),
    "Asia/Riyadh": (0x8A, 0x8E),
    "Africa/Mogadishu": (0x8B,),
    "Europe/Moscow": (0x8C, 0x90),
    "Africa/Nairobi": (0x8D,),
    "Asia/Aden": (0x8F,),
    "Europe/Volgograd": (0x91,),
    "Asia/Dubai": (0x92,),
    "Asia/Muscat": (0x93,),
    "Indian/Mauritius": (0x94,),
    "Asia/Karachi": (0x96,),
    "Indian/Maldives": (0x97,),
    "Asia/Samarkand": (0x98,),
    "Asia/Yekaterinburg": (0x99,),
    "Asia/Omsk": (0x9A,),
    "Asia/Dhaka": (0x9B,),
    "Asia/Novosibirsk": (0x9C,),
    "Asia/Shanghai": (0x9D, 0x9E, 0xA3, 0xA6),
    "Asia/Hong_Kong": (0x9F,),
    "Asia/Kuala_Lumpur": (0xA0,),
    "Asia/Manila": (0xA1,),
    "Australia/Perth": (0xA2,),
    "Asia/Singapore": (0xA4,),
    "Asia/Taipei": (0xA5,),
    "Asia/Ulaanbaatar": (0xA7,),
    "Australia/Darwin": (0xA8,),
    "Australia/Adelaide": (0xA9,),
    "Australia/Brisbane": (0xAA,),
    "Australia/Melbourne": (0xAB, 0xAE, 0xAF),
    "Pacific/Guam": (0xAC,),
    "Australia/Hobart": (0xAD,),
    "Asia/Vladivostok": (0xB0,),
    "Asia/Magadan": (0xB1,),
    "Pacific/Noumea": (0xB2,),
    "Asia/Anadyr": (0xB3,),
    "Pacific/Auckland": (0xB4,),
    "America/Adak": (0xB5,),
    "Pacific/Pago_Pago": (0xB6,),
    "Asia/Tehran": (0xB7,),
    "Asia/Kabul": (0xB8,),
    "Asia/Kolkata": (0xB9, 0xBB, 0xBC, 0xBD),
    "Asia/Colombo": (0xBA,),
    "Asia/Kathmandu": (0xBE,),
    "Asia/Tokyo": (0xBF, 0xC2),
    "Asia/Pyongyang": (0xC0,),
    "Asia/Seoul": (0xC1,),
    "Asia/Yakutsk": (0xC3,),
    "Europe/Athens": (0xC4,),
    "Asia/Rangoon": (0xC5,),
    "Asia/Ho_Chi_Minh": (0xC6, 0xC9),
    "Asia/Bangkok": (0xC8,),
    "Asia/Jakarta": (0xCA,),
    "Asia/Krasnoyarsk": (0xCB,),
    "Asia/Kuwait": (0xCC,),
    "Asia/Phnom_Penh": (0xCD,),
}
_CITY_TIMEZONE_NAMES: dict[int, str] = {
    city: zone for zone, cities in _CITIES_BY_ZONE.items() for city in cities
}

_PREFERENCES_SOURCE = "device_preferences"


def _half_hour_code(data: bytes) -> DeviceClock | None:
    """2892-byte file (early firmware): a code of half hours around UTC-12:30 at 0xB10."""
    (code,) = struct.unpack_from("<h", data, 0xB10)
    if not 0 <= code <= 48:
        return None
    seconds = ((code - 0x19) >> 1) * _HOUR + (_HOUR if code & 1 else 0)
    return DeviceClock.fixed_offset(seconds, source=_PREFERENCES_SOURCE)


def _minutes_from_minus_eight(data: bytes) -> DeviceClock | None:
    """2924-byte file (5G era): minutes relative to UTC-8 at 0xB22."""
    (minutes,) = struct.unpack_from("<h", data, 0xB22)
    return DeviceClock.fixed_offset(minutes * 60 - 8 * _HOUR, source=_PREFERENCES_SOURCE)


def _city_code(data: bytes) -> DeviceClock | None:
    """2952–2960-byte files (later firmware): a city id at 0xB70 naming a real zone."""
    (city,) = struct.unpack_from("<H", data, 0xB70)
    zone = _CITY_TIMEZONE_NAMES.get(city)
    return DeviceClock.from_timezone_name(zone, city_id=city) if zone else None


_PREFERENCE_DECODERS: dict[int, Callable[[bytes], DeviceClock | None]] = {
    2892: _half_hour_code,
    2924: _minutes_from_minus_eight,
    2952: _city_code,
    2956: _city_code,
    2960: _city_code,
}


def load_device_clock(ipod_root: str | Path, *, database_offset: int | None = None) -> DeviceClock:
    """The iPod's clock: Preferences file first, then the database header offset, then UTC."""
    path = Path(ipod_root) / "iPod_Control" / "Device" / "Preferences"
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    decoder = _PREFERENCE_DECODERS.get(len(data))
    clock = decoder(data) if decoder else None
    if clock is not None:
        logger.debug("Device clock from %s: %s", path, clock.name)
        return clock
    if database_offset is not None:
        return DeviceClock.fixed_offset(database_offset)
    return DeviceClock.utc()


def zone_changed_since_write(
    context: DeviceClock, database_offset: int | None, *, now: int | None = None,
) -> bool:
    """True when the device's current offset differs from the one in the database header.

    Only meaningful when the clock came from the Preferences file; a clock derived
    from the header itself can never disagree with it.
    """
    if database_offset is None or context.source != _PREFERENCES_SOURCE:
        return False
    instant = int(time.time() if now is None else now)
    return context.offset_at_unix(instant) != int(database_offset)
