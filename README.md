# ImageAutomation

Batch-generates images for YouTube videos using Google Flow (Pro account, no API available), one image per prompt, numbered to match your script beat by beat: `prompts.txt` line 47 → `047.jpg`.

Built for real production runs of 200–300 images per video — resumable, fault-isolated, and safe to leave unattended for hours. See `docs/PRD.md` for the full requirements and `docs/ARCHITECTURE.md` for how it works internally.

## Requirements

- Python 3.11+
- Google Chrome installed
- A Google Flow **Pro** account (the web tool at labs.google/fx/tools/flow)

## First time on a new machine

```bash
python -m venv venv
venv\Scripts\activate            # Windows
source venv/bin/activate         # macOS / Linux
pip install -r requirements.txt

python src/setup.py
```

`setup.py` checks everything needed and walks you through whatever's missing:

| Check | Automatic? |
|---|---|
| Python packages | Tells you to `pip install -r requirements.txt` |
| Google Chrome found | Auto-detected on Windows, macOS, and Linux |
| Browser profile created | Created for you |
| **Google sign-in** | **Manual — one time. A Chrome window opens, you sign in, it detects when you're done.** |
| Prompts file | Tells you if it's missing or unparseable |
| Flow project chosen | Lists your projects and saves your pick to `.env` |

Run `python src/setup.py --check` any time for a read-only status report (changes nothing, opens nothing).

**Why sign-in stays manual:** Google actively blocks scripted credential entry, and storing your password in this project would mean a leak of the repo compromises your entire Google account. So you sign in once by hand; the session is saved in `flow/_profile/` and reused forever after. Everything else — launching Chrome, navigating to your project, generating, downloading — is automatic.

Treat `flow/_profile/` like a password: never commit it, never share it (already gitignored).

The same setup lives in the web UI under **Setup**, with the same checks and buttons, if you'd rather not use the terminal.

## Web UI (easiest way to use it)

```bash
python src/web_ui.py
```

Then open **http://127.0.0.1:8765**. You get a prompts editor, a Start/Stop/Dry-run panel, live progress with ETA, an activity log, and a thumbnail grid of every beat showing exactly which are done, failed, or still pending.

It drives the same engine as the CLI — same numbering guarantees, same resume, same circuit breaker — so you can freely switch between them. Runs one batch at a time (a second Start is refused while one is in progress, since two concurrent runs would interleave prompts into the same browser and scramble the beat mapping).

**Sessions.** Opening the page always starts at a session chooser rather than silently resuming whatever video was open last — pick **Create new session** for a new video, or click a previous session to carry on finishing its images (it shows how many beats are already done). **Switch session** in the header reopens the chooser any time. If a run is in progress, the page reattaches to it instead of showing the chooser, so an accidental refresh three hours into a 300-image batch doesn't lose sight of it.

**Choosing where things live.** The Run panel takes a prompts file and an output folder, and both work two ways: type/paste a path, or click **Browse…** to open a real folder/file picker on your machine. (The picker is opened by the local server process, because a browser deliberately never reveals real filesystem paths — it reports `C:\fakepath\…`.) Under the two boxes, the panel states the resolved absolute folder images will save to, and whether it already has files in it or will be created. If a picker can't open on your system, typing the path works exactly the same.

For prompts specifically you can also click **Upload** to load a file's contents straight into the editor — that fills the editor only; **Save** then writes it to whichever prompts file is currently set, which the confirmation message names.

Progress shown per session only counts images generated from the prompt text currently in the file — if you edit a beat, it stops counting as done, because the run will regenerate it.

**Starting a new video:** use **Create new session** in the chooser. Type a name and it creates a fresh, empty Flow project *and* a matching unique prompts/output pair (`prompts/<name>.txt`, `output/<name>/`) in one step — so one video's beats can never collide with another's, locally or in Flow. Flow's own "create project" action has genuinely unpredictable timing (seen anywhere from a few seconds to over two minutes on the same account) — if it doesn't confirm right away, the local files are still ready; wait a minute and click **Choose project**, which reliably finds it once it's actually done.

Bound to `127.0.0.1` only, deliberately: this page can spend real credits and drives your logged-in browser, so it isn't reachable from other machines on your network.

**Beyond the basics:**
- **Click any beat's thumbnail** to see the full image, full narration, and the exact prompt sent to Flow (the gallery caption is trimmed to 60 characters). A **Regenerate this beat** button there starts a real run scoped to that one beat only — it never picks up whatever mode/limit happens to be set in the Run panel.
- **Search and filter the Images grid** — a text box matches narration or prompt text, and All/Done/Outstanding/Failed chips narrow it by status. Useful once a video has more than a screenful of beats.
- **A time estimate appears before you start**, based on this session's own history (`N beats outstanding · ~Xs/beat · ~Yh estimated`), so you know roughly what you're committing to before a multi-hour run begins.
- **Open folder** next to the save-location line reveals the output folder in your OS file manager.
- **A run finishing gets a sound + desktop notification** — useful since these runs are meant to be left unattended for hours. The browser will ask permission the first time you click Start or Dry run.

## Every real run (CLI)

**1. Just run it.** Chrome launches itself, pointed at the already-authenticated profile from step 0 — no manual browser step needed (confirmed working from a fully closed Chrome, 2026-08-15). It also navigates into a project on its own if it lands on Flow's home page — see `FLOW_PROJECT_URL` below if you have more than one project, since it will refuse to guess which one you mean.

Chrome auto-launch never touches your Google login — it only starts a process against the profile you already authenticated in step 0. If you ever see a "not logged into Google" error, that means the session expired; re-run the one-time login step, don't try to work around it.

To disable auto-launch and manage Chrome yourself, set `FLOW_AUTO_LAUNCH_CHROME=false` in `.env`, then:

```bash
chrome.exe --remote-debugging-port=9222 --user-data-dir="<path-to-this-repo>\flow\_profile"
```

**2. Write your prompts** in `prompts/prompts.txt` (see `prompts/example_script.txt` for a working sample). Your own prompt files stay out of git — `.gitignore` tracks only the example, so an unreleased video's script never lands in a public repo. Three formats are supported, auto-detected:

**BEAT-block format** (recommended — matches pasting straight out of a script-writing chat session):

```
BEAT 8
"Medicine had a problem."

Hand-drawn 2D cartoon. Thick black outlines. Flat colors. White background. Large
flat red warning triangle centered, thick black outline, bold black exclamation
mark inside...

BEAT 9
"And then."

Hand-drawn 2D cartoon. Thick black outlines...
```

- `BEAT <N>` is the beat number — **this becomes the output filename** (`BEAT 8` → `008.jpg`), not its position in the file. Beat numbers don't need to be contiguous or start at 1; missing numbers between the lowest and highest are reported at startup as a sanity check, not an error.
- The quoted line right after `BEAT N` is narration/script reference text — carried into the manifest for your own reference, but **never sent to Flow**.
- Everything after that, up to the next `BEAT` line, is the actual image prompt sent to Flow. It can span multiple lines; they're joined into one prompt.
- Duplicate beat numbers are rejected before anything is generated (two beats writing to the same filename would silently overwrite one image with another).

**Timestamped script format** (what a script-writing chat tends to produce directly):

```
0:00 — "You just decided to watch this video."
Image: A stickman figure centered on pure white background, dot eyes...
Video: White frame holds for 0.5 seconds. Stickman fades in...

0:03 — "Or did you?"
Image: Same stickman. A bold red X drawn through the label...
Video: The X draws over 0.3 seconds.
```

- One beat per timestamp line; the beat number is the block's **position** in the file (first block → `001.jpg`), since timestamps aren't stable identities.
- **Only the `Image:` line becomes the prompt** — the timestamp line is narration, and `Video:` lines are motion direction for a video tool. Sending those to an image generator would produce a confidently wrong image, so they're dropped.
- A block with no `Image:` line is a hard error naming the timestamp, rather than a silently empty beat.

**Plain format** (for quick/simple use — what the tool used before beat-blocks existed): one prompt per line, no `BEAT` headers and no timestamp/`Image:` structure anywhere in the file. Line position (among non-blank lines) becomes the image number.

Detection order is BEAT → timestamped script → plain. Plain is last on purpose: it accepts literally any text, so it must never get first refusal on a structured file (that's how a 97-beat script once parsed as 291 one-line "prompts").

Whichever format you use, the rule is the same: **the number is permanent and never shifts.** A failed beat leaves a gap; it never renumbers anything after it.

**3. Run it:**

```bash
python src/main.py
```

Images land in `output/NNN.<ext>` (extension matches whatever Flow actually serves — JPEG in practice). Progress is a `tqdm` bar with an ETA; a `manifest.json` in the same folder tracks per-beat status and is what makes resume possible.

## If it gets interrupted

Just re-run the same command. Already-completed beats are skipped — nothing is regenerated, nothing extra is charged to your account. If a beat failed, it's retried automatically (bounded retries with backoff).

If five beats fail in a row — the signature of exhausted credits, a dropped session, or an "image generation error" that isn't resolving — the run doesn't just give up. It pauses (`FLOW_COOLDOWN_SECONDS`, default 5 minutes) and tries again, up to `FLOW_MAX_COOLDOWNS` times (default 3), since a lot of what trips this resolves on its own within minutes. A single success at any point resets the count. Only after all the cooldowns are exhausted does it stop for real — cleanly, with everything completed so far saved and nothing renumbered. Set `FLOW_MAX_COOLDOWNS=0` to skip straight to stopping, no pauses.

## CLI flags

```
python src/main.py --help
```

| Flag | Use it for |
|---|---|
| `--prompts-file PATH` | Use a different prompts file than `prompts/prompts.txt` |
| `--output-dir PATH` | Use a different output folder (its own `manifest.json` lives inside it) |
| `--limit N` | Process at most N beats this run — useful for working through a big batch in supervised chunks |
| `--only 5,12,40-45` | Regenerate only these specific beats, regardless of their current status — the tool prints exactly this kind of command at the end of every run for whatever's missing |
| `--retry-failed` | Retry only beats currently marked failed, skip anything never attempted |
| `--no-resume` | Ignore the manifest and regenerate everything, including beats that already succeeded — **spends credits on all of them**, printed as a warning before it runs |
| `--dry-run` | Connects to Flow and reports exactly what would run — **spends zero credits** |

Run `--dry-run` before any large batch. It validates that Chrome is reachable, a project is resolvable, and shows exactly which beats are selected — the cheapest way to catch a setup mistake before it costs anything.

## Configuration

Every timeout, retry count, and path has a default and can be overridden via `.env` (copy `.env.example`) or the CLI flags above. See `.env.example` for the full list, including `FLOW_PROJECT_URL` (pin a run to a specific Flow project — recommended, since the automation won't guess among several) and `FLOW_AUTO_CREATE_PROJECT` (let it click "New project" itself when none exist — off by default).

## When something breaks

Flow has no API and no stability contract — Google can change its UI at any time. Before assuming the code is broken:

```bash
python tests/manual/inspect_flow_dom.py
```

Read-only, zero credits. It checks whether the automation's selectors still match the live page and reports exactly what it finds. `docs/FLOW_UI_NOTES.md` has the history of what's been verified against the real UI and when.

## Testing

```bash
python tests/simulate_run.py
```

A full offline simulation of batch runs (resume, retries, the circuit breaker, sequence-integrity under injected failures, all the CLI flags) — no browser, no network, no credits, runs in under a second. This is the regression guard for the one rule that must never break: **the output filename number is always the script beat number** — a failed beat leaves a gap, it never shifts a later beat's number.

## License

MIT — see [LICENSE](LICENSE).

Note that this drives Google Flow's web UI, which has no public API and no stability contract. Automating a product outside its intended interface may conflict with its Terms of Service; it's intended to be run by an account owner against their own paid account, at their own risk. Google can change the UI at any time and break the selectors — see "When something breaks" above.
