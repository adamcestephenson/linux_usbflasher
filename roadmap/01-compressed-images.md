# 1. Compressed images

Support `.img.xz`, `.gz`, `.zst`, `.bz2` and `.zip`.

## Why

Many images ship compressed (Raspberry Pi OS, Armbian, Home Assistant).
`image.py` currently rejects them with "Compressed images are not supported
yet".

## Approach

- `write_image()` reads the source from a file descriptor with `os.readv`.
  Change it to read from a file-like object, with a small reader class per
  format, so decompression happens as the data streams to the drive.
- Python's standard library covers the formats: `gzip`, `bz2`, `lzma`
  (xz), `zipfile`, and `compression.zstd` (Python 3.14 only).
- `verify()` needs no change: it compares against the digests recorded
  during the write, so nothing is decompressed a second time.
- `inspect_image()` should look through the compression layer, so the ISO
  hybrid check still works on `.iso.xz`.

## Open issues

- **Unknown uncompressed size.** The "does it fit" check and the progress
  bar need the size up front.
  - zip stores it reliably.
  - zstd frames usually store it.
  - xz stores it in the index (needs a small parser; `lzma` does not expose
    it directly).
  - gzip only stores it modulo 4 GB, so it can't be trusted for large
    images.
  - bz2 stores nothing.
  - When the size is unknown: show an indeterminate or compressed-bytes
    progress bar, and fail cleanly if the drive fills up.
- **zstd needs Python 3.14.** `pyproject.toml` says `>=3.11`. Either make zstd
  optional (detect the module) or raise the minimum.
- **Multi-file zips.** Decide what to do: pick the single `.img`/`.iso`
  inside, or refuse.
- **Decompression bombs.** Cap output at the drive size.

## Tests

Round-trip each format against a temp file, a truncated archive, a corrupt
archive, and an image larger than the drive.
