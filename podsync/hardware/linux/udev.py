"""The optional udev rule that lets an unprivileged process read an iPod's Apple serial.

Without root, Linux exposes the FireWire GUID and USB PID but usually not the
product serial (VPD page 0x80) that pins the exact model.  The packaged rule
(``assets/linux/61-podsync.rules``) runs ``scsi_id`` at hotplug time and
publishes ``ID_PODSYNC_PRODUCT_SERIAL``.  This module reports whether the
rule is needed / installed / current, and produces a self-checking shell
snippet the user can paste to install it — podsync never escalates itself.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources

from podsync.hardware.catalog.models import IPOD_USB_PIDS

RULE_VERSION = "2"
RULE_FILENAME = "61-podsync.rules"
RULE_DESTINATION = f"/etc/udev/rules.d/{RULE_FILENAME}"

_HOST_RULE_PATHS = (
    f"/etc/udev/rules.d/{RULE_FILENAME}",
    f"/run/udev/rules.d/{RULE_FILENAME}",
    f"/usr/local/lib/udev/rules.d/{RULE_FILENAME}",
    f"/usr/lib/udev/rules.d/{RULE_FILENAME}",
    f"/lib/udev/rules.d/{RULE_FILENAME}",
)

__all__ = [
    "LinuxIdentityIntegration", "LinuxIntegrationState",
    "RULE_DESTINATION", "RULE_FILENAME", "RULE_VERSION",
    "describe_linux_identity_integration", "udev_rule_needed",
    "udev_rule_text",
]


class LinuxIntegrationState(StrEnum):
    """Whether the udev rule is irrelevant, working, missing, outdated or needs a retrigger."""

    NOT_APPLICABLE = "not_applicable"
    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    REFRESH_REQUIRED = "refresh_required"
    RULE_OUTDATED = "rule_outdated"


@dataclass(frozen=True)
class LinuxIdentityIntegration:
    """The rule's state, an explanation, and the shell snippet that fixes it."""

    state: LinuxIntegrationState
    explanation: str
    setup_instructions: str = ""

    @property
    def needs_setup(self) -> bool:
        return self.state not in {LinuxIntegrationState.NOT_APPLICABLE, LinuxIntegrationState.READY}


def udev_rule_text() -> str:
    """The packaged udev rule text."""
    return (
        resources.files("podsync").joinpath("assets", "linux", RULE_FILENAME)
        .read_text(encoding="utf-8")
    )


def _installed_rule_state() -> tuple[bool, bool]:
    """``(installed, identical to ours)`` for the first rule file udev would read."""
    for path in _HOST_RULE_PATHS:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        return True, text == udev_rule_text()
    return False, False


def _setup_instructions(mount_path: str) -> str:
    rule = udev_rule_text().rstrip()
    mount = shlex.quote(str(mount_path))
    return f"""(
set -eu
MOUNT={mount}
RULE_DEST={RULE_DESTINATION}
RULE_EXPECTED_VERSION={RULE_VERSION}
PATH="/usr/sbin:/usr/bin:/sbin:/bin${{PATH:+:$PATH}}"
log() {{ printf 'podsync: %s\\n' "$*" >&2; }}
die() {{ printf 'podsync Linux identity setup failed: %s\\n' "$*" >&2; exit 1; }}
if [ -e /.dockerenv ] || [ -e /run/.containerenv ] || [ -n "${{container:-}}" ] || \\
   [ -n "${{DISTROBOX_ENTER_PATH:-}}" ] || [ -n "${{FLATPAK_ID:-}}" ]; then
  die "run this on the host system, not inside Distrobox, Toolbox, Flatpak or another container"
fi
for cmd in findmnt sed readlink lsblk id mkdir mktemp cp chmod rm mv udevadm grep; do
  command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
done
[ -d "$MOUNT" ] || die "mount point not found: $MOUNT"
PART="$(findmnt -n -o SOURCE --target "$MOUNT" | sed 's/\\[.*$//')"
PART="$(readlink -f "$PART")"
case "$PART" in /dev/*) ;; *) die "not a block device: $PART" ;; esac
[ -b "$PART" ] || die "not a block device: $PART"
PKNAME="$(lsblk -ndo PKNAME "$PART" || true)"
if [ -n "$PKNAME" ]; then DISK="/dev/$PKNAME"; else DISK="$PART"; fi
SYSNAME="$(basename "$DISK")"
printf '%s' "$SYSNAME" | grep -Eq '^[a-zA-Z0-9._-]+$' || die "unexpected device name: $SYSNAME"
SYSPATH="/sys/class/block/$SYSNAME"
[ -e "$SYSPATH" ] || die "sysfs entry missing: $SYSPATH"
VENDOR="$(cat "$SYSPATH/device/vendor" 2>/dev/null || true)"
MODEL="$(cat "$SYSPATH/device/model" 2>/dev/null || true)"
case "$VENDOR" in Apple*) ;; *) die "not an Apple disk: $VENDOR" ;; esac
case "$MODEL" in iPod*) ;; *) die "not an iPod disk: $MODEL" ;; esac
[ -d "$RULE_DEST" ] && die "$RULE_DEST is a directory"
if [ "$(id -u)" -eq 0 ]; then AS_ROOT=""; elif command -v sudo >/dev/null 2>&1; then AS_ROOT="sudo"; \\
elif command -v doas >/dev/null 2>&1; then AS_ROOT="doas"; else die "root privileges are required"; fi
$AS_ROOT mkdir -p -m 0755 /etc/udev/rules.d
STAGE="$(mktemp "${{TMPDIR:-/tmp}}/podsync-udev.XXXXXX")"
NEW="/etc/udev/rules.d/.61-podsync.rules.new.$$"
trap 'rm -f "$STAGE"; $AS_ROOT rm -f "$NEW"' EXIT HUP INT TERM
cat > "$STAGE" <<'PODSYNC_UDEV_RULE'
{rule}
PODSYNC_UDEV_RULE
$AS_ROOT cp "$STAGE" "$NEW"
$AS_ROOT chmod 0644 "$NEW"
[ -d "$RULE_DEST" ] && die "$RULE_DEST is a directory"
$AS_ROOT mv -f "$NEW" "$RULE_DEST"
$AS_ROOT udevadm control --reload-rules
if udevadm trigger --help 2>&1 | grep -q settle; then
  $AS_ROOT udevadm trigger --action=change --subsystem-match=block --sysname-match="$SYSNAME" --settle
else
  $AS_ROOT udevadm trigger --action=change --subsystem-match=block --sysname-match="$SYSNAME"
  $AS_ROOT udevadm settle -t 15 || true
fi
PROPS="$(udevadm info --query=property --name="$DISK" || true)"
SERIAL="$(printf '%s\\n' "$PROPS" | sed -n 's/^ID_PODSYNC_PRODUCT_SERIAL=//p')"
VERSION="$(printf '%s\\n' "$PROPS" | sed -n 's/^ID_PODSYNC_RULE_VERSION=//p')"
if [ -n "$SERIAL" ]; then
  printf 'ID_PODSYNC_PRODUCT_SERIAL=%s\\n' "$SERIAL"
  exit 0
fi
log "disk=$DISK sysfs=$SYSPATH vendor=$VENDOR model=$MODEL"
log "udevadm $(udevadm --version 2>/dev/null || echo unknown)"
if [ "$VERSION" = "$RULE_EXPECTED_VERSION" ]; then
  log "rule version matched but scsi_id produced no serial"
elif [ -n "$VERSION" ]; then
  log "a different rule version ($VERSION) takes precedence"
else
  log "rule version marker missing"
fi
SCSI_ID="$(command -v scsi_id || true)"
[ -z "$SCSI_ID" ] && [ -x /usr/lib/udev/scsi_id ] && SCSI_ID=/usr/lib/udev/scsi_id
[ -z "$SCSI_ID" ] && [ -x /lib/udev/scsi_id ] && SCSI_ID=/lib/udev/scsi_id
[ -n "$SCSI_ID" ] && $AS_ROOT "$SCSI_ID" --page=0x80 --whitelisted --device="$DISK" || true
$AS_ROOT udevadm test --action=change "$SYSPATH" 2>&1 | \\
  grep -Ei '61-podsync|podsync|scsi_id|apple|ipod|error|failed|invalid|unknown' || true
die "rule installed, but the Apple product serial was not published; diagnostics are above"
)
"""


def describe_linux_identity_integration(
    mount_path: str, *, product_serial: str = "", platform: str | None = None,
) -> LinuxIdentityIntegration:
    """State of the udev integration for the iPod at *mount_path*."""
    active_platform = sys.platform if platform is None else platform
    if not active_platform.startswith("linux"):
        return LinuxIdentityIntegration(
            LinuxIntegrationState.NOT_APPLICABLE,
            "Linux host integration is not applicable on this platform.",
        )
    if product_serial.strip():
        return LinuxIdentityIntegration(
            LinuxIntegrationState.READY, "The Apple product serial is already available.",
        )
    installed, current = _installed_rule_state()
    state = (LinuxIntegrationState.REFRESH_REQUIRED if current else LinuxIntegrationState.RULE_OUTDATED) \
        if installed else LinuxIntegrationState.SETUP_REQUIRED
    return LinuxIdentityIntegration(state, _EXPLANATIONS[state], _setup_instructions(mount_path))


_EXPLANATIONS = {
    LinuxIntegrationState.SETUP_REQUIRED: "The podsync Linux identity rule is not installed on this host.",
    LinuxIntegrationState.RULE_OUTDATED:
        "The highest-priority podsync Linux identity rule is disabled or outdated.",
    LinuxIntegrationState.REFRESH_REQUIRED: (
        "The identity rule is installed, but this iPod has not published its Apple "
        "product serial. Reinstalling and triggering only this block device will retry "
        "without disconnecting it."
    ),
}


def udev_rule_needed(device: object | None, *, platform: str | None = None) -> bool:
    """True on Linux for a device that looks like an iPod but has no Apple serial yet."""
    active_platform = sys.platform if platform is None else platform
    if not active_platform.startswith("linux") or device is None:
        return False
    if str(getattr(device, "serial", "") or "").strip():
        return False
    if str(getattr(device, "path", "") or "").strip():
        return True
    try:
        pid = int(getattr(device, "usb_pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid in IPOD_USB_PIDS:
        return True
    guid = str(getattr(device, "firewire_guid", "") or "").strip()
    return bool(re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{16}", guid))
