"""First-run readiness checks.

Everything a fresh machine needs before a batch can run, checked one at a
time so a new user is told exactly which step is missing rather than
hitting a generic failure deep inside a run.

    python src/setup.py          # interactive walkthrough
    python src/setup.py --check  # report only, change nothing

The one step that can never be automated is the Google login itself (see
docs/ARCHITECTURE.md §7). Everything around it is automated; the login is
reduced to "a browser window opens, you sign in, we detect when you're
done".
"""

import time
from pathlib import Path

import chrome_launcher
import config

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_UNKNOWN = "unknown"


def _check(check_id, label, status, detail, fix=None, action=None):
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "fix": fix,
        "action": action,
    }


def check_dependencies():

    missing = []

    for module, package in [
        ("playwright", "playwright"),
        ("requests", "requests"),
        ("dotenv", "python-dotenv"),
        ("tqdm", "tqdm"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        return _check(
            "dependencies",
            "Python packages",
            STATUS_FAIL,
            f"Missing: {', '.join(missing)}",
            fix="pip install -r requirements.txt",
        )

    return _check(
        "dependencies", "Python packages", STATUS_OK, "All required packages installed"
    )


def check_chrome():

    try:
        exe = chrome_launcher.find_chrome_executable()
    except chrome_launcher.ChromeLaunchError as e:
        return _check(
            "chrome",
            "Google Chrome",
            STATUS_FAIL,
            str(e),
            fix="Install Chrome from google.com/chrome, or set "
                "FLOW_CHROME_EXECUTABLE in .env to its full path.",
        )

    return _check("chrome", "Google Chrome", STATUS_OK, exe)


def profile_looks_used(profile_dir=None):
    """Whether this directory looks like a Chrome profile that has actually
    been opened at least once (as opposed to an empty folder we created).
    """

    profile_dir = Path(profile_dir or config.CHROME_PROFILE_DIR)

    return (profile_dir / "Default").is_dir() and any(
        (profile_dir / "Default" / name).exists()
        for name in ("Preferences", "Cookies", "Network")
    )


def check_profile():

    profile_dir = Path(config.CHROME_PROFILE_DIR)

    if not profile_dir.exists():
        return _check(
            "profile",
            "Browser profile",
            STATUS_FAIL,
            f"Not created yet: {profile_dir}",
            fix="Run the first-time login — a Chrome window opens and you "
                "sign into Google once.",
            action="login",
        )

    if not profile_looks_used(profile_dir):
        return _check(
            "profile",
            "Browser profile",
            STATUS_WARN,
            f"Folder exists but looks empty: {profile_dir}",
            fix="Run the first-time login to populate it.",
            action="login",
        )

    return _check("profile", "Browser profile", STATUS_OK, str(profile_dir))


def check_login(timeout=8):
    """Verify the saved session is actually signed in.

    Only meaningful when Chrome is already running with the profile — this
    never launches anything, because a readiness check shouldn't have the
    side effect of opening browser windows.
    """

    if not chrome_launcher.is_cdp_reachable(config.CDP_URL, timeout=2):
        return _check(
            "login",
            "Google sign-in",
            STATUS_UNKNOWN,
            "Chrome isn't running yet, so this can't be verified now. It's "
            "checked automatically when a run starts.",
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _check("login", "Google sign-in", STATUS_UNKNOWN,
                      "Playwright not installed")

    playwright = None

    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(config.CDP_URL)

        if not browser.contexts:
            return _check("login", "Google sign-in", STATUS_UNKNOWN,
                          "Chrome has no browser context")

        urls = []

        for page in browser.contexts[0].pages:
            try:
                urls.append(page.url)
            except Exception:
                continue

        if any("accounts.google.com" in url for url in urls):
            return _check(
                "login",
                "Google sign-in",
                STATUS_FAIL,
                "Chrome is on a Google sign-in page — the session isn't active.",
                fix="Sign in once in that window; it then persists.",
                action="login",
            )

        if any(config.FLOW_TOOLS_URL_MARKER in url for url in urls):
            return _check("login", "Google sign-in", STATUS_OK,
                          "Flow is open and not redirecting to sign-in")

        return _check(
            "login",
            "Google sign-in",
            STATUS_UNKNOWN,
            "Chrome is running but no Flow tab is open to check against.",
        )

    except Exception as e:
        return _check("login", "Google sign-in", STATUS_UNKNOWN, f"Could not check: {e}")

    finally:
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass


def check_prompts(prompts_file=None):

    path = Path(prompts_file or config.PROMPTS_FILE)

    if not path.exists():
        return _check(
            "prompts",
            "Prompts file",
            STATUS_FAIL,
            f"Not found: {path}",
            fix="Create it, or paste your beats into the web UI and save.",
        )

    try:
        from prompt_loader import load_prompts

        prompts = load_prompts(path)
    except Exception as e:
        return _check(
            "prompts",
            "Prompts file",
            STATUS_FAIL,
            f"{type(e).__name__}: {e}",
            fix="Fix the prompts file — see README for the two accepted formats.",
        )

    return _check("prompts", "Prompts file", STATUS_OK,
                  f"{len(prompts)} beat(s) in {path}")


def check_project():

    if config.PROJECT_URL:
        return _check("project", "Flow project", STATUS_OK, config.PROJECT_URL)

    return _check(
        "project",
        "Flow project",
        STATUS_WARN,
        "No FLOW_PROJECT_URL set.",
        fix="Fine if the account has exactly one project (it's picked "
            "automatically). With several, the run stops and asks rather "
            "than guessing — pin one to be safe.",
        action="pick_project",
    )


def update_env(key, value, env_path=None):
    """Set one key in .env, preserving every other line and any comments.

    Creates the file if absent. Values are written raw (no quoting) to match
    how python-dotenv reads them back and how .env.example is written.
    """

    env_path = Path(env_path or (config.PROJECT_ROOT / ".env"))

    lines = []

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    replaced = False
    out = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                if not replaced:
                    out.append(f"{key}={value}")
                    replaced = True
                continue
        out.append(line)

    if not replaced:
        out.append(f"{key}={value}")

    env_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")

    return env_path


def _logged_in_signals(page):
    """Positive evidence that this Flow page belongs to a signed-in session.

    Checked as positive signals rather than "no sign-in page", because Flow
    can render a marketing splash while perfectly well authenticated (see
    docs/FLOW_UI_NOTES.md, 2026-08-15) — absence of a login screen alone
    would give false positives.
    """

    try:
        url = page.url
    except Exception:
        return False

    if "accounts.google.com" in url:
        return False

    if config.FLOW_PROJECT_URL_MARKER in url:
        return True

    try:
        project_links = page.eval_on_selector_all(
            f"a[href*='{config.FLOW_PROJECT_URL_MARKER}']", "els => els.length"
        )
        if project_links:
            return True
    except Exception:
        pass

    try:
        buttons = page.eval_on_selector_all(
            "button", "els => els.map(e => (e.innerText || '').trim())"
        )
        if any("New project" in text for text in buttons):
            return True
    except Exception:
        pass

    return False


def wait_for_login(timeout=300, poll=3, on_progress=None):
    """Launch Chrome at Flow and wait for the user to finish signing in.

    Uses the user's real Chrome against the real profile directory — the
    same browser and profile the automation later attaches to — so there's
    no cross-browser profile mismatch and nothing extra to download.

    Returns True once a signed-in Flow page is detected, False on timeout.
    """

    from playwright.sync_api import sync_playwright

    chrome_launcher.ensure_chrome_running(config.CDP_URL)

    playwright = sync_playwright().start()

    try:
        browser = playwright.chromium.connect_over_cdp(config.CDP_URL)

        if not browser.contexts:
            return False

        context = browser.contexts[0]
        start = time.time()

        while time.time() - start < timeout:

            for page in list(context.pages):
                try:
                    if config.FLOW_TOOLS_URL_MARKER in page.url and _logged_in_signals(page):
                        return True
                except Exception:
                    continue

            if on_progress:
                on_progress(int(time.time() - start))

            time.sleep(poll)

        return False

    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def discover_projects():
    """List the account's existing Flow projects as absolute URLs.

    Requires Chrome to already be running and signed in.
    """

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()

    try:
        browser = playwright.chromium.connect_over_cdp(config.CDP_URL)

        if not browser.contexts:
            return []

        context = browser.contexts[0]

        page = None

        for candidate in context.pages:
            try:
                if config.FLOW_TOOLS_URL_MARKER in candidate.url:
                    page = candidate
                    break
            except Exception:
                continue

        if page is None:
            page = context.new_page()
            page.goto(config.FLOW_ORIGIN + config.FLOW_TOOLS_URL_MARKER)

        # A project page lists no siblings; go to the dashboard to enumerate.
        if config.FLOW_PROJECT_URL_MARKER in page.url:
            page.goto(config.FLOW_ORIGIN + config.FLOW_TOOLS_URL_MARKER)

        page.wait_for_timeout(2000)

        hrefs = page.eval_on_selector_all(
            f"a[href*='{config.FLOW_PROJECT_URL_MARKER}']",
            "els => els.map(e => e.getAttribute('href'))",
        )

        seen = []

        for href in hrefs:
            if not href:
                continue
            url = href if href.startswith("http") else config.FLOW_ORIGIN + href
            if url not in seen:
                seen.append(url)

        return seen

    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def create_new_project(timeout=150, poll=2):
    """Create a brand-new, empty Flow project and return its URL.

    Requires Chrome already running and signed in. Unlike normal startup
    (which only auto-creates when zero projects exist), this always creates
    one — it's for the explicit "start a new video" action, where the point
    is a guaranteed-empty project regardless of how many already exist.

    Clicks "New project" exactly once and waits, rather than retrying on
    timeout. Confirmed live (2026-08-15) that this click's completion time
    is genuinely inconsistent — anywhere from ~3s to well over a minute on
    the same account back to back — and, critically, that a "timed out"
    attempt sometimes turns out to have succeeded anyway once given more
    time. Retrying by clicking again risks creating a second project once
    the first one lands late; this project creates duplicate projects that
    way and is why this only ever clicks once.
    """

    from playwright.sync_api import sync_playwright

    chrome_launcher.ensure_chrome_running(config.CDP_URL)

    playwright = sync_playwright().start()

    try:
        browser = playwright.chromium.connect_over_cdp(config.CDP_URL)

        if not browser.contexts:
            raise RuntimeError("Chrome has no browser context")

        context = browser.contexts[0]
        page = context.new_page()

        page.goto(config.FLOW_ORIGIN + config.FLOW_TOOLS_URL_MARKER)
        page.wait_for_timeout(1500)

        buttons = page.locator("button")
        target = None

        for i in range(buttons.count()):
            try:
                if "New project" in buttons.nth(i).inner_text():
                    target = buttons.nth(i)
                    break
            except Exception:
                continue

        if target is None:
            raise RuntimeError(
                "Could not find a 'New project' button on the Flow landing page."
            )

        existing_pages = set(context.pages)
        target.click()

        start = time.time()

        while time.time() - start < timeout:

            try:
                if config.FLOW_PROJECT_URL_MARKER in page.url:
                    return page.url
            except Exception:
                pass

            for candidate in context.pages:
                if candidate in existing_pages:
                    continue
                try:
                    if config.FLOW_PROJECT_URL_MARKER in candidate.url:
                        return candidate.url
                except Exception:
                    continue

            time.sleep(poll)

        raise RuntimeError(
            f"Clicked 'New project' but nothing opened within {timeout}s. "
            f"This has been seen to still complete late — check the Chrome "
            f"window before trying again, since clicking a second time "
            f"could create two projects if the first one lands after all."
        )

    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def safe_session_name(name):
    """Turn a free-text session name into a filesystem-safe slug.

    Used to derive prompts/output paths that can't collide with an existing
    file through path separators or traversal (../), and can't produce an
    empty or dot-only name that would resolve to something unexpected.
    """

    import re

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-.")

    # Some filesystems cap individual filenames around 255 bytes; this
    # slug becomes part of both a filename and a directory name, so stay
    # well under that even before Windows' historical 260-char full-path
    # limit is considered.
    slug = slug[:60].strip("-")

    return slug or time.strftime("session-%Y%m%d-%H%M%S")


def run_all(prompts_file=None, include_login=True):

    checks = [
        check_dependencies(),
        check_chrome(),
        check_profile(),
    ]

    if include_login:
        checks.append(check_login())

    checks.extend([check_prompts(prompts_file), check_project()])

    blocking = [c for c in checks if c["status"] == STATUS_FAIL]

    return {
        "checks": checks,
        "ready": not blocking,
        "blocking": [c["id"] for c in blocking],
    }
