# Build Plan — YoutubeImageAutomation

Status: All phases done; running in real production use | Last updated: 2026-08-15

Scope: make the existing MVP capable of unattended 200–300 image runs with guaranteed sequence integrity. Re-phased on 2026-08-14 when the real workload (200–300 images/video, script-mapped) became known — resilience moved from "nice, later" to the critical path.

## Phase overview

| Phase | Name | Status |
|---|---|---|
| 0 | Documentation & Planning | Done |
| 1 | Configuration Foundation | Done |
| 2 | Batch Resilience Core | Done (verified in simulation) |
| 3 | Scale Hardening & Live Validation | Done — validated in real production use against a 97-beat script. Gallery virtualization at scale never materialized as a problem in practice, and was deliberately not chased further: a wrong image would be visible immediately, which is cheaper than hunting a threshold that may not exist |
| 4 | Operator UX | Done |
| 5 | Testing & Validation | Done — `tests/simulate_run.py` at 102 checks covers what this phase envisioned (sequence integrity, resume, retries, circuit breaker, cooldowns, all CLI flags, all three prompt formats, stale-image invalidation, duration estimates). A separate pytest suite was deliberately not added: it would have duplicated the same coverage in a second framework for no gain |
| — | Web UI (unplanned, added on request) | Done — session model, prompts editor with upload, native path pickers, live progress, gallery with search/filter, per-beat detail + regenerate, finish notifications |

---

## Phase 0 — Documentation & Planning ✅

Doc set (`DEVELOPMENT.md`, `docs/*`) established so any fresh-context session can resume correctly. Rewritten 2026-08-14 for the 200–300 image workload.

---

## Phase 1 — Configuration Foundation ✅

- `src/config.py` reads all tunables from `.env` with sane defaults; `.env.example` documents each.
- All CWD-relative paths replaced with project-root-anchored paths.
- `requirements.txt` corrected (added `playwright`; removed 17 unused packages from the abandoned Gemini approach).

---

## Phase 2 — Batch Resilience Core ✅

**Goal**: a 300-prompt run survives failures and interruptions without ever corrupting numbering.

Delivered and verified via `tests/simulate_run.py` (now 102 checks, all green) plus real-network download tests. Caveat: this phase verified against a *simulated* browser — live UI validation is Phase 3.

Tasks:
- `src/manifest.py` — atomic ledger, hash-based prompt reconciliation, pending-index queries, summary stats. (FR4, FR7)
- `src/prompt_loader.py` — return `(index, text)` pairs anchored to file line position. (FR1)
- `src/downloader.py` — atomic writes via temp+rename, streaming, HTTP retry, timeouts. (FR8, FR5)
- `src/flow_automation.py` — config-driven timeouts, set-difference new-image detection, single-round-trip DOM reads. (FR9)
- `src/main.py` — resume, per-prompt retry with backoff, fault isolation, circuit breaker, summary. (FR2, FR3, FR5, FR6)

**Acceptance criteria**: a simulated 300-prompt run with injected failures completes without aborting; numbering never shifts; killing and re-running skips completed work; the manifest survives a mid-write kill.

---

## Phase 3 — Scale Hardening & Live Validation

**Goal**: close the "known unknowns" in `docs/ARCHITECTURE.md` §6 against the real Flow UI, and handle what's found.

Tasks:
- Run `tests/manual/inspect_flow_dom.py` (read-only, zero credits) against a live Flow project; record findings in `docs/FLOW_UI_NOTES.md`.
- **Resolve the DOM-virtualization question first** — if Flow unmounts offscreen images, the before/after URL set comparison needs a different anchor (e.g. scoping to a specific results container, or a generation-count attribute).
- Handle multi-image-per-click if that's what Flow does.
- Handle project capacity limits if 300 images can't live in one project.
- Add interstitial/dialog handling for long sessions (quota warnings, re-auth).
- Validate end-to-end with a small real run (~5 prompts, user-approved).

**Acceptance criteria**: a real 20-image run completes correctly with verified numbering; documented answers to all four unknowns.

---

## Phase 4 — Operator UX ✅

**Goal**: make a multi-hour run pleasant to supervise.

Delivered:
- CLI flags via `argparse`: `--prompts-file`, `--output-dir`, `--limit`, `--only` (with a range parser, `5,12,40-45`), `--retry-failed`, `--no-resume` (mutually exclusive selection modes), `--dry-run`. Config supplies all defaults.
- `--dry-run` connects to Flow and reports exactly what would run, without ever calling `generate_image`/`download_image` — zero credits spent, verified by test (TEST 14 in `tests/simulate_run.py`).
- `tqdm` progress bar with ETA wraps the batch loop; per-prompt detail lines use `tqdm.write()` so they don't corrupt the bar.
- Run summary prints a copy-pasteable `--only <missing>` command for exactly the gaps.
- Two additional safety warnings added beyond the original scope, both found necessary while building this: `--no-resume` warns loudly before regenerating everything; selecting an already-successful beat via `--only`/`--no-resume` warns it will be overwritten and re-charged.
- `README.md` written from the real, verified setup flow (not aspirational) — includes the correct `flow/_profile` path discovered during Phase 3 live testing.

**Acceptance criteria**: met. `--help` documents every flag; `--dry-run` spends zero credits (test-verified); the summary makes gaps obvious and directly actionable.

Test coverage: 15 new checks added to `tests/simulate_run.py` (TESTS 9–14) covering every new flag and their interaction with selection/resume — total suite now 49 checks, all passing.

---

## Phase 5 — Testing & Validation

Tasks:
- `pytest` coverage for `prompt_loader`, `manifest` (resume, hash-change, atomicity), `downloader` (mocked HTTP), and numbering invariants under failure.
- A property-style test asserting numbering never shifts regardless of failure pattern.
- Move interactive scripts under `tests/manual/`; document that they need a live session.
- `docs/MANUAL_SMOKE_TEST.md` for the Playwright path.

**Acceptance criteria**: `pytest` green with no browser required; numbering invariant covered by a test that would fail if someone reintroduced counter-based naming.
