# How to use Flow Image Automation

Your day-to-day manual. For how the code works internally, see the `docs/` folder instead — this file is just "what do I click."

## The web link

**http://127.0.0.1:8765**

This only works on this laptop (it's not reachable from anywhere else). It's live automatically once you're logged in — see "Starting it" below.

---

## Starting it

You normally don't need to do anything — it starts by itself.

A Windows Task Scheduler entry called **`FlowImageAutomation-WebUI`** launches it quietly (no console window) about 15 seconds after you log in. Just open a browser and go to the link above.

**If the link ever says "can't be reached":** the background task isn't running for some reason. Start it yourself:

1. Open a terminal (PowerShell) in the project folder: `D:\yt\YoutubeImageAutomation`
2. Run:
   ```
   python src/web_ui.py
   ```
3. Leave that window open, and open http://127.0.0.1:8765 in your browser.

(Closing that terminal window stops the server — see "Stopping it" below.)

---

## Stopping it

Pick whichever matches what you're trying to do:

| I want to... | Do this |
|---|---|
| Stop it right now, just this once (it'll come back next login) | Task Manager → find the **Python** process → End Task |
| Stop it right now AND never have it auto-start again | See "Turning autostart off" below |
| Stop a specific batch run, but keep the web UI open | Click **Stop** in the web UI's Run panel (finishes the current image, then stops cleanly) |

---

## Turning autostart on/off

The autostart is a normal Windows Task Scheduler entry — nothing hidden about it, and nothing here depends on Claude Code or any AI subscription. You can inspect or remove it any time.

**Easiest way (no typing):**
1. Press `Win + R`, type `taskschd.msc`, press Enter.
2. In the left panel, click **Task Scheduler Library**.
3. Find **FlowImageAutomation-WebUI** in the list.
4. Right-click it →
   - **Disable** — turns off autostart, keeps the entry so you can turn it back on later.
   - **Enable** — turns autostart back on.
   - **Delete** — removes it completely.

**Or from PowerShell**, if you prefer commands:
```powershell
# turn off
Disable-ScheduledTask -TaskName "FlowImageAutomation-WebUI"

# turn back on
Enable-ScheduledTask -TaskName "FlowImageAutomation-WebUI"

# remove completely
Unregister-ScheduledTask -TaskName "FlowImageAutomation-WebUI" -Confirm:$false
```

Turning autostart off does **not** delete anything else — your prompts, output images, and settings are untouched. It only stops the web server from launching automatically at login.

---

## Using it — the normal workflow

### 1. Get your prompts file ready

A `.txt` file, one image prompt per beat, numbered so beat 47 always becomes `047.jpg`. Several formats are auto-detected (BEAT/PART/SCENE/SHOT/STEP/CLIP blocks, timestamped scripts, numbered lists) — you don't need to pick one, just write it the way your script-writing chat naturally produces it, and paste it in.

If the tool can't recognize the format at all, or spots something wrong with it, it refuses to run and tells you clearly instead of silently generating garbage — see "If the prompts file has a problem" below for what each message means.

### 2. Open the web UI

Go to **http://127.0.0.1:8765**.

### 3. Point it at your files

- **Prompts file** — type the path, or click **Browse…** and pick it.
- **Output folder** — where the numbered `.jpg` files get saved. Type a path or Browse…
- Click **Save** if you typed/uploaded a new prompts file (this writes it to the path shown).

### 4. Pick a Flow project (first time, or to switch)

Under **Setup**, click **Choose project** to pick which Google Flow project the images generate into, or **New project** to create a fresh one. You only need to do this once per video, unless you want to switch.

### 5. Choose what to run

The **What to run** dropdown:
- **Remaining beats (resume)** — the normal choice. Generates whatever hasn't succeeded yet. Safe to re-run after any interruption; it skips what's already done.
- **Only failed beats** — retries just the ones that errored last time.
- **Beat interval…** — generate only a specific range, e.g. beats 40 through 45 (fill in **From beat** / **To beat**), or a comma list like `5,12,40-45`.
- **Everything (re-generates all)** — regenerates every beat, even ones that already succeeded. Asks for confirmation first, since it spends credits on beats you already have.

**Limit** (optional) — cap how many beats this run does, regardless of mode.

### 6. Dry run first (optional but recommended for a new file)

Click **Dry run** — connects to Flow and reports what it *would* do, without spending any credits or generating anything. Good for catching a bad path or project before committing to hours of real generation.

### 7. Start

Click **Start**. Progress, per-beat status, and live logs show in the Run panel. A run of 200–300 images takes hours — you can close the browser tab; the run keeps going on the server (as long as the machine stays on and Chrome isn't closed). Reopen the tab any time to check progress.

### 8. If it gets interrupted

Network blip, laptop sleep, Chrome crash, you hit Stop — doesn't matter. Just come back and click **Start** again with the same prompts file and output folder. Already-finished beats are automatically skipped; nothing gets renumbered or double-generated.

### 9. When it's done

Click **Open folder** to jump straight to the output images on disk. Filenames are `001.jpg`, `002.jpg`, etc. — matching your prompts file's beat numbers exactly.

---

## If the prompts file has a problem

Loading a prompts file (Save, or Start) can refuse with one of these. All of them mean it caught something *before* spending any credits on it — none of them are "the tool is broken."

- **"Duplicate BEAT N in ..."** — the same beat number appears twice, back-to-back, with nothing real in between (a genuine copy-paste mistake, not a preview/draft pattern it already knows how to untangle on its own — see the next point). Open the file, find the two `BEAT N` (or `PART N`, etc.) headers, and remove or renumber one.

- **"Parsed X prompt(s) from ..., but the file's own text claims [Y]"** — the file itself says how many prompts it should contain (e.g. it says "all 184 image prompts" somewhere), but the actual parsed count doesn't match. This usually means leftover chat text — an early draft, a "good to proceed?" confirmation — got copied into the file along with the real prompts. Note: if the numbering restarted cleanly (beat 1, 2, 3 as a preview, a confirmation line, then beat 1, 2, 3... again for real), the tool now detects and discards the earlier draft automatically — you'd only see this message if something about the file didn't match that exact pattern. Open the file and check the start of it for a stray preview/chat section.

- **"...looks like it's laid out in '\<WORD> \<number>' blocks, but that keyword isn't one this tool recognizes yet"** — your file uses a numbering keyword (like `CHAPTER 3`, `SEGMENT 12`) that isn't one of the known ones (`Beat`, `Part`, `Scene`, `Shot`, `Step`, `Clip`). Tell me the file's header format and I'll add it — this is a five-minute code change, not something you need to work around by hand.

- **"...doesn't match any recognized format... but its mix of short and long lines suggests it IS structured"** — the file doesn't use a numbered keyword at all, but its line pattern doesn't look like one plain prompt per line either. Same fix: share the file and I'll add support for its actual layout.

In every case: the tool is refusing to guess rather than silently sending header text, narration, or half a chat conversation to Flow as bogus image prompts (that's exactly what caused the 184-beats-read-as-935 incident this whole system was built to prevent). If you hit one of these, the fastest path is just sending me the file.

---

## If Google logs you out

Flow sessions occasionally expire. If a run fails immediately with a message about needing to sign in, open the Chrome window this tool uses (it's a separate, dedicated browser profile — not your regular Chrome) and sign back into Google there manually. This tool never stores or enters your Google password itself — signing in is always a manual, one-time step.

---

## Quick reference

| Thing | Value |
|---|---|
| Web UI link | http://127.0.0.1:8765 |
| Project folder | `D:\yt\YoutubeImageAutomation` |
| Prompts file (default) | `prompts\prompts.txt` |
| Output folder (default) | `output\` |
| Autostart task name | `FlowImageAutomation-WebUI` |
| Manual start command | `python src/web_ui.py` |
