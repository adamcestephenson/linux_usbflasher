# Roadmap

Ideas for future versions, roughly in priority order. Nothing here is
committed work: each file is a starting point for a detailed plan.

| # | Feature | Value | Effort |
|---|---------|-------|--------|
| 1 | [Compressed images](01-compressed-images.md) | High | Small-medium |
| 2 | [Restore a drive to normal storage](02-restore-drive.md) | High | Small |
| 3 | [Checksum check before flashing](03-checksum-check.md) | Medium-high | Small |
| 4 | [Flash from a URL](04-flash-from-url.md) | Medium | Medium |
| 5 | [Flash several drives at once](05-multi-drive.md) | Medium | Medium-large |

Recommended order: 1 then 2. They fix the two most common gaps, are mostly
self-contained, and need no new dependencies. Feature 4 builds on 1, and
feature 3 pairs naturally with 4.

## Also considered

Lower priority, or much bigger:

- **Windows ISO support.** Windows ISOs are not hybrid, so they need files
  copied onto a formatted drive (and `install.wim` splitting for FAT32)
  instead of a raw write. Large.
- **Cloning one drive to another.** Use a drive as the source instead of a
  file.
- **Recently used images.** A short list on the Image card.
- **Flatpak packaging.** Needs extra permissions to talk to udisks, so it is
  not a straightforward port.
- **Faster writes with `O_DIRECT`.** Currently `O_SYNC` with 4 MiB chunks;
  worth measuring before changing.

## Where things are today

For context when planning, the relevant existing pieces:

- `flasher.py`: `write_image()` reads the source sequentially from a file
  descriptor and records per-chunk BLAKE2b digests; `verify()` reads the
  drive back and compares against them.
- `job.py`: `FlashJob` runs one complete flash in a worker thread.
- `drives.py`: drive discovery and the safety rules (`classify_drives`).
- `image.py`: `inspect_image()` validates the source file and rejects
  compressed extensions.
- `udisks.py`: all privileged udisks2 calls.
