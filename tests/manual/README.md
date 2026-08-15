# Manual scripts

These are **not** automated tests and `pytest` does not run them. Each needs a real Chrome with a live, logged-in Google session. They exist to set up and inspect that session.

| Script | Purpose | Costs credits? |
|---|---|---|
| `flow_profile_setup.py` | One-time per machine: opens your real Chrome (auto-detected on Windows/macOS/Linux) so you can sign into Google/Flow by hand, creating the browser profile. Same login flow `python src/setup.py` walks through as part of its full readiness check — use this script directly if you just want the login step. | No |
| `inspect_flow_dom.py` | Read-only inspection of an open Flow project: verifies selectors, counts images, measures scan cost, and detects DOM virtualization. Run this whenever the automation starts failing mysteriously. | **No** |

## Typical use

```bash
# First time only, on any new machine
python tests/manual/flow_profile_setup.py

# Everything after that is automatic — just run:
python src/web_ui.py      # web UI at http://127.0.0.1:8765
python src/main.py        # or the CLI

# Sanity-check the UI before a long run
python tests/manual/inspect_flow_dom.py
```

Chrome no longer needs to be launched by hand — `python src/main.py` / `python src/web_ui.py` start it automatically against the already-authenticated profile from the login step above. See `python src/setup.py --check` for a full readiness report, or set `FLOW_AUTO_LAUNCH_CHROME=false` in `.env` to manage Chrome yourself.

`inspect_flow_dom.py` is the first thing to run when a run fails unexpectedly — it distinguishes "Google changed the UI" from "our bug" without spending a single credit.
