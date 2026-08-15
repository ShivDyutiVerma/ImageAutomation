import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def utc_now():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prompt_hash(text):

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Manifest:
    """Durable per-prompt ledger backing both resume and the final report.

    Keys are prompt indices (1-based line positions in prompts.txt) stored as
    strings, because JSON object keys are always strings. The index is the
    permanent identity of a prompt: it decides the output filename and never
    shifts when other prompts fail.
    """

    def __init__(self, path, data=None):

        self.path = Path(path)

        self.data = data or {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "items": {},
        }

    @classmethod
    def load(cls, path):

        path = Path(path)

        if not path.exists():
            return cls(path)

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"Manifest at {path} is unreadable ({e}). Move it aside to start "
                f"a fresh run, but note that resume information will be lost."
            )

        version = data.get("schema_version")

        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Manifest at {path} has schema version {version!r}, expected "
                f"{SCHEMA_VERSION}. Refusing to use it rather than risk "
                f"misinterpreting completed work."
            )

        data.setdefault("items", {})

        return cls(path, data)

    def save(self):
        """Write atomically so a crash mid-save cannot corrupt the ledger.

        This runs after every prompt during multi-hour runs, so a torn write
        here would cost the entire run's resume information.
        """

        self.data["updated_at"] = utc_now()

        self.path.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_path = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=".manifest-",
            suffix=".tmp",
        )

        try:

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.path)

        except BaseException:

            try:
                os.unlink(temp_path)
            except OSError:
                pass

            raise

    def entry(self, index):

        return self.data["items"].get(str(index))

    def reconcile(self, prompts):
        """Align the ledger with the current prompts file.

        prompts is (index, prompt_text, narration) triples. Returns a report
        describing what changed. A prompt whose text was edited is reset to
        pending: its previous image belongs to different text and would
        silently desynchronize the video edit.

        Narration is reference metadata only (see prompt_loader.py) — it is
        always refreshed to whatever's currently in the file, but it is
        deliberately excluded from the change hash, so editing narration
        alone never triggers a regeneration.
        """

        report = {"new": [], "changed": [], "stale": []}

        items = self.data["items"]

        seen = set()

        for index, text, narration in prompts:

            key = str(index)
            seen.add(key)

            digest = prompt_hash(text)
            existing = items.get(key)

            if existing is None:

                items[key] = {
                    "index": index,
                    "prompt": text,
                    "narration": narration,
                    "prompt_hash": digest,
                    "status": STATUS_PENDING,
                    "file": None,
                    "url": None,
                    "attempts": 0,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                }

                report["new"].append(index)

            elif existing.get("prompt_hash") != digest:

                existing.update(
                    {
                        "prompt": text,
                        "narration": narration,
                        "prompt_hash": digest,
                        "status": STATUS_PENDING,
                        "file": None,
                        "url": None,
                        "attempts": 0,
                        "error": None,
                        "started_at": None,
                        "finished_at": None,
                    }
                )

                report["changed"].append(index)

            else:

                existing["narration"] = narration

        for key in items:
            if key not in seen:
                report["stale"].append(int(key))

        report["stale"].sort()

        return report

    def pending_indices(self):
        """Indices still needing generation, in ascending script order."""

        return sorted(
            int(key)
            for key, item in self.data["items"].items()
            if item.get("status") != STATUS_SUCCESS
        )

    def is_complete(self, index):

        entry = self.entry(index)

        return entry is not None and entry.get("status") == STATUS_SUCCESS

    def mark_started(self, index):

        entry = self.data["items"][str(index)]
        entry["started_at"] = utc_now()

    def record_success(self, index, filename, url, attempts):

        entry = self.data["items"][str(index)]

        entry.update(
            {
                "status": STATUS_SUCCESS,
                "file": filename,
                "url": url,
                "attempts": attempts,
                "error": None,
                "finished_at": utc_now(),
            }
        )

    def record_failure(self, index, error, attempts):

        entry = self.data["items"][str(index)]

        entry.update(
            {
                "status": STATUS_FAILED,
                "attempts": attempts,
                "error": str(error)[:500],
                "finished_at": utc_now(),
            }
        )

    def counts(self):

        totals = {STATUS_SUCCESS: 0, STATUS_FAILED: 0, STATUS_PENDING: 0}

        for item in self.data["items"].values():
            status = item.get("status", STATUS_PENDING)
            totals[status] = totals.get(status, 0) + 1

        return totals

    def missing_indices(self):
        """Script beats with no image — the gaps the user must fill."""

        return sorted(
            int(key)
            for key, item in self.data["items"].items()
            if item.get("status") != STATUS_SUCCESS
        )
