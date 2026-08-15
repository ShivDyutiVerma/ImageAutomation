"""First-run setup for a new machine.

    python src/setup.py            walk through anything that's missing
    python src/setup.py --check    report only, change nothing

Signing into Google is the one step that must be done by hand (see
docs/ARCHITECTURE.md §7 — it is never automated, on purpose). This script
reduces it to: a Chrome window opens, you sign in, it detects when you're
done, and the session persists for every run after that.
"""

import argparse
import sys

import config
import setup_check

SYMBOL = {
    setup_check.STATUS_OK: "[ ok ]",
    setup_check.STATUS_FAIL: "[FAIL]",
    setup_check.STATUS_WARN: "[warn]",
    setup_check.STATUS_UNKNOWN: "[ -- ]",
}


def report(result):

    print("=" * 62)
    print("SETUP CHECK")
    print("=" * 62)

    for check in result["checks"]:

        print(f"  {SYMBOL[check['status']]}  {check['label']}: {check['detail']}")

        if check["fix"] and check["status"] != setup_check.STATUS_OK:
            print(f"          -> {check['fix']}")

    print("=" * 62)

    if result["ready"]:
        print("Ready to run.")
    else:
        print(f"Not ready — blocked on: {', '.join(result['blocking'])}")

    print()

    return result["ready"]


def ask(question, default="y"):

    suffix = "[Y/n]" if default == "y" else "[y/N]"

    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if not answer:
        answer = default

    return answer.startswith("y")


def do_login():

    print()
    print("-" * 62)
    print("GOOGLE SIGN-IN (one time)")
    print("-" * 62)
    print("A Chrome window will open at Google Flow.")
    print("Sign into the Google account that has your Flow Pro subscription.")
    print("This window is the tool's own browser profile — signing in here")
    print("does not touch your normal Chrome profile.")
    print()
    print("Waiting for you to finish (up to 5 minutes)...")
    print()

    def on_progress(seconds):
        if seconds and seconds % 30 == 0:
            print(f"  ...still waiting ({seconds}s)")

    try:
        ok = setup_check.wait_for_login(on_progress=on_progress)
    except Exception as e:
        print(f"  Could not complete sign-in: {type(e).__name__}: {e}")
        return False

    if ok:
        print("  Signed in. The session is saved and will be reused from now on.")
    else:
        print("  Timed out without detecting a signed-in Flow page.")
        print("  If you did sign in, just re-run this — it only needs to see")
        print("  the Flow page once while signed in.")

    return ok


def do_pick_project():

    print()
    print("-" * 62)
    print("FLOW PROJECT")
    print("-" * 62)
    print("Pinning a project keeps a video's images together and stops the")
    print("automation having to guess when the account has several.")
    print()

    try:
        projects = setup_check.discover_projects()
    except Exception as e:
        print(f"  Could not list projects: {type(e).__name__}: {e}")
        return False

    if not projects:
        print("  No existing projects found.")
        print("  Create one in the Chrome window, then re-run this — or set")
        print("  FLOW_AUTO_CREATE_PROJECT=true in .env to let the automation")
        print("  create one itself on the next run.")
        return False

    for i, url in enumerate(projects, start=1):
        print(f"  {i}. {url}")

    print()

    try:
        choice = input(f"Which project? [1-{len(projects)}, blank to skip] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if not choice:
        return False

    try:
        index = int(choice)
        if not 1 <= index <= len(projects):
            raise ValueError
    except ValueError:
        print("  Not a valid choice — skipping.")
        return False

    path = setup_check.update_env("FLOW_PROJECT_URL", projects[index - 1])

    print(f"  Saved FLOW_PROJECT_URL to {path}")
    print("  (restart any running command for it to take effect)")

    return True


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Check and complete first-run setup for a new machine."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report readiness only; make no changes and open nothing",
    )
    args = parser.parse_args(argv)

    result = setup_check.run_all()
    ready = report(result)

    if args.check:
        return 0 if ready else 1

    by_id = {c["id"]: c for c in result["checks"]}

    # Nothing below can succeed without these, and neither is fixable from
    # here — installing packages or a browser is the user's call.
    for blocker in ("dependencies", "chrome"):
        if by_id[blocker]["status"] == setup_check.STATUS_FAIL:
            print(f"Fix '{blocker}' first: {by_id[blocker]['fix']}")
            return 1

    needs_login = any(
        by_id[c]["status"] in (setup_check.STATUS_FAIL, setup_check.STATUS_WARN)
        and by_id[c].get("action") == "login"
        for c in ("profile", "login")
        if c in by_id
    )

    if needs_login:
        if ask("Open a Chrome window to sign into Google now?"):
            do_login()
    else:
        print("Google session looks set up already.")

    if not config.PROJECT_URL:
        if ask("Pick which Flow project to use?"):
            do_pick_project()

    print()
    print("Re-checking...")
    print()

    # Config was read at import time; re-read so a project just written to
    # .env is reflected instead of showing a stale "not set".
    import importlib

    importlib.reload(config)
    importlib.reload(setup_check)

    final = setup_check.run_all()
    ready = report(final)

    if ready:
        print("Next: python src/web_ui.py   (then open http://127.0.0.1:8765)")
        print("  or: python src/main.py     (command line)")

    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
