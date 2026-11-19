import os

import pytest

gi = pytest.importorskip("gi")
from gi.repository import Gio  # noqa: E402

from usb_flasher.udisks import UDisksClient, UDisksError, _wrap  # noqa: E402


def remote_error(name, message):
    return Gio.DBusError.new_for_dbus_error(name, message)


def test_auth_dismissed():
    e = _wrap(remote_error("org.freedesktop.UDisks2.Error.NotAuthorizedDismissed", "x"), "open")
    assert isinstance(e, UDisksError)
    assert e.auth_dismissed and e.not_authorized
    assert str(e) == "Authentication was cancelled"


def test_not_authorized():
    e = _wrap(remote_error("org.freedesktop.UDisks2.Error.NotAuthorizedCanObtain", "x"),
              "open the drive for writing")
    assert not e.auth_dismissed and e.not_authorized
    assert str(e) == "Not authorized to open the drive for writing"


def test_other_error_strips_remote_prefix():
    e = _wrap(remote_error("org.freedesktop.UDisks2.Error.Failed",
                           "Error opening device /dev/sdb: Device or resource busy"),
              "open the drive for writing")
    assert not e.not_authorized
    assert str(e) == ("Failed to open the drive for writing: "
                      "Error opening device /dev/sdb: Device or resource busy")
    assert e.dbus_error == "org.freedesktop.UDisks2.Error.Failed"


@pytest.mark.skipif(not os.path.exists("/run/dbus/system_bus_socket"), reason="no system bus")
def test_real_snapshot_smoke():
    """Read-only query of the real udisks2."""
    try:
        snap = UDisksClient().snapshot()
    except UDisksError as e:
        pytest.skip(str(e))
    assert any("Block" in ifaces for ifaces in snap.values())
    for ifaces in snap.values():
        if "Block" in ifaces:
            assert ifaces["Block"]["Device"].startswith("/dev/")


class RecordingConn:
    """Stands in for Gio.DBusConnection; records calls, never touches a bus."""

    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply = reply
        self.error = error

    def call_sync(self, bus, path, iface, method, params, reply_type, flags, timeout, cancel):
        self.calls.append((path, iface, method, params.unpack() if params else None, flags))
        if self.error:
            raise self.error
        return self.reply


def test_create_partition_table_call():
    conn = RecordingConn()
    UDisksClient(conn).create_partition_table("/org/freedesktop/UDisks2/block_devices/sdb", "dos")
    path, iface, method, args, flags = conn.calls[0]
    assert (iface, method) == ("org.freedesktop.UDisks2.Block", "Format")
    assert args == ("dos", {})
    assert flags & Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION


def test_create_partition_and_format_call():
    from gi.repository import GLib
    part = "/org/freedesktop/UDisks2/block_devices/sdb1"
    conn = RecordingConn(reply=GLib.Variant("(o)", (part,)))
    result = UDisksClient(conn).create_partition_and_format(
        "/org/freedesktop/UDisks2/block_devices/sdb", fs_type="vfat", label="STICK",
        partition_type="0x0c", mkfs_args=["-F", "32"])
    assert result == part
    _, iface, method, args, flags = conn.calls[0]
    assert (iface, method) == ("org.freedesktop.UDisks2.PartitionTable", "CreatePartitionAndFormat")
    offset, size, ptype, name, opts, fs_type, fmt_opts = args
    assert (offset, size, ptype, name, opts, fs_type) == (0, 0, "0x0c", "", {}, "vfat")
    assert fmt_opts == {"label": "STICK", "mkfs-args": ["-F", "32"]}
    assert flags & Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION


def test_format_errors_are_wrapped():
    err = remote_error("org.freedesktop.UDisks2.Error.NotAuthorizedDismissed", "x")
    with pytest.raises(UDisksError) as e:
        UDisksClient(RecordingConn(error=err)).create_partition_table("/b", "dos")
    assert e.value.auth_dismissed
    err = remote_error("org.freedesktop.UDisks2.Error.Failed", "mkfs failed")
    with pytest.raises(UDisksError) as e:
        UDisksClient(RecordingConn(error=err)).create_partition_and_format(
            "/b", fs_type="exfat", label="")
    assert str(e.value) == "Failed to format the drive: mkfs failed"
