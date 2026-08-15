"""Read-only inspection of a live Flow project tab.

Generates NOTHING and spends NO credits. It only reads the DOM of a project
you already have open, to answer the questions the automation depends on:

  1. Is the project tab findable, and are the selectors still correct?
  2. How many generated images does the page expose, and in what order?
  3. Does the page virtualize (unmount offscreen images)? This matters because
     new-image detection compares URL sets before and after a generation — if
     images silently disappear and reappear on scroll, that comparison needs a
     different anchor.
  4. How expensive is a full image scan? (It runs every poll, for hours.)

Usage:
  1. Start Chrome:
       chrome.exe --remote-debugging-port=9222 --user-data-dir="<repo>/flow/_profile"
  2. Open your Flow project tab.
  3. python tests/manual/inspect_flow_dom.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import config  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def section(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():

    with sync_playwright() as p:

        section("CONNECTION")

        try:
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
        except Exception as e:
            print(f"FAILED to attach at {config.CDP_URL}")
            print(f"  {e}")
            print("\nStart Chrome with --remote-debugging-port=9222 first.")
            return 1

        print(f"Attached to {config.CDP_URL}")

        if not browser.contexts:
            print("No browser contexts.")
            return 1

        context = browser.contexts[0]

        section("OPEN TABS")

        flow_page = None

        for page in context.pages:
            try:
                url = page.url
            except Exception:
                continue

            marker = ""
            if config.FLOW_PROJECT_URL_MARKER in url:
                marker = "  <-- FLOW PROJECT"
                if flow_page is None:
                    flow_page = page

            print(f"  {url[:100]}{marker}")

        if flow_page is None:
            print(
                f"\nNo tab matched '{config.FLOW_PROJECT_URL_MARKER}'. "
                "Open your Flow project."
            )
            return 1

        section("CONTROLS")

        by_role = flow_page.get_by_role("button", name="Create")
        print(f"  get_by_role('button', name='Create') -> {by_role.count()} match(es)")

        buttons = flow_page.locator("button")
        total_buttons = buttons.count()
        text_matches = []

        for i in range(total_buttons):
            try:
                text = buttons.nth(i).inner_text().strip()
            except Exception:
                continue
            if "Create" in text:
                text_matches.append((i, text[:40]))

        print(f"  buttons on page: {total_buttons}")
        print(f"  buttons containing 'Create': {text_matches or 'NONE'}")

        editors = flow_page.locator('[contenteditable="true"]')
        print(f"  contenteditable elements: {editors.count()}")

        section("IMAGES")

        start = time.time()
        srcs = flow_page.eval_on_selector_all(
            "img", "els => els.map(e => e.getAttribute('src') || '')"
        )
        fast_elapsed = time.time() - start

        matching = [s for s in srcs if config.IMAGE_URL_MARKER in s]

        print(f"  total <img> elements     : {len(srcs)}")
        print(f"  matching '{config.IMAGE_URL_MARKER}': {len(matching)}")
        print(f"  single-eval scan took    : {fast_elapsed:.3f}s")

        if not matching:
            print(
                "\n  WARNING: no images matched the marker. Either this project "
                "has no generations yet, or FLOW_IMAGE_URL_MARKER is outdated."
            )
            print("  First few raw srcs:")
            for s in srcs[:5]:
                print(f"    {s[:110]}")

        for i, src in enumerate(matching[:3]):
            print(f"    [{i}] {src[:110]}")

        if len(matching) > 3:
            print(f"    ... and {len(matching) - 3} more")

        section("PER-ELEMENT SCAN COST (the old approach)")

        sample = min(len(srcs), 25)
        start = time.time()
        images = flow_page.locator("img")
        for i in range(sample):
            try:
                images.nth(i).get_attribute("src")
            except Exception:
                pass
        slow_elapsed = time.time() - start

        print(f"  reading {sample} images one-by-one: {slow_elapsed:.3f}s")

        if sample:
            per = slow_elapsed / sample
            print(f"  ~{per:.3f}s per image -> {per * 300:.1f}s for 300 images, per poll")
            print(f"  single-eval equivalent: {fast_elapsed:.3f}s regardless of count")

        section("VIRTUALIZATION CHECK")

        print("  Scrolling to top, then bottom, counting images at each step.")
        print("  If counts change, the page unmounts offscreen images and the")
        print("  before/after URL comparison needs a container-scoped anchor.\n")

        def count_now():
            found = flow_page.eval_on_selector_all(
                "img", "els => els.map(e => e.getAttribute('src') || '')"
            )
            return len([s for s in found if config.IMAGE_URL_MARKER in s])

        baseline = count_now()

        try:
            flow_page.keyboard.press("Home")
            time.sleep(1.5)
            at_top = count_now()

            flow_page.keyboard.press("End")
            time.sleep(1.5)
            at_bottom = count_now()
        except Exception as e:
            print(f"  Could not scroll: {e}")
            at_top = at_bottom = baseline

        print(f"  baseline: {baseline}   after Home: {at_top}   after End: {at_bottom}")

        if len({baseline, at_top, at_bottom}) > 1:
            print("\n  >>> VIRTUALIZED. Record this in docs/FLOW_UI_NOTES.md —")
            print("  >>> set-difference detection needs revisiting (Phase 3).")
        else:
            print("\n  >>> Stable count. Set-difference detection is safe.")

        section("DONE")
        print("Record findings in docs/FLOW_UI_NOTES.md.")
        print("Nothing was generated; no credits were spent.")

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
