import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

import config


class ChromeLaunchError(Exception):
    pass


# Ordered best-guess install locations per platform, so a fresh machine
# usually needs no configuration at all. FLOW_CHROME_EXECUTABLE overrides
# all of this when someone has Chrome somewhere unusual.
if sys.platform == "darwin":
    COMMON_CHROME_PATHS = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    CHROME_COMMAND_NAMES = ["google-chrome", "chromium"]

elif sys.platform.startswith("linux"):
    COMMON_CHROME_PATHS = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ]
    CHROME_COMMAND_NAMES = [
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    ]

else:
    COMMON_CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    CHROME_COMMAND_NAMES = ["chrome", "chrome.exe"]


def manual_launch_command(cdp_url=None, profile_dir=None):
    """The command a user would type to start Chrome by hand, correct for
    whatever platform this is actually running on — shown in error messages
    when auto-launch can't do it for them.
    """

    cdp_url = cdp_url or config.CDP_URL
    profile_dir = profile_dir or config.CHROME_PROFILE_DIR
    port = cdp_url.rsplit(":", 1)[-1]

    try:
        exe = find_chrome_executable()
    except ChromeLaunchError:
        exe = "google-chrome" if sys.platform != "win32" else "chrome.exe"

    quote = '"' if sys.platform == "win32" else "'"

    return (
        f'{exe} --remote-debugging-port={port} '
        f'--user-data-dir={quote}{profile_dir}{quote}'
    )


def is_cdp_reachable(cdp_url, timeout=2):

    try:
        response = requests.get(f"{cdp_url}/json/version", timeout=timeout)
        return response.status_code == 200
    except requests.RequestException:
        return False


def find_chrome_executable():

    if config.CHROME_EXECUTABLE:

        if Path(config.CHROME_EXECUTABLE).exists():
            return config.CHROME_EXECUTABLE

        raise ChromeLaunchError(
            f"FLOW_CHROME_EXECUTABLE is set to {config.CHROME_EXECUTABLE!r} "
            f"but that file doesn't exist."
        )

    for name in CHROME_COMMAND_NAMES:
        found = shutil.which(name)
        if found:
            return found

    for candidate in COMMON_CHROME_PATHS:
        if Path(candidate).exists():
            return candidate

    raise ChromeLaunchError(
        "Could not find Google Chrome on PATH or in the usual install "
        "locations for this platform. Install Chrome, or set "
        "FLOW_CHROME_EXECUTABLE in .env to its full path."
    )


def launch_chrome(cdp_url, profile_dir, start_url):
    """Launch Chrome pointed at an already-authenticated profile.

    No credentials are ever provided here — the profile directory already
    holds a logged-in session from the one-time manual login (python
    src/setup.py, or tests/manual/flow_profile_setup.py directly). This only
    starts a process.

    Detached so the browser survives after this script exits — it's meant to
    stay open and get reused across runs, not be tied to this process.
    """

    exe = find_chrome_executable()

    port = cdp_url.rsplit(":", 1)[-1]

    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        start_url,
    ]

    # Detaching is platform-specific and the two mechanisms are not
    # interchangeable: passing Windows-only creationflags on POSIX raises
    # ValueError (confirmed against cpython's subprocess source, not
    # guessed), and start_new_session (setsid) is a no-op on Windows. Each
    # branch only sets the kwarg its own platform understands.
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(args, **popen_kwargs)
    except OSError as e:
        raise ChromeLaunchError(f"Could not start Chrome ({exe}): {e}")


def ensure_chrome_running(cdp_url=None, profile_dir=None, start_url=None, timeout=None):
    """No-op if Chrome is already reachable at cdp_url. Otherwise launches it
    against the configured profile and waits for it to come up.
    """

    cdp_url = cdp_url or config.CDP_URL
    profile_dir = profile_dir or config.CHROME_PROFILE_DIR
    start_url = start_url or (config.FLOW_ORIGIN + config.FLOW_TOOLS_URL_MARKER)
    timeout = timeout or config.CHROME_LAUNCH_TIMEOUT

    if is_cdp_reachable(cdp_url):
        return

    launch_chrome(cdp_url, profile_dir, start_url)

    start = time.time()

    while time.time() - start < timeout:

        if is_cdp_reachable(cdp_url):
            time.sleep(1)  # let the initial page begin loading
            return

        time.sleep(0.5)

    raise ChromeLaunchError(
        f"Launched Chrome but it never became reachable at {cdp_url} within "
        f"{timeout}s."
    )
