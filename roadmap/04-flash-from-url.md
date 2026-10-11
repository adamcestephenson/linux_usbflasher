# 4. Flash from a URL

Paste a download link; the app streams it straight to the drive with no
temporary copy.

## Why

Saves the disk space and the wait of downloading a multi-GB image first.

## Approach

- Builds on the reader abstraction from [feature 1](01-compressed-images.md):
  an HTTP reader is just another source, and compressed downloads work too.
- Standard library `urllib` is enough. The size comes from the
  `Content-Length` header when the server sends one.
- Computing the SHA-256 while streaming pairs with
  [feature 3](03-checksum-check.md): compare against an expected checksum at
  the end.
- UI: a "Select Image" option for entering a URL, and a "Downloading" state
  on the progress page.

## Open issues

- A failed or interrupted download leaves a half-written drive, and retrying
  means starting over. Consider an optional "download to a cache first"
  mode.
- Resuming is not possible when streaming straight to the drive.
- Redirects, HTTPS certificate errors, and servers without `Content-Length`.
- Verification reads back from the drive, which is fine, since digests were
  recorded during the write.
- Only allow `http(s)`; refuse `file://` and other schemes.
