"""GTK application."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from . import __version__  # noqa: E402

APP_ID = "io.github.adamcestephenson.UsbFlasher"


class FlasherApp(Adw.Application):
    def __init__(self, *, allow_loop: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.allow_loop = allow_loop
        self._client = None

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", self._on_quit)
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("window.close", ["<Primary>w"])

    def _get_client(self):
        if self._client is None:
            from .udisks import UDisksClient

            self._client = UDisksClient()
        return self._client

    def _create_window(self) -> Gtk.Window:
        from .drives import DriveMonitor, DrivesError
        from .window import FlasherWindow

        try:
            monitor = DriveMonitor()
        except DrivesError as e:
            window = Adw.ApplicationWindow(application=self, title="USB Flasher",
                                           default_width=480, default_height=360)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            view.set_content(Adw.StatusPage(
                icon_name="dialog-error-symbolic", title="Drive Service Unavailable",
                description=f"{e}.\n\nUSB Flasher needs udisks2 to find and write drives.",
            ))
            window.set_content(view)
            return window
        return FlasherWindow(self, monitor=monitor, client_factory=self._get_client,
                             allow_loop=self.allow_loop)

    def do_activate(self):
        window = self.get_active_window() or self._create_window()
        window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        window = self.get_active_window()
        if files and hasattr(window, "set_image") and window.worker is None:
            window._open_file(files[0])

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name="USB Flasher", application_icon="drive-removable-media-symbolic",
            version=__version__, developer_name="Adam Stephenson",
            website="https://github.com/adamcestephenson/linux_usbflasher",
            comments="Write disk images to USB drives and SD cards.",
        )
        about.present(self.get_active_window())

    def _on_quit(self, *_args):
        busy = [w for w in self.get_windows() if getattr(w, "worker", None) is not None]
        if not busy:
            self.quit()
            return
        # Go through close-request so the active flash is confirmed first.
        # (A plain close() would also just dismiss any open dialog.)
        for window in busy:
            window.close()
