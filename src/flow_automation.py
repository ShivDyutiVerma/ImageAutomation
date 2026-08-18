import time

from playwright.sync_api import sync_playwright

import chrome_launcher
import config


class FlowSetupError(Exception):
    """Environment is not usable at all — retrying cannot help."""


class FlowGenerationError(Exception):
    """A single prompt failed — worth retrying."""


class FlowAutomation:

    def __init__(self, cdp_url=None):

        self.cdp_url = cdp_url or config.CDP_URL

        if config.AUTO_LAUNCH_CHROME:

            try:
                chrome_launcher.ensure_chrome_running(self.cdp_url)
            except chrome_launcher.ChromeLaunchError as e:
                raise FlowSetupError(
                    f"{e}\n"
                    f"You can also start it yourself:\n"
                    f"  {chrome_launcher.manual_launch_command(self.cdp_url)}"
                )

        self.playwright = sync_playwright().start()

        try:
            self.browser = self.playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            self.playwright.stop()
            raise FlowSetupError(
                f"Could not attach to Chrome at {self.cdp_url}.\n"
                f"Start Chrome with remote debugging and the Flow profile:\n"
                f"  {chrome_launcher.manual_launch_command(self.cdp_url)}\n"
                f"then open your Flow project tab.\n"
                f"Underlying error: {e}"
            )

        if not self.browser.contexts:
            self.close()
            raise FlowSetupError("Attached to Chrome but it has no browser context.")

        self.context = self.browser.contexts[0]

        self._wait_for_any_page_url()

        try:
            self.flow_page = self._find_flow_page()
        except Exception:
            self.close()
            raise

    def _wait_for_any_page_url(self, timeout=15):
        """Block until at least one open tab reports a non-empty URL.

        Right after Chrome auto-launches, CDP can be reachable and the
        context/page objects can already exist while the initial tab is
        still mid-navigation and reports an empty URL — that's "not loaded
        yet", not "no tab exists", and every downstream tab-scan needs to
        not mistake one for the other.
        """

        start = time.time()

        while time.time() - start < timeout:

            for page in self.context.pages:
                try:
                    if page.url:
                        return
                except Exception:
                    continue

            time.sleep(0.5)

    def _open_urls(self):

        urls = []

        for page in self.context.pages:
            try:
                urls.append(page.url[:100])
            except Exception:
                pass

        return urls

    def _go_to_configured_project(self):
        """Force navigation to FLOW_PROJECT_URL, even if some other project
        tab happens to already be open. An explicit configuration must win
        over whatever tab is sitting open — otherwise a stale tab from a
        previous run could silently redirect this run's images into the
        wrong project.
        """

        target = config.PROJECT_URL

        for page in self.context.pages:

            try:
                if page.url.rstrip("/") == target.rstrip("/"):
                    return page
            except Exception:
                continue

        for page in self.context.pages:

            try:
                url = page.url
            except Exception:
                continue

            if config.FLOW_TOOLS_URL_MARKER in url:
                page.goto(target)
                return self._wait_for_project(page)

        # No Flow tab at all (e.g. Chrome was already open on unrelated
        # pages). The target project is explicitly configured, so there's
        # nothing ambiguous to resolve — just open it in a new tab rather
        # than making the user do it by hand.
        try:
            page = self.context.new_page()
            page.goto(target)
            return self._wait_for_project(page)
        except Exception as e:
            raise FlowSetupError(
                f"Could not open {target} in a new tab: {e}\n"
                f"Currently open tabs: {self._open_urls() or 'none'}"
            )

    def _find_flow_page(self):
        """Locate a usable Flow project tab, navigating there automatically
        if only the Flow landing/dashboard page is open.

        Deliberately does not guess among several existing projects — mixing
        one video's images into the wrong project is a real cost, so ambiguity
        is surfaced as an error rather than resolved silently.
        """

        if config.PROJECT_URL:
            return self._go_to_configured_project()

        for page in self.context.pages:

            try:
                url = page.url
            except Exception:
                continue

            if config.FLOW_PROJECT_URL_MARKER in url:
                return page

        landing = None

        for page in self.context.pages:

            try:
                url = page.url
            except Exception:
                continue

            if config.FLOW_TOOLS_URL_MARKER in url:
                landing = page
                break

        if landing is None:

            open_urls = self._open_urls()

            if any("accounts.google.com" in url for url in open_urls):
                raise FlowSetupError(
                    "The Chrome profile isn't logged into Google (redirected "
                    "to a sign-in page). This can't be automated — Google "
                    "deliberately blocks scripted login. Run "
                    "tests/manual/flow_profile_setup.py to log in by hand "
                    "once; the session then persists for every future run."
                )

            raise FlowSetupError(
                "No Flow tab found. Open "
                f"{config.FLOW_ORIGIN}{config.FLOW_TOOLS_URL_MARKER} in the "
                f"Chrome window attached at {self.cdp_url}.\n"
                f"Currently open tabs: {open_urls or 'none'}"
            )

        projects = self._discover_projects(landing)

        if len(projects) == 1:
            landing.goto(config.FLOW_ORIGIN + projects[0])
            return self._wait_for_project(landing)

        if len(projects) > 1:
            listing = "\n".join(f"  {config.FLOW_ORIGIN}{p}" for p in projects)
            raise FlowSetupError(
                f"Found {len(projects)} existing Flow projects and won't guess "
                f"which one this run belongs to:\n{listing}\n"
                f"Set FLOW_PROJECT_URL in .env to the one you want, or open it "
                f"manually in the Chrome tab before running."
            )

        if config.AUTO_CREATE_PROJECT:
            return self._click_new_project(landing)

        raise FlowSetupError(
            "No existing Flow projects found. Either create one manually in "
            "the Chrome window, or set FLOW_AUTO_CREATE_PROJECT=true in .env "
            "to let the automation click 'New project' for you."
        )

    def _discover_projects(self, page):
        """Existing project links on the landing page, as relative hrefs."""

        try:
            hrefs = page.eval_on_selector_all(
                f"a[href*='{config.FLOW_PROJECT_URL_MARKER}']",
                "els => els.map(e => e.getAttribute('href'))",
            )
        except Exception as e:
            raise FlowSetupError(f"Could not read the Flow project list: {e}")

        seen = []

        for href in hrefs:
            if href and href not in seen:
                seen.append(href)

        return seen

    def _click_new_project(self, page):

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
            raise FlowSetupError(
                "Could not find a 'New project' button on the Flow landing page."
            )

        existing_pages = set(self.context.pages)

        target.click()

        return self._wait_for_project(page, existing_pages)

    def _wait_for_project(self, page, existing_pages=None, timeout=None):
        """Poll until a page's URL is a project URL, whether that's the same
        tab navigating or a new tab opening — 'New project' hasn't been
        exercised yet, so both are handled without assuming which it does.
        """

        timeout = timeout or config.NEW_PROJECT_TIMEOUT
        existing_pages = existing_pages or set()

        start = time.time()

        while True:

            try:
                if config.FLOW_PROJECT_URL_MARKER in page.url:
                    return page
            except Exception:
                pass

            for candidate in self.context.pages:

                if candidate in existing_pages:
                    continue

                try:
                    if config.FLOW_PROJECT_URL_MARKER in candidate.url:
                        return candidate
                except Exception:
                    continue

            if time.time() - start > timeout:
                raise FlowSetupError(
                    f"Timed out after {timeout}s waiting for a Flow project to open."
                )

            time.sleep(1)

    def close(self):

        try:
            self.playwright.stop()
        except Exception:
            pass

    def bring_to_front(self):

        self.flow_page.bring_to_front()

    def close_popups(self):
        """Dismiss any overlay (banner, settings panel) that could be
        covering the prompt box or Create button.

        Confirmed live (2026-08-14, docs/FLOW_UI_NOTES.md): Escape alone is
        not reliable here — a leftover open panel survived 15 straight
        Escape-backed attempts across a real run. The likely cause is that
        Chrome's OS-level window focus, not just the active tab, gates
        whether the page's keyboard handler even sees the key during an
        unattended run where the window sits in the background. A button
        click is dispatched directly at the element regardless of window
        focus, so it doesn't have that failure mode. Both are kept: Escape
        is cheap and handles simple cases, the button scan is what actually
        recovers from a stuck panel.
        """

        try:
            self.flow_page.keyboard.press("Escape")
            time.sleep(1)
        except Exception as e:
            print(f"  (could not send Escape: {e})")

        self._click_dismiss_buttons()

    def _click_dismiss_buttons(self):

        try:
            buttons = self.flow_page.locator("button")
            count = buttons.count()
        except Exception:
            return

        for i in range(count):

            try:
                text = buttons.nth(i).inner_text()
            except Exception:
                continue

            lines = [line.strip() for line in text.splitlines() if line.strip()]

            # Matches the Material Symbols "close" icon ligature as the first
            # line, e.g. "close\nClose" or "close\nDismiss" - both confirmed
            # live on real overlays (a settings panel, a promo banner).
            if lines and lines[0].lower() == "close":

                try:
                    buttons.nth(i).click(timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    continue

    def _create_button(self):
        """Find the actual submit control, not just any element mentioning
        "Create". Confirmed against the live UI (2026-08-14, see
        docs/FLOW_UI_NOTES.md): a single project page can have several
        unrelated matches — e.g. a "Create a few versions of an image"
        suggestion chip, and a second icon button with a different function —
        so matching on text alone is not reliable enough to click blindly.

        The real submit button pairs a Material Symbols "arrow_forward" (send)
        icon with the text "Create"; that combination is checked first. If
        the icon-name signal ever stops matching (Google changes the icon),
        this falls back to the rightmost element whose visible label is
        exactly "Create" — a plain descriptive sentence like the suggestion
        chip above never satisfies that exact match.
        """

        candidates = self.flow_page.locator("button", has_text="Create")

        count = candidates.count()

        fallback = None
        fallback_x = -1

        for i in range(count):

            button = candidates.nth(i)

            try:
                text = button.inner_text()
            except Exception:
                continue

            lines = [line.strip() for line in text.splitlines() if line.strip()]

            if not lines or lines[-1] != "Create":
                continue

            if "arrow_forward" in lines:
                return button

            try:
                box = button.bounding_box()
                x = box["x"] if box else -1
            except Exception:
                x = -1

            if x > fallback_x:
                fallback_x = x
                fallback = button

        return fallback

    def _page_ready(self):
        """Both the submit control and the prompt box must be present —
        confirmed live (2026-08-15) that a stale tab can show a Create-like
        button while the actual prompt editor is missing, which would
        otherwise pass this check and fail later inside fill_prompt().
        """

        if self._create_button() is None:
            return False

        if self.flow_page.locator('[contenteditable="true"]').count() == 0:
            return False

        return True

    def _project_missing(self):
        """The 'Something went wrong. Back to projects.' screen Flow shows
        when a project ID no longer exists — deleted or expired server-side
        — rather than any ordinary page-load hiccup. Checked ahead of
        _page_ready()'s poll loop so this fails in seconds instead of
        burning the full READY_TIMEOUT (and then MAX_ATTEMPTS retries, and
        then the consecutive-failure cooldown cycle) against a project that
        can never become ready no matter how long it waits.

        Confirmed live (2026-08-18): this exact text is what renders, and
        the page's own flow.projectInitialData tRPC call returns HTTP 400
        for a project in this state — that's Google's backend rejecting the
        project ID itself, not a rendering delay. See docs/FLOW_UI_NOTES.md.
        """

        try:
            text = self.flow_page.inner_text("body")
        except Exception:
            return False

        return "Something went wrong" in text and "Back to projects" in text

    def wait_until_ready(self, timeout=None):
        """Poll until the page is usable, reloading once partway through the
        timeout if it never becomes ready.

        Confirmed live (2026-08-15): a tab left open for hours (in this case,
        across the host machine going to sleep mid-run) can get stuck
        rendering Flow's marketing splash instead of the actual project
        editor, even though the URL still points at the right project and
        the session is still authenticated. A reload reliably recovers it —
        proven by reproducing the exact failure and confirming a reload
        fixed it before adding this. See docs/FLOW_UI_NOTES.md.
        """

        timeout = timeout or config.READY_TIMEOUT

        start = time.time()
        reloaded = False

        while True:

            if self._project_missing():
                raise FlowSetupError(
                    f"This Flow project no longer exists — {self.flow_page.url} "
                    f"shows Flow's own 'Something went wrong' screen, which "
                    f"means the project was deleted or expired server-side "
                    f"(confirmed: Flow's projectInitialData API returns HTTP "
                    f"400 for it). This can't be fixed by retrying. Pick a "
                    f"different project via Setup -> Choose project, or "
                    f"create a new one."
                )

            if self._page_ready():
                return

            elapsed = time.time() - start

            if not reloaded and elapsed > timeout / 2:

                try:
                    self.flow_page.reload()
                    time.sleep(2)
                except Exception:
                    pass

                reloaded = True

            if elapsed > timeout:
                raise FlowGenerationError(
                    f"Flow UI not ready after {timeout}s "
                    f"(no Create button or prompt editor found, even after a reload)."
                )

            time.sleep(1)

    def get_generated_urls(self):
        """Distinct generated-image URLs currently in the page, in one CDP call.

        Reading each <img> individually costs a round-trip per element, so with
        hundreds of images accumulated in a project that grows into hundreds of
        round-trips on every poll. Collecting them in a single page evaluation
        keeps polling cost flat no matter how far into a 300-image run we are.

        Confirmed live (2026-08-14, docs/FLOW_UI_NOTES.md): a single generation
        renders the same image in two places at once — the chat panel thumbnail
        and the main canvas — as two <img> tags sharing one src. Deduplicating
        here (not just at the call site) means every caller, including the
        before/after set-difference in wait_for_new_images, sees one entry per
        actual generation rather than one per DOM placement.
        """

        try:
            srcs = self.flow_page.eval_on_selector_all(
                "img",
                "els => els.map(e => e.getAttribute('src') || '')",
            )
        except Exception as e:
            raise FlowGenerationError(f"Could not read images from the page: {e}")

        matching = [src for src in srcs if config.IMAGE_URL_MARKER in src]

        return list(dict.fromkeys(matching))

    def fill_prompt(self, prompt):

        editor = self.flow_page.locator('[contenteditable="true"]')

        if editor.count() == 0:
            raise FlowGenerationError("Prompt editor not found on the page.")

        editor.first.click()

        self.flow_page.keyboard.press("Control+A")
        self.flow_page.keyboard.press("Backspace")
        self.flow_page.keyboard.type(prompt, delay=20)

    def click_create(self):

        button = self._create_button()

        if button is None:
            raise FlowGenerationError("Create button not found.")

        button.click()

    def wait_for_new_images(self, before, timeout=None):
        """Wait until image URLs appear that weren't present before the click.

        Comparing sets rather than watching a fixed position means this works
        whether Flow prepends, appends, or reorders results, and it reveals how
        many images a single generation produces.
        """

        timeout = timeout or config.GENERATION_TIMEOUT

        start = time.time()

        while True:

            current = self.get_generated_urls()

            new = [url for url in current if url not in before]

            if new:

                # Re-check after one interval so a transient DOM re-render
                # can't be mistaken for a finished generation.
                time.sleep(config.POLL_INTERVAL)

                confirmed = [
                    url for url in self.get_generated_urls() if url not in before
                ]

                if confirmed:
                    return confirmed

            if time.time() - start > timeout:
                raise FlowGenerationError(
                    f"No new image after {timeout}s. The generation may have "
                    f"failed, be unusually slow, or the account may be out of credits."
                )

            time.sleep(config.POLL_INTERVAL)

    def resolve_image_url(self, image_url):

        if image_url.startswith("http"):
            target = image_url
        else:
            target = config.FLOW_ORIGIN + image_url

        temp_page = self.context.new_page()

        try:
            temp_page.goto(target)
            return temp_page.url
        except Exception as e:
            raise FlowGenerationError(f"Could not resolve image URL: {e}")
        finally:
            try:
                temp_page.close()
            except Exception:
                pass

    def generate_image(self, prompt):

        self.bring_to_front()
        self.close_popups()
        self.wait_until_ready()

        before = set(self.get_generated_urls())

        self.fill_prompt(prompt)
        self.click_create()

        new_images = self.wait_for_new_images(before)

        if len(new_images) > 1:
            # Genuinely distinct generations from one click, not the known
            # same-image-in-two-places duplication (already filtered out in
            # get_generated_urls) — worth knowing about if it ever happens.
            print(f"  (Flow produced {len(new_images)} distinct images; using the first)")

        return self.resolve_image_url(new_images[0])
