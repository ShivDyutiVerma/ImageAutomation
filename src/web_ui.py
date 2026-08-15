"""Local web UI for the Flow image automation.

    python src/web_ui.py

Then open http://127.0.0.1:8765

Deliberately stdlib-only (no Flask/FastAPI) — this is a single-user local
control panel, and the project keeps its dependency list honest.

It drives the same run_batch() the CLI does, so the rules that matter
(index-derived filenames, resume, retry, circuit breaker) can't drift
between the two front ends.
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import config
import file_picker
import main as batch
import setup_check
from manifest import Manifest, prompt_hash
from prompt_loader import detect_gaps, load_prompts

WEB_DIR = Path(__file__).resolve().parent / "web"

# Bound to loopback only, never 0.0.0.0: this endpoint can start runs that
# spend real Pro credits and drive a logged-in browser, so it must not be
# reachable from anywhere else on the network.
HOST = "127.0.0.1"
PORT = 8765

# Output filenames are always a zero-padded beat number plus an extension
# decided from the downloaded content. Anything else is not ours to serve.
IMAGE_NAME = re.compile(r"^\d{3,}\.[A-Za-z0-9]{1,5}$")

MAX_BODY_BYTES = 5 * 1024 * 1024


class RunState:
    """Tracks the single in-flight run. Only one may exist at a time —
    Flow is one interactive browser session, and two concurrent runs would
    interleave prompts and corrupt the beat-to-image mapping.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.stop_requested = False
        self.log = []
        self.current = None
        self.started_at = None
        self.finished_at = None
        self.result = None
        self.error = None
        self.durations = []
        self.selected = []
        self.done_count = 0
        # Which session the in-flight run belongs to, so a page reload during
        # a multi-hour batch reattaches to it instead of blanking to the
        # session chooser and losing sight of a run that is still going.
        self.active_session = None

    def snapshot(self):

        with self.lock:

            eta = None

            if self.running and self.durations and self.selected:
                average = sum(self.durations) / len(self.durations)
                remaining = max(len(self.selected) - self.done_count, 0)
                eta = average * remaining

            return {
                "running": self.running,
                "stop_requested": self.stop_requested,
                "log": self.log[-200:],
                "current": self.current,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "result": self.result,
                "error": self.error,
                "selected_count": len(self.selected),
                "done_count": self.done_count,
                "eta_seconds": eta,
            }

    def add_log(self, message, level="info"):
        with self.lock:
            self.log.append(
                {"time": time.strftime("%H:%M:%S"), "message": message, "level": level}
            )


STATE = RunState()


def _run_worker(prompts_file, output_dir, only, retry_failed, no_resume, limit, dry_run):

    def on_event(event):

        kind = event["type"]

        if kind == "start":

            with STATE.lock:
                STATE.selected = event["selected"]
                STATE.done_count = 0

            STATE.add_log(
                f"{event['total_prompts']} prompt(s) loaded, "
                f"{len(event['selected'])} selected for this run"
                + ("  [DRY RUN]" if event["dry_run"] else "")
            )

            for label, key, level in [
                ("Prompt text edited, will regenerate", "changed", "warn"),
                ("In manifest but no longer in prompts file", "stale", "warn"),
                ("Missing from beat numbering", "gaps", "warn"),
                ("Existing image files with no manifest record (will be overwritten)",
                 "unclaimed", "warn"),
                ("Already have an image (will be regenerated, spending credits)",
                 "already_done", "warn"),
            ]:
                if event.get(key):
                    STATE.add_log(
                        f"{label}: {batch.compress_ranges(event[key])}", level
                    )

            if not event["selected"]:
                STATE.add_log("Nothing to do for this selection.", "warn")

        elif kind == "dry_run":
            STATE.add_log(f"Connected OK: {event['connected_url']}", "ok")
            STATE.add_log("Dry run only — no credits spent.", "ok")

        elif kind == "beat_start":
            with STATE.lock:
                STATE.current = {
                    "index": event["index"],
                    "position": event["position"],
                    "total": event["total"],
                    "narration": event["narration"],
                    "prompt": event["prompt"][:300],
                }
            label = event["narration"] or event["prompt"][:80]
            STATE.add_log(
                f"beat {event['index']:03d} ({event['position']}/{event['total']}): {label}"
            )

        elif kind == "beat_done":

            with STATE.lock:
                STATE.done_count = event["position"]
                if event["succeeded"]:
                    STATE.durations.append(event["seconds"])

            if event["succeeded"]:
                STATE.add_log(
                    f"beat {event['index']:03d} saved as {event['file']} "
                    f"({event['seconds']:.0f}s)",
                    "ok",
                )
            else:
                STATE.add_log(
                    f"beat {event['index']:03d} FAILED: {event['error']}", "error"
                )

        elif kind == "cooldown":

            with STATE.lock:
                STATE.current = {
                    "cooldown": True,
                    "attempt": event["attempt"],
                    "max_attempts": event["max_attempts"],
                    "seconds": event["seconds"],
                }

            STATE.add_log(
                f"Several beats failed in a row — pausing "
                f"{event['seconds']}s in case it's transient (cooldown "
                f"{event['attempt']}/{event['max_attempts']}), then trying "
                f"again. Stop cancels immediately instead of waiting it out.",
                "warn",
            )

        elif kind == "finished":
            with STATE.lock:
                STATE.current = None

    try:
        result = batch.run_batch(
            prompts_file=prompts_file,
            output_dir=output_dir,
            only=only,
            retry_failed=retry_failed,
            no_resume=no_resume,
            limit=limit,
            dry_run=dry_run,
            on_event=on_event,
            should_stop=lambda: STATE.stop_requested,
        )

        with STATE.lock:
            STATE.result = result

        if result.get("aborted_reason"):
            STATE.add_log(f"ABORTED: {result['aborted_reason']}", "error")
        else:
            STATE.add_log(
                f"Finished: {result['succeeded']} succeeded, {result['failed']} failed",
                "ok",
            )

    except Exception as e:
        with STATE.lock:
            STATE.error = f"{type(e).__name__}: {e}"
        STATE.add_log(f"ERROR: {type(e).__name__}: {e}", "error")

    finally:
        with STATE.lock:
            STATE.running = False
            STATE.stop_requested = False
            STATE.finished_at = time.time()
            STATE.current = None


def session_paths(slug):
    """The canonical prompts/output pair for a session name.

    Must match _handle_new_session exactly — if discovery and creation ever
    disagreed about where a session lives, resuming one would silently point
    at a different folder than the one its images are in.
    """

    return (
        config.PROJECT_ROOT / "prompts" / f"{slug}.txt",
        config.PROJECT_ROOT / "output" / slug,
    )


def list_sessions():
    """Every session on disk, newest first, with enough progress to choose by.

    A session is a prompts file paired with its output folder by name. Counts
    come from the manifest where one exists, and are reported as 'done out of
    total' so a half-finished video is obvious at a glance.
    """

    prompts_dir = config.PROJECT_ROOT / "prompts"

    if not prompts_dir.is_dir():
        return []

    sessions = []

    for path in prompts_dir.glob("*.txt"):

        slug = path.stem
        _, output_dir = session_paths(slug)

        # The legacy default keeps pointing at the configured output dir
        # rather than output/prompts/, so existing setups aren't orphaned.
        if slug == "prompts":
            output_dir = config.OUTPUT_DIR

        wanted = {}

        try:
            prompts = load_prompts(path)
            total = len(prompts)
            wanted = {str(i): prompt_hash(text) for i, text, _ in prompts}
            error = None
        except Exception as e:
            total = 0
            error = f"{type(e).__name__}: {e}"

        done = 0
        manifest_file = output_dir / "manifest.json"

        if manifest_file.exists():
            try:
                items = Manifest.load(manifest_file).data["items"]
                # Same rule the gallery uses: an image counts as done only if
                # it was generated from the text the file holds now. Otherwise
                # the chooser would advertise progress a run is about to redo.
                done = sum(
                    1 for key, item in items.items()
                    if item.get("status") == "success"
                    and key in wanted
                    and item.get("prompt_hash") == wanted[key]
                )
            except Exception:
                done = 0

        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0

        sessions.append(
            {
                "slug": slug,
                "prompts_file": str(path),
                "output_dir": str(output_dir),
                "total": total,
                "done": done,
                "error": error,
                "modified": modified,
            }
        )

    sessions.sort(key=lambda s: s["modified"], reverse=True)

    return sessions


def build_beats(prompts_file, output_dir):
    """Merge the prompts file with the manifest into one per-beat view.

    Returns (beats, error). A prompts file that won't parse is reported
    rather than raised, so the page can show the problem while the user is
    still editing rather than just failing to load.
    """

    try:
        prompts = load_prompts(Path(prompts_file))
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    output_dir = Path(output_dir)
    manifest_file = output_dir / "manifest.json"

    entries = {}

    if manifest_file.exists():
        try:
            entries = Manifest.load(manifest_file).data["items"]
        except Exception as e:
            return [], f"Manifest unreadable: {e}"

    beats = []

    for index, text, narration in prompts:

        entry = entries.get(str(index), {})
        status = entry.get("status", "pending")
        filename = entry.get("file")

        # An image only belongs to this beat if it was generated from the
        # text the file holds *now*. When the prompts file is replaced (a new
        # video reusing an output folder, or an edited beat), the manifest
        # still points at the previous run's image — showing that as this
        # beat's finished output is how a wrong image silently reaches the
        # edit. The run itself resets these on reconcile; the view must agree
        # with the run rather than report work that is about to be redone.
        if entry and entry.get("prompt_hash") != prompt_hash(text):
            status = "stale"
            filename = None

        # Trust the file only if it's actually on disk — a manifest can
        # outlive the images if a directory was moved or cleaned out.
        if filename and not (output_dir / filename).exists():
            filename = None
            if status == "success":
                status = "missing_file"

        beats.append(
            {
                "index": index,
                "narration": narration,
                "prompt": text,
                "status": status,
                "file": filename,
                "attempts": entry.get("attempts", 0),
                "error": entry.get("error"),
            }
        )

    return beats, None


def average_duration(output_dir):
    """Mean generation time from this session's own completed beats, or None
    if none have finished yet.

    Used to turn "87 pending" into an actual time estimate before Start is
    pressed — the run's own live ETA (main.py's rolling average) only exists
    once a run is already in progress, but a user deciding whether to kick
    off a 3-6 hour batch wants that estimate beforehand, from history.
    """

    manifest_file = Path(output_dir) / "manifest.json"

    if not manifest_file.exists():
        return None

    try:
        items = Manifest.load(manifest_file).data["items"]
    except Exception:
        return None

    durations = []

    for item in items.values():

        if item.get("status") != "success":
            continue

        started, finished = item.get("started_at"), item.get("finished_at")

        if not started or not finished:
            continue

        try:
            delta = (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds()
        except ValueError:
            continue

        if delta > 0:
            durations.append(delta)

    if not durations:
        return None

    return sum(durations) / len(durations)


def reveal_in_file_manager(path):
    """Open the platform's file manager with `path` selected/open.

    Same platform branch shape as chrome_launcher.py, for the same reason:
    this project supports Windows/macOS/Linux but is only actually run and
    verified on Windows, so each branch is implemented against documented
    platform behavior rather than assumed.
    """

    path = str(path)

    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - local-only server, local-only path
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Handler(BaseHTTPRequestHandler):

    server_version = "FlowAutomationUI"

    def log_message(self, *args):
        pass  # keep the console clean for run output

    # -- helpers ---------------------------------------------------------

    def _send(self, status, body, content_type="application/json", extra_headers=None):

        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")

        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)

        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):

        length = int(self.headers.get("Content-Length") or 0)

        if length <= 0:
            return {}

        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large")

        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _paths(self, data):

        prompts_file = config.resolve_path(
            data.get("prompts_file") or str(config.PROMPTS_FILE)
        )
        output_dir = config.resolve_path(
            data.get("output_dir") or str(config.OUTPUT_DIR)
        )

        return prompts_file, output_dir

    # -- routes ----------------------------------------------------------

    def do_GET(self):

        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            page = WEB_DIR / "index.html"
            if not page.exists():
                return self._send(500, "index.html missing", "text/plain")
            return self._send(200, page.read_text(encoding="utf-8"), "text/html; charset=utf-8")

        if route == "/api/state":
            return self._handle_state(parsed)

        if route == "/api/prompts":
            return self._handle_read_prompts(parsed)

        if route == "/api/sessions":
            # A run in progress owns the page: the chooser must not be able to
            # strand a multi-hour batch behind a UI showing a different video.
            with STATE.lock:
                active = STATE.active_session if STATE.running else None

            return self._send(
                200, {"sessions": list_sessions(), "active": active}
            )

        if route == "/api/setup":
            from urllib.parse import parse_qs

            query = parse_qs(parsed.query)
            prompts_file = query.get("prompts_file", [None])[0]

            return self._send(200, setup_check.run_all(prompts_file=prompts_file))

        if route.startswith("/img/"):
            return self._handle_image(route)

        return self._send(404, {"error": "not found"})

    def _handle_state(self, parsed):

        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        prompts_file = config.resolve_path(
            query.get("prompts_file", [str(config.PROMPTS_FILE)])[0]
        )
        output_dir = config.resolve_path(
            query.get("output_dir", [str(config.OUTPUT_DIR)])[0]
        )

        beats, error = build_beats(prompts_file, output_dir)

        gaps = []
        if not error:
            try:
                gaps = detect_gaps(load_prompts(prompts_file))
            except Exception:
                gaps = []

        counts = {"success": 0, "failed": 0, "pending": 0,
                  "missing_file": 0, "stale": 0}
        for beat in beats:
            counts[beat["status"]] = counts.get(beat["status"], 0) + 1

        return self._send(
            200,
            {
                "run": STATE.snapshot(),
                "beats": beats,
                "counts": counts,
                "gaps": gaps,
                "prompts_error": error,
                "prompts_file": str(prompts_file),
                "output_dir": str(output_dir),
                "prompts_exists": prompts_file.exists(),
                # Where files actually land, resolved to an absolute path, so
                # the UI can state it plainly instead of echoing back whatever
                # relative text happens to be typed in the box.
                "output_dir_exists": output_dir.is_dir(),
                "output_file_count": (
                    sum(1 for p in output_dir.iterdir()
                        if p.is_file() and p.suffix.lower() != ".json")
                    if output_dir.is_dir() else 0
                ),
                "avg_duration_seconds": average_duration(output_dir),
            },
        )

    def _handle_read_prompts(self, parsed):
        """Return the prompts file verbatim.

        Deliberately the raw bytes rather than anything reconstructed from
        parsed beats — round-tripping through the parser would rewrite a
        plain one-per-line file into BEAT format and quietly change the
        user's chosen format out from under them.
        """

        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        prompts_file = config.resolve_path(
            query.get("prompts_file", [str(config.PROMPTS_FILE)])[0]
        )

        if not prompts_file.exists():
            return self._send(200, {"exists": False, "text": ""})

        try:
            text = prompts_file.read_text(encoding="utf-8")
        except OSError as e:
            return self._send(500, {"error": f"could not read prompts file: {e}"})

        return self._send(200, {"exists": True, "text": text})

    def _handle_image(self, route):

        name = unquote(route[len("/img/"):])
        parts = name.split("/", 1)

        if len(parts) != 2:
            return self._send(404, {"error": "not found"})

        # The output directory arrives as a URL-safe token rather than a raw
        # path so a crafted request can't walk the filesystem.
        try:
            output_dir = Path(bytes.fromhex(parts[0]).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "bad path token"})

        filename = parts[1]

        if not IMAGE_NAME.match(filename):
            return self._send(404, {"error": "not found"})

        path = output_dir / filename

        try:
            resolved = path.resolve()
            if resolved.parent != output_dir.resolve():
                return self._send(404, {"error": "not found"})
        except Exception:
            return self._send(404, {"error": "not found"})

        if not resolved.is_file():
            return self._send(404, {"error": "not found"})

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        return self._send(200, resolved.read_bytes(), content_type)

    def do_POST(self):

        route = urlparse(self.path).path

        try:
            data = self._read_json()
        except Exception as e:
            return self._send(400, {"error": f"bad request body: {e}"})

        if route == "/api/prompts":
            return self._handle_save_prompts(data)

        if route == "/api/start":
            return self._handle_start(data)

        if route == "/api/stop":
            return self._handle_stop()

        if route == "/api/setup/login":
            return self._handle_login()

        if route == "/api/setup/projects":
            return self._handle_projects()

        if route == "/api/setup/project":
            return self._handle_pick_project(data)

        if route == "/api/setup/new-session":
            return self._handle_new_session(data)

        if route == "/api/browse":
            return self._handle_browse(data)

        if route == "/api/open-folder":
            return self._handle_open_folder(data)

        return self._send(404, {"error": "not found"})

    def _handle_open_folder(self, data):
        """Reveal the output folder in the OS file manager.

        Read-only as far as this app is concerned — it hands off to the OS
        and never touches Flow or the run state, so it stays available while
        a batch is running.
        """

        output_dir = config.resolve_path(data.get("output_dir") or "")

        if not output_dir.is_dir():
            return self._send(
                404,
                {"error": f"Folder does not exist yet: {output_dir}"},
            )

        try:
            reveal_in_file_manager(output_dir)
        except Exception as e:
            return self._send(
                500, {"error": f"Could not open a file manager: {e}"}
            )

        return self._send(200, {"opened": str(output_dir)})

    def _handle_browse(self, data):
        """Open a native file/folder picker on this machine.

        Deliberately not gated behind the run lock: choosing where files go
        touches nothing Flow-related and must stay usable while a batch runs.
        """

        kind = data.get("kind")

        if kind not in ("directory", "file"):
            return self._send(400, {"error": "kind must be 'directory' or 'file'"})

        try:
            chosen = file_picker.browse(kind, data.get("initial"))
        except file_picker.PickerUnavailable as e:
            return self._send(503, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        # A cancelled dialog is a normal outcome, not an error — the caller
        # keeps whatever path was already set.
        return self._send(200, {"path": chosen, "cancelled": chosen is None})

    def _handle_login(self):
        """Open Chrome for the one-time Google sign-in and wait for it.

        Runs in a worker thread and reuses the same single-run state, so a
        sign-in and a batch can never be in flight at once — both drive the
        same browser.
        """

        with STATE.lock:

            if STATE.running:
                return self._send(409, {"error": "Something is already running."})

            STATE.running = True
            STATE.stop_requested = False
            STATE.log = []
            STATE.current = None
            STATE.result = None
            STATE.error = None
            STATE.selected = []
            STATE.done_count = 0
            STATE.started_at = time.time()
            STATE.finished_at = None

        def worker():
            STATE.add_log("Opening Chrome — sign into Google in that window.", "warn")
            try:
                ok = setup_check.wait_for_login(
                    on_progress=lambda s: (
                        STATE.add_log(f"still waiting for sign-in ({s}s)…")
                        if s and s % 30 == 0 else None
                    )
                )
                if ok:
                    STATE.add_log(
                        "Signed in. The session is saved for future runs.", "ok"
                    )
                else:
                    STATE.add_log(
                        "Timed out without seeing a signed-in Flow page. If you did "
                        "sign in, click Check again.",
                        "error",
                    )
            except Exception as e:
                with STATE.lock:
                    STATE.error = f"{type(e).__name__}: {e}"
                STATE.add_log(f"Sign-in failed: {type(e).__name__}: {e}", "error")
            finally:
                with STATE.lock:
                    STATE.running = False
                    STATE.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()

        return self._send(200, {"started": True})

    def _handle_projects(self):

        with STATE.lock:
            if STATE.running:
                return self._send(409, {"error": "Something is already running."})

        try:
            return self._send(200, {"projects": setup_check.discover_projects()})
        except Exception as e:
            return self._send(
                500,
                {"error": f"Could not list projects ({type(e).__name__}: {e}). "
                          f"Chrome may not be running or signed in yet."},
            )

    def _handle_pick_project(self, data):

        url = (data.get("url") or "").strip()

        if not url:
            return self._send(400, {"error": "No project URL given."})

        if config.FLOW_PROJECT_URL_MARKER not in url:
            return self._send(
                400,
                {"error": f"That doesn't look like a Flow project URL "
                          f"(expected it to contain "
                          f"'{config.FLOW_PROJECT_URL_MARKER}')."},
            )

        try:
            path = setup_check.update_env("FLOW_PROJECT_URL", url)
        except OSError as e:
            return self._send(500, {"error": f"Could not write .env: {e}"})

        # config was read at import time; keep the live process consistent
        # with what was just written rather than needing a restart.
        config.PROJECT_URL = url

        return self._send(200, {"saved": True, "env_file": str(path), "url": url})

    def _handle_new_session(self, data):
        """Start a new video: a fresh Flow project AND fresh local prompts/
        output paths, all in one action.

        Both halves matter — a new Flow project alone doesn't stop two
        videos' beat numbers colliding in the *same* local output folder,
        and a new local folder alone doesn't stop images landing in the
        wrong Flow project. Solving only one still lets files clash.
        """

        with STATE.lock:

            if STATE.running:
                return self._send(409, {"error": "Something is already running."})

            STATE.running = True
            STATE.stop_requested = False
            STATE.log = []
            STATE.current = None
            STATE.result = None
            STATE.error = None
            STATE.selected = []
            STATE.done_count = 0
            STATE.started_at = time.time()
            STATE.finished_at = None

        raw_name = (data.get("name") or "").strip()
        slug = setup_check.safe_session_name(raw_name)

        prompts_file = config.PROJECT_ROOT / "prompts" / f"{slug}.txt"
        output_dir = config.PROJECT_ROOT / "output" / slug

        if prompts_file.exists() or output_dir.exists():
            with STATE.lock:
                STATE.running = False
            return self._send(
                409,
                {"error": f"A session named '{slug}' already exists "
                          f"({prompts_file.name}). Choose a different name."},
            )

        def worker():
            STATE.add_log(f"Creating a new Flow project for '{slug}'…", "warn")
            STATE.add_log(
                "Confirmed live: this can take anywhere from a few seconds to "
                "over two minutes — please wait rather than clicking again.",
            )
            try:
                url = setup_check.create_new_project()
                setup_check.update_env("FLOW_PROJECT_URL", url)
                config.PROJECT_URL = url

                with STATE.lock:
                    STATE.result = {
                        "new_session": True,
                        "slug": slug,
                        "prompts_file": str(prompts_file),
                        "output_dir": str(output_dir),
                        "project_url": url,
                    }

                STATE.add_log(f"New project ready: {url}", "ok")
                STATE.add_log(
                    f"Write this video's beats into {prompts_file.name} and save.",
                    "ok",
                )

            except Exception as e:
                # The local half of this (a unique prompts/output path) is
                # still valid and useful even if Flow's side timed out — and
                # confirmed live that a "timed out" project sometimes finishes
                # moments later anyway. Hand back a partial result rather than
                # discarding it: the project can be attached with "Choose
                # project" once it appears, without redoing the naming.
                with STATE.lock:
                    STATE.error = f"{type(e).__name__}: {e}"
                    STATE.result = {
                        "new_session": True,
                        "slug": slug,
                        "prompts_file": str(prompts_file),
                        "output_dir": str(output_dir),
                        "project_url": None,
                    }
                STATE.add_log(f"Could not confirm the new project: {e}", "error")
                STATE.add_log(
                    "The local prompts/output paths are still set up. Wait a "
                    "minute, then use \"Choose project\" — the project may "
                    "have finished after all and will show up there.",
                    "warn",
                )

            finally:
                with STATE.lock:
                    STATE.running = False
                    STATE.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()

        return self._send(200, {"started": True, "slug": slug})

    def _handle_save_prompts(self, data):

        prompts_file, _ = self._paths(data)
        text = data.get("text", "")

        try:
            prompts_file.parent.mkdir(parents=True, exist_ok=True)
            prompts_file.write_text(text, encoding="utf-8")
        except OSError as e:
            return self._send(500, {"error": f"could not write prompts file: {e}"})

        try:
            prompts = load_prompts(prompts_file)
        except Exception as e:
            return self._send(
                200,
                {"saved": True, "valid": False, "error": f"{type(e).__name__}: {e}"},
            )

        return self._send(
            200,
            {
                "saved": True,
                "valid": True,
                "count": len(prompts),
                "gaps": detect_gaps(prompts),
            },
        )

    def _handle_start(self, data):

        with STATE.lock:

            if STATE.running:
                return self._send(
                    409,
                    {"error": "A run is already in progress. Stop it first."},
                )

            prompts_file, output_dir = self._paths(data)

            if not prompts_file.exists():
                return self._send(400, {"error": f"Prompts file not found: {prompts_file}"})

            mode = data.get("mode", "resume")
            limit = data.get("limit")

            # An empty field means "no limit". A supplied value goes through
            # the same validation as the CLI's --limit, so 0 is rejected in
            # both front ends rather than meaning "nothing" in one and
            # "everything" in the other.
            if limit in (None, ""):
                limit = None
            else:
                try:
                    limit = batch.positive_int(limit)
                except Exception as e:
                    return self._send(400, {"error": f"limit {e}"})

            STATE.running = True
            STATE.stop_requested = False
            STATE.log = []
            STATE.current = None
            STATE.result = None
            STATE.error = None
            STATE.durations = []
            STATE.selected = []
            STATE.done_count = 0
            STATE.started_at = time.time()
            STATE.finished_at = None
            STATE.active_session = {
                "prompts_file": str(prompts_file),
                "output_dir": str(output_dir),
            }

            STATE.thread = threading.Thread(
                target=_run_worker,
                kwargs={
                    "prompts_file": prompts_file,
                    "output_dir": output_dir,
                    "only": data.get("only") or None,
                    "retry_failed": mode == "retry_failed",
                    "no_resume": mode == "no_resume",
                    "limit": limit,
                    "dry_run": bool(data.get("dry_run")),
                },
                daemon=True,
            )
            STATE.thread.start()

        return self._send(200, {"started": True})

    def _handle_stop(self):

        with STATE.lock:

            if not STATE.running:
                return self._send(400, {"error": "No run in progress."})

            STATE.stop_requested = True

        STATE.add_log(
            "Stop requested — will finish the current beat, then stop.", "warn"
        )

        return self._send(200, {"stopping": True})


def serve(host=HOST, port=PORT):

    server = ThreadingHTTPServer((host, port), Handler)

    print("=" * 62)
    print("FLOW IMAGE AUTOMATION — web UI")
    print("=" * 62)
    print(f"  Open:     http://{host}:{port}")
    print(f"  Prompts:  {config.PROMPTS_FILE}")
    print(f"  Output:   {config.OUTPUT_DIR}")
    print()
    print("  Local only — not reachable from other machines.")
    print("  Ctrl+C to stop.")
    print("=" * 62)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    serve()
