# Architecture — YoutubeImageAutomation

Status: reflects code as of 2026-08-14. Update as each phase lands so this never drifts from reality.

## 1. Why this shape

Flow's Pro web tier has no API, and its login is **deliberately never automated** (see §7). So the system splits into a one-time manual authentication step and a repeatable automated run that borrows the resulting session:

1. **One-time** — `tests/manual/flow_profile_setup.py` opens a real Chrome via `launch_persistent_context(user_data_dir="flow/_profile")`; the user logs in by hand. `flow/_profile/` now holds an authenticated Chrome profile.
2. **Every run** — `src/chrome_launcher.py` checks whether Chrome is already reachable over CDP; if not, it launches Chrome itself against that same already-authenticated profile (no credentials involved — see §7). The tool then `connect_over_cdp`s in, exactly as if the user had launched it by hand.

Attaching to a profile-backed browser (rather than a fresh incognito/temp one) keeps the session indistinguishable from normal use and avoids ever re-authenticating. Auto-launch (added 2026-08-15, `AUTO_LAUNCH_CHROME` in config, on by default) removes the one remaining manual step from every *run*, without touching the one manual step that must stay manual — login.

## 2. Data flow

```
prompts/prompts.txt ──> prompt_loader ──┐
                                        │
output/manifest.json ──> manifest ──────┼──> main (orchestrator)
                                        │        │
                                        │        │ for each PENDING index only
                                        │        ▼
                                        │   flow_automation ──> Chrome/CDP ──> Flow UI
                                        │        │                  (type, Create, wait)
                                        │        │ resolved image URL
                                        │        ▼
                                        │    downloader ──> output/047.png  (atomic)
                                        │        │
                                        └────────┘ record result, save manifest atomically
```

The manifest is both the **resume ledger** and the **run report**. It is read at startup to decide what to skip, and written after every single prompt so that a crash at prompt 250 loses at most one prompt's work.

## 3. Module responsibilities

| Module | Responsibility |
|---|---|
| `src/config.py` | Single source of every tunable (CDP URL, paths, timeouts, retries, limits). Reads `.env`, falls back to defaults. Nothing else hardcodes literals. |
| `src/prompt_loader.py` | Reads `prompts.txt` → list of `(index, text, narration)` triples. Auto-detects two formats: **BEAT-block** (`BEAT N` / `"narration"` / prompt paragraph — index is the beat number itself, narration carried through for reference but never sent to Flow) and **plain** (one prompt per line, index is 1-based position among non-blank lines, narration always `None`). Rejects duplicate beat numbers before anything is generated; reports (non-fatally) gaps between the lowest and highest beat number present. |
| `src/manifest.py` | Load/save the run ledger atomically; reconcile against current prompts (hash-based change detection, keyed on prompt text only — narration changes alone don't trigger regeneration); carries narration through as reference metadata; expose pending indices and summary stats. |
| `src/flow_automation.py` | All Playwright/CDP interaction with the Flow UI: find the project tab, wait for readiness, type prompt, click Create, detect the newly generated image, resolve its final URL. |
| `src/downloader.py` | Fetch an image URL to `output/NNN.png` atomically, with HTTP-level retry. |
| `src/main.py` | `run_batch()` holds all orchestration — resume, per-prompt retry, fault isolation, circuit breaker — and is UI-agnostic, emitting structured events rather than printing. `main()` is a thin CLI front end that renders those events. |
| `src/chrome_launcher.py` | Starts Chrome against the already-authenticated profile if it isn't already reachable over CDP. Never handles credentials (§7). |
| `src/web_ui.py` + `src/web/index.html` | Local web front end (stdlib HTTP server, loopback-only). Calls the same `run_batch()` in a background thread, so front ends can't diverge on the rules that matter. Enforces one run at a time. |

## 4. Design decisions that exist because of the 200–300 image scale

### 4.1 Numbering is derived, never counted

`enumerate(prompts, start=1)` over *all* prompts gives each prompt a permanent index. That index produces the filename (`f"{index:03d}.png"`) and the manifest key. Skipped and failed prompts do not shift anything, because nothing increments on success. This is the mechanism enforcing the sequence-integrity contract in `DEVELOPMENT.md`.

### 4.2 New-image detection is set-difference, not position

The original code assumed the newest image is `urls[0]` and watched for that slot to change. That assumption is unverified against the live UI and breaks outright if Flow appends rather than prepends.

Instead: snapshot the set of image URLs before clicking Create, then poll until *new* URLs appear, and take the difference. This is order-independent — correct whether Flow prepends, appends, or reorders — and it also reveals whether Flow emits several images per click (the difference set size).

### 4.3 DOM reads are one round-trip, not N

`locator("img")` followed by a per-element `.get_attribute("src")` loop costs one CDP round-trip *per image*. With 300 images accumulated in a project, polling every 2s meant hundreds of round-trips per poll — cost growing linearly through the run.

Replaced with a single `eval_on_selector_all` that returns all `src` values in one call, filtered in Python. Constant round-trip cost regardless of project size.

### 4.4 Circuit breaker

An expired session or exhausted credits makes *every* remaining prompt fail identically. Without a breaker, 250 remaining prompts × 3 retries × a 180s timeout is over 100 hours of pointless waiting. After N consecutive failures the run aborts and reports the likely cause. Consecutive-failure count resets on any success.

### 4.5 Atomic writes everywhere

Both the manifest and each image are written to a temporary file in the destination directory and then `os.replace`d into place — an atomic rename on Windows and POSIX alike. Without this, a crash mid-write leaves a truncated `047.png` that resume would count as complete, silently putting a corrupt image into the video edit.

### 4.6 Prompt-change detection via hashing

Each manifest entry stores a hash of its prompt text. On startup, hashes are recompared:

- unchanged + success → skip
- changed → reset to pending and regenerate (the user rewrote that beat)
- new index → pending
- index no longer in the file → retained but reported as stale

Without this, editing line 47 and resuming would leave the *old* image in place while the manifest claimed success.

### 4.7 No concurrency

Rejected deliberately (see PRD non-goals): Flow is one interactive session in one tab; parallel generation risks both sequence corruption and account flags.

## 5. Failure taxonomy

| Failure | Detection | Response |
|---|---|---|
| Chrome not running / CDP refused | `connect_over_cdp` raises | Abort immediately with setup instructions — nothing is recoverable |
| No Flow project tab open | No tab URL matches `/flow/project/` | Abort immediately with instructions |
| Flow UI not ready (no Create button) | `wait_until_ready` timeout | Fail the prompt, retry |
| Generation exceeds timeout | No new image URL within the window | Fail the prompt, retry |
| Download fails (non-200, network) | HTTP status / exception | Retry at HTTP layer, then fail the prompt |
| Session expired / out of credits | Many consecutive prompt failures | Circuit breaker aborts the run |
| Selector broke (UI change) | Same as above, plus a suspiciously fast universal failure | Circuit breaker; then re-derive selectors via the inspection script |

## 6. Known unknowns (resolve with `tests/manual/inspect_flow_dom.py` — read-only, spends no credits)

1. Does one Create click produce one image or several? (set-difference detection tolerates both; confirming lets us handle the extras deliberately)
2. Can one Flow project hold 300 images, or does the UI paginate/virtualize? **If the DOM virtualizes and unmounts offscreen images, the before/after URL set comparison could produce false positives — this is the most important unknown to verify.**
3. Is the `media.getMediaUrlRedirect` src pattern still current?
4. Does a long session surface interstitials (quota warnings, re-auth prompts) that need dismissing?

## 7. Security

- `flow/_profile/` is a live authenticated Google session — treat as a credential. Never commit, never print, never share.
- `.env` is gitignored. It currently holds a `GEMINI_API_KEY` that no code reads (leftover from a removed approach); harmless but should be cleaned up eventually.
- Generation costs real money — never run generation to "test" code.
- **Google login is never automated, deliberately (decided 2026-08-15).** `chrome_launcher.py` only ever *starts a process* against an already-authenticated profile — it never touches a username or password. Reasons this is a hard line, not a preference:
  - Storing a Google password anywhere in this project (even "encrypted") means leaking this codebase leaks the whole account, not just a Flow session — categorically worse than the browser-profile risk above, which a revoke/re-login can contain.
  - Google actively detects and blocks scripted credential entry on its sign-in form — that's precisely the mechanism behind "this browser may not be secure" blocks, and triggering it risks the account being flagged or locked.
  - It's unnecessary: the one-time manual login (`tests/manual/flow_profile_setup.py`) persists indefinitely in the profile directory, which is *why* every real run since has needed zero manual login.
  - If the session ever does expire (rare — hasn't happened once in this project's real usage so far), `flow_automation.py` detects a redirect to `accounts.google.com` and fails with a clear message pointing at re-running the manual setup script, rather than trying to route around it.
