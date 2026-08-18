import argparse
import time
from pathlib import Path

from tqdm import tqdm

import config
from downloader import download_image
from flow_automation import FlowAutomation, FlowSetupError
from manifest import Manifest
from prompt_loader import detect_gaps, load_prompts


def format_duration(seconds):

    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"

    if minutes:
        return f"{minutes}m {secs}s"

    return f"{secs}s"


def compress_ranges(indices):
    """Render [1,2,3,7,9,10] as '1-3, 7, 9-10'.

    With 300 beats per video a raw list of missing numbers is unreadable, and
    the whole point of the summary is telling the user what to regenerate.
    """

    if not indices:
        return "none"

    indices = sorted(indices)

    groups = []
    start = previous = indices[0]

    for index in indices[1:]:

        if index == previous + 1:
            previous = index
            continue

        groups.append((start, previous))
        start = previous = index

    groups.append((start, previous))

    return ", ".join(
        str(a) if a == b else f"{a}-{b}"
        for a, b in groups
    )


def parse_index_spec(spec, valid_indices):
    """Parse '5,12,40-45' into a sorted list of ints, the inverse of
    compress_ranges. Every requested index must exist in the prompts file —
    a typo here should fail loudly, not silently do nothing or crash deep
    inside the run.
    """

    valid = set(valid_indices)
    result = set()

    for part in spec.split(","):

        part = part.strip()

        if not part:
            continue

        if "-" in part:

            bounds = part.split("-")

            if len(bounds) != 2:
                raise ValueError(f"Invalid range {part!r} in --only")

            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid range {part!r} in --only")

            if start > end:
                raise ValueError(f"Invalid range {part!r} in --only (start > end)")

            result.update(range(start, end + 1))

        else:

            try:
                result.add(int(part))
            except ValueError:
                raise ValueError(f"Invalid index {part!r} in --only")

    unknown = sorted(result - valid)

    if unknown:
        raise ValueError(
            f"--only references beat(s) not in the prompts file: "
            f"{compress_ranges(unknown)}"
        )

    return sorted(result)


def select_indices(prompts, manifest, only=None, retry_failed=False,
                   no_resume=False, limit=None):
    """Decide which prompt indices this run should process, in ascending
    script order. Exactly one selection mode applies at a time (argparse
    enforces --only / --retry-failed / --no-resume are mutually exclusive
    for the CLI); limit then trims whichever list was chosen.
    """

    all_indices = [index for index, _, _ in prompts]

    if only:
        selected = parse_index_spec(only, all_indices)

    elif retry_failed:
        selected = [
            index
            for index in all_indices
            if (entry := manifest.entry(index)) and entry.get("status") == "failed"
        ]

    elif no_resume:
        selected = all_indices

    else:
        selected = manifest.pending_indices()

    if limit is not None:
        selected = selected[:limit]

    return selected


def print_summary(manifest, elapsed, output_dir, aborted_reason=None):
    """Report on the run. Paths come from the caller, not config defaults —
    with --output-dir in play those differ, and printing the default would
    point the user at a directory the run never wrote to.
    """

    counts = manifest.counts()
    missing = manifest.missing_indices()

    print("\n" + "=" * 62)
    print("RUN SUMMARY")
    print("=" * 62)

    if aborted_reason:
        print(f"ABORTED: {aborted_reason}\n")

    print(f"  Succeeded : {counts.get('success', 0)}")
    print(f"  Failed    : {counts.get('failed', 0)}")
    print(f"  Pending   : {counts.get('pending', 0)}")
    print(f"  Elapsed   : {format_duration(elapsed)}")
    print(f"  Images    : {output_dir}")
    print(f"  Manifest  : {manifest.path}")

    if missing:
        print(f"\n  Missing beats: {compress_ranges(missing)}")
        print(f"  To regenerate just these: python src/main.py --only {compress_ranges(missing)}")
    else:
        print("\n  Every prompt has an image.")

    print("=" * 62)


def process_prompt(flow, index, prompt, manifest, output_dir):
    """Generate and download one prompt, retrying transient failures.

    output_dir is passed explicitly rather than left to download_image's
    config default: the manifest already lives in the caller's chosen
    directory, so defaulting the image somewhere else would split the two
    apart and leave resume trusting a manifest whose images aren't there.

    Returns True on success. Raises FlowSetupError upward, since that means the
    environment itself is broken and no further prompt can succeed.
    """

    stem = f"{index:03d}"

    manifest.mark_started(index)

    last_error = None

    for attempt in range(1, config.MAX_ATTEMPTS + 1):

        try:

            url = flow.generate_image(prompt)
            saved = download_image(url, stem, output_dir=output_dir)
            filename = Path(saved).name

            manifest.record_success(index, filename, url, attempt)
            manifest.save()

            tqdm.write(f"  saved {saved}")

            return True

        except FlowSetupError:
            raise

        except Exception as e:

            last_error = e

            if attempt < config.MAX_ATTEMPTS:
                backoff = config.RETRY_BACKOFF * attempt
                tqdm.write(f"  attempt {attempt} failed: {e}")
                tqdm.write(f"  retrying in {backoff}s...")
                time.sleep(backoff)

    manifest.record_failure(index, last_error, config.MAX_ATTEMPTS)
    manifest.save()

    tqdm.write(f"  FAILED after {config.MAX_ATTEMPTS} attempts: {last_error}")

    return False


def positive_int(value):
    """--limit must be at least 1.

    0 is rejected rather than interpreted: one reading ("run nothing") makes
    the flag pointless, the other ("no limit") could spend hundreds of
    images' worth of credits on someone who asked for none. Neither guess is
    safe, so refuse and let the user say what they meant.
    """

    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"must be a whole number, got {value!r}")

    if number < 1:
        raise argparse.ArgumentTypeError(
            f"must be 1 or more (got {number}); omit --limit to run every "
            f"selected beat"
        )

    return number


def build_arg_parser():

    parser = argparse.ArgumentParser(
        description=(
            "Batch-generate images from Google Flow, one image per beat, "
            "numbered to match the script beat. Prompt formats are "
            "auto-detected: BEAT-block (number from the BEAT header), "
            "timestamped script (0:00 / Image: / Video: — number from block "
            "position, only the Image: line is used), or plain one-per-line."
        )
    )

    parser.add_argument(
        "--prompts-file",
        default=str(config.PROMPTS_FILE),
        help=f"Path to the prompts file (default: {config.PROMPTS_FILE})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(config.OUTPUT_DIR),
        help=f"Directory to save images and the manifest in (default: {config.OUTPUT_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="N",
        help="Process at most N prompts this run, e.g. to work through a large batch in supervised chunks",
    )

    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--only",
        metavar="SPEC",
        help="Regenerate only these beats, e.g. '5,12,40-45' (ignores current status)",
    )
    selection.add_argument(
        "--retry-failed",
        action="store_true",
        help="Process only beats currently marked failed, skipping never-attempted ones",
    )
    selection.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the manifest and regenerate every prompt. Spends credits on everything, including already-completed beats.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect to Flow and report what would run, without generating or spending any credits",
    )

    return parser


def _interruptible_sleep(seconds, should_stop, chunk=2):
    """time.sleep that can be cut short by should_stop().

    A cooldown can be minutes long; without this, cancelling a run would
    mean waiting out the entire cooldown regardless. Returns False if cut
    short, True if it ran the full duration.
    """

    remaining = seconds

    while remaining > 0:

        if should_stop and should_stop():
            return False

        step = min(chunk, remaining)
        time.sleep(step)
        remaining -= step

    return True


def run_batch(
    prompts_file,
    output_dir,
    only=None,
    retry_failed=False,
    no_resume=False,
    limit=None,
    dry_run=False,
    on_event=None,
    should_stop=None,
):
    """Run a batch. UI-agnostic: emits structured events instead of printing.

    Both the CLI and the web UI drive this same function, so the rules that
    matter — index-derived filenames, resume, retry, the circuit breaker —
    exist in exactly one place and can't drift between the two front ends.

    on_event(dict) receives progress events; should_stop() is polled between
    prompts so a caller can cancel cleanly at a beat boundary rather than
    mid-generation (which would waste the credits already spent on it).
    """

    def emit(kind, **data):
        if on_event:
            try:
                on_event({"type": kind, **data})
            except Exception:
                pass

    prompts_file = Path(prompts_file)
    output_dir = Path(output_dir)
    manifest_file = output_dir / "manifest.json"

    prompts = load_prompts(prompts_file)
    prompt_by_index = {index: text for index, text, _ in prompts}
    narration_by_index = {index: narration for index, _, narration in prompts}

    manifest = Manifest.load(manifest_file)
    report = manifest.reconcile(prompts)
    manifest.save()

    gaps = detect_gaps(prompts)

    selected = select_indices(
        prompts, manifest,
        only=only, retry_failed=retry_failed, no_resume=no_resume, limit=limit,
    )

    # only/no_resume can deliberately re-select an already-successful beat;
    # make the credit cost of that explicit rather than silent.
    already_done = [index for index in selected if manifest.is_complete(index)]

    # Images can exist on disk that the manifest does NOT account for — e.g.
    # from runs made before this ledger existed. Regenerating them silently
    # would overwrite work and spend credits for nothing, so say so first.
    # Excludes already_done, which is the same file being reported accurately
    # by the check above; without that these two would double-report the same
    # beats under contradictory labels. Matched by stem, not a fixed
    # extension — see docs/FLOW_UI_NOTES.md on why the extension isn't assumed.
    unclaimed = [
        index
        for index in selected
        if index not in already_done and any(output_dir.glob(f"{index:03d}.*"))
    ]

    emit(
        "start",
        prompts_file=str(prompts_file),
        output_dir=str(output_dir),
        manifest_file=str(manifest_file),
        total_prompts=len(prompts),
        completed=len(prompts) - len(manifest.pending_indices()),
        selected=selected,
        changed=report["changed"],
        stale=report["stale"],
        gaps=gaps,
        unclaimed=unclaimed,
        already_done=already_done,
        no_resume=no_resume,
        dry_run=dry_run,
    )

    started = time.time()

    def result(aborted_reason=None):
        counts = manifest.counts()
        return {
            "succeeded": counts.get("success", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
            "missing": manifest.missing_indices(),
            "elapsed": time.time() - started,
            "output_dir": str(output_dir),
            "manifest_file": str(manifest_file),
            "aborted_reason": aborted_reason,
        }

    if not selected:
        outcome = result()
        emit("finished", **outcome)
        return outcome

    flow = FlowAutomation()

    if dry_run:

        url = flow.flow_page.url

        try:
            # Confirms the project actually loads (catches a deleted/expired
            # project immediately, with a clear reason) rather than just
            # reading the URL of a tab that may be showing an error screen.
            # No credits spent — this only waits for the UI to render.
            flow.wait_until_ready()
        finally:
            flow.close()

        outcome = result()
        outcome["dry_run"] = True
        outcome["connected_url"] = url
        emit("dry_run", connected_url=url, selected=selected)
        emit("finished", **outcome)
        return outcome

    consecutive_failures = 0
    cooldowns_used = 0
    aborted_reason = None

    try:

        for position, index in enumerate(selected, start=1):

            if should_stop and should_stop():
                aborted_reason = "Stopped by request. Re-run to resume."
                break

            prompt = prompt_by_index[index]
            narration = narration_by_index.get(index)

            emit(
                "beat_start",
                index=index,
                position=position,
                total=len(selected),
                prompt=prompt,
                narration=narration,
            )

            beat_started = time.time()

            succeeded = process_prompt(flow, index, prompt, manifest, output_dir)

            entry = manifest.entry(index) or {}

            emit(
                "beat_done",
                index=index,
                position=position,
                total=len(selected),
                succeeded=succeeded,
                file=entry.get("file"),
                error=entry.get("error"),
                seconds=time.time() - beat_started,
            )

            if succeeded:
                # A real success proves the account/session is actually
                # working again, not just that this one beat got lucky —
                # full trust restored, including the cooldown budget.
                consecutive_failures = 0
                cooldowns_used = 0
            else:
                consecutive_failures += 1

            if consecutive_failures >= config.CONSECUTIVE_FAILURE_LIMIT:

                if cooldowns_used < config.MAX_COOLDOWNS:

                    cooldowns_used += 1

                    emit(
                        "cooldown",
                        attempt=cooldowns_used,
                        max_attempts=config.MAX_COOLDOWNS,
                        seconds=config.COOLDOWN_SECONDS,
                    )

                    if not _interruptible_sleep(
                        config.COOLDOWN_SECONDS, should_stop
                    ):
                        aborted_reason = (
                            "Stopped by request during cooldown. Re-run to resume."
                        )
                        break

                    # Give the next beat a full run at it rather than
                    # counting the cooldown itself as part of the streak.
                    consecutive_failures = 0
                    continue

                aborted_reason = (
                    f"{config.CONSECUTIVE_FAILURE_LIMIT} prompts failed in a row, "
                    f"even after {cooldowns_used} cooldown "
                    f"{'retry' if cooldowns_used == 1 else 'retries'} of "
                    f"{config.COOLDOWN_SECONDS}s each. This usually means the "
                    f"Google session expired or the account is genuinely out of "
                    f"credits. Completed work is saved — fix the cause and "
                    f"re-run to continue."
                )
                break

            if index != selected[-1]:
                time.sleep(config.DELAY_BETWEEN_PROMPTS)

    except FlowSetupError as e:
        aborted_reason = str(e)

    except KeyboardInterrupt:
        aborted_reason = "Interrupted. Re-run the same command to resume."

    finally:
        manifest.save()
        flow.close()

    outcome = result(aborted_reason)
    emit("finished", **outcome)
    return outcome


def main(argv=None):
    """CLI front end: parse args, render run_batch's events to the terminal."""

    args = build_arg_parser().parse_args(argv)

    prompts_file = config.resolve_path(args.prompts_file)
    output_dir = config.resolve_path(args.output_dir)

    if args.no_resume:
        print(
            "  WARNING: --no-resume ignores the manifest — every prompt will be "
            "regenerated, including ones that already succeeded, spending "
            "credits on all of them."
        )

    state = {"bar": None}

    def on_event(event):

        kind = event["type"]

        if kind == "start":

            print("=" * 62)
            print("FLOW IMAGE AUTOMATION" + ("  [DRY RUN]" if event["dry_run"] else ""))
            print("=" * 62)
            print(f"  Prompts   : {event['total_prompts']} ({event['prompts_file']})")
            print(f"  Completed : {event['completed']}")
            print(f"  Selected  : {len(event['selected'])} beat(s) this run"
                  + (f" -> {compress_ranges(event['selected'])}"
                     if event["selected"] else ""))

            if event["changed"]:
                print(f"  Changed   : {compress_ranges(event['changed'])} "
                      f"(prompt text edited — will regenerate)")

            if event["stale"]:
                print(f"  Stale     : {compress_ranges(event['stale'])} "
                      f"(in manifest but no longer in prompts file)")

            if event["gaps"]:
                print(f"  Gaps      : beat(s) {compress_ranges(event['gaps'])} missing "
                      f"from the numbering — check this is intentional, not a "
                      f"copy-paste slip")

            if event["unclaimed"]:
                print(
                    f"\n  NOTE: {len(event['unclaimed'])} image file(s) already exist "
                    f"for beats {compress_ranges(event['unclaimed'])}\n"
                    f"        but have no manifest record, so they will be regenerated "
                    f"and overwritten.\n"
                    f"        If they are already good, move them aside first to avoid "
                    f"spending credits."
                )

            if event["already_done"]:
                print(
                    f"\n  NOTE: {len(event['already_done'])} selected beat(s) already "
                    f"have a successful image: "
                    f"{compress_ranges(event['already_done'])}\n"
                    f"        Continuing will regenerate and overwrite them, spending "
                    f"credits again."
                )

            if not event["selected"]:
                print("\nNothing to do — no beats matched the current selection.")
            else:
                print("=" * 62)
                state["bar"] = tqdm(total=len(event["selected"]), unit="image")

        elif kind == "dry_run":
            print(f"\nConnected OK: {event['connected_url']}")
            print(f"Would generate {len(event['selected'])} image(s): "
                  f"{compress_ranges(event['selected'])}")
            print("No credits spent — this was a dry run.")

        elif kind == "beat_start":
            bar = state["bar"]
            if bar:
                bar.set_description(f"beat {event['index']:03d}")
            label = (f'"{event["narration"]}"' if event["narration"]
                     else event["prompt"][:100])
            tqdm.write(f"\nbeat {event['index']:03d}: {label}")

        elif kind == "beat_done":
            if state["bar"]:
                state["bar"].update(1)

        elif kind == "cooldown":
            tqdm.write(
                f"\n{config.CONSECUTIVE_FAILURE_LIMIT} beats failed in a row — "
                f"pausing {event['seconds']}s in case it's transient "
                f"(cooldown {event['attempt']}/{event['max_attempts']}), "
                f"then trying again. Ctrl+C to stop instead."
            )

        elif kind == "finished":
            if state["bar"]:
                state["bar"].close()
                state["bar"] = None

    try:
        outcome = run_batch(
            prompts_file=prompts_file,
            output_dir=output_dir,
            only=args.only,
            retry_failed=args.retry_failed,
            no_resume=args.no_resume,
            limit=args.limit,
            dry_run=args.dry_run,
            on_event=on_event,
        )
    finally:
        if state["bar"]:
            state["bar"].close()

    if outcome.get("dry_run"):
        return

    manifest = Manifest.load(Path(outcome["manifest_file"]))
    print_summary(
        manifest,
        outcome["elapsed"],
        outcome["output_dir"],
        outcome["aborted_reason"],
    )


if __name__ == "__main__":

    try:
        main()
    except (FlowSetupError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"\nERROR: {e}")
        raise SystemExit(1)
