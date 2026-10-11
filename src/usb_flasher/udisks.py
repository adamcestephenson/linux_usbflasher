"""Privileged operations through udisks2.

Every call is a synchronous D-Bus call on a ``Gio.DBusConnection``, which is
thread-safe, so a :class:`UDisksClient` can be used from a worker thread.
Calls that may need authorization allow interactive polkit authentication
and wait indefinitely for the user to answer the prompt.
"""

from __future__ import annotations

import os
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .drives import (  # noqa: E402
    UDISKS_BUS_NAME,
    UDISKS_IFACE_PREFIX,
    UDISKS_ROOT,
    Snapshot,
    snapshot_from_managed_objects,
)

_NO_TIMEOUT = GLib.MAXINT  # polkit prompts can take as long as the user needs
_CALL_FLAGS = Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION

_ERR = "org.freedesktop.UDisks2.Error."


class UDisksError(Exception):
    """A udisks2 call failed."""

    def __init__(self, message: str, *, dbus_error: str | None = None):
        super().__init__(message)
        self.dbus_error = dbus_error

    @property
    def auth_dismissed(self) -> bool:
        """The user cancelled the password prompt."""
        return self.dbus_error == _ERR + "NotAuthorizedDismissed"

    @property
    def not_authorized(self) -> bool:
        return bool(self.dbus_error and self.dbus_error.startswith(_ERR + "NotAuthorized"))


def _wrap(e: GLib.Error, action: str) -> UDisksError:
    remote = Gio.DBusError.get_remote_error(e)
    message = e.message
    # Gio.DBusError.strip_remote_error() can't modify PyGObject's copy of the
    # error, so strip the "GDBus.Error:<name>: " prefix ourselves.
    prefix = f"GDBus.Error:{remote}: " if remote else None
    if prefix and message.startswith(prefix):
        message = message[len(prefix):]
    if remote == _ERR + "NotAuthorizedDismissed":
        text = "Authentication was cancelled"
    elif remote and remote.startswith(_ERR + "NotAuthorized"):
        text = f"Not authorized to {action}"
    else:
        text = f"Failed to {action}: {message}"
    return UDisksError(text, dbus_error=remote)


def _no_options() -> dict[str, GLib.Variant]:
    return {}


class UDisksClient:
    def __init__(self, connection: Gio.DBusConnection | None = None):
        try:
            self.conn = connection or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            raise UDisksError(f"Cannot connect to the system bus: {e.message}") from e

    def _call(self, path: str, iface: str, method: str, params: GLib.Variant | None,
              reply: str | None, action: str, *, fd_list: bool = False):
        try:
            if fd_list:
                return self.conn.call_with_unix_fd_list_sync(
                    UDISKS_BUS_NAME, path, iface, method, params,
                    GLib.VariantType(reply) if reply else None,
                    _CALL_FLAGS, _NO_TIMEOUT, None, None,
                )
            return self.conn.call_sync(
                UDISKS_BUS_NAME, path, iface, method, params,
                GLib.VariantType(reply) if reply else None,
                _CALL_FLAGS, _NO_TIMEOUT, None,
            )
        except GLib.Error as e:
            raise _wrap(e, action) from e

    def snapshot(self) -> Snapshot:
        """Fresh view of all udisks objects (bypasses any proxy cache)."""
        result = self._call(
            UDISKS_ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
            None, "(a{oa{sa{sv}}})", "query udisks2",
        )
        objects: dict[str, Any] = result.unpack()[0]
        return snapshot_from_managed_objects(objects)

    def unmount(self, fs_path: str) -> None:
        self._call(
            fs_path, UDISKS_IFACE_PREFIX + "Filesystem", "Unmount",
            GLib.Variant("(a{sv})", (_no_options(),)), None, "unmount the drive",
        )

    def open_exclusive(self, block_path: str) -> int:
        """Open the block device read-write, exclusively, with O_SYNC.

        Returns a file descriptor owned by the caller. Exclusive access keeps
        anything (e.g. an automounter) from mounting partitions until the fd
        is closed.
        """
        options = {"flags": GLib.Variant("i", os.O_EXCL | os.O_SYNC)}
        result, fds = self._call(
            block_path, UDISKS_IFACE_PREFIX + "Block", "OpenDevice",
            GLib.Variant("(sa{sv})", ("rw", options)), "(h)",
            "open the drive for writing", fd_list=True,
        )
        try:
            return fds.get(result.unpack()[0])
        except GLib.Error as e:
            raise UDisksError(f"Failed to receive file descriptor: {e.message}") from e

    def rescan(self, block_path: str) -> None:
        """Ask the kernel/udev to re-read the partition table."""
        self._call(
            block_path, UDISKS_IFACE_PREFIX + "Block", "Rescan",
            GLib.Variant("(a{sv})", (_no_options(),)), None, "rescan the drive",
        )

    def power_off(self, drive_path: str) -> None:
        """Safely remove the drive (flushes, spins down, cuts USB power)."""
        self._call(
            drive_path, UDISKS_IFACE_PREFIX + "Drive", "PowerOff",
            GLib.Variant("(a{sv})", (_no_options(),)), None, "eject the drive",
        )
