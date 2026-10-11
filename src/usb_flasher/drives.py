"""Drive discovery and safety filtering via udisks2.

Two layers:

* Pure logic (:func:`classify_drives`) operating on a plain snapshot of
  udisks2 objects: ``{object_path: {interface: {property: value}}}`` where
  ``interface`` is the short name ("Block", "Drive", "Partition", ...) and
  values are already unpacked/decoded Python values. Each "Block" dict may
  also carry a synthetic ``"Holders"`` key: a list of device paths
  (e.g. ``["/dev/dm-0"]``) read from ``/sys/class/block/<dev>/holders``,
  used to follow LUKS/LVM/RAID/device-mapper stacks.

* I/O (:func:`snapshot_from_manager`, :class:`DriveMonitor`) that builds
  the snapshot from the system bus and watches for hotplug.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

UDISKS_BUS_NAME = "org.freedesktop.UDisks2"
UDISKS_ROOT = "/org/freedesktop/UDisks2"
UDISKS_IFACE_PREFIX = "org.freedesktop.UDisks2."
NO_OBJECT = "/"

# Drives larger than this get a "are you sure" warning, like Etcher.
LARGE_DRIVE_THRESHOLD = 64 * 1000**3

REMOVABLE_BUSES = ("usb", "sdio")

# A drive with anything mounted at one of these is never selectable.
SYSTEM_MOUNT_POINTS = ("/",)
SYSTEM_MOUNT_PREFIXES = (
    "/boot", "/efi", "/home", "/var", "/usr", "/opt", "/srv", "/root",
    "/etc", "/nix", "/snap",
)

Snapshot = Mapping[str, Mapping[str, Mapping[str, Any]]]


@dataclass
class DriveInfo:
    object_path: str  # udisks Block object path of the whole disk
    drive_path: str | None  # udisks Drive object path (None for loop devices)
    device: str  # e.g. /dev/sdb
    vendor: str
    model: str
    size: int  # bytes
    bus: str  # "usb", "sdio", "loop", "" ...
    removable: bool
    hidden: bool = False  # not a removable/flashable target at all
    reasons: list[str] = field(default_factory=list)  # why not selectable
    warnings: list[str] = field(default_factory=list)  # selectable, but caution
    mount_points: list[str] = field(default_factory=list)  # to unmount first
    backing_file: str | None = None  # loop devices only

    @property
    def selectable(self) -> bool:
        return not self.hidden and not self.reasons

    @property
    def label(self) -> str:
        if self.bus == "loop":
            return f"Loop device ({self.backing_file or 'unknown file'})"
        name = " ".join(p for p in (self.vendor.strip(), self.model.strip()) if p)
        return name or self.device


def is_system_mount(mount_point: str) -> bool:
    if mount_point in SYSTEM_MOUNT_POINTS:
        return True
    return any(
        mount_point == p or mount_point.startswith(p + "/")
        for p in SYSTEM_MOUNT_PREFIXES
    )


def _iface(snapshot: Snapshot, path: str, name: str) -> Mapping[str, Any] | None:
    obj = snapshot.get(path)
    return obj.get(name) if obj else None


def _device_index(snapshot: Snapshot) -> dict[str, str]:
    """Map /dev path -> block object path."""
    index = {}
    for path, obj in snapshot.items():
        block = obj.get("Block")
        if block and block.get("Device"):
            index[block["Device"]] = path
    return index


def _children(snapshot: Snapshot, dev_index: dict[str, str], path: str) -> list[str]:
    """Block objects stacked directly on top of ``path``."""
    out = []
    for other, obj in snapshot.items():
        if other == path:
            continue
        part = obj.get("Partition")
        if part and part.get("Table") == path:
            out.append(other)
            continue
        block = obj.get("Block")
        if block and block.get("CryptoBackingDevice") == path:
            out.append(other)
    block = _iface(snapshot, path, "Block") or {}
    for dev in block.get("Holders", ()):
        holder = dev_index.get(dev)
        if holder and holder not in out:
            out.append(holder)
    return out


def _descendants(snapshot: Snapshot, dev_index: dict[str, str], root: str) -> list[str]:
    """``root`` and every block stacked on it (partitions, LUKS, LVM, RAID...)."""
    seen: list[str] = []
    stack = [root]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.append(path)
        stack.extend(_children(snapshot, dev_index, path))
    return seen


def _mount_points(snapshot: Snapshot, path: str) -> list[str]:
    fs = _iface(snapshot, path, "Filesystem")
    return list(fs.get("MountPoints", ())) if fs else []


def _block_holding_path(snapshot: Snapshot, file_path: str) -> str | None:
    """Block object whose filesystem contains ``file_path`` (longest mount match)."""
    best, best_len = None, -1
    for path in snapshot:
        for mp in _mount_points(snapshot, path):
            prefix = mp.rstrip("/") + "/"
            if (file_path == mp or file_path.startswith(prefix)) and len(mp) > best_len:
                best, best_len = path, len(mp)
    return best


def _is_whole_disk(obj: Mapping[str, Mapping[str, Any]]) -> bool:
    return "Block" in obj and "Partition" not in obj


def classify_drives(
    snapshot: Snapshot,
    *,
    allow_loop: bool = False,
    include_hidden: bool = False,
    source_path: str | os.PathLike | None = None,
) -> list[DriveInfo]:
    """Return candidate target drives, sorted by device path.

    ``source_path`` is the image being flashed; the drive holding it is
    marked unselectable. Hidden drives (internal disks, empty card readers,
    virtual devices) are only returned when ``include_hidden`` is set.
    """
    dev_index = _device_index(snapshot)
    source_block = None
    if source_path is not None:
        source_block = _block_holding_path(snapshot, str(Path(source_path).resolve()))

    drives = []
    for path, obj in snapshot.items():
        if not _is_whole_disk(obj):
            continue
        block = obj["Block"]
        loop = obj.get("Loop")
        drive_path = block.get("Drive", NO_OBJECT)
        drive = _iface(snapshot, drive_path, "Drive") if drive_path != NO_OBJECT else None

        # Only real drives and (optionally) loop devices are candidates.
        # This skips dm/LUKS cleartext devices, zram, md arrays, etc.
        if drive is None and loop is None:
            continue
        if drive is None and block.get("CryptoBackingDevice", NO_OBJECT) != NO_OBJECT:
            continue

        info = DriveInfo(
            object_path=path,
            drive_path=drive_path if drive is not None else None,
            device=block.get("PreferredDevice") or block.get("Device", ""),
            vendor=(drive or {}).get("Vendor", ""),
            model=(drive or {}).get("Model", ""),
            size=int(block.get("Size", 0)),
            bus=(drive or {}).get("ConnectionBus", "") if drive is not None else "loop",
            removable=bool(drive and (drive.get("Removable") or drive.get("MediaRemovable"))),
            backing_file=loop.get("BackingFile") if loop else None,
        )

        # --- Hidden: not something we should even offer. ---
        if drive is None:
            if not allow_loop:
                info.hidden = True
                info.reasons.append("Loop device")
        elif not (info.removable or info.bus in REMOVABLE_BUSES):
            info.hidden = True
            info.reasons.append("Not a removable drive")
        if (drive is not None and drive.get("MediaAvailable") is False) or info.size <= 0:
            info.hidden = True
            info.reasons.append("No media")

        # --- Visible but unselectable: dangerous to overwrite. ---
        # udisks marks every loop device as "system"; for loops we rely on the
        # mount/swap/stacking checks below instead.
        if block.get("HintSystem") and loop is None:
            info.reasons.append("System drive")
        if block.get("ReadOnly"):
            info.reasons.append("Read-only")

        descendants = _descendants(snapshot, dev_index, path)
        system_mounts: list[str] = []
        swap = False
        stacked: list[str] = []
        for d in descendants:
            for mp in _mount_points(snapshot, d):
                (system_mounts if is_system_mount(mp) else info.mount_points).append(mp)
            swapspace = _iface(snapshot, d, "Swapspace")
            if swapspace and swapspace.get("Active"):
                swap = True
            d_block = _iface(snapshot, d, "Block") or {}
            # Anything not a partition of this disk (dm-crypt, LVM, md...).
            if d != path and _iface(snapshot, d, "Partition") is None:
                stacked.append(d_block.get("PreferredDevice") or d_block.get("Device", d))

        if system_mounts:
            info.reasons.append(
                "Contains system mounts: " + ", ".join(sorted(set(system_mounts)))
            )
        if swap:
            info.reasons.append("In use as swap")
        if stacked and not system_mounts:
            # e.g. an unlocked LUKS volume or active LVM/RAID; the device
            # can't be opened exclusively until these are closed.
            info.reasons.append(
                "In use by " + ", ".join(sorted(set(stacked)))
                + " (lock or deactivate first)"
            )
        if source_block is not None and source_block in descendants:
            info.reasons.append("Contains the source image")

        if info.size > LARGE_DRIVE_THRESHOLD and not info.hidden:
            info.warnings.append(
                f"Large drive ({format_size(info.size)}). Make sure this is the right one."
            )

        if info.hidden and not include_hidden:
            continue
        drives.append(info)

    drives.sort(key=lambda d: d.device)
    return drives


def format_size(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# udisks2 / D-Bus I/O
# --------------------------------------------------------------------------

_BYTESTRING_PROPS = {"Device", "PreferredDevice", "BackingFile"}
_BYTESTRING_LIST_PROPS = {"MountPoints", "Symlinks"}


def _decode_bytestring(value: Iterable[int]) -> str:
    return bytes(value).rstrip(b"\0").decode("utf-8", "surrogateescape")


def _unpack_props(proxy) -> dict[str, Any]:
    props = {}
    for name in proxy.get_cached_property_names() or ():
        value = proxy.get_cached_property(name)
        if value is None:
            continue
        value = value.unpack()
        if name in _BYTESTRING_PROPS:
            value = _decode_bytestring(value)
        elif name in _BYTESTRING_LIST_PROPS:
            value = [_decode_bytestring(v) for v in value]
        props[name] = value
    return props


def read_holders(device: str, sysfs: str = "/sys/class/block") -> list[str]:
    """Device paths of kernel holders of ``device`` (dm/md devices on top)."""
    try:
        resolved = os.path.realpath(device)
        names = os.listdir(os.path.join(sysfs, os.path.basename(resolved), "holders"))
    except OSError:
        return []
    return sorted("/dev/" + n for n in names)


def snapshot_from_manager(manager) -> dict[str, dict[str, dict[str, Any]]]:
    """Build a :data:`Snapshot` from a ``Gio.DBusObjectManagerClient``."""
    snapshot: dict[str, dict[str, dict[str, Any]]] = {}
    for obj in manager.get_objects():
        ifaces = {}
        for proxy in obj.get_interfaces():
            name = proxy.get_interface_name()
            if not name.startswith(UDISKS_IFACE_PREFIX):
                continue
            ifaces[name[len(UDISKS_IFACE_PREFIX):]] = _unpack_props(proxy)
        block = ifaces.get("Block")
        if block is not None and block.get("Device"):
            block["Holders"] = read_holders(block["Device"])
        snapshot[obj.get_object_path()] = ifaces
    return snapshot


class DrivesError(Exception):
    """udisks2 is unavailable or returned an error."""


def _make_monitor_class():
    # Deferred so the pure logic above is importable without PyGObject.
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib, GObject

    class DriveMonitor(GObject.Object):
        """Watches udisks2 and emits ``changed`` (debounced) on hotplug or
        property changes. Must be used from a thread running a GLib main loop.
        """

        __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

        DEBOUNCE_MS = 250

        def __init__(self, manager=None):
            super().__init__()
            if manager is None:
                try:
                    manager = Gio.DBusObjectManagerClient.new_for_bus_sync(
                        Gio.BusType.SYSTEM,
                        Gio.DBusObjectManagerClientFlags.NONE,
                        UDISKS_BUS_NAME, UDISKS_ROOT, None, None, None,
                    )
                except GLib.Error as e:
                    raise DrivesError(f"Cannot connect to udisks2: {e.message}") from e
                if manager.get_name_owner() is None:
                    raise DrivesError("udisks2 is not running")
            self.manager = manager
            self._pending = 0
            for sig in (
                "object-added", "object-removed", "interface-added",
                "interface-removed", "interface-proxy-properties-changed",
            ):
                manager.connect(sig, self._on_change)

        def _on_change(self, *_args):
            if not self._pending:
                self._pending = GLib.timeout_add(self.DEBOUNCE_MS, self._emit)

        def _emit(self):
            self._pending = 0
            self.emit("changed")
            return GLib.SOURCE_REMOVE

        def snapshot(self):
            return snapshot_from_manager(self.manager)

        def drives(self, **kwargs) -> list[DriveInfo]:
            return classify_drives(self.snapshot(), **kwargs)

    return DriveMonitor


def __getattr__(name):
    if name == "DriveMonitor":
        cls = _make_monitor_class()
        globals()["DriveMonitor"] = cls
        return cls
    raise AttributeError(name)
