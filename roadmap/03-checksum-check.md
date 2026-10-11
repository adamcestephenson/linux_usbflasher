# 3. Checksum check before flashing

Catch a corrupted or tampered download before writing it.

## Why

A bad download is the most common cause of "the stick doesn't boot".
Verification today only proves the drive matches the file, not that the file
is the one the project published.

## Approach

- The engine already computes the image's SHA-256 while streaming.
- Option A: let the user paste an expected SHA-256 on the Image card.
- Option B: look for a `SHA256SUMS`, `<image>.sha256` or `<image>.sha256sum`
  file next to the image and match the entry for that filename.
- Show the result on the Image card (match / mismatch / no checksum found).
- Decide when to hash:
  - **Before writing:** reads the whole image an extra time, but a bad image
    never touches the drive.
  - **During writing:** no extra read, but a mismatch is only reported at the
    end, after the drive has been overwritten.

## Open issues

- Signature checking (`SHA256SUMS.gpg`) is out of scope for a first version.
- Hashing a multi-GB image before writing takes noticeable time; run it in the
  background as soon as the image is selected, and cache the result.
- With compressed images (feature 1), publishers usually checksum the
  compressed file, so hash that, not the decompressed stream.
