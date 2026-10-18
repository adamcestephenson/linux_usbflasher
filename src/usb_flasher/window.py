"""Main window: select image -> select target -> flash, then progress and result."""

from __future__ import annotations

import os
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .drive_picker import DrivePicker, drive_subtitle, unusable_reasons  # noqa: E402
from .drives import DriveInfo, format_size  # noqa: E402
from .flasher import Progress, Status, format_eta  # noqa: E402
from .image import (  # noqa: E402
    COMPRESSED_EXTENSIONS, SUPPORTED_EXTENSIONS, ImageError, ImageInfo, inspect_image,
)
from .job import FlashJob, FlashOutcome, Stage, eject_drive  # noqa: E402

def describe_size(img: ImageInfo) -> str:
    if img.size is None:
        return f"{format_size(img.file_size)} compressed, uncompressed size unknown"
    if img.compression:
        return f"{format_size(img.size)} ({format_size(img.file_size)} compressed)"
    return format_size(img.size)


STAGE_TITLES = {
    Stage.PREPARING: "Preparing…",
    Stage.UNMOUNTING: "Unmounting…",
    Stage.OPENING: "Waiting for Authorization…",
    Stage.WRITING: "Flashing…",
    Stage.VERIFYING: "Verifying…",
    Stage.FINISHING: "Finishing…",
    Stage.EJECTING: "Ejecting…",
}
DATA_STAGES = (Stage.WRITING, Stage.VERIFYING)


def _label(text: str = "", *classes: str, **props) -> Gtk.Label:
    label = Gtk.Label(label=text, **props)
    for c in classes:
        label.add_css_class(c)
    return label


class _StepCard(Gtk.Box):
    """One of the three columns on the main page."""

    def __init__(self, icon: str, title: str, button_label: str,
                 on_clicked: Callable[[Gtk.Button], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         margin_top=24, margin_bottom=24, margin_start=18, margin_end=18,
                         valign=Gtk.Align.START)
        self.icon = Gtk.Image(icon_name=icon, pixel_size=56)
        self.icon.add_css_class("dim-label")
        self.title = _label(title, "title-4")
        self.detail = _label("", "dim-label", wrap=True, justify=Gtk.Justification.CENTER,
                             max_width_chars=22, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.warning = _label("", "warning", "caption", wrap=True,
                              justify=Gtk.Justification.CENTER, max_width_chars=26,
                              visible=False)
        self.button = Gtk.Button(label=button_label, halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.connect("clicked", on_clicked)
        for w in (self.icon, self.title, self.detail, self.warning, self.button):
            self.append(w)

    def set_warning(self, text: str | None) -> None:
        self.warning.set_label(text or "")
        self.warning.set_visible(bool(text))


class FlasherWindow(Adw.ApplicationWindow):
    def __init__(self, application=None, *, monitor, client_factory: Callable[[], object],
                 allow_loop: bool = False):
        super().__init__(application=application, title="USB Flasher",
                         default_width=820, default_height=520)
        self.set_size_request(360, 400)
        self.monitor = monitor
        self.client_factory = client_factory
        self.allow_loop = allow_loop

        self.image: ImageInfo | None = None
        self.target: DriveInfo | None = None

        # Flash state (main thread only, except where noted).
        self.worker: threading.Thread | None = None
        self.cancel_event: threading.Event | None = None
        self.flash_target: DriveInfo | None = None
        self.last_outcome: FlashOutcome | None = None
        self._close_when_done = False
        self._stop_dialog: Adw.AlertDialog | None = None
        self._inhibit_cookie = 0
        self._pulse_source = 0
        self._stage = Stage.PREPARING
        self._progress_lock = threading.Lock()
        self._pending_progress: Progress | None = None  # written by worker thread
        self._progress_scheduled = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("About USB Flasher", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Main Menu"))
        view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_select_page(), "select")
        self.stack.add_named(self._build_progress_page(), "progress")
        self.stack.add_named(self._build_result_page(), "result")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self._monitor_handler = monitor.connect("changed", self._on_drives_changed)
        self.connect("close-request", self._on_close_request)
        self._refresh()

    # ------------------------------------------------------------------
    # Page construction
    # ------------------------------------------------------------------

    def _build_select_page(self) -> Gtk.Widget:
        self.image_card = _StepCard("media-optical-symbolic", "Image", "Select Image…",
                                    lambda _b: self.choose_image())
        self.target_card = _StepCard("drive-removable-media-symbolic", "Target",
                                     "Select Target…", lambda _b: self.choose_target())
        self.flash_card = _StepCard("document-save-symbolic", "Flash", "Flash!",
                                    lambda _b: self.confirm_flash())
        self.flash_card.button.add_css_class("suggested-action")

        self.verify_check = Gtk.CheckButton(label="Verify after writing", active=True,
                                            halign=Gtk.Align.CENTER)
        self.eject_check = Gtk.CheckButton(label="Eject when done", active=False,
                                           halign=Gtk.Align.CENTER)
        self.flash_card.insert_child_after(self.verify_check, self.flash_card.button)
        self.flash_card.insert_child_after(self.eject_check, self.verify_check)

        cards = Gtk.Box(spacing=12, homogeneous=True, valign=Gtk.Align.CENTER)
        for card in (self.image_card, self.target_card, self.flash_card):
            frame = Gtk.Box(hexpand=True)
            frame.add_css_class("card")
            card.set_hexpand(True)
            frame.append(card)
            cards.append(frame)

        # Stack the steps vertically on narrow windows.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        narrow.add_setter(cards, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(narrow)

        hint = _label(f"Drop a disk image ({', '.join(SUPPORTED_EXTENSIONS)}, "
                      f"or compressed: {', '.join(COMPRESSED_EXTENSIONS)}) "
                      "anywhere in this window", "dim-label", "caption", wrap=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24)
        box.append(cards)
        box.append(hint)
        clamp = Adw.Clamp(maximum_size=860, child=box)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_progress_page(self) -> Gtk.Widget:
        self.progress_title = _label("", "title-1", wrap=True)
        self.progress_subtitle = _label("", "dim-label", wrap=True,
                                        justify=Gtk.Justification.CENTER)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_detail = _label("", "dim-label", "numeric")
        self.cancel_button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self.cancel_button.add_css_class("pill")
        self.cancel_button.connect("clicked", lambda _b: self.cancel_flash())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, margin_start=36, margin_end=36)
        icon = Gtk.Image(icon_name="document-save-symbolic", pixel_size=64)
        icon.add_css_class("dim-label")
        box.append(icon)
        box.append(self.progress_title)
        box.append(self.progress_subtitle)
        box.append(Gtk.Box(height_request=12))
        box.append(self.progress_bar)
        box.append(self.progress_detail)
        box.append(Gtk.Box(height_request=12))
        box.append(self.cancel_button)
        return Adw.Clamp(maximum_size=520, child=box)

    def _build_result_page(self) -> Gtk.Widget:
        self.result_page = Adw.StatusPage()
        self.result_checksum = _label("", "monospace", "caption", "dim-label",
                                      selectable=True, wrap=True, wrap_mode=Pango.WrapMode.CHAR,
                                      visible=False)
        self.result_warning = _label("", "warning", wrap=True, visible=False,
                                     justify=Gtk.Justification.CENTER)
        self.again_button = Gtk.Button(label="Flash Another", halign=Gtk.Align.CENTER)
        self.again_button.add_css_class("pill")
        self.again_button.add_css_class("suggested-action")
        self.again_button.connect("clicked", lambda _b: self.reset())
        self.eject_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.eject_button.set_child(Adw.ButtonContent(icon_name="media-eject-symbolic",
                                                      label="Eject"))
        self.eject_button.add_css_class("pill")
        self.eject_button.connect("clicked", lambda _b: self.eject())

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(self.eject_button)
        buttons.append(self.again_button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.result_checksum)
        box.append(self.result_warning)
        box.append(buttons)
        self.result_page.set_child(Adw.Clamp(maximum_size=520, child=box))
        return self.result_page

    # ------------------------------------------------------------------
    # Selection page
    # ------------------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def _show_error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _refresh(self) -> None:
        img = self.image
        if img:
            self.image_card.detail.set_label(f"{img.path.name}\n{describe_size(img)}")
            self.image_card.button.set_label("Change…")
            self.image_card.set_warning(" ".join(img.warnings) or None)
        else:
            self.image_card.detail.set_label("No image selected")
            self.image_card.button.set_label("Select Image…")
            self.image_card.set_warning(None)

        drive = self.target
        reasons = unusable_reasons(drive, img) if drive else []
        if drive:
            self.target_card.detail.set_label(f"{drive.label}\n{drive_subtitle(drive)}")
            self.target_card.button.set_label("Change…")
            self.target_card.set_warning("; ".join(reasons + drive.warnings) or None)
        else:
            self.target_card.detail.set_label("No target selected")
            self.target_card.button.set_label("Select Target…")
            self.target_card.set_warning(None)

        ready = img is not None and drive is not None and not reasons
        self.flash_card.button.set_sensitive(ready)
        if ready:
            self.flash_card.detail.set_label("Ready")
        elif img is None:
            self.flash_card.detail.set_label("Select an image first")
        elif drive is None:
            self.flash_card.detail.set_label("Select a target drive")
        else:
            self.flash_card.detail.set_label("The selected target can't be used")

    def choose_image(self) -> None:
        filters = Gio.ListStore.new(Gtk.FileFilter)
        images = Gtk.FileFilter(name="Disk Images")
        for ext in SUPPORTED_EXTENSIONS + COMPRESSED_EXTENSIONS:
            images.add_suffix(ext.lstrip("."))
        everything = Gtk.FileFilter(name="All Files")
        everything.add_pattern("*")
        filters.append(images)
        filters.append(everything)
        dialog = Gtk.FileDialog(title="Select Image", filters=filters, default_filter=images)
        if self.image:
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.image.path.parent)))

        def on_done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return  # dismissed
            self._open_file(file)

        dialog.open(self, None, on_done)

    def _open_file(self, file: Gio.File) -> bool:
        path = file.get_path()
        if path is None:
            self._show_error("Unsupported Location", "Only local files can be flashed.")
            return False
        return self.set_image(path)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        if self.stack.get_visible_child_name() != "select":
            return False
        return self._open_file(value)

    def set_image(self, path: str | os.PathLike) -> bool:
        try:
            info = inspect_image(path)
        except ImageError as e:
            self._show_error("Can't Use This Image", str(e))
            return False
        self.image = info
        self._revalidate_target()
        self._refresh()
        return True

    def choose_target(self) -> None:
        picker = DrivePicker(
            self.monitor, image=self.image, allow_loop=self.allow_loop,
            selected=self.target.object_path if self.target else None,
            on_selected=self.set_target,
        )
        picker.present(self)

    def set_target(self, drive: DriveInfo) -> None:
        self.target = drive
        self._refresh()

    def _current_drive(self, object_path: str) -> DriveInfo | None:
        for d in self.monitor.drives(
            allow_loop=self.allow_loop,
            source_path=self.image.path if self.image else None,
        ):
            if d.object_path == object_path:
                return d
        return None

    def _revalidate_target(self) -> None:
        if self.target is None:
            return
        fresh = self._current_drive(self.target.object_path)
        if fresh is None:
            self.toast(f"{self.target.label} was removed")
        self.target = fresh

    def _on_drives_changed(self, _monitor) -> None:
        if self.worker is not None:
            return  # the job re-checks the drive itself
        self._revalidate_target()
        self._refresh()

    # ------------------------------------------------------------------
    # Flashing
    # ------------------------------------------------------------------

    def confirm_flash(self) -> None:
        self._revalidate_target()
        self._refresh()
        if not self.flash_card.button.get_sensitive():
            return
        drive, img = self.target, self.image
        body = (f"All data on {drive.label} ({drive.device}, {format_size(drive.size)}) "
                f"will be permanently erased and replaced with {img.path.name}.")
        for w in drive.warnings + img.warnings:
            body += f"\n\n{w}"
        if drive.mount_points:
            body += "\n\nIt will be unmounted first."
        dialog = Adw.AlertDialog(heading=f"Erase {drive.label}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("flash", "Erase and Flash")
        dialog.set_response_appearance("flash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "flash" and self.start_flash())
        dialog.present(self)

    def start_flash(self) -> None:
        if self.worker is not None or self.image is None or self.target is None:
            return
        try:
            client = self.client_factory()
        except Exception as e:  # noqa: BLE001
            self._show_error("Can't Connect to udisks2", str(e))
            return

        self.flash_target = self.target
        self.cancel_event = threading.Event()
        job = FlashJob(
            client, self.image.path, self.target.object_path,
            verify=self.verify_check.get_active(), eject=self.eject_check.get_active(),
            allow_loop=self.allow_loop,
            on_stage=lambda s: GLib.idle_add(self._on_stage, s),
            on_progress=self._on_progress_threaded,
            cancel=self.cancel_event,
        )

        self.progress_subtitle.set_label(
            f"{self.image.path.name} → {self.target.label} ({self.target.device})")
        self.progress_bar.set_fraction(0)
        self.progress_detail.set_label("")
        self.cancel_button.set_label("Cancel")
        self.cancel_button.set_sensitive(True)
        self._on_stage(Stage.PREPARING)
        self.stack.set_visible_child_name("progress")

        app = self.get_application()
        if app is not None:
            self._inhibit_cookie = app.inhibit(
                self, Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.LOGOUT,
                "Flashing a drive")

        self.worker = threading.Thread(target=self._run_job, args=(job,), daemon=True,
                                       name="flash-job")
        self.worker.start()

    def _run_job(self, job: FlashJob) -> None:  # worker thread
        try:
            outcome = job.run()
        except Exception as e:  # noqa: BLE001 - never leave the UI stuck
            outcome = FlashOutcome(Status.ERROR, job.stage, f"Unexpected error: {e}")
        GLib.idle_add(self._on_job_done, outcome)

    def _on_progress_threaded(self, progress: Progress) -> None:  # worker thread
        with self._progress_lock:
            self._pending_progress = progress
            if self._progress_scheduled:
                return
            self._progress_scheduled = True
        GLib.idle_add(self._apply_progress)

    def _apply_progress(self) -> bool:
        with self._progress_lock:
            p = self._pending_progress
            self._progress_scheduled = False
        if p is None or self._stage not in DATA_STAGES:
            return GLib.SOURCE_REMOVE
        if p.indeterminate:
            self._start_pulse()
            detail = f"{format_size(p.bytes_done)} written"
        else:
            self._stop_pulse()
            self.progress_bar.set_fraction(p.fraction)
            detail = (f"{p.fraction * 100:.0f}% · {format_size(p.bytes_done)} of "
                      f"{format_size(p.total)}")
        if p.speed > 0:
            detail += f" · {format_size(int(p.speed))}/s · {format_eta(p.eta)} left"
        self.progress_detail.set_label(detail)
        return GLib.SOURCE_REMOVE

    def _on_stage(self, stage: Stage) -> bool:
        self._stage = stage
        if not (self.cancel_event and self.cancel_event.is_set()):
            self.progress_title.set_label(STAGE_TITLES[stage])
        with self._progress_lock:
            self._pending_progress = None
        if stage in DATA_STAGES:
            self._stop_pulse()
            self.progress_bar.set_fraction(0)
            self.progress_detail.set_label("")
        else:
            self.progress_detail.set_label(
                "Enter your password if prompted" if stage is Stage.OPENING else "")
            self._start_pulse()
        return GLib.SOURCE_REMOVE

    def _start_pulse(self) -> None:
        if not self._pulse_source:
            self._pulse_source = GLib.timeout_add(100, self._pulse)

    def _pulse(self) -> bool:
        self.progress_bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _stop_pulse(self) -> None:
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def cancel_flash(self) -> None:
        if self.cancel_event is None or self.worker is None:
            return
        self.cancel_event.set()
        self.cancel_button.set_sensitive(False)
        self.cancel_button.set_label("Cancelling…")
        self.progress_title.set_label("Cancelling…")

    def _on_job_done(self, outcome: FlashOutcome) -> bool:
        self._stop_pulse()
        if self.worker is not None:
            self.worker.join()
        self.worker = None
        self.last_outcome = outcome
        app = self.get_application()
        if self._inhibit_cookie and app is not None:
            app.uninhibit(self._inhibit_cookie)
        self._inhibit_cookie = 0

        if self._close_when_done:
            self.close()
            return GLib.SOURCE_REMOVE

        self._show_result(outcome)
        self.stack.set_visible_child_name("result")
        if app is not None and not self.is_active():
            note = Gio.Notification.new(self.result_page.get_title())
            note.set_body(self.result_page.get_description())
            app.send_notification("flash-done", note)
        return GLib.SOURCE_REMOVE

    def _show_result(self, outcome: FlashOutcome) -> None:
        drive = self.flash_target
        page = self.result_page
        self.result_checksum.set_visible(False)
        self.eject_button.set_visible(False)
        self.eject_button.set_sensitive(True)

        if outcome.status is Status.SUCCESS:
            page.set_icon_name("object-select-symbolic")
            page.set_title("Flash Complete")
            verified = " and verified" if self.verify_check.get_active() else ""
            desc = f"{self.image.path.name} was written{verified} to {drive.label}."
            if outcome.ejected:
                desc += " The drive was ejected and can be removed."
            page.set_description(desc)
            if outcome.sha256:
                self.result_checksum.set_label(f"SHA-256: {outcome.sha256}")
                self.result_checksum.set_visible(True)
            self.eject_button.set_visible(drive.drive_path is not None and not outcome.ejected)
        elif outcome.status is Status.CANCELLED:
            page.set_icon_name("process-stop-symbolic")
            page.set_title("Flash Cancelled")
            page.set_description(
                outcome.message if outcome.drive_modified
                else "No changes were made to the drive.")
        else:
            page.set_icon_name("dialog-error-symbolic")
            page.set_title("Flash Failed")
            page.set_description(outcome.message or "Unknown error")

        self.result_warning.set_label("\n".join(outcome.warnings))
        self.result_warning.set_visible(bool(outcome.warnings))

    def eject(self) -> None:
        drive = self.flash_target
        if drive is None or drive.drive_path is None:
            return
        self.eject_button.set_sensitive(False)

        def work():
            try:
                eject_drive(self.client_factory(), drive.object_path, drive.drive_path)
                error = None
            except Exception as e:  # noqa: BLE001
                error = str(e)
            GLib.idle_add(done, error)

        def done(error):
            if error:
                self.eject_button.set_sensitive(True)
                self._show_error("Eject Failed", error)
            else:
                self.eject_button.set_visible(False)
                self.toast(f"{drive.label} can now be removed")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, daemon=True, name="eject").start()

    def reset(self) -> None:
        self.last_outcome = None
        self._revalidate_target()
        self._refresh()
        self.stack.set_visible_child_name("select")

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def _on_close_request(self, _window) -> bool:
        if self.worker is None:
            if self._monitor_handler:
                self.monitor.disconnect(self._monitor_handler)
                self._monitor_handler = 0
            return False  # allow close
        if self._close_when_done or self._stop_dialog is not None:
            return True  # already asking, or stopping and closing when the job ends

        dialog = Adw.AlertDialog(
            heading="Stop Flashing?",
            body="The drive will be left with an incomplete image and won't be "
                 "usable until it is flashed again.",
        )
        dialog.add_response("continue", "Keep Flashing")
        dialog.add_response("stop", "Stop and Close")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("continue")
        dialog.set_close_response("continue")

        def on_response(_d, response):
            if response == "stop" and self.worker is not None:
                self._close_when_done = True
                self.cancel_flash()

        def on_closed(_d):
            self._stop_dialog = None

        dialog.connect("response", on_response)
        dialog.connect("closed", on_closed)
        self._stop_dialog = dialog
        dialog.present(self)
        return True
