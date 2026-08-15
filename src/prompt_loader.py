import re

import config

# "BEAT 8", "Beat 12:", tolerant of a trailing colon and extra whitespace.
# MULTILINE so ^/$ anchor per line — this is matched both against whole
# multi-line file contents (_looks_like_beat_blocks) and single split lines
# (_parse_beat_blocks); without it, ^/$ would only anchor to the very start
# and end of whatever string is passed in, silently breaking detection
# against the full file text.
BEAT_HEADER = re.compile(r"^\s*BEAT\s+(\d+)\s*:?\s*$", re.IGNORECASE | re.MULTILINE)

# Timestamped script format, as produced by the user's script-writing chat:
#
#   0:00 — "You just decided to watch this video."
#   Image: A stickman figure centered on pure white background...
#   Video: White frame holds for 0.5 seconds...
#
# One block per beat. Only the Image: line is an image prompt — the timestamp
# line is narration and the Video: line is motion direction for a video tool,
# neither of which should ever be sent to Flow as a prompt.
TIMESTAMP_HEADER = re.compile(
    r"^\s*(\d{1,3}:\d{2})\s*[—–\-:]\s*(.*)$", re.MULTILINE
)
IMAGE_LINE = re.compile(r"^\s*image\s*:\s*(.*)$", re.IGNORECASE)
VIDEO_LINE = re.compile(r"^\s*video\s*:\s*(.*)$", re.IGNORECASE)

QUOTE_PAIRS = {'"': '"', "“": "”"}


def _match_narration(line):
    """A line that is entirely one quoted sentence, e.g. '"Medicine had a
    problem."' — straight or curly quotes. Returns the inner text, or None if
    the line isn't a narration line (narration is optional; a beat with no
    narration line just goes straight to its prompt).
    """

    text = line.strip()

    if len(text) < 2:
        return None

    open_char, close_char = text[0], text[-1]

    if QUOTE_PAIRS.get(open_char) == close_char:
        return text[1:-1].strip()

    return None


def _looks_like_beat_blocks(raw_text):

    return bool(BEAT_HEADER.search(raw_text))


def _looks_like_script_blocks(raw_text):
    """Timestamped script format needs BOTH signals to claim a file.

    A timestamp alone is too weak — a plain prompt could legitimately mention
    one. Requiring an Image: line as well means this only ever fires on files
    actually laid out as script blocks.
    """

    if not TIMESTAMP_HEADER.search(raw_text):
        return False

    return any(IMAGE_LINE.match(line) for line in raw_text.splitlines())


def _strip_quotes(text):

    text = text.strip()

    if len(text) >= 2 and QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1].strip()

    return text


def _parse_script_blocks(raw_text, path):
    """Parse the 'timestamp / Image: / Video:' script format.

    A new beat starts at every timestamp line. Its number is the block's
    position (1-based), because this format carries no explicit beat numbers —
    the timestamps are positions in the video, not stable identities, and
    deriving a filename from '0:00' is not possible.

    Only the Image: line becomes the prompt. Video: lines are deliberately
    dropped: this tool generates images, and feeding a motion description to
    an image generator produces a wrong image while looking like it worked.
    """

    lines = raw_text.splitlines()
    n = len(lines)

    entries = []
    position = 0
    i = 0

    while i < n:

        header = TIMESTAMP_HEADER.match(lines[i])

        if not header:
            i += 1
            continue

        timestamp = header.group(1)
        narration = _strip_quotes(header.group(2)) or None
        position += 1
        i += 1

        image_parts = []

        # Consume up to the next timestamp line, collecting only Image: content.
        while i < n and not TIMESTAMP_HEADER.match(lines[i]):

            found = IMAGE_LINE.match(lines[i])

            if found:
                image_parts.append(found.group(1).strip())

            i += 1

        prompt_text = " ".join(part for part in image_parts if part)

        if not prompt_text:
            raise ValueError(
                f"The beat at {timestamp} in {path} has no 'Image:' line, so "
                f"there is no image prompt for it. Every beat needs one — add "
                f"it, or remove the beat."
            )

        entries.append((position, prompt_text, narration))

    return entries


def _parse_beat_blocks(raw_text, path):
    """Parse the 'BEAT N / "narration" / prompt paragraph' format.

    The beat number is the prompt's permanent identity here — not its
    position in the file — since that's the number the user actually thinks
    and talks about ("beat 47"), and script beats can be renumbered or have
    gaps independent of file order. Narration is reference metadata only
    (carried through to the manifest for the user's benefit); it is never
    sent to Flow and never affects the prompt's change-detection hash.
    """

    lines = raw_text.splitlines()
    n = len(lines)

    entries = []
    i = 0

    while i < n:

        header = BEAT_HEADER.match(lines[i])

        if not header:
            i += 1
            continue

        beat_number = int(header.group(1))
        i += 1

        while i < n and not lines[i].strip():
            i += 1

        narration = None

        if i < n:
            found = _match_narration(lines[i])
            if found is not None:
                narration = found
                i += 1

        while i < n and not lines[i].strip():
            i += 1

        prompt_lines = []

        while i < n and not BEAT_HEADER.match(lines[i]):
            prompt_lines.append(lines[i])
            i += 1

        prompt_text = " ".join(line.strip() for line in prompt_lines if line.strip())

        if not prompt_text:
            raise ValueError(
                f"BEAT {beat_number} in {path} has no prompt text — nothing "
                f"found between its narration line and the next BEAT header."
            )

        entries.append((beat_number, prompt_text, narration))

    seen = {}

    for beat_number, prompt_text, narration in entries:

        if beat_number in seen:
            raise ValueError(
                f"Duplicate BEAT {beat_number} in {path}. Beat numbers "
                f"become the output filename, so duplicates would silently "
                f"overwrite one image with another."
            )

        seen[beat_number] = True

    entries.sort(key=lambda e: e[0])

    return entries


def detect_gaps(prompts):
    """Missing beat numbers between the lowest and highest present.

    Informational only — gaps are a valid, common state (an intentionally
    removed beat), not an error. Surfaced so a copy-paste mistake while
    assembling the prompts file is easy to notice rather than silently
    producing a video with a beat missing.
    """

    numbers = sorted(index for index, _, _ in prompts)

    gaps = []

    for a, b in zip(numbers, numbers[1:]):
        if b - a > 1:
            gaps.extend(range(a + 1, b))

    return gaps


def load_prompts(prompts_file=None):
    """Load prompts as (index, prompt_text, narration) triples.

    Three input formats are auto-detected:

    - BEAT-block format (see _parse_beat_blocks): index is the BEAT number.
    - Timestamped script format (see _parse_script_blocks): index is the
      block's position; only its Image: line becomes the prompt.
    - Plain format: one prompt per non-blank line; index is that line's
      1-based position among non-blank lines, narration is always None.

    Order matters: plain format is the fallback and would happily accept a
    structured file, turning every timestamp and Video: line into its own
    bogus image prompt. The structured formats are therefore tested first.

    The index is the prompt's permanent identity in both cases: it decides
    the output filename and is never inferred from a running counter of
    successes, so a failed prompt can never shift a later one's number.
    """

    path = prompts_file or config.PROMPTS_FILE

    if not path.exists():
        raise FileNotFoundError(
            f"Prompts file not found: {path}"
        )

    raw_text = path.read_text(encoding="utf-8")

    if _looks_like_beat_blocks(raw_text):
        prompts = _parse_beat_blocks(raw_text, path)
    elif _looks_like_script_blocks(raw_text):
        prompts = _parse_script_blocks(raw_text, path)
    else:
        prompts = [
            (i, line.strip(), None)
            for i, line in enumerate(
                (l for l in raw_text.splitlines() if l.strip()), start=1
            )
        ]

    if not prompts:
        raise ValueError(
            f"No prompts found in {path}"
        )

    return prompts
