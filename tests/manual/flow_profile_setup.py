"""One-time Google/Flow login, creating the persistent Chrome profile.

Run this once per machine. A Chrome window opens; sign into your Google
account and make sure Flow loads. The script detects when you're done and
exits — closing the window yourself also works.

    python tests/manual/flow_profile_setup.py

This is the same login flow `python src/setup.py` walks you through as part
of its full readiness check; use this directly if you just want the login
step on its own. Both use your real, already-installed Chrome (auto-detected
for Windows/macOS/Linux) against the same profile directory — never
Playwright's own bundled browser, which would create a profile a different
build of Chromium wrote, not guaranteed compatible with the real Chrome
every actual run attaches to.

Treat the profile directory as a credential afterwards: never commit or
share it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import config  # noqa: E402
import setup_check  # noqa: E402


def main():

    print(f"Profile directory: {config.CHROME_PROFILE_DIR}")
    print("\nOpening Chrome. Sign into Google and make sure Flow loads.")
    print("Waiting for you to finish (up to 5 minutes)...\n")

    def on_progress(seconds):
        if seconds and seconds % 30 == 0:
            print(f"  ...still waiting ({seconds}s)")

    try:
        ok = setup_check.wait_for_login(on_progress=on_progress)
    except Exception as e:
        print(f"Could not complete sign-in: {type(e).__name__}: {e}")
        return 1

    if ok:
        print("Signed in. The session is saved and every future run reuses it.")
        return 0

    print("Timed out without detecting a signed-in Flow page.")
    print("If you did sign in, just re-run this script.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
