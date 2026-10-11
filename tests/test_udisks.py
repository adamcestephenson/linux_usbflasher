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
