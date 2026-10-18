"""Dialog for choosing the target drive. The list updates live on hotplug."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .drives import DriveInfo, format_size  # noqa: E402
from .image import ImageInfo  # noqa: E402

BUS_LABELS = {"usb": "USB", "sdio": "SD card", "loop": "Loop", "": ""}


def drive_subtitle(drive: DriveInfo) -> str:
    parts = [drive.device, format_size(drive.size)]
    bus = BUS_LABELS.get(drive.bus, drive.bus.upper())
    if bus:
        parts.append(bus)
    return " · ".join(parts)


def unusable_reasons(drive: DriveInfo, image: ImageInfo | None) -> list[str]:
    """Why ``drive`` can't be picked for ``image`` (empty if it can)."""
    reasons = list(drive.reasons)
    if image is not None and image.size is not None and drive.size < image.size:
        reasons.append(f"Too small for this image (needs {format_size(image.size)})")
    return reasons


class DrivePicker(Adw.Dialog):
    def __init__(
        self,
        monitor,
        *,
        image: ImageInfo | None,
        allow_loop: bool,
        selected: str | None,
        on_selected: Callable[[DriveInfo], None],
    ):
        super().__init__(title="Select Target", content_width=460, content_height=420)
        self.monitor = monitor
        self.image = image
        self.allow_loop = allow_loop
        self.selected = selected
        self.on_selected = on_selected
        self.rows: dict[str, Adw.ActionRow] = {}

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row_activated)

        hint = Gtk.Label(
            label="Only removable drives are shown. Drives that can't be used are greyed out.",
            wrap=True, xalign=0,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(self.list)
        box.append(hint)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, vexpand=True)
        scroller.set_child(box)

        empty = Adw.StatusPage(
            icon_name="drive-removable-media-symbolic",
            title="No Drives Found",
            description="Plug in a USB drive or SD card.",
        )
        empty.add_css_class("compact")

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scroller, "list")
        self.stack.add_named(empty, "empty")

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.stack)
        self.set_child(view)

        self._handler = monitor.connect("changed", lambda _m: self.refresh())
        self.connect("closed", self._on_closed)
        self.refresh()

    def _on_closed(self, _dialog):
        if self._handler:
            self.monitor.disconnect(self._handler)
            self._handler = 0

    def drives(self) -> list[DriveInfo]:
        return self.monitor.drives(
            allow_loop=self.allow_loop,
            source_path=self.image.path if self.image else None,
        )

    def refresh(self) -> None:
        self.list.remove_all()
        self.rows.clear()
        drives = self.drives()
        for drive in drives:
            self.list.append(self._make_row(drive))
        self.stack.set_visible_child_name("list" if drives else "empty")

    def _make_row(self, drive: DriveInfo) -> Adw.ActionRow:
        reasons = unusable_reasons(drive, self.image)
        notes = reasons or drive.warnings
        subtitle = drive_subtitle(drive)
        if notes:
            subtitle += "\n" + "\n".join(notes)
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(drive.label),
            subtitle=GLib.markup_escape_text(subtitle),
            subtitle_lines=0,
            activatable=not reasons,
            sensitive=not reasons,
        )
        icon = "media-removable-symbolic" if drive.bus == "sdio" else "drive-removable-media-symbolic"
        row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=32))
        if drive.warnings and not reasons:
            warn = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text=drive.warnings[0])
            warn.add_css_class("warning")
            row.add_suffix(warn)
        if drive.object_path == self.selected:
            row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
        row.drive = drive
        self.rows[drive.object_path] = row
        return row

    def _on_row_activated(self, _list, row):
        drive = getattr(row, "drive", None)
        if drive is None or unusable_reasons(drive, self.image):
            return
        self.close()
        self.on_selected(drive)
