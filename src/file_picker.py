"""Native OS file/folder pickers for the local web UI.

A browser deliberately never reveals a real filesystem path — `<input
type=file>` reports `C:\\fakepath\\name.txt`, and the File System Access API
hands back an opaque handle. So the picker has to be opened by this process,
which is fine precisely because the web UI is bound to 127.0.0.1 and is
therefore always running on the same machine as the person using it.

The dialog runs in a short-lived subprocess rather than in the server's own
threads: tkinter is not thread-safe, and an HTTP handler thread that creates
Tk windows can wedge or crash the whole server while a multi-hour batch is
in flight. A separate process cannot take the server down with it.
"""

import subprocess
import sys
from pathlib import Path

import config

# Long enough that a user can browse unhurriedly, bounded so an abandoned
# dialog can't leak a process for the lifetime of the server.
PICKER_TIMEOUT = 300


class PickerUnavailable(RuntimeError):
    """No usable GUI toolkit — the user must type the path instead."""


def browse(kind, initial=None):
    """Open a native picker and return the chosen absolute path.

    kind is "directory" or "file". Returns None if the user cancelled, which
    is a normal outcome and must leave the current value untouched rather
    than clearing it.
    """

    if kind not in ("directory", "file"):
        raise ValueError(f"unknown picker kind: {kind!r}")

    initial = str(initial) if initial else ""

    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), kind, initial],
            capture_output=True,
            text=True,
            timeout=PICKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise PickerUnavailable(
            "The file picker was left open too long. Close it and try again, "
            "or type the path directly."
        )

    if result.returncode != 0:
        raise PickerUnavailable(
            "Could not open a file picker on this machine"
            + (f" ({result.stderr.strip().splitlines()[-1]})"
               if result.stderr.strip() else "")
            + ". Type the path into the box instead."
        )

    chosen = (result.stdout or "").strip()

    if not chosen:
        return None

    return str(Path(chosen))


def _run_dialog(kind, initial):
    """Child-process half: show the dialog, print the path, exit."""

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    # Without this the dialog can open behind the browser window, which looks
    # exactly like the button doing nothing.
    root.attributes("-topmost", True)

    start = initial or str(config.PROJECT_ROOT)

    # An initial directory that no longer exists makes some platforms open in
    # an arbitrary place; walk up to the nearest one that does.
    start_path = Path(start)
    while not start_path.is_dir() and start_path != start_path.parent:
        start_path = start_path.parent

    if kind == "directory":
        chosen = filedialog.askdirectory(
            title="Choose the output folder for this video's images",
            initialdir=str(start_path),
            mustexist=False,  # allow naming a folder that doesn't exist yet
        )
    else:
        chosen = filedialog.askopenfilename(
            title="Choose a prompts file",
            initialdir=str(start_path),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )

    root.destroy()

    sys.stdout.write(chosen or "")


if __name__ == "__main__":
    _run_dialog(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
