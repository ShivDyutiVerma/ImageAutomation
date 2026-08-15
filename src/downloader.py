import os
import tempfile
import time

import requests

import config


class DownloadError(Exception):
    pass


# Flow's CDN has been observed serving JPEG regardless of what the UI implies
# (confirmed live 2026-08-14, see docs/FLOW_UI_NOTES.md) — the extension is
# therefore decided from what the server actually returns, never assumed.
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAGIC_BYTE_EXTENSIONS = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def _detect_extension(content_type, first_bytes):

    if content_type:

        mime = content_type.split(";")[0].strip().lower()
        ext = CONTENT_TYPE_EXTENSIONS.get(mime)

        if ext:
            return ext

    for magic, ext in MAGIC_BYTE_EXTENSIONS:
        if first_bytes.startswith(magic):
            return ext

    if first_bytes[:4] == b"RIFF" and first_bytes[8:12] == b"WEBP":
        return ".webp"

    return ".bin"


def download_image(url, stem, output_dir=None, attempts=None, timeout=None):
    """Download an image to output_dir/{stem}{detected-extension}, atomically.

    stem is the permanent identity (e.g. "047") — the sequence-integrity
    contract in DEVELOPMENT.md is about that number, not the extension, so the
    extension is decided from the actual downloaded content rather than
    assumed by the caller. Returns the full saved path so callers can see
    which extension was chosen.

    The file is streamed to a temporary file in the destination directory and
    only renamed into place once complete. A run killed mid-download therefore
    never leaves a truncated 047.jpg behind — which resume would otherwise
    mistake for finished work and drop into the video edit.
    """

    output_dir = output_dir or config.OUTPUT_DIR
    attempts = attempts or config.DOWNLOAD_ATTEMPTS
    timeout = timeout or config.DOWNLOAD_TIMEOUT

    output_dir.mkdir(parents=True, exist_ok=True)

    last_error = None

    for attempt in range(1, attempts + 1):

        try:
            return _fetch_to(url, output_dir, stem, timeout)

        except (requests.RequestException, DownloadError) as e:

            last_error = e

            if attempt < attempts:
                time.sleep(2 * attempt)

    raise DownloadError(
        f"Download failed after {attempts} attempts: {last_error}"
    )


def _fetch_to(url, output_dir, stem, timeout):

    fd, temp_path = tempfile.mkstemp(
        dir=str(output_dir),
        prefix=f".{stem}-",
        suffix=".part",
    )

    try:

        written = 0
        first_bytes = b""
        content_type = None

        # The descriptor is handed to a file object immediately so that any
        # later failure still closes it; on Windows a leaked handle would
        # block the cleanup unlink below.
        with os.fdopen(fd, "wb") as f:

            with requests.get(url, stream=True, timeout=timeout) as response:

                if response.status_code != 200:
                    raise DownloadError(
                        f"HTTP {response.status_code} for {url[:120]}"
                    )

                content_type = response.headers.get("Content-Type", "")

                for chunk in response.iter_content(chunk_size=64 * 1024):

                    if chunk:

                        if not first_bytes:
                            first_bytes = chunk[:16]

                        f.write(chunk)
                        written += len(chunk)

            f.flush()
            os.fsync(f.fileno())

        if written == 0:
            raise DownloadError(f"Empty response body for {url[:120]}")

        ext = _detect_extension(content_type, first_bytes)
        filepath = output_dir / f"{stem}{ext}"

        os.replace(temp_path, filepath)

        return str(filepath)

    except BaseException:

        try:
            os.unlink(temp_path)
        except OSError:
            pass

        raise
