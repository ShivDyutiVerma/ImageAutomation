"""Offline end-to-end simulation of full runs.

Runs the real orchestrator against a fake browser and a fake downloader, so a
300-image run can be exercised in under a second with no Chrome, no network,
and no credits spent.

Its most important job is guarding the sequence-integrity contract: output
filename number == prompts.txt line number == script beat. If someone ever
reintroduces success-counter-based naming, TEST 2 fails loudly.

    python tests/simulate_run.py

Exits non-zero if any check fails.
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="flowsim-"))

os.environ["FLOW_PROMPTS_FILE"] = str(WORK / "prompts.txt")
os.environ["FLOW_OUTPUT_DIR"] = str(WORK / "output")
os.environ["FLOW_MANIFEST_FILE"] = str(WORK / "output" / "manifest.json")
os.environ["FLOW_DELAY_BETWEEN_PROMPTS"] = "0"
os.environ["FLOW_RETRY_BACKOFF"] = "0"
os.environ["FLOW_MAX_ATTEMPTS"] = "2"
os.environ["FLOW_CONSECUTIVE_FAILURE_LIMIT"] = "5"
# Cooldowns off by default so every existing test (esp. TEST 5) keeps its
# exact pre-cooldown-feature behavior: hard stop, no pause-and-retry. The
# dedicated cooldown tests below turn it on via a direct config override.
os.environ["FLOW_MAX_COOLDOWNS"] = "0"

sys.path.insert(0, str(REPO / "src"))

import config  # noqa: E402
import main as main_mod  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import prompt_loader  # noqa: E402
from flow_automation import FlowGenerationError, FlowSetupError  # noqa: E402
from manifest import Manifest  # noqa: E402

PASS = FAIL = 0


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}   {extra}")


def heading(title):
    print("\n" + "-" * 62)
    print(title)
    print("-" * 62)


def run_quiet(argv=None):
    """Run the orchestrator, suppressing its console output."""
    with contextlib.redirect_stdout(io.StringIO()):
        main_mod.main(argv)


def run_capturing(argv=None):
    """Run the orchestrator, returning its console output as text."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main_mod.main(argv)
    return buf.getvalue()


def write_prompts(lines):
    (WORK / "prompts.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakePage:
    url = "https://labs.google/fx/tools/flow/project/fake-test-project"


class FakeFlow:

    fail_for = set()
    raise_setup_at = None
    project_missing = False
    calls = []

    def __init__(self, *args, **kwargs):
        self.flow_page = FakePage()

    def wait_until_ready(self, timeout=None):
        if FakeFlow.project_missing:
            raise FlowSetupError(
                "This Flow project no longer exists -- simulated"
            )

    def generate_image(self, prompt):
        self.wait_until_ready()
        FakeFlow.calls.append(prompt)
        index = int(prompt.split("#")[1].split()[0])
        if FakeFlow.raise_setup_at == index:
            raise FlowSetupError("simulated browser loss")
        if index in FakeFlow.fail_for:
            raise FlowGenerationError(f"simulated generation failure for {index}")
        return f"https://example.test/img/{index}"

    def close(self):
        pass


def fake_download(url, stem, **kwargs):
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_DIR / f"{stem}.png"
    path.write_bytes(b"FAKE-IMAGE-" + url.encode())
    return str(path)


main_mod.FlowAutomation = FakeFlow
main_mod.download_image = fake_download


def reset(n=300, fail_for=(), setup_at=None):
    shutil.rmtree(WORK / "output", ignore_errors=True)
    write_prompts([f"prompt #{i} describing beat {i}" for i in range(1, n + 1)])
    FakeFlow.fail_for = set(fail_for)
    FakeFlow.raise_setup_at = setup_at
    FakeFlow.project_missing = False
    FakeFlow.calls = []


def images():
    if not config.OUTPUT_DIR.exists():
        return []
    return sorted(p.name for p in config.OUTPUT_DIR.glob("*.png"))


heading("TEST 1: clean 300-prompt run")
reset(300)
run_quiet()
imgs = images()
check("300 images produced", len(imgs) == 300, f"got {len(imgs)}")
check("numbered 001..300 with no gaps",
      imgs == [f"{i:03d}.png" for i in range(1, 301)])
manifest = Manifest.load(config.MANIFEST_FILE)
check("manifest records 300 successes", manifest.counts()["success"] == 300)
check("no missing beats", manifest.missing_indices() == [])

heading("TEST 2: SEQUENCE INTEGRITY - failures gap, never shift")
reset(60, fail_for={17, 18, 42})
run_quiet()
imgs = images()
check("57 images (3 failed)", len(imgs) == 57, f"got {len(imgs)}")
check("017.png absent", "017.png" not in imgs)
check("018.png absent", "018.png" not in imgs)
check("042.png absent", "042.png" not in imgs)
check("016.png present", "016.png" in imgs)
check("019.png present - NOT shifted down into 017", "019.png" in imgs)
check("060.png present - tail not renumbered", "060.png" in imgs)
manifest = Manifest.load(config.MANIFEST_FILE)
check("manifest reports exactly beats 17,18,42 missing",
      manifest.missing_indices() == [17, 18, 42], manifest.missing_indices())
entry = manifest.entry(19)
check("beat 19 maps to 019.png", entry["file"] == "019.png", entry["file"])
check("beat 19 holds prompt #19", "#19 " in entry["prompt"])

heading("TEST 3: RESUME - completed work is never redone")
reset(50, fail_for={10, 20})
run_quiet()
check("first pass: 48 ok + 2 failures retried twice = 52 calls",
      len(FakeFlow.calls) == 52, len(FakeFlow.calls))
FakeFlow.fail_for = set()
FakeFlow.calls = []
run_quiet()
check("resume regenerated only the 2 failures", len(FakeFlow.calls) == 2)
check("all 50 images now present", len(images()) == 50)
check("nothing missing", Manifest.load(config.MANIFEST_FILE).missing_indices() == [])

heading("TEST 4: resume after a crash mid-run")
reset(100, setup_at=34)
run_quiet()
completed = len(images())
check("run stopped partway", 30 <= completed <= 34, completed)
FakeFlow.raise_setup_at = None
FakeFlow.calls = []
run_quiet()
check("all 100 present after resume", len(images()) == 100)
check("only the remainder was regenerated",
      len(FakeFlow.calls) == 100 - completed, len(FakeFlow.calls))
check("no gaps", images() == [f"{i:03d}.png" for i in range(1, 101)])

heading("TEST 5: CIRCUIT BREAKER - abort when everything fails")
reset(200, fail_for=set(range(1, 201)))
run_quiet()
check("aborted early instead of grinding through 200",
      len(FakeFlow.calls) < 40, f"{len(FakeFlow.calls)} calls")
check("stopped at limit (5 failures x 2 attempts)", len(FakeFlow.calls) == 10)
check("no images written", images() == [])

heading("TEST 6: PROMPT EDIT - only the changed beat regenerates")
reset(30)
run_quiet()
lines = [f"prompt #{i} describing beat {i}" for i in range(1, 31)]
lines[6] = "prompt #7 COMPLETELY REWRITTEN beat"
write_prompts(lines)
FakeFlow.calls = []
run_quiet()
check("only beat 7 regenerated", len(FakeFlow.calls) == 1, FakeFlow.calls)
check("regenerated with the new text",
      bool(FakeFlow.calls) and "REWRITTEN" in FakeFlow.calls[0])
check("beat 7 still maps to 007.png",
      Manifest.load(config.MANIFEST_FILE).entry(7)["file"] == "007.png")
check("still 30 images", len(images()) == 30)

heading("TEST 7: blank lines never consume a number")
reset(1)
write_prompts(["prompt #1 a", "", "   ", "prompt #2 b", "", "prompt #3 c"])
shutil.rmtree(WORK / "output", ignore_errors=True)
FakeFlow.calls = []
run_quiet()
check("3 images from 3 real prompts", images() == ["001.png", "002.png", "003.png"],
      images())

heading("TEST 8: manifest atomicity and corruption handling")
path = WORK / "atomic.json"
man = Manifest(path)
man.data["items"]["1"] = {"index": 1, "status": "success", "prompt": "x",
                          "prompt_hash": "abc", "file": "001.png"}
man.save()
check("manifest written", path.exists())
check("no temp files left behind", list(path.parent.glob(".manifest-*.tmp")) == [])
check("reloads correctly", Manifest.load(path).entry(1)["file"] == "001.png")
path.write_text("{ not valid json", encoding="utf-8")
try:
    Manifest.load(path)
    check("corrupt manifest rejected", False)
except RuntimeError as e:
    check("corrupt manifest rejected with a clear error", "unreadable" in str(e))

heading("TEST 9: CLI - --only regenerates a specific beat, ignoring status")
reset(20)
run_quiet()
check("baseline: 20 images", len(images()) == 20)
FakeFlow.calls = []
run_quiet(["--only", "7"])
check("--only 7 touched exactly one prompt", len(FakeFlow.calls) == 1, FakeFlow.calls)
check("it was beat 7", bool(FakeFlow.calls) and "#7 " in FakeFlow.calls[0])
check("still 20 images (7 overwritten, not duplicated)", len(images()) == 20)

heading("TEST 10: CLI - --only rejects an out-of-range beat")
try:
    run_quiet(["--only", "999"])
    check("out-of-range --only raises", False)
except ValueError as e:
    check("out-of-range --only raises ValueError", "999" in str(e), str(e))

heading("TEST 11: CLI - --limit caps the selection")
reset(10, fail_for=set())
shutil.rmtree(WORK / "output", ignore_errors=True)
FakeFlow.calls = []
run_quiet(["--limit", "4"])
check("--limit 4 processed exactly 4", len(FakeFlow.calls) == 4, len(FakeFlow.calls))
check("only the first 4 beats in script order", images() == ["001.png", "002.png", "003.png", "004.png"], images())

heading("TEST 12: CLI - --retry-failed skips never-attempted beats")
reset(10, fail_for={3, 6})
run_quiet()
check("2 failed, 8 succeeded", len(images()) == 8)
FakeFlow.fail_for = set()
FakeFlow.calls = []
run_quiet(["--retry-failed"])
check("--retry-failed touched only the 2 failed beats", len(FakeFlow.calls) == 2, FakeFlow.calls)
check("all 10 present now", len(images()) == 10)

heading("TEST 13: CLI - --no-resume regenerates everything, including successes")
reset(5)
run_quiet()
check("baseline: 5 images", len(images()) == 5)
FakeFlow.calls = []
run_quiet(["--no-resume"])
check("--no-resume touched all 5, not just pending", len(FakeFlow.calls) == 5, FakeFlow.calls)

heading("TEST 14: CLI - --dry-run spends nothing")
reset(6)
FakeFlow.calls = []
run_quiet(["--dry-run"])
check("--dry-run made zero generate calls", FakeFlow.calls == [], FakeFlow.calls)
check("--dry-run wrote zero images", images() == [], images())
check(
    "manifest still shows all pending (dry-run didn't touch state)",
    Manifest.load(config.MANIFEST_FILE).counts()["pending"] == 6,
)

heading("TEST 15: BEAT-block format - beat number IS the sequence, not line position")
shutil.rmtree(WORK / "output", ignore_errors=True)
beat_numbers = [10, 11, 12, 15]  # deliberately non-contiguous, doesn't start at 1
lines = []
for n in beat_numbers:
    lines.append(f"BEAT {n}")
    lines.append(f'"narration text for beat {n}"')
    lines.append("")
    lines.append(f"a detailed visual style prompt for beat #{n} marker")
    lines.append("")
write_prompts(lines)
FakeFlow.calls = []
FakeFlow.fail_for = set()
output = run_capturing()
check("4 images produced", len(images()) == 4, images())
check(
    "filenames use the BEAT numbers themselves (010,011,012,015), not 1..4",
    images() == ["010.png", "011.png", "012.png", "015.png"],
    images(),
)
manifest = Manifest.load(config.MANIFEST_FILE)
check(
    "narration preserved in the manifest, separate from the prompt",
    manifest.entry(15)["narration"] == "narration text for beat 15",
    manifest.entry(15),
)
check(
    "the actual Flow-bound prompt is the visual text, not the narration",
    "visual style prompt for beat #10" in manifest.entry(10)["prompt"]
    and "narration text" not in manifest.entry(10)["prompt"],
)
check("gap (13-14) surfaced in the run's own output", "13-14" in output, output)

heading("TEST 16: BEAT-block format - failures gap by BEAT NUMBER, never shift")
shutil.rmtree(WORK / "output", ignore_errors=True)
beat_numbers = [5, 6, 7, 8, 9]
lines = []
for n in beat_numbers:
    lines.append(f"BEAT {n}")
    lines.append(f'"narration {n}"')
    lines.append("")
    lines.append(f"visual prompt content #{n} marker")
    lines.append("")
write_prompts(lines)
FakeFlow.calls = []
FakeFlow.fail_for = {7}
run_quiet()
imgs = images()
check("4 of 5 succeeded", len(imgs) == 4, imgs)
check("007.png absent (the failure)", "007.png" not in imgs)
check("008.png present and NOT shifted into 007's slot", "008.png" in imgs)
check("009.png present, tail untouched", "009.png" in imgs)
manifest = Manifest.load(config.MANIFEST_FILE)
check("manifest pinpoints beat 7 as the only gap", manifest.missing_indices() == [7])

heading("TEST 17: BEAT-block format - duplicate beat number rejected before any credits spent")
shutil.rmtree(WORK / "output", ignore_errors=True)
write_prompts([
    "BEAT 3", '"a"', "", "prompt a", "",
    "BEAT 3", '"b"', "", "prompt b", "",
])
FakeFlow.calls = []
try:
    run_quiet()
    check("duplicate BEAT raises before generating anything", False)
except ValueError as e:
    check("duplicate BEAT raises ValueError", "Duplicate BEAT 3" in str(e), str(e))
check("no generate_image calls happened", FakeFlow.calls == [], FakeFlow.calls)

heading("TEST 18: --output-dir keeps images and manifest TOGETHER")
# Regression guard: --output-dir once routed the manifest to the requested
# directory while images silently went to the config default, leaving resume
# trusting a manifest whose images weren't beside it.
reset(4)
alt_dir = WORK / "alt_output"
shutil.rmtree(alt_dir, ignore_errors=True)
FakeFlow.calls = []


def fake_download_to(url, stem, output_dir=None, **kwargs):
    target = output_dir or config.OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{stem}.png"
    path.write_bytes(b"FAKE-IMAGE-" + url.encode())
    return str(path)


main_mod.download_image = fake_download_to
run_quiet(["--output-dir", str(alt_dir)])

alt_images = sorted(p.name for p in alt_dir.glob("*.png")) if alt_dir.exists() else []
default_images = images()

check("images landed in the requested --output-dir", len(alt_images) == 4, alt_images)
check("manifest is in the same directory as the images",
      (alt_dir / "manifest.json").exists())
check("nothing leaked into the default output dir", default_images == [], default_images)

alt_manifest = Manifest.load(alt_dir / "manifest.json")
check("manifest records 4 successes", alt_manifest.counts()["success"] == 4)
check(
    "every file the manifest names actually exists beside it",
    all((alt_dir / e["file"]).exists() for e in alt_manifest.data["items"].values()),
)

main_mod.download_image = fake_download  # restore for any later tests

heading("TEST 19: pre-existing-file vs already-done are reported distinctly")
# These two warnings once double-reported the same beats under contradictory
# labels ("no manifest record" for beats the manifest clearly recorded).
reset(3)
run_quiet()
check("baseline: 3 images", len(images()) == 3)

# All 3 have both a file on disk AND a success record -> already_done only.
output = run_capturing(["--only", "1-3"])
check("already-done beats reported as such", "already have a successful image" in output)
check(
    "already-done beats NOT also called 'no manifest record'",
    "have no manifest record" not in output,
    output,
)

# Delete the manifest but keep the images -> now genuinely unclaimed.
config.MANIFEST_FILE.unlink()
output = run_capturing()
check(
    "file-without-manifest-record reported as unclaimed",
    "have no manifest record" in output,
    output,
)

heading("TEST 20: --limit 0 is rejected, not silently guessed")
import argparse as _argparse
for bad in ["0", "-3"]:
    try:
        main_mod.positive_int(bad)
        check(f"--limit {bad} rejected", False)
    except _argparse.ArgumentTypeError as e:
        check(f"--limit {bad} rejected with a clear message", "1 or more" in str(e))
check("--limit 5 still accepted", main_mod.positive_int("5") == 5)

heading("TEST 21: COOLDOWN - a transient outage recovers without aborting")
# config is read live (config.XXX at call time, not bound at import), so
# tests can override it directly rather than needing env vars set before
# the module was first imported.
config.MAX_COOLDOWNS = 2
config.COOLDOWN_SECONDS = 0
try:
    reset(15, fail_for={1, 2, 3, 4, 5})  # first 5 fail, rest recover
    output = run_capturing()
    check("run did not abort", "ABORTED" not in output, output)
    check("10 succeeded (6-15)", len(images()) == 10, len(images()))
    manifest = Manifest.load(config.MANIFEST_FILE)
    check("beats 1-5 recorded failed, not silently dropped",
          manifest.missing_indices() == [1, 2, 3, 4, 5], manifest.missing_indices())
    check("exactly one cooldown was needed before recovery",
          output.lower().count("cooldown") == 1, output)
finally:
    config.MAX_COOLDOWNS = 0

heading("TEST 22: COOLDOWN - a permanent failure still gives up cleanly")
config.MAX_COOLDOWNS = 2
config.COOLDOWN_SECONDS = 0
try:
    reset(50, fail_for=set(range(1, 51)))  # nothing ever succeeds
    FakeFlow.calls = []
    output = run_capturing()
    check("run DID abort after exhausting cooldowns", "ABORTED" in output, output)
    check("mentions cooldown retries in the reason", "cooldown" in output.lower(), output)
    check("no images written", images() == [])
    # 3 bursts of 5 consecutive failures (2 cooldowns used = tries 3 times
    # total), each beat retried FLOW_MAX_ATTEMPTS(2) times.
    check("stopped well short of grinding through all 50",
          len(FakeFlow.calls) < 50 * 2, len(FakeFlow.calls))
    check("exactly 3 bursts x 5 beats x 2 attempts = 30 calls",
          len(FakeFlow.calls) == 30, len(FakeFlow.calls))
finally:
    config.MAX_COOLDOWNS = 0

heading("TEST 23: COOLDOWN - a stop request cancels immediately, not after the wait")
config.MAX_COOLDOWNS = 3
config.COOLDOWN_SECONDS = 3600  # would hang the test for an hour if the interrupt check is broken
try:
    reset(20, fail_for=set(range(1, 21)))
    FakeFlow.calls = []

    # should_stop() is polled once per beat and then repeatedly inside the
    # cooldown wait. False for the 5-beat failing burst that triggers the
    # first cooldown, True from then on -- which lands on the very first
    # check *inside* _interruptible_sleep, before any real time.sleep runs,
    # so this resolves in well under a second if interruption works.
    calls = {"n": 0}

    def should_stop_after_burst():
        calls["n"] += 1
        return calls["n"] > 5

    import time as _time

    start_time = _time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        result = main_mod.run_batch(
            prompts_file=WORK / "prompts.txt",
            output_dir=WORK / "output",
            should_stop=should_stop_after_burst,
        )
    elapsed = _time.time() - start_time

    check("returned in well under a second, not after the 1h cooldown",
          elapsed < 5, f"{elapsed:.1f}s")
    check("aborted reason names the stop, not a timeout",
          "stop" in (result.get("aborted_reason") or "").lower(),
          result.get("aborted_reason"))
finally:
    config.MAX_COOLDOWNS = 0
    config.COOLDOWN_SECONDS = 300

heading("TEST 24: timestamped script format parses to one beat per block")
# Regression for a real, user-reported failure: a script file laid out as
# "0:00 - narration / Image: ... / Video: ..." was falling through to plain
# format, which made EVERY line its own image prompt -- 3x the beats, and
# two thirds of them timestamps and motion directions rather than images.
script_file = WORK / "script_format.txt"
script_file.write_text(
    '0:00 — "You just decided to watch this video."\n'
    "Image: A stickman figure centered on pure white background.\n"
    "Video: White frame holds for 0.5 seconds, then fades.\n"
    "\n"
    '0:03 — "Or did you?"\n'
    "Image: Same stickman with a bold red X drawn through the label.\n"
    "Video: The X draws over 0.3 seconds.\n"
    "\n"
    '0:04 — "Because here is the thing."\n'
    "Image: Pure white background with large hand-drawn block letters.\n"
    "Video: Text cuts in with no fade.\n",
    encoding="utf-8",
)

parsed = prompt_loader.load_prompts(script_file)

check("one beat per timestamp block, not one per line",
      len(parsed) == 3, f"{len(parsed)} beats")
check("beats numbered by block position",
      [i for i, _, _ in parsed] == [1, 2, 3],
      [i for i, _, _ in parsed])
check("prompt comes from the Image: line, with the prefix stripped",
      parsed[0][1] == "A stickman figure centered on pure white background.",
      parsed[0][1])
check("narration comes from the timestamp line, unquoted",
      parsed[0][2] == "You just decided to watch this video.",
      parsed[0][2])
check("no Video: direction ever leaks into a prompt",
      not any("Video:" in text or "holds for" in text for _, text, _ in parsed),
      [t for _, t, _ in parsed])
check("no timestamp ever leaks into a prompt",
      not any(re.search(r"\d+:\d{2}", text) for _, text, _ in parsed),
      [t for _, t, _ in parsed])

# A block with no Image: line is a real authoring mistake -- it must be a
# loud error, never a silently skipped or empty beat.
bad_script = WORK / "script_missing_image.txt"
bad_script.write_text(
    '0:00 — "Fine beat."\n'
    "Image: A valid prompt.\n"
    "\n"
    '0:05 — "This one has no image line."\n'
    "Video: Something moves.\n",
    encoding="utf-8",
)
try:
    prompt_loader.load_prompts(bad_script)
    check("a block with no Image: line is rejected", False, "no error raised")
except ValueError as e:
    check("a block with no Image: line is rejected", True)
    check("the error names the offending timestamp", "0:05" in str(e), str(e))

# The structured formats must win over the plain fallback, and each other's
# files must not be misdetected.
check("BEAT format still wins over script detection",
      len(prompt_loader.load_prompts(WORK / "prompts.txt")) == 20)

plain_with_time = WORK / "plain_mentioning_time.txt"
plain_with_time.write_text(
    "a clock showing 3:45 in the afternoon\n"
    "another plain prompt\n",
    encoding="utf-8",
)
check("a plain prompt merely mentioning a time is NOT script format",
      len(prompt_loader.load_prompts(plain_with_time)) == 2,
      len(prompt_loader.load_prompts(plain_with_time)))


heading("TEST 25: a changed prompt invalidates its recorded image")
# The web view used to trust manifest status alone, so an image generated
# from older text showed as this beat's finished output -- exactly how a
# wrong image reaches the edit unnoticed.
reset(3)
with contextlib.redirect_stdout(io.StringIO()):
    main_mod.run_batch(prompts_file=WORK / "prompts.txt", output_dir=WORK / "output")

man = Manifest.load(WORK / "output" / "manifest.json")
check("beat 1 recorded as success before editing",
      man.entry(1)["status"] == "success")

original_hash = man.entry(1)["prompt_hash"]
edited = manifest_mod.prompt_hash("completely different prompt text")
check("editing a prompt changes its hash", original_hash != edited)

# reconcile is what a run does; it must reset the edited beat, not keep it.
man.reconcile([(1, "completely different prompt text", None),
               (2, "prompt #2 describing beat 2", None),
               (3, "prompt #3 describing beat 3", None)])
check("an edited beat is reset to pending, not left success",
      man.entry(1)["status"] == "pending", man.entry(1)["status"])
check("its stale image reference is cleared",
      man.entry(1)["file"] is None, man.entry(1)["file"])
check("untouched beats keep their success",
      man.entry(2)["status"] == "success", man.entry(2)["status"])


heading("TEST 26: web UI - pre-run duration estimate from real history")
# Backs the Run panel's "N beats outstanding, ~Xh estimated" line -- shown
# BEFORE Start is pressed, from whatever this session has already finished,
# since deciding whether to kick off a 3-6 hour batch wants that answer
# upfront, not only once a run is already going.
import web_ui  # noqa: E402  (imported here, not at module load, since it
                # pulls in the whole server module for one function)

reset(0)
# Timestamps set directly rather than via a real/simulated run: manifest
# timestamps are second-resolution, and a simulated generation completes
# within the same second, which would make every duration compute as
# exactly 0 and get filtered out -- a timing artifact of the test harness,
# not of the real Flow generations (30-60s each) this estimate is for.
man = Manifest(WORK / "output" / "manifest.json")
man.reconcile([(1, "prompt one", None), (2, "prompt two", None)])
man.data["items"]["1"].update(
    status="success", started_at="2026-01-01T00:00:00+00:00",
    finished_at="2026-01-01T00:00:40+00:00")  # 40s
man.data["items"]["2"].update(
    status="success", started_at="2026-01-01T00:01:00+00:00",
    finished_at="2026-01-01T00:01:20+00:00")  # 20s
man.save()

avg = web_ui.average_duration(WORK / "output")
check("average of two known durations is their mean",
      avg == 30.0, avg)

reset(0)  # fresh output dir, no manifest at all
check("no manifest yet -> no estimate, not a crash",
      web_ui.average_duration(WORK / "output") is None)

man = Manifest.load(WORK / "output" / "manifest.json") if \
      (WORK / "output" / "manifest.json").exists() else Manifest(WORK / "output" / "manifest.json")
man.reconcile([(1, "only a pending beat, never run", None)])
man.save()
check("only-pending manifest -> no estimate (nothing finished)",
      web_ui.average_duration(WORK / "output") is None)


heading("TEST 27: dry run checks the project actually loads")
# Regression guard for the 2026-08-18 incident: a dry run used to just read
# flow.flow_page.url and report "connected", even when that URL pointed at
# a deleted project showing Flow's own error screen. It must now actually
# wait for the page to be ready, so a dead project is caught immediately
# instead of only surfacing once a real (credit-spending) run starts.
reset(3)
outcome = main_mod.run_batch(
    config.PROMPTS_FILE, config.OUTPUT_DIR, dry_run=True,
)
check("a healthy project reports a normal dry-run outcome",
      outcome.get("dry_run") is True, outcome)
check("no images generated during a dry run",
      FakeFlow.calls == [], FakeFlow.calls)

heading("TEST 28: a deleted/expired project is caught immediately, not retried")
reset(3)
FakeFlow.project_missing = True
try:
    main_mod.run_batch(config.PROMPTS_FILE, config.OUTPUT_DIR, dry_run=True)
    check("dry run against a missing project raises FlowSetupError", False)
except FlowSetupError as e:
    check("the error names the actual problem", "no longer exists" in str(e), str(e))

reset(3)
FakeFlow.project_missing = True
outcome = main_mod.run_batch(config.PROMPTS_FILE, config.OUTPUT_DIR)
check("a real run aborts immediately with a clear reason, not a generic retry loop",
      bool(outcome.get("aborted_reason")) and "no longer exists" in outcome["aborted_reason"],
      outcome.get("aborted_reason"))
check("not one generate_image call succeeded against a dead project",
      FakeFlow.calls == [], FakeFlow.calls)


print("\n" + "=" * 62)
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print("=" * 62)

shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAIL else 0)
