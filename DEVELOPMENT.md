# Development notes

Orientation for anyone working on this codebase. Read it before making changes.

## What this project is

YoutubeImageAutomation batch-generates AI images for YouTube videos using Google Labs' **Flow** (labs.google/fx/tools/flow) on a **Pro account that has no API access**. Because there is no API for the web Pro tier, the only way to automate it is to drive the real web UI in a real, logged-in Chrome session. That is what this project does: Playwright attaches over the Chrome DevTools Protocol (CDP) to an already-running, already-authenticated Chrome, types each prompt into Flow, clicks Create, waits for the new image, resolves its URL, and downloads it.

## The actual production workload — read this before making any design decision

- **200–300 images per video.** Not a handful. Every design decision must hold at that scale.
- **A run takes hours.** At roughly 30–60s per generation plus delays, 300 images is a 3–6 hour unattended run. It *will* be interrupted — network blips, session expiry, machine sleep, Chrome crashes, Ctrl+C. Surviving interruption is a core feature, not a nicety.
- **Images map 1:1 to a video script.** The full pipeline is: write the script → divide it into beats → write one image prompt per beat → compile into `prompts.txt`. Only the last step — turning a finished prompts file into a numbered image set — is this tool's job. Script/prompt authoring is deliberately out of scope (see `docs/PRD.md` §4 and §7 Non-goals) — not because it can't be automated, but because judging script/prompt quality is a real design problem of its own, deferred for later.

### Sequence integrity is sacred

The output filename number **is** the script beat number:

```
prompts.txt line 47  →  047.jpg  →  script beat 47
```

This invariant must never break. Concretely:

- **Gaps are acceptable and must be loud.** If prompt 47 fails, `047.jpg` must NOT exist, and the failure must be clearly reported so it can be regenerated on its own.
- **Shifts are catastrophic and must be impossible.** If prompt 47 fails, prompt 48 still becomes `048.jpg` — never `047.jpg`. Numbering comes from the prompt's line index, never from a running counter of successes. (The extension is derived from what Flow actually serves — confirmed JPEG in practice — never assumed; see `docs/FLOW_UI_NOTES.md`.)
- A silently misnumbered image corrupts the entire video edit and is worse than a missing one, because it goes unnoticed until the edit is done. Never trade correctness of numbering for convenience.

## Start here

1. `docs/PROGRESS.md` if present — current status, what's done, what's next. It's a local working note and is gitignored, so a fresh clone won't have one.
2. `docs/BUILD_PLAN.md` for phase goals, tasks, files, and acceptance criteria.
3. `docs/ARCHITECTURE.md` before touching `src/`.
4. `docs/PRD.md` has the why.

## Hard constraints — do not violate

- **Never commit `flow/_profile/`.** It's a real Chrome user-data-dir with a live, logged-in Google session — treat it as a credential. It's gitignored; if it ever appears staged, unstage it.
- **Never delete `flow/_profile/`** without explicit confirmation — it's the only thing preventing a manual re-login.
- **Never renumber outputs.** See "Sequence integrity" above.
- **Generating images costs real Pro credits.** Never kick off generation runs to "test" something. Verify against the DOM read-only (see `tests/manual/inspect_flow_dom.py`), or against fixtures. If a live generation test is genuinely needed, keep it to 1–2 prompts.
- `output/` and `flow/_profile/` are runtime artifacts, never committed.
- This scrapes a live Google product's DOM. There is no stability contract. Selectors can break silently when Google ships UI changes — treat an automation failure as "the UI may have changed," not only "our bug."

## How to run it

1. **First time on a machine:** `python src/setup.py` — checks dependencies, Chrome (auto-detected on Windows/macOS/Linux), the browser profile, Google sign-in, the prompts file, and which Flow project to use; walks through anything missing. `--check` reports without changing anything. Signing into Google is the one manual step — see below.
2. `python src/web_ui.py` → `http://127.0.0.1:8765` for the web UI, or `python src/main.py` for the CLI. Both drive the same `run_batch()`. Chrome launches itself against the already-authenticated profile if it isn't already running (`chrome_launcher.py`, `AUTO_LAUNCH_CHROME=true` by default), navigates to a project on its own, reads `prompts/prompts.txt`, writes `output/NNN.jpg`, tracks state in `output/manifest.json`.
3. Interrupted? Re-run the identical command. Completed images are skipped automatically.

**Google login is never automated — this is a hard line, not a missing feature.** `chrome_launcher.py` only ever starts a Chrome process against a profile that's already logged in; it never touches a username or password. Storing a Google password in this codebase would mean any leak of the repo leaks the whole account, and Google actively detects and blocks scripted credential entry anyway. If the session ever does expire, the automation detects the redirect to `accounts.google.com` and fails with a message pointing at step 1 above — never attempt to route around this.

## Conventions

- No comments unless they explain a non-obvious *why* (a timing constraint, a Flow UI quirk).
- No new hardcoded literals — config belongs in `src/config.py` / `.env`.
- Anything touching numbering, resume, or the manifest needs extra care; that's where a subtle bug silently ruins a 300-image run. Run `python tests/simulate_run.py` before and after any such change.

## Doc map

- `docs/PRD.md` — what and why, goals/non-goals, success criteria, risks.
- `docs/ARCHITECTURE.md` — how it works, module responsibilities, scale design.
- `docs/BUILD_PLAN.md` — phased plan and what was delivered in each.
- `docs/FLOW_UI_NOTES.md` — what's been confirmed against the live Flow UI, and when. Check this before assuming a failure is our bug.
- `docs/PROGRESS.md` — live status, local working notes (gitignored). **Read first if present.**
