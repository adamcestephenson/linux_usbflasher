"""Small dialog for choosing the filesystem and label when restoring a drive."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .drive_picker import drive_subtitle  # noqa: E402
from .drives import DriveInfo  # noqa: E402
from .restore import (  # noqa: E402
    FAT32_FILE_LIMIT_NOTE, Filesystem, RestoreOptions, check_label, default_filesystem,
)

FILESYSTEMS = list(Filesystem)
DEFAULT_LABEL = "USB DRIVE"


class RestoreDialog(Adw.Dialog):
    def __init__(self, drive: DriveInfo, *, on_confirmed: Callable[[RestoreOptions], None]):
        super().__init__(title="Restore Drive", content_width=420)
        self.drive = drive
        self.on_confirmed = on_confirmed

        self.fs_row = Adw.ComboRow(
            title="Filesystem",
            model=Gtk.StringList.new([fs.value for fs in FILESYSTEMS]),
            selected=FILESYSTEMS.index(default_filesystem(drive.size)),
        )
        self.label_row = Adw.EntryRow(title="Label", text=DEFAULT_LABEL)
        self.label_row.connect("changed", lambda _r: self._update())
        self.fs_row.connect("notify::selected", lambda *_a: self._update())

        group = Adw.PreferencesGroup()
        group.add(self.fs_row)
        group.add(self.label_row)

        self.note = Gtk.Label(wrap=True, xalign=0)
        self.note.add_css_class("dim-label")
        self.note.add_css_class("caption")
        self.error = Gtk.Label(wrap=True, xalign=0)
        self.error.add_css_class("error")
        self.error.add_css_class("caption")

        self.button = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", lambda _b: self._continue())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(Gtk.Label(label=f"{drive.label} · {drive_subtitle(drive)}",
                             wrap=True, xalign=0))
        box.append(group)
        box.append(self.note)
        box.append(self.error)
        box.append(self.button)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(box)
        self.set_child(view)
        self.set_default_widget(self.button)
        self._update()

    @property
    def filesystem(self) -> Filesystem:
        return FILESYSTEMS[self.fs_row.get_selected()]

    def options(self) -> RestoreOptions:
        return RestoreOptions(self.filesystem, self.label_row.get_text())

    def _update(self) -> None:
        fs = self.filesystem
        if fs is Filesystem.FAT32:
            self.note.set_label(FAT32_FILE_LIMIT_NOTE + " Choose exFAT for larger files.")
        else:
            self.note.set_label("exFAT works on Windows, macOS and Linux, "
                                "but some older devices only read FAT32.")
        problem = check_label(fs, self.label_row.get_text().strip())
        self.error.set_label(problem or "")
        self.error.set_visible(bool(problem))
        self.button.set_sensitive(problem is None)

    def _continue(self) -> None:
        if not self.button.get_sensitive():
            return
        options = self.options()
        self.close()
        self.on_confirmed(options)
