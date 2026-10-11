"""Command-line entry point. The GUI will be added in a later milestone."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .drives import DriveInfo, DrivesError, format_size


def _print_drives(drives: list[DriveInfo]) -> None:
    if not drives:
        print("No drives found.")
        return
    for d in drives:
        if d.selectable:
            state = "OK"
        elif d.hidden:
            state = "HIDDEN"
        else:
            state = "BLOCKED"
        print(f"{d.device:<14} {format_size(d.size):>10}  {d.bus or '-':<5} {state:<8} {d.label}")
        for r in d.reasons:
            print(f"{'':16}- {r}")
        for w in d.warnings:
            print(f"{'':16}! {w}")
        if d.mount_points:
            print(f"{'':16}  mounted: {', '.join(d.mount_points)} (will be unmounted)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usb-flasher")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--list-drives", action="store_true", help="list candidate target drives")
    parser.add_argument("--all", action="store_true", help="also show hidden (non-removable) drives")
    parser.add_argument("--watch", action="store_true", help="with --list-drives: relist on hotplug")
    parser.add_argument("--source", metavar="IMAGE", help="mark the drive containing IMAGE as unusable")
    parser.add_argument(
        "--dev-allow-loop", action="store_true",
        help="developer option: offer loop devices as targets",
    )
    args = parser.parse_args(argv)

    if not args.list_drives:
        parser.error("nothing to do (the GUI is not implemented yet; try --list-drives)")

    from .drives import DriveMonitor

    try:
        monitor = DriveMonitor()
    except DrivesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    kwargs = dict(allow_loop=args.dev_allow_loop, include_hidden=args.all, source_path=args.source)
    _print_drives(monitor.drives(**kwargs))

    if args.watch:
        from gi.repository import GLib

        def on_changed(_m):
            print("\n--- drives changed ---")
            _print_drives(monitor.drives(**kwargs))

        monitor.connect("changed", on_changed)
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
