"""GUI tests. Run on a private Broadway (headless) display when
gtk4-broadwayd is available so no windows appear on the desktop."""

import atexit
import copy
import os
import shutil
import subprocess
import threading
import time

import pytest


def _start_broadway():
    broadwayd = shutil.which("gtk4-broadwayd")
    if not broadwayd or os.environ.get("USB_FLASHER_TEST_NATIVE_DISPLAY") == "1":
        return
    num = 50 + os.getpid() % 40
    proc = subprocess.Popen([broadwayd, f":{num}"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    atexit.register(proc.terminate)
    socket = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"broadway{num + 1}.socket")
    for _ in range(50):
        if os.path.exists(socket) or proc.poll() is not None:
            break
        time.sleep(0.05)
    os.environ["GDK_BACKEND"] = "broadway"
    os.environ["BROADWAY_DISPLAY"] = f":{num}"


_start_broadway()

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from fakes import B, D, MB, TARGET_SIZE, AuthDismissed, FakeClient, stick_snapshot  # noqa: E402
from usb_flasher.drive_picker import DrivePicker  # noqa: E402
from usb_flasher.drives import classify_drives  # noqa: E402
from usb_flasher.window import FlasherWindow  # noqa: E402


class FakeMonitor(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, client):
        super().__init__()
        self.client = client

    def drives(self, **kwargs):
        return classify_drives(self.client.snap, **kwargs)


class BlockingClient(FakeClient):
    """open_exclusive() waits until ``release`` is set."""

    def __init__(self, *a):
        super().__init__(*a)
        self.release = threading.Event()
        self.opening = threading.Event()

    def open_exclusive(self, block_path):
        self.opening.set()
        assert self.release.wait(10)
        return super().open_exclusive(block_path)


def spin(cond, timeout=10.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for condition")
        if not ctx.iteration(False):
            time.sleep(0.005)


def respond(dialog, response):
    """What clicking a dialog button does: emit the response, then close."""
    dialog.emit("response", response)
    dialog.force_close()


def spin_for(seconds):
    end = time.monotonic() + seconds
    spin(lambda: time.monotonic() > end, seconds + 1)


@pytest.fixture
def image(tmp_path):
    data = os.urandom(MB + 4321)
    p = tmp_path / "distro.img"
    p.write_bytes(data)
    return p, data


@pytest.fixture
def drive_file(tmp_path):
    p = tmp_path / "drive.bin"
    p.write_bytes(b"\xaa" * TARGET_SIZE)
    return p


def make_window(client):
    monitor = FakeMonitor(client)
    win = FlasherWindow(monitor=monitor, client_factory=lambda: client)
    win.present()
    spin_for(0.05)
    return win, monitor


@pytest.fixture
def ui(drive_file):
    client = FakeClient(drive_file, stick_snapshot())
    win, monitor = make_window(client)
    yield win, client, monitor
    if win.cancel_event:
        win.cancel_event.set()
    if win.worker:
        win.worker.join(10)
    win.destroy()


def stick(monitor):
    (d,) = monitor.drives()
    return d


def test_initial_state(ui):
    win, _, _ = ui
    assert win.stack.get_visible_child_name() == "select"
    assert not win.flash_card.button.get_sensitive()
    assert win.image_card.detail.get_label() == "No image selected"
    assert win.target_card.detail.get_label() == "No target selected"


def test_select_image_and_target_enables_flash(ui, image):
    win, _, monitor = ui
    assert win.set_image(image[0])
    assert "distro.img" in win.image_card.detail.get_label()
    assert not win.flash_card.button.get_sensitive()

    win.set_target(stick(monitor))
    assert "SanDisk Ultra" in win.target_card.detail.get_label()
    assert "/dev/sdb" in win.target_card.detail.get_label()
    assert win.flash_card.button.get_sensitive()
    assert not win.target_card.warning.get_visible()


def test_invalid_image_shows_error(ui, tmp_path):
    win, _, _ = ui
    bad = tmp_path / "notes.txt"
    bad.write_text("hi")
    assert not win.set_image(bad)
    assert win.image is None
    dialog = win.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert "Unsupported" in dialog.get_body()


def test_non_hybrid_iso_warning_shown(ui, tmp_path):
    win, _, _ = ui
    iso = tmp_path / "win.iso"
    buf = bytearray(64 * 1024)
    buf[32769:32774] = b"CD001"
    iso.write_bytes(bytes(buf))
    assert win.set_image(iso)
    assert win.image_card.warning.get_visible()
    assert "hybrid" in win.image_card.warning.get_label()


def test_target_too_small_blocks_flash(drive_file, image):
    client = FakeClient(drive_file, stick_snapshot(size=MB))
    win, monitor = make_window(client)
    try:
        win.set_image(image[0])
        win.set_target(stick(monitor))
        assert not win.flash_card.button.get_sensitive()
        assert "Too small" in win.target_card.warning.get_label()
    finally:
        win.destroy()


def test_target_removed_is_cleared(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    client.snap = {}
    monitor.emit("changed")
    assert win.target is None
    assert not win.flash_card.button.get_sensitive()


def test_target_becoming_unsafe_disables_flash(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    client.snap[B + "sdb"]["Block"]["ReadOnly"] = True
    monitor.emit("changed")
    assert win.target is not None
    assert not win.flash_card.button.get_sensitive()
    assert "Read-only" in win.target_card.warning.get_label()


def test_confirm_dialog_then_flash(ui, image, drive_file):
    win, client, monitor = ui
    path, data = image
    win.set_image(path)
    win.set_target(stick(monitor))

    win.confirm_flash()
    dialog = win.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    assert "SanDisk Ultra" in dialog.get_heading()
    assert "/run/media/u/STICK" not in dialog.get_body()  # we only say "unmounted first"
    assert "unmounted first" in dialog.get_body()
    assert win.stack.get_visible_child_name() == "select"  # nothing until confirmed

    respond(dialog, "flash")
    spin(lambda: win.stack.get_visible_child_name() == "result")

    assert win.result_page.get_title() == "Flash Complete"
    assert "verified" in win.result_page.get_description()
    assert win.result_checksum.get_visible()
    assert win.eject_button.get_visible()
    assert drive_file.read_bytes()[:len(data)] == data
    assert client.names() == ["unmount", "open", "rescan"]
    assert win.worker is None


def test_cancel_confirm_does_nothing(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.confirm_flash()
    respond(win.get_visible_dialog(), "cancel")
    spin_for(0.1)
    assert win.stack.get_visible_child_name() == "select"
    assert client.names() == []


def test_flash_without_verify_and_with_eject(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.verify_check.set_active(False)
    win.eject_check.set_active(True)
    win.start_flash()
    spin(lambda: win.stack.get_visible_child_name() == "result")
    assert win.result_page.get_title() == "Flash Complete"
    assert "verified" not in win.result_page.get_description()
    assert "ejected" in win.result_page.get_description()
    assert not win.eject_button.get_visible()
    assert client.names()[-1] == "power_off"


def test_eject_button(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.start_flash()
    spin(lambda: win.stack.get_visible_child_name() == "result")
    win.eject_button.emit("clicked")
    spin(lambda: not win.eject_button.get_visible())
    assert client.calls[-1] == ("power_off", D + "stick")


def test_flash_error_result(ui, image):
    win, client, monitor = ui
    client.open_error = RuntimeError("Failed to open the drive for writing: busy")
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.start_flash()
    spin(lambda: win.stack.get_visible_child_name() == "result")
    assert win.result_page.get_title() == "Flash Failed"
    assert "busy" in win.result_page.get_description()
    assert not win.eject_button.get_visible()
    assert not win.result_checksum.get_visible()


def test_auth_dismissed_result(ui, image):
    win, client, monitor = ui
    client.open_error = AuthDismissed("Authentication was cancelled")
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.start_flash()
    spin(lambda: win.stack.get_visible_child_name() == "result")
    assert win.result_page.get_title() == "Flash Cancelled"
    assert win.result_page.get_description() == "No changes were made to the drive."


def test_flash_another_returns_to_selection(ui, image):
    win, client, monitor = ui
    win.set_image(image[0])
    win.set_target(stick(monitor))
    win.start_flash()
    spin(lambda: win.stack.get_visible_child_name() == "result")
    win.again_button.emit("clicked")
    assert win.stack.get_visible_child_name() == "select"
    assert win.image is not None and win.target is not None
    assert win.flash_card.button.get_sensitive()


def test_cancel_button(drive_file, image):
    client = BlockingClient(drive_file, stick_snapshot())
    win, monitor = make_window(client)
    try:
        win.set_image(image[0])
        win.set_target(stick(monitor))
        win.start_flash()
        spin(client.opening.is_set)
        assert win.stack.get_visible_child_name() == "progress"
        spin(lambda: win.progress_title.get_label() == "Waiting for Authorization…")
        assert "password" in win.progress_detail.get_label()

        win.cancel_button.emit("clicked")
        assert not win.cancel_button.get_sensitive()
        client.release.set()
        spin(lambda: win.stack.get_visible_child_name() == "result")
        assert win.result_page.get_title() == "Flash Cancelled"
        assert drive_file.read_bytes() == b"\xaa" * TARGET_SIZE
    finally:
        client.release.set()
        win.destroy()


def test_drive_changes_ignored_while_flashing(drive_file, image):
    client = BlockingClient(drive_file, stick_snapshot())
    win, monitor = make_window(client)
    try:
        win.set_image(image[0])
        win.set_target(stick(monitor))
        win.start_flash()
        spin(client.opening.is_set)
        saved = client.snap
        client.snap = {}
        monitor.emit("changed")
        assert win.target is not None
        client.snap = saved
        client.release.set()
        spin(lambda: win.stack.get_visible_child_name() == "result")
    finally:
        client.release.set()
        win.destroy()


def test_close_while_flashing_asks_then_cancels(drive_file, image):
    client = BlockingClient(drive_file, stick_snapshot())
    win, monitor = make_window(client)
    closed = []
    win.connect("destroy", lambda _w: closed.append(True))
    try:
        win.set_image(image[0])
        win.set_target(stick(monitor))
        win.start_flash()
        spin(client.opening.is_set)

        win.close()
        spin_for(0.1)
        assert win.get_visible()  # close was refused
        dialog = win.get_visible_dialog()
        assert dialog.get_heading() == "Stop Flashing?"

        # A second close while asking (libadwaita dismisses the dialog) must not
        # stack dialogs or stop the flash.
        win.close()
        spin_for(0.1)
        assert win.get_visible_dialog() in (None, dialog)
        assert win.get_visible() and not win.cancel_event.is_set()

        win.close()
        spin_for(0.1)
        dialog = win.get_visible_dialog()
        respond(dialog, "continue")
        spin_for(0.1)
        assert not win.cancel_event.is_set()
        assert win.get_visible() and win.worker is not None
        win.close()
        spin_for(0.1)
        respond(win.get_visible_dialog(), "stop")
        assert win.cancel_event.is_set()

        client.release.set()
        spin(lambda: not win.get_visible())
        assert win.worker is None
        assert drive_file.read_bytes() == b"\xaa" * TARGET_SIZE
    finally:
        client.release.set()
        if not closed:
            win.destroy()


def test_close_when_idle_is_immediate(ui):
    win, _, monitor = ui
    win.close()
    spin(lambda: not win.get_visible())
    assert win._monitor_handler == 0


def test_drive_picker(drive_file, image):
    snap = stick_snapshot()
    other = stick_snapshot()
    other[B + "sdc"] = other.pop(B + "sdb")
    other[B + "sdc"]["Block"].update(Device="/dev/sdc", PreferredDevice="/dev/sdc",
                                     Drive=D + "other", HintSystem=True)
    other[D + "other"] = other.pop(D + "stick")
    snap.update({k: v for k, v in other.items() if k != B + "sdb1"})
    client = FakeClient(drive_file, copy.deepcopy(snap))
    win, monitor = make_window(client)
    picked = []
    try:
        win.set_image(image[0])
        picker = DrivePicker(monitor, image=win.image, allow_loop=False, selected=None,
                             on_selected=picked.append)
        picker.present(win)
        spin_for(0.1)
        rows = picker.rows
        assert set(rows) == {B + "sdb", B + "sdc"}
        assert rows[B + "sdb"].get_sensitive() and rows[B + "sdb"].get_activatable()
        assert not rows[B + "sdc"].get_sensitive()
        assert "System drive" in rows[B + "sdc"].get_subtitle()
        assert picker.stack.get_visible_child_name() == "list"

        # Hotplug: the stick goes away.
        del client.snap[B + "sdb"], client.snap[B + "sdb1"]
        monitor.emit("changed")
        assert set(picker.rows) == {B + "sdc"}
        client.snap = {}
        monitor.emit("changed")
        assert picker.stack.get_visible_child_name() == "empty"

        client.snap = copy.deepcopy(snap)
        monitor.emit("changed")
        picker.list.emit("row-activated", picker.rows[B + "sdb"])
        assert [d.device for d in picked] == ["/dev/sdb"]
        spin_for(0.3)
        assert picker._handler == 0  # disconnected from the monitor on close
    finally:
        win.destroy()
