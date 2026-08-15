# Flow UI notes

Findings from live inspection against the real Flow UI, not assumptions. Update this file whenever the automation is re-verified or a selector needs fixing — it's the fastest way to tell "Google changed the UI" from "our bug."

## 2026-08-14 — Project auto-navigation + Create button disambiguation

**Context**: Chrome was reachable over CDP but only had the Flow landing page open (`https://labs.google/fx/tools/flow`), not a project. The automation required a project tab to already be open; this session added automatic navigation into a project.

### Landing page structure (confirmed)

- Existing projects render as `<a href="/fx/tools/flow/project/<uuid>">` — no visible link text, so `eval_on_selector_all` extracting `href` is the right approach (not `inner_text`).
- The project list container has `data-testid="virtuoso-item-list"` — it's built on **react-virtuoso**, a virtualizing list library. This is framework-level evidence that Flow's frontend uses virtualization elsewhere, which raises the likelihood (not yet direct confirmation) that the in-project generated-image gallery also virtualizes. **Still the single most important unresolved unknown** — see "Still open" below.
- "New project" button: `<button>` containing icon ligature `add_2` and text "New project".

### Create button — real ambiguity found and fixed

A project page can have **three** different elements whose text contains "Create":
1. A suggestion chip, e.g. "Create a few versions of an image" — part of an empty-state prompt-idea list (`parentText` showed sibling chips like "Develop a storyboard", "Rename my assets"). Large element (~246×80px). **Not a submit control.**
2. `add_2\nCreate` — a small (32×32) icon button near the left edge of the prompt input row. Function not confirmed; **not the submit button** (ruled out by position/grouping, see below).
3. `arrow_forward\nCreate` — a small (32×32) icon button grouped in the same toolbar as `article_spark\nAgent Instructions` and `tune\nSettings`, at the right end of that group. This **is** the real submit button — `arrow_forward` is Google's standard Material Symbols "send" icon.

The old code (`get_by_role("button", name="Create").first`) or a naive text scan would pick whichever of these three comes first in DOM order — no guarantee it's the submit button. Fixed in `flow_automation.py`'s `_create_button()`:
- Only considers buttons whose **last visible line is exactly** `"Create"` (excludes the suggestion chip, which is a full sentence).
- Prefers a match that also contains the line `"arrow_forward"` (the confirmed submit icon) — unambiguous when present.
- Falls back to the rightmost remaining candidate by x-position if the icon signal ever stops matching.

Verified against all 3 real projects in the account via `wait_until_ready()` (the actual production path): resolved correctly in each.

### Other confirmations

- `[contenteditable="true"]` — still exactly 1 match per project. Selector unchanged and correct.
- `IMAGE_URL_MARKER = "media.getMediaUrlRedirect"` — **confirmed correct**, not just inherited. One of the 3 real projects had 3 real generated images and all 3 matched this marker.
- DOM elements (notably the Create button) are **not guaranteed present immediately after navigation** — one project took longer to render its toolbar. Calling `_create_button()` directly right after navigating found nothing; `wait_until_ready()`'s polling loop handled it correctly once given a couple seconds. This is exactly what that method is for — no code change needed, just confirms the existing design is doing its job.
- A `reCAPTCHA enterprise` iframe/worker was present in the browser context during testing (`google.com/recaptcha/enterprise/...`). Not confirmed whether this is a standard invisible badge Google embeds site-wide or something that can surface a visible challenge under automated interaction patterns. **Watch for this during Phase 3's real generation test** — if a visible CAPTCHA ever appears, the run needs to pause for the user, not retry blindly.

### Bug found *because* of this live testing (not from the UI itself)

`FLOW_PROJECT_URL` was being silently ignored whenever any tab happened to already be on a different project's URL — the old `_find_flow_page()` returned the first already-open project tab unconditionally, before ever checking config. Caught when testing against project A then B and observing both "navigations" land on project A. Fixed: when `FLOW_PROJECT_URL` is set, it now always wins, navigating away from whatever's currently open if needed (`_go_to_configured_project`).

## 2026-08-14 (same day) — Real generation test: 3 live images, 3 credits spent

Live testing that spends credits was done in a **new, dedicated project**, never in one already holding real work — existing projects were only ever inspected read-only.

### What was run

1. `AUTO_CREATE_PROJECT`'s underlying click ("New project") exercised for real via a standalone script — confirmed it navigates the *same tab* to a new project URL (doesn't open a new tab). `_wait_for_project`'s dual-path handling (same-tab nav or new-tab) was correct to build defensively; same-tab is what actually happens.
2. `FlowAutomation.generate_image()` run directly against the new project: prompt "a simple red apple on a plain white background, product photography" → **succeeded in 52.5s**, produced a real, correct image.
3. The full **`main.py` orchestrator** run for real (not just the automation class) against the same project with 2 more prompts (bicycle, coffee cup) via env-var config pointing at scratch prompts/output/manifest files. Both succeeded; manifest, sequential naming, and the run summary all came out correct. ~53s/image, consistent with the single-prompt test.

All 3 images were visually verified — correct content matching their prompts, no mixups.

### Real bugs found and fixed by this testing

1. **Duplicate URL, not duplicate image.** `get_generated_urls()` returned the same media ID twice (one `<img>` in the chat panel, one in the main canvas) — logged as "Flow returned 2 images," which was factually wrong. Confirmed by inspecting both matched `src` values: identical `name=` parameter. **Fixed**: `get_generated_urls()` now dedupes (`dict.fromkeys`), so every caller — including `wait_for_new_images`' set-difference logic — sees one entry per actual generation, not one per DOM placement.
2. **Wrong file extension — a real, meaningful bug.** Flow's CDN (`flow-content.google`) returns **JPEG** (confirmed via magic bytes `FF D8 FF E0 ... JFIF`), but the pipeline unconditionally named every file `NNN.png`. **Fixed**: `downloader.py` now detects the real format from the HTTP `Content-Type` header, with a magic-byte sniff fallback, and picks the extension accordingly (`.jpg` confirmed in practice). `download_image()`'s interface changed from `(url, filename)` to `(url, stem)` — the stem (e.g. `"047"`) is the permanent identity; the extension is derived from content, never assumed. `main.py` and the "unclaimed files" pre-existing-image check were updated to match (glob by stem, not by a hardcoded `.png` suffix).

Both bugs were caught only because real output was inspected byte-for-byte rather than trusted at a glance — the earlier simulation suite couldn't have caught either, since it fakes both the browser and the download.

### Answered: previously "still open" unknowns

1. **One Create click → one image**, not several. The "x2 quantity" seen in Agent settings (below) does not apply to the direct prompt-box flow this automation uses. Confirmed across 3 separate real generations — always exactly 1 distinct image.
2. **No interstitials encountered** across 3 generations plus the earlier project-creation and navigation testing. A reCAPTCHA Enterprise element was present in the browser context throughout, but never surfaced a visible, blocking challenge. Not conclusively ruled out for longer sessions, but no evidence of a problem at small scale.
3. **DOM virtualization of the in-project gallery**: still not directly confirmed or ruled out — 3 images in the test project is nowhere near a plausible virtualization threshold (typically ~20–50+ items). This remains the one meaningfully open risk for the 200–300-image workload; see below.

### New feature discovery: "Agent" vs. direct generation are two different paths

The toolbar button pair `article_spark "Agent Instructions"` + `tune "Settings"` opens a panel titled **"Agent settings"**, not a generic project/generation settings panel. It contains:
- **"Confirm before generating": Always / Never** — gates an autonomous "Agent" that can decide on its own to generate media (e.g., in response to a vague high-level request) and would ask before spending credits, unless set to "Never."
- **Image generation defaults**: aspect ratio (16:9/4:3/1:1/3:4/9:16, 16:9 selected), **quantity x1–x4 (x2 selected)**, model ("Nano Banana 2").
- **Video generation defaults**: separate aspect ratio/quantity/model settings (Flow also does video — out of scope here).

This automation drives the **direct** path — filling the prompt box and clicking the explicit `arrow_forward`/"Create" submit button — not the conversational Agent. That's why no confirmation dialog was ever encountered and why quantity stayed at 1 despite the Agent default showing x2: those settings belong to the Agent's own autonomous behavior, not the direct submit action. This is good for automation purposes: direct submission is predictable, synchronous, and one request in gives one result out, with no autonomous decision-making in the way.

**Worth double-checking independently**: if the account-wide default of x2 ever turns out to apply more broadly than observed here, that would mean paying for 2 image generations per prompt while only using 1 — a real cost concern at 300 images. Nothing in 3 real tests suggested this, but it was only 3 data points.

### Other visual confirmations

- Main gallery renders newest-first, top to bottom (matches DOM order in the cases checked) — consistent with, though not required by, the set-difference detection design.
- A project auto-titles itself from the first prompt used in it (e.g. "Red Apple Product Phot...") — cosmetic, no action needed.
- Per-image hover controls exist (favorite, a regenerate/variation icon, more-options menu) — not used by this automation, noted for awareness only.

## 2026-08-14 (same day) — 10-image real batch: full success, one real reliability bug fixed, one urgent operational finding

User authorized a 10-image real batch in the same dedicated test project, specifically to exercise download correctness and sequential naming at slightly larger scale.

### First attempt: aborted by the circuit breaker (correctly)

All 10 prompts were queued; the run aborted after 5 consecutive failures, all `"Flow UI not ready after 60s (no Create button found)"`. Root cause: the "Agent settings" panel from the earlier UI-exploration session (see above) had been left open and was covering the prompt box and Create button. **This was leftover state from manual testing, not a Flow or automation defect** — but it exposed a real gap: `close_popups()` only pressed Escape, and Escape did not reliably dismiss the panel across all 15 retry attempts (3 attempts × 5 prompts, 18 minutes). 0 images produced, but 0 harm either — the circuit breaker did exactly its job (stopping fast instead of grinding through all 10 with full retries against a state that could never succeed) and the manifest cleanly recorded 5 failures with 5 still pending, ready to resume.

**Fixed**: `close_popups()` now also scans for and clicks any button whose icon-ligature is exactly `"close"` (matches both a settings-panel close button and an unrelated promo-banner dismiss button, both confirmed live), in addition to Escape. Deliberately did **not** also match `"arrow_back"`-labeled buttons — the same icon is used both by safe panel-internal "Back" controls and by the page-level "Go Back" navigation button, and misclicking the latter mid-run would navigate away from the project entirely. That ambiguity was judged not worth the risk for what is, realistically, a rare scenario (production code never opens Settings itself; this only happens if a human leaves an overlay open during a live run).

Likely explanation for why Escape alone was unreliable: Chrome's OS-level window focus, not just which tab is active, may gate whether a page's Escape handler even fires — plausible for a long unattended run where the browser window sits in the background while the user does other things. Button clicks are dispatched directly at the element via CDP and don't have that dependency.

### Second attempt: 10/10 succeeded

Same project, same prompts, after manually clearing the stuck panel. All 10 generated and downloaded successfully, ~9m22s total (~56s/image, consistent with earlier single-image timing). Verified thoroughly, not just trusted:
- All 10 files present as `001.jpg`–`010.jpg`, exactly matching expected sequence, no gaps, no extras.
- Every file's magic bytes confirmed valid JPEG.
- 4-image visual spot-check (001, 003, 007, 010) confirmed correct content matching each prompt.
- Manifest fields (status, file, prompt, timestamps) all correct.

### Urgent operational finding: a second, undocumented Chrome profile directory

While cleaning up after the batch, `git status` revealed an untracked `flow/` directory (324MB) at the repo root — **not** the documented `flow_profile/`. Its contents (`flow/_profile/Default/...`) are a structurally identical, fully authenticated Chrome profile, and its file timestamps show **far more recent activity than `flow_profile/` (1143 vs 204 files touched during this session)** — meaning the Chrome window actually used for all of today's live testing was very likely running against `flow/_profile/`, not the `flow_profile/` this project's docs and `.gitignore` have exclusively referenced.

**This directory was NOT covered by `.gitignore`** — a `git add -A` at any point today would have staged a live, authenticated Google session for commit. Fixed immediately: added `flow/` to `.gitignore` alongside `flow_profile/`.

**Resolved**: the authoritative path is `flow/_profile` — a `flow` folder containing a `_profile` subfolder, matching the recent-activity evidence above. All docs and code now reference it consistently. Worth remembering as a bug shape: two launch mechanisms quietly writing to two *different* directories that look like the same one is very hard to spot, because everything works right up until the wrong one is used.

## 2026-08-15 — Chrome auto-launch, and a real machine-sleep interruption

Chrome auto-launch was added to remove the manual `chrome.exe ...` step. Automating the login itself was considered and rejected. Login was deliberately **not** automated — see `docs/ARCHITECTURE.md` §7 for why (credential storage risk, Google's own anti-automation detection, and it being unnecessary since the profile is already authenticated from the one-time manual setup). Built `src/chrome_launcher.py`: checks if CDP is already reachable, and if not, launches Chrome pointed at the existing authenticated profile with no credentials involved at all.

### Real bugs found via live testing, both fixed

1. **CDP-reachable ≠ tab-loaded.** Right after Chrome auto-launches, the debugging port can respond before the initial tab has actually navigated anywhere — its URL reads as empty for a second or two. The first live test failed immediately because tab-scanning logic treated an empty URL as "no tab exists." Fixed: `FlowAutomation.__init__` now waits (bounded, 15s) for at least one tab to report a non-empty URL before any scanning logic runs.

2. **A tab left open for hours can get stuck on Flow's marketing splash instead of the real project editor**, even with the correct URL and a still-valid session. Reproduced directly: after the machine-sleep interruption below, the automated run failed 3/3 with "Prompt editor not found," and inspecting the live page showed the "Create with Google Flow" marketing splash rendered at the project URL — not a login page, not an error, just stuck client-side state. **Confirmed a plain `page.reload()` fixes it** before writing any recovery code. Fixed properly: `wait_until_ready()` now checks for both the Create button *and* the prompt editor (the old check only verified the button, which could apparently be satisfied by something else on the stuck page), and reloads once partway through its timeout if the page never becomes ready.

### A real production-relevant finding: machine sleep during a run

During the first live BEAT-format test, the host machine went to sleep mid-run. Evidence: beat 10 started at 19:27:24 and didn't finish until 22:06:04 — a genuine ~2h39m gap on a single prompt's 3 retry attempts (each of which should take well under a minute to fail). The eventual error, "Target page, context or browser has been closed," is consistent with a CDP WebSocket connection freezing (not erroring) for the duration of the suspension, then finally surfacing as closed on wake.

**The resilience design held up correctly under this real, unplanned interruption**: no crash, no misnumbered files, all 3 beats cleanly recorded as `failed` with clear error messages, and `--only 8-10` correctly resumed just those three once the machine was awake again.

**Operationally significant for real 200-300 image runs**, which can take 3-6 hours: if the machine sleeps at any point, the run will stall exactly like this. Recommended: disable sleep/screen-lock (e.g. via `powercfg`) for the duration of a real batch. Not yet built: automatic detection/recovery from a multi-hour freeze (the reload-based recovery above handles a *stuck page*, not a *fully suspended OS*) — worth revisiting if it becomes a recurring problem in real use.

### Confirmed working end-to-end after both fixes

Full unattended run from a cold start: Chrome fully closed → `python src/main.py` → auto-launched Chrome against the already-authenticated profile → auto-navigated to the configured project → parsed a real BEAT-block prompts file (BEAT 8-10) → generated and downloaded all 3 images correctly named `008.jpg`/`009.jpg`/`010.jpg` → visually confirmed each matches its prompt exactly. Zero manual browser interaction at any point.

## Still open

1. **Does the in-project image gallery virtualize at scale?** Still unresolved — needs either a much larger real batch or a naturally large existing project to observe. 13 images now exist in the test project (3 + 10) — still nowhere near a plausible ~20-50+ virtualization threshold. This remains the single most important unknown for the 200–300-image workload: if the results area unmounts offscreen images, the before/after URL-set comparison in `wait_for_new_images` could need to scope its scan to a specific container instead of the whole page.
2. **Long-session interstitials** (quota warnings, re-auth, a CAPTCHA turning visible) — not encountered across ~13 minutes of real generation activity; unconfirmed over the multi-hour span a real 200–300 image run would take.
3. **Actual per-generation credit cost** — not measured with a before/after balance check. Recommended before a large real run: check the account's credit balance, generate a known number of images, check again.
4. ~~Which Chrome profile directory is authoritative~~ — resolved, see above (`flow/_profile`).
