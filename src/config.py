import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _str(name, default):

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    return raw.strip()


def _int(name, default):

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name} must be a whole number, got {raw!r}"
        )

    if value < 0:
        raise ValueError(
            f"{name} must not be negative, got {value}"
        )

    return value


def _bool(name, default):

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_path(raw):
    """Anchor a relative path to the project root, never the working
    directory, so behavior doesn't depend on where a command is run from.

    Shared by .env parsing (_path) and CLI argument parsing (main.py), so a
    path typed on the command line follows the same rule as one in .env.
    """

    path = Path(raw).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


def _path(name, default):

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    return resolve_path(raw.strip())


CDP_URL = _str("FLOW_CDP_URL", "http://localhost:9222")

PROMPTS_FILE = _path("FLOW_PROMPTS_FILE", PROJECT_ROOT / "prompts" / "prompts.txt")
OUTPUT_DIR = _path("FLOW_OUTPUT_DIR", PROJECT_ROOT / "output")
MANIFEST_FILE = _path("FLOW_MANIFEST_FILE", OUTPUT_DIR / "manifest.json")

READY_TIMEOUT = _int("FLOW_READY_TIMEOUT", 60)
GENERATION_TIMEOUT = _int("FLOW_GENERATION_TIMEOUT", 180)
POLL_INTERVAL = _int("FLOW_POLL_INTERVAL", 2)
DELAY_BETWEEN_PROMPTS = _int("FLOW_DELAY_BETWEEN_PROMPTS", 3)

# Flow can produce a second (or later) image from a single Create click,
# arriving staggered rather than all at once -- confirmed live (2026-08-18)
# with gaps of 10-14s between what looked like two consecutive beats but
# were actually one beat's own delayed second image. wait_for_new_images
# waits for the set of new images to hold steady for SETTLE_STABLE_SECONDS
# before considering a beat's generation finished, so a straggler lands
# within THIS beat's wait rather than getting mistaken for the next beat's
# own result. SETTLE_TIMEOUT bounds that stabilization phase on its own,
# so a straggler that never stops trickling in can't consume the whole
# GENERATION_TIMEOUT budget.
SETTLE_STABLE_SECONDS = _int("FLOW_SETTLE_STABLE_SECONDS", 20)
SETTLE_TIMEOUT = _int("FLOW_SETTLE_TIMEOUT", 60)

MAX_ATTEMPTS = _int("FLOW_MAX_ATTEMPTS", 3)
RETRY_BACKOFF = _int("FLOW_RETRY_BACKOFF", 10)

# An expired session or exhausted credits fails every remaining prompt
# identically. Without this ceiling a 300-prompt run would spend days
# retrying against a browser that can never succeed.
CONSECUTIVE_FAILURE_LIMIT = _int("FLOW_CONSECUTIVE_FAILURE_LIMIT", 5)

# When the limit above is hit, don't give up immediately -- pause and try
# again, since a lot of what trips this (a brief Flow outage, a momentary
# rate limit, credits that top up on their own schedule) resolves within
# minutes on its own. Still bounded: after MAX_COOLDOWNS cycles of "wait,
# still failing", give up for real rather than pausing forever against a
# genuinely dead session or truly exhausted credits.
COOLDOWN_SECONDS = _int("FLOW_COOLDOWN_SECONDS", 300)
MAX_COOLDOWNS = _int("FLOW_MAX_COOLDOWNS", 3)

DOWNLOAD_TIMEOUT = _int("FLOW_DOWNLOAD_TIMEOUT", 120)
DOWNLOAD_ATTEMPTS = _int("FLOW_DOWNLOAD_ATTEMPTS", 3)

IMAGE_URL_MARKER = _str("FLOW_IMAGE_URL_MARKER", "media.getMediaUrlRedirect")
FLOW_ORIGIN = _str("FLOW_ORIGIN", "https://labs.google")
FLOW_PROJECT_URL_MARKER = _str("FLOW_PROJECT_URL_MARKER", "/flow/project/")
FLOW_TOOLS_URL_MARKER = _str("FLOW_TOOLS_URL_MARKER", "/fx/tools/flow")

# If set, the automation navigates straight to this project instead of
# guessing among whatever projects already exist in the account. This is the
# recommended way to pin a specific video's run to a specific Flow project.
PROJECT_URL = _str("FLOW_PROJECT_URL", None)

# When no project is open or configured and none exist yet, click "New
# project" automatically. Off by default: creating a project is a mutating
# action on the user's account and should be opted into deliberately.
AUTO_CREATE_PROJECT = _bool("FLOW_AUTO_CREATE_PROJECT", False)

NEW_PROJECT_TIMEOUT = _int("FLOW_NEW_PROJECT_TIMEOUT", 30)

# Chrome auto-launch. The profile here is already authenticated (one-time
# manual login via tests/manual/flow_profile_setup.py) — launching Chrome
# against it needs no credentials at all, so this is safe to default on.
# See docs/ARCHITECTURE.md for why actual login is never automated.
AUTO_LAUNCH_CHROME = _bool("FLOW_AUTO_LAUNCH_CHROME", True)
CHROME_PROFILE_DIR = _path("FLOW_CHROME_PROFILE_DIR", PROJECT_ROOT / "flow" / "_profile")
CHROME_EXECUTABLE = _str("FLOW_CHROME_EXECUTABLE", None)
CHROME_LAUNCH_TIMEOUT = _int("FLOW_CHROME_LAUNCH_TIMEOUT", 30)


def describe():

    return {
        "cdp_url": CDP_URL,
        "prompts_file": str(PROMPTS_FILE),
        "output_dir": str(OUTPUT_DIR),
        "manifest_file": str(MANIFEST_FILE),
        "ready_timeout": READY_TIMEOUT,
        "generation_timeout": GENERATION_TIMEOUT,
        "poll_interval": POLL_INTERVAL,
        "delay_between_prompts": DELAY_BETWEEN_PROMPTS,
        "max_attempts": MAX_ATTEMPTS,
        "retry_backoff": RETRY_BACKOFF,
        "consecutive_failure_limit": CONSECUTIVE_FAILURE_LIMIT,
        "download_timeout": DOWNLOAD_TIMEOUT,
        "download_attempts": DOWNLOAD_ATTEMPTS,
    }
