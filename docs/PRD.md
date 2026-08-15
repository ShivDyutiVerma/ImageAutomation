# Product Requirements Document — YoutubeImageAutomation

Status: Living document | Last updated: 2026-08-14 | Owner: Shiv

## 1. Problem statement

The user produces YouTube videos from a written script. Each script is broken into beats, and each beat needs a generated image — **200 to 300 images per video**. Images are generated with Google Labs' **Flow** on a **Pro account, which exposes no API** for the web tier. Doing this by hand means typing 200–300 prompts into a web UI one at a time, waiting ~30–60s each, and manually downloading and renaming every result. That is many hours of mechanical work per video and is completely impractical to sustain.

## 2. Goal

One command generates an entire video's image set — 200–300 images — unattended, correctly numbered in script order, resumable after any interruption, with a clear report of exactly what succeeded and what didn't.

## 3. Users

A single user (the project owner), running this locally on Windows against their own Google Flow Pro account, to produce assets for their own channel. Personal tool; not hosted, not multi-user.

## 4. The workflow this serves

The full pipeline, only the last stage of which this tool automates:

```
1. Write the video script (typically with an AI assistant)
2. Manually divide the script into ~150-300 parts (one per beat/shot)
3. Manually author one image prompt per part, matching that beat
4. Compile all beats into prompts/prompts.txt
                    ↓  (this tool takes over from here)
5. prompts/prompts.txt          BEAT 47
                    ↓
   output/047.jpg               ← beat 47's image
                    ↓
6. video editor: images dropped onto the timeline in numeric order
```

Steps 1–3 (scripting, dividing, and prompt-writing) are **deliberately manual and out of scope** — not because they couldn't be automated, but because judging whether a script is good and whether a prompt actually fits its beat needs human judgment, and automating that well would take real design work. See Non-goals (§7) — this may become a later phase, but is explicitly deferred for now. This tool's job starts at step 4: take a finished prompts file and turn it into a correctly-numbered image set, unattended.

Step 4's output format (confirmed against a real working script, 2026-08-15) is **BEAT-block**, one block per beat:

```
BEAT 47
"narration or script line for this beat — reference only, never sent to Flow"

The actual image prompt goes here, describing the beat's visual — this exact
text is what gets typed into Flow.
```

The beat number is the prompt's permanent identity — it becomes the output filename directly, regardless of the block's position in the file. A simpler plain-text format (one prompt per line) is also supported for quick/informal use; see `README.md` for both.

**Filename number = beat number.** This is the contract that makes the tool useful. A missing image is a visible, fixable gap. A *misnumbered* image silently desynchronizes every subsequent beat and corrupts the edit — it is the single worst failure this system can produce.

## 5. Current state

Working end-to-end at small scale (proven with 2 prompts): prompt loading, Flow UI automation over CDP, image URL resolution, download to `output/NNN.png`. Login persistence via a manually-authenticated Chrome profile (`flow/_profile/`).

Recently fixed (2026-08-14): CWD-dependent paths, a misplaced `time` import, an unbounded hang in `wait_until_ready`, a leaked Chrome tab in `resolve_image_url`, and a `requirements.txt` that omitted `playwright` while carrying 17 unused packages.

**Not yet fit for 200–300 images**: the batch loop aborts entirely on the first failure, there is no resume, no retry, no run record, and the DOM scan cost grows linearly with the number of images already in the project.

## 6. Requirements

### Must have (a 300-image run is impossible without these)

- **FR1 — Sequence integrity.** Output filename is derived from the prompt's line index, never from a success counter. A failed prompt leaves a gap; it never causes a later image to take its number.
- **FR2 — Fault isolation.** One prompt failing never aborts the batch. The run continues and records the failure.
- **FR3 — Resume.** Re-running the same command after any interruption skips already-completed images and continues where it stopped. No regenerating 200 images because the run died at 250 (that would waste hours and real Pro credits).
- **FR4 — Run manifest.** A durable, per-prompt record (index, prompt, prompt hash, status, output file, attempts, error, timestamps), written atomically after every prompt so a crash can't corrupt it.
- **FR5 — Bounded retry.** Transient failures (slow generation, flaky download) retry a bounded number of times with backoff before being marked failed.
- **FR6 — Circuit breaker.** If N prompts fail consecutively — the signature of an expired session or exhausted credits — abort the whole run immediately with a clear diagnosis. Burning 250 prompts × 3 retries × 180s against a logged-out browser is unacceptable.
- **FR7 — Prompt-change detection.** If `prompts.txt` is edited between runs, resume must not map old results onto new prompts. Per-prompt hashing detects which specific lines changed and regenerates only those.
- **FR8 — Atomic image writes.** A download interrupted mid-write must never leave a truncated `047.png` that resume mistakes for a completed image.

### Should have

- **FR9 — Scale-safe DOM reads.** Scanning the Flow page must not get progressively slower as hundreds of images accumulate in the project.
- **FR10 — Operator visibility.** For a multi-hour run: progress, ETA, and a final summary (succeeded / failed / skipped, elapsed, output location, list of missing indices to regenerate).
- **FR11 — Configuration.** CDP URL, paths, timeouts, retry counts settable via `.env` and CLI flags, not hardcoded.

### Nice to have

- **FR12 — Automated tests** for the browser-independent parts; a documented manual smoke test for the Playwright path.

## 7. Non-goals / out of scope

- **Script writing, script division into beats, and per-beat prompt authoring** (workflow steps 1-3 in §4). Done by hand, typically in a separate AI chat session. Deferred deliberately: automating it well requires judging script/prompt quality, which is a real design problem on its own, not a quick add-on. If picked up later, it becomes its own phase with its own PRD — not a silent scope-creep into this tool's job of "receive a finished prompts file, generate the images."
- Selecting/ranking among multiple candidate images per prompt.
- YouTube upload, thumbnail text overlay, or video assembly.
- ~~Any GUI/dashboard — CLI only.~~ **Reversed 2026-08-15**: a local web UI (`src/web_ui.py`, `http://127.0.0.1:8765`) now exists alongside the CLI. Both drive the same `run_batch()`, so the rules that matter (index-derived filenames, resume, retry, circuit breaker) live in one place and can't diverge between front ends. Still explicitly *not* in scope: any hosted/multi-user/network-reachable version — the server binds to loopback only, because it can spend real credits and drive a logged-in browser.
- Parallel/concurrent generation. Flow is a single interactive session driven through one browser tab; parallelism risks both account flags and sequence corruption. Explicitly rejected.

## 8. Constraints & assumptions

- **No API** on the Flow Pro web tier — DOM automation is the only option, with no stability contract.
- **Requires a manually-authenticated Google session.** Cold-start login cannot be automated; it's a one-time manual step producing `flow/_profile/`.
- **Generation consumes real Pro credits/quota.** Wasted regeneration has a direct monetary cost — a primary reason resume (FR3) is a must-have rather than a convenience.
- Single machine, single Chrome instance, one Flow project tab. Windows-first.
- Unknown until verified against the live UI: whether Flow emits one or several images per Create click, and whether a single Flow project can hold 300 images or must be split. The system is built to tolerate both; `tests/manual/inspect_flow_dom.py` resolves these read-only, without spending credits.
- Automating a product's web UI outside its intended interface may conflict with its Terms of Service. This is a personal-use tool, intended to be run by an account owner against their own paid account, at their own risk.

## 9. Success metrics

- A 300-prompt run completes unattended, producing correctly numbered images, without a single failure aborting it.
- Killing the run at any point and re-running the same command resumes without regenerating a single completed image.
- After a run, the user can see at a glance exactly which script beats are missing images.
- A logged-out or out-of-credits browser is detected and reported within a few prompts, not after hours of futile retries.

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Flow UI/DOM changes | Automation breaks, possibly silently | Explicit failure detection + manifest; read-only inspection script to re-derive selectors quickly |
| Google session expires mid-run | Every remaining prompt fails | Circuit breaker (FR6) aborts fast with a clear diagnosis |
| Pro credits exhausted mid-run | Remaining prompts fail | Same circuit breaker; manifest preserves all completed work for a later resume |
| Misnumbered output | **Corrupts the whole video edit, discovered late** | Index-derived naming (FR1), prompt hashing (FR7), atomic writes (FR8) |
| Multi-hour run interrupted | Hours + credits wasted | Resume (FR3) + per-prompt atomic manifest (FR4) |
| `flow/_profile/` committed | Live session credentials leaked | `.gitignore` + hard constraint in `DEVELOPMENT.md` |
