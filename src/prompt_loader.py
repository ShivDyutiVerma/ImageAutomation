import re

import config

# "BEAT 8", "PART 12:", "BEAT 3 — ...". Tolerant of a colon, hyphen, or
# em/en dash separator, with optional inline narration after it — e.g.
# BEAT 1: "Egypt looks like one of the worst places on Earth" or
# BEAT 1 — "Yesterday, you were confident." — which group(2) captures.
# "PART" is accepted as a synonym for "BEAT" since some scripts use it
# instead. A bare "BEAT 8" with no separator must still end the line, so
# plain prose that happens to start with "BEAT <number>" doesn't get
# mistaken for a header. MULTILINE so ^/$ anchor per line — this is matched
# both against whole multi-line file contents (_looks_like_beat_blocks) and
# single split lines (_parse_beat_blocks); without it, ^/$ would only anchor
# to the very start and end of whatever string is passed in, silently
# breaking detection against the full file text.
BEAT_HEADER = re.compile(
    r"^\s*(?:BEAT|PART)\s+(\d+)\s*(?:[:\-—–]\s*(.*))?$",
    re.IGNORECASE | re.MULTILINE,
)

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

# A bare numbered-list marker carrying inline narration, e.g. '1. "..."' —
# the same positional-block shape as TIMESTAMP_HEADER, just numbered instead
# of timestamped. Requires narration text on the marker's own line (the "+"
# instead of "*"), so an ordinary numbered list inside a prompt paragraph
# doesn't get mistaken for a beat marker.
NUMBERED_HEADER = re.compile(r"^\s*(\d{1,4})[.)]\s+(.+)$", re.MULTILINE)

# "Image:" or "Image Prompt:" — only this line's content is ever sent to
# Flow as a prompt. "Video:"/"Video Prompt:" is motion direction for a video
# tool and is always dropped. "Script:"/"Narration:" is an alternative to a
# bare quoted narration line, used by some script exports instead.
IMAGE_LINE = re.compile(r"^\s*image(?:\s*prompt)?\s*:\s*(.*)$", re.IGNORECASE)
VIDEO_LINE = re.compile(r"^\s*video(?:\s*prompt)?\s*:\s*(.*)$", re.IGNORECASE)
NARRATION_LINE = re.compile(r"^\s*(?:script|narration)\s*:\s*(.*)$", re.IGNORECASE)

QUOTE_PAIRS = {'"': '"', "“": "”"}

# High-confidence phrasings of a stated total — "all 184 image prompts",
# "184 image prompts", "all 86 prompts", "all N beats" — anchored to a
# domain word so ordinary prompt prose ("holding all 3 balloons") can't
# false-positive. Cross-checked against the actual parsed count in
# load_prompts: AI-assistant chat exports sometimes leave a draft
# confirmation ("Good to proceed with all 184?") or an earlier preview
# section mixed into the file, and a mismatch is exactly the symptom of
# that — see docs/PROGRESS.md 2026-08-18 for the case that motivated this.
STATED_COUNT_PATTERNS = [
    re.compile(r"\ball\s+(\d{1,4})\s+image\s*prompts?\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+image\s*prompts?\b", re.IGNORECASE),
    re.compile(r"\ball\s+(\d{1,4})\s+prompts?\b", re.IGNORECASE),
    re.compile(r"\ball\s+(\d{1,4})\s+beats?\b", re.IGNORECASE),
]


def _stated_counts(raw_text):
    """Every distinct total the file's own text claims to contain, via
    STATED_COUNT_PATTERNS. Returns an empty set if the file never states one
    — most files don't, and that's fine; this is a cross-check, not a
    requirement.
    """

    found = set()

    for pattern in STATED_COUNT_PATTERNS:
        for m in pattern.finditer(raw_text):
            found.add(int(m.group(1)))

    return found


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
    """Timestamped (or numbered-list) script format needs BOTH signals to
    claim a file.

    A timestamp or numbered marker alone is too weak — a plain prompt could
    legitimately mention one. Requiring an Image: line as well means this
    only ever fires on files actually laid out as script blocks.
    """

    if not (TIMESTAMP_HEADER.search(raw_text) or NUMBERED_HEADER.search(raw_text)):
        return False

    return any(IMAGE_LINE.match(line) for line in raw_text.splitlines())


def _strip_quotes(text):

    text = text.strip()

    if len(text) >= 2 and QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1].strip()

    return text


def _script_marker(line):
    """A block-start line for the script family: a MM:SS timestamp or a
    bare numbered-list marker (see NUMBERED_HEADER) — both carry narration
    as their own trailing text and use the block's file position, not the
    marker's number, as the beat's identity.
    """

    return TIMESTAMP_HEADER.match(line) or NUMBERED_HEADER.match(line)


def _parse_script_blocks(raw_text, path):
    """Parse the 'timestamp (or numbered) / Image: / Video:' script format.

    A new beat starts at every marker line. Its number is the block's
    position (1-based), because this format carries no stable beat identity —
    a timestamp is a position in the video and a numbered-list marker is
    just the file's own order, so deriving a filename from either directly
    is not possible (or not safe against reordering).

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

        header = _script_marker(lines[i])

        if not header:
            i += 1
            continue

        marker = header.group(1)
        narration = _strip_quotes(header.group(2)) or None
        position += 1
        i += 1

        image_parts = []

        # Consume up to the next marker line, collecting only Image: content.
        while i < n and not _script_marker(lines[i]):

            found = IMAGE_LINE.match(lines[i])

            if found:
                image_parts.append(found.group(1).strip())

            i += 1

        prompt_text = " ".join(part for part in image_parts if part)

        if not prompt_text:
            raise ValueError(
                f"The beat at {marker} in {path} has no 'Image:' line, so "
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

    Narration may appear inline on the header line itself
    (BEAT 1: "narration"), alone on the line right after the header (in
    quotes), or as a labeled "Script:"/"Narration:" line — all three are
    accepted since AI-assistant-generated scripts vary in which they use.

    The body between one header and the next becomes the prompt verbatim,
    UNLESS it contains one or more "Image:"/"Image Prompt:" labeled lines —
    in that case only those (label stripped) are used, and any
    "Video:"/"Video Prompt:" line is dropped, the same rule the timestamped
    script format uses and for the same reason: a motion direction fed to an
    image generator produces a wrong image while looking like it worked.
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
        inline_trailing = (header.group(2) or "").strip()
        i += 1

        narration = _strip_quotes(inline_trailing) or None if inline_trailing else None

        if narration is None:

            while i < n and not lines[i].strip():
                i += 1

            if i < n:
                found = _match_narration(lines[i])
                if found is not None:
                    narration = found
                    i += 1
                else:
                    found = NARRATION_LINE.match(lines[i])
                    if found is not None:
                        narration = found.group(1).strip() or None
                        i += 1

        while i < n and not lines[i].strip():
            i += 1

        prompt_lines = []
        image_lines = []

        while i < n and not BEAT_HEADER.match(lines[i]):

            line = lines[i]

            if line.strip():

                video_found = VIDEO_LINE.match(line)
                image_found = None if video_found else IMAGE_LINE.match(line)

                if video_found:
                    pass
                elif image_found:
                    image_lines.append(image_found.group(1).strip())
                else:
                    prompt_lines.append(line)

            i += 1

        if image_lines:
            prompt_text = " ".join(part for part in image_lines if part)
        else:
            prompt_text = " ".join(line.strip() for line in prompt_lines if line.strip())

        if not prompt_text:
            raise ValueError(
                f"BEAT {beat_number} in {path} has no prompt text — nothing "
                f"found between its narration line and the next BEAT header."
            )

        entries.append((beat_number, prompt_text, narration))

    # AI-assistant chat exports sometimes restart numbering from the
    # beginning after a preview/confirmation exchange gets copied into the
    # file along with the real content (see docs/PROGRESS.md 2026-08-18) —
    # recognizable as the file's very first beat number reappearing later,
    # with OTHER, higher beat numbers appearing in between (a preview that
    # actually counted up, not just an immediate back-to-back repeat — an
    # accidental copy-paste duplicate like "BEAT 3, BEAT 3" right after each
    # other must still be rejected below, not silently "recovered"). When a
    # real restart is detected, only the LAST pass through the numbers is
    # kept; everything before it was a discarded draft, so it's cut
    # automatically rather than requiring a manual trim. If duplicates
    # remain after the cut, something else is going on and this falls
    # through to the loud error below instead of guessing further. The
    # stated-count cross-check in load_prompts is the confirmation that the
    # cut landed on the right beat number.
    if entries:

        first_number = entries[0][0]
        first_positions = [i for i, e in enumerate(entries) if e[0] == first_number]

        if len(first_positions) > 1:

            last_idx = first_positions[-1]
            between = {e[0] for e in entries[1:last_idx]}

            if between - {first_number}:
                entries = entries[last_idx:]

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

    stated = _stated_counts(raw_text)

    if stated and len(prompts) not in stated:
        raise ValueError(
            f"Parsed {len(prompts)} prompt(s) from {path}, but the file's "
            f"own text claims {sorted(stated)}. That mismatch usually means "
            f"leftover chat text — a draft confirmation, an earlier preview "
            f"section — got copied into the file along with the real "
            f"prompts. Check the file before trusting this count."
        )

    return prompts
