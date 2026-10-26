#!/usr/bin/env python3
"""Find, resume, label, or start Kiro sessions from a curses picker. (Option 1) + (Option 2).

## Why this exists

Kiro's native `--sessions` dashboard already searches, filters, sorts and
groups, but every row is named by the model from the first prompt, so the list
is full of "please help me to perform the following..." and three identical
"Clone Personal Forks..." rows. added a label layer (`kiro_sessions.py`)
that attaches a name you choose, but Kiro's own picker cannot show it. This is
"`--sessions`, but showing my labels over the auto-titles, with fuzzy filter,
that resumes the chosen session in place."

Option 2 makes it the full front door: you can label or rename the
highlighted session inline (ctrl-e, written through `kiro_sessions.py`), and
start a fresh session from a selectable "+ new session" entry, not only resume.

Naming a session at the source is impossible on Kiro 2.25.0 (verified,):
no `--title` flag, no autotitle setting, and a manually set title is
regenerated on the next message. So the label lives outside Kiro, and this
launcher reads it rather than fixing the title.

## Design

- **Single source of truth.** Session data comes only from `kiro_sessions`,
  never from Kiro's store directly and never by re-implementing enumeration.
  By default the launcher imports `kiro_sessions` and calls its
  `list_sessions`/`load_labels`/`_display_rows` IN-PROCESS, which drops
  the second-interpreter startup and the JSON-over-a-pipe round trip the
  subprocess path paid; the rows are identical because it is the same provider
  code. The subprocess path (`load_rows`, `kiro_sessions.py list --json`) is
  kept as the injectable-runner seam the tests use. The rows carry `sessionId`,
  `name` (label over auto-title), `title`, `label`, `cwd`, `updatedAt`,
  `status`, and a ready `resume` command. If this needs a field the provider
  lacks, add it to `kiro_sessions.py` first.
- **Pure logic is separated from the curses UI** so it can be tested
  headlessly: `load_rows`, `filter_rows`, and `resume_argv` are pure; the
  curses loop is a thin shell over them. The UI itself is not unit-tested
  (curses needs a real terminal); its logic is.
- **curses, not fzf or textual.** Neither fzf nor textual can be assumed
  present, and a goal here is zero install. `curses` is in the
  Python stdlib, present on every machine, zero install, and a filterable list
  with arrow-key select is squarely within it. (design note.)
- **Launch by exec.** On select, the process is REPLACED (os.execv by absolute
  path) with `kiro-cli chat --resume-id <id>`, run from the session's own
  `cwd`, so there is no lingering wrapper process and you land in the session
  as if you had typed the command. cwd matters: sessions are keyed by working
  directory, and kiro-cli is resolved to an absolute path BEFORE the chdir so
  the directory change cannot alter which binary runs.
- **Two layouts, chosen by width.** On a wide terminal (>= TWO_PANE_MIN_COLS)
  the launcher draws a command center: a project-grouped left rail (a status
  dot, the label-over-title name, and a short relative time) beside a detail
  pane that shows the highlighted session in full (status, exact + relative
  time, label, auto-title, session id, wrapped cwd, and the exact resume
  command). Below the threshold it falls back to a single richer column that
  carries the label/auto marker, status, relative time and the home-collapsed
  cwd inline, since there is no detail pane. The width->layout decision and all
  the formatting (grouping, relative time, path collapse, pane split, detail
  content) are pure functions, so the layout is unit-tested without a terminal,
  keeping the pure-logic / curses-shell split the module already holds.
- **Default scope is the current directory.** You launch from the project you
  are working in, so its small, scannable session list is what you want first.
  `--all` (or the in-UI tab toggle) widens to every workspace for the
  occasional cross-project hunt. A fleet can hold thousands of sessions across
  150+ cwds (measured ~4000), so an all-cwds default would face you with the
  whole pile every time; the current-directory default plus fuzzy filter keeps
  the common case immediate (scoped to how the launcher is used).
- **Labels are operator-typed US-English ASCII.** Rendering is sized for that:
  one character is one column, control chars are stripped as cheap paste
  insurance, and the list clips by character count. The launcher does not try
  to measure true Unicode display width or defend against labels made of
  zero-width code points, because the operator sets every label and does not
  type invisible Unicode into a tool whose only job is to show it back
  (scoped to real usage).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Pure logic (unit-tested; no curses, no real subprocess in tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One selectable session, projected from kiro_sessions.py JSON."""
    session_id: str
    name: str          # label over auto-title (what the user scans)
    title: str         # Kiro's auto-title (kept so search can match it)
    label: str | None  # the chosen label, or None
    cwd: str
    updated_at: str
    status: str

    @property
    def name_lower(self) -> str:
        """The display name (label over auto-title), lowercased for matching.
        Recomputed per call: this is a property, not a cached field, which is a
        few milliseconds over the whole fleet and keeps Row a plain dataclass."""
        return self.name.lower()

    @property
    def title_lower(self) -> str:
        """Kiro's auto-title, lowercased for matching. Matched SEPARATELY from
        the name so a token cannot match across the name/title boundary."""
        return self.title.lower()

    @property
    def id_lower(self) -> str:
        return self.session_id.lower()


def rows_from_json(payload: list[dict]) -> list[Row]:
    """Project kiro_sessions.py JSON rows into Row objects, skipping any row
    with no usable sessionId (it could not be resumed, so it must not appear as
    a choice). The label is taken as-is when it is a non-empty string; a JSON
    null (the provider's signal for no label) becomes None. The literal string
    "None" is NOT special-cased, because the provider emits real null for
    absence, so a string "None" is a label the operator set."""
    rows: list[Row] = []
    for r in payload:
        sid = r.get("sessionId")
        if not isinstance(sid, str) or not sid.strip():
            continue
        label = r.get("label")
        # A missing label is JSON null -> Python None. Only an actual non-empty
        # string is a label. We do NOT special-case the literal "None": the
        # provider emits real null for absence, so a string "None" is a label
        # the operator deliberately set and must be kept.
        if not isinstance(label, str) or not label:
            label = None
        rows.append(
            Row(
                session_id=sid,
                name=str(r.get("name") or r.get("title") or "(no title)"),
                title=str(r.get("title") or ""),
                label=label,
                cwd=str(r.get("cwd") or ""),
                updated_at=str(r.get("updatedAt") or ""),
                status=str(r.get("status") or ""),
            )
        )
    return rows


@dataclass(frozen=True)
class NewSessionEntry:
    """A selectable list entry that starts a fresh session instead of resuming
    one. It makes the launcher a front door: new-session is a row
    you can arrow to and select, not only a hidden key. A distinct type (not a
    Row) so the Enter handler branches on it explicitly and the resume-only
    logic (resume_blocker, cwd) never applies to it."""
    label_text: str = "+ new session (kiro-cli chat --v3)"


# One shared instance; it carries no per-row state.
NEW_SESSION = NewSessionEntry()


def picker_entries(rows: list[Row]) -> list:
    """The selectable entries in picker order: the new-session entry first,
    then the session rows. Keeping this pure means navigation,
    clamping and the index->action mapping are all tested without curses. The
    new-session entry is always present, even when the filtered row list is
    empty, so you can always start fresh from the launcher."""
    return [NEW_SESSION, *rows]


def entry_index_for_session(rows: list[Row], session_id: str) -> int | None:
    """The entries index of the row with this session_id, or None if absent. entries are [new-session, *rows], so a row at rows[i] is at
    entries index i+1. Used to keep the just-edited session highlighted after a
    reload instead of snapping to the top. Pure, so the
    re-selection rule is tested without curses."""
    for i, r in enumerate(rows):
        if r.session_id == session_id:
            return i + 1
    return None


def plan_label_edit(had_label: bool, new_label):
    """Decide what an inline label edit should do, given whether the row ALREADY
    had a label and what the editor returned. Pure, so the Esc /
    empty / write branches are tested without curses. Returns:
      ("cancel", None)  - the editor was escaped (new_label is None): write nothing.
      ("noop", None)    - empty submission on an unlabeled row: nothing to remove.
      ("write", text)   - write `text` (a non-empty label SETS; an empty/
                          whitespace label on a LABELED row removes it)."""
    if new_label is None:
        return ("cancel", None)
    if not new_label.strip() and not had_label:
        return ("noop", None)
    return ("write", new_label)


def _subsequence_match(needle: str, haystack: str) -> bool:
    """True if every character of `needle` appears in `haystack` in order (a
    subsequence), e.g. "mmhd" matches "murmur hud". Both are already lowercased
    by the caller. This is the fuzzy half; token handling is in filter_rows."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _token_matches(token: str, row: Row) -> bool:
    """A token matches a row if it is a subsequence of the row's display name
    OR of its auto-title (checked separately so a match never spans the
    name/title boundary), OR a PREFIX of the sessionId. Subsequence gives the
    fuzzy name/title search; id-prefix lets you paste the start of a known id
    without a two-character token matching arbitrary hex anywhere in a UUID."""
    return (
        _subsequence_match(token, row.name_lower)
        or _subsequence_match(token, row.title_lower)
        or row.id_lower.startswith(token)
    )


def filter_rows(rows: list[Row], query: str) -> list[Row]:
    """Return rows matching `query`. The query is split on whitespace into
    tokens, and a row matches when EVERY token matches (see _token_matches):
    each token is a subsequence of the name+title, or a prefix of the
    sessionId. Subsequence, not substring, so "mmhd" finds "Murmur HUD" and
    "synth renew" finds "Synthesia renewal" regardless of word order (acceptance: fuzzy query). An empty query returns all rows unchanged."""
    q = query.strip().lower()
    if not q:
        return rows
    tokens = q.split()
    return [row for row in rows if all(_token_matches(tok, row) for tok in tokens)]


def clamp_selection(selected: int, count: int) -> int:
    """Keep a selection index inside a list of `count` rows. Returns 0 for an
    empty list so an index is never negative, which would otherwise wrap to the
    end of the next non-empty frame. This is the one place the
    picker's three movement sites (reload, up, down) get their clamp, so a test
    can exercise the real rule instead of restating it."""
    if count <= 0:
        return 0
    return max(0, min(selected, count - 1))


def initial_selection(n_rows: int) -> int:
    """Where selection lands after the list is (re)filtered. Entries are
    [new-session, *rows], so index 0 is the new-session entry and index 1 is the
    first real row. Land on the first REAL row when there is one, so the
    universal "type to filter, press Enter" habit resumes the top match rather
    than starting a new session. With no
    matching rows, land on the new-session entry (index 0), which is then the
    only entry and the natural action."""
    return 1 if n_rows > 0 else 0


def resume_argv(session_id: str) -> list[str]:
    """The argv that resumes a session. A single builder so the exec path and
    any test agree on the exact command. Never shell-interpolated: argv form
    means a session id can never be interpreted as shell syntax."""
    if not session_id or not session_id.strip():
        raise ValueError("refusing to build a resume command for an empty session id")
    return ["kiro-cli", "chat", "--resume-id", session_id]


def new_session_argv() -> list[str]:
    """Start a fresh next-generation (v3) session. The launcher offers this so
    it can double as a front door, and the `kn` alias uses the same path."""
    return ["kiro-cli", "chat", "--v3"]


def label_argv(script: str, session_id: str, label: str) -> list[str]:
    """The argv that writes a label through kiro_sessions.py. A
    single builder so the write path and any test agree on the exact command.
    argv form, never shell-interpolated, so a label with shell metacharacters
    is one argument. The `--` terminator before the positional label stops the
    provider's argparse from reading a label like "--remove" or "-x" as an
    OPTION rather than text. The provider validates a non-empty id
    and label; this only refuses an empty id (which could not be resumed
    afterwards anyway)."""
    if not session_id or not session_id.strip():
        raise ValueError("refusing to build a label command for an empty session id")
    return [sys.executable, script, "label", session_id, "--", label]


def remove_label_argv(script: str, session_id: str) -> list[str]:
    """The argv that removes a session's label through kiro_sessions.py
    (`label <id> --remove`). Used when the inline editor is submitted empty on
    an already-labeled row, so the row reverts to Kiro's auto-title."""
    if not session_id or not session_id.strip():
        raise ValueError("refusing to build a remove-label command for an empty session id")
    return [sys.executable, script, "label", session_id, "--remove"]


def _char_cols(ch: str) -> int:
    """Terminal columns for one character: 2 for an East Asian wide/fullwidth
    character, 1 otherwise. Just enough width awareness to clip correctly:
    Kiro's auto-titles (shown for unlabeled sessions) are not operator-typed
    and CAN contain CJK, so wide characters must count as two columns or a
    wide title spills across result rows. This deliberately does NOT
    chase zero-width code points: those only arise in a label the operator
    typed themselves, which does not happen (see module docstring)."""
    import unicodedata

    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip_line(text: str, columns: int) -> str:
    """Sanitize `text` for a single terminal row and clip it to `columns`
    terminal columns.

    Control characters (including embedded newlines and tabs) are replaced with
    a space, so a stray paste or a wide-content auto-title cannot spill a label
    onto the next result row. Clipping counts terminal columns, with East Asian
    wide characters as two, because Kiro's auto-titles (shown for unlabeled
    sessions) are outside the operator's control and can contain CJK.
    Operator labels are ASCII, where one character is one column, so this is a
    no-op widthwise for them."""
    if columns <= 0:
        return ""
    out = []
    used = 0
    for ch in text:
        safe = " " if (ch != " " and not ch.isprintable()) else ch
        w = _char_cols(safe)
        if used + w > columns:
            break
        out.append(safe)
        used += w
    return "".join(out)


def line_cols(text: str) -> int:
    """Terminal columns `text` occupies. Used to decide whether a row is wide
    enough to actually show, so Enter cannot resume a row clipped to nothing in
    a pathologically narrow terminal."""
    return sum(_char_cols(ch) for ch in text)


def prompt_display(prompt: str, buf: str, avail_cols: int) -> str:
    """What a one-line editor shows for `prompt` + `buf` in `avail_cols` columns. Budgets by DISPLAY WIDTH, not character count, so a wide-char
    (CJK) prompt cannot eat the whole line and hide the buffer. The prompt is truncated to leave room for the buffer, and
    the TAIL of the buffer is shown so the characters just typed stay visible
    rather than being clipped off the right. Pure, so the budgeting is tested
    without curses."""
    if avail_cols <= 0:
        return ""
    # Leave at least 8 columns for the buffer when the prompt is long.
    prompt_cap = max(0, avail_cols - 8)
    shown_prompt = clip_line(prompt, prompt_cap)
    buf_cols = max(0, avail_cols - line_cols(shown_prompt))
    # Take the buffer tail that fits buf_cols columns (walk from the end).
    tail = ""
    used = 0
    for ch in reversed(buf):
        w = _char_cols(ch)
        if used + w > buf_cols:
            break
        tail = ch + tail
        used += w
    return clip_line(shown_prompt + tail, avail_cols)


def tail_stderr(text: str, limit: int = 400) -> str:
    """The most useful part of provider stderr for a one-line banner. A Python
    traceback ends with the exception line (e.g. "PermissionError: ..."), and
    everything above it is stack frames, so the LAST non-empty line carries the
    cause. Return that final line; if it
    is itself longer than `limit`, keep its tail. Pure, so the rule is tested
    without a provider."""
    s = (text or "").strip()
    if not s:
        return ""
    last_line = s.splitlines()[-1].strip()
    chosen = last_line or s
    if len(chosen) <= limit:
        return chosen
    return "..." + chosen[-limit:]


# ---------------------------------------------------------------------------
# Presentation helpers (pure; unit-tested) for the command-center layout
#
# These turn the raw provider fields into the scannable forms the two-pane and
# single-column renderers both draw: a collapsed path, a project name, a
# humanized relative time, a status dot, and the grouping of rows under their
# cwd. They are pure so the layout is tested without a terminal, holding the
# same pure-logic / curses-shell split the rest of the module keeps.
# ---------------------------------------------------------------------------


# Below this terminal width the two-pane command center is not worth drawing:
# the detail pane would be too narrow to show a wrapped cwd or a resume command
# without clipping to noise, so the single-column fallback (Option A) is used
# instead. Chosen so an 80-column terminal falls back and a typical wide window
# (>= 90) gets two panes.
TWO_PANE_MIN_COLS = 90

# The detail pane's share of the width in two-pane mode. The left rail keeps the
# majority because scanning the list is the common act; the detail pane needs
# only enough to show the fields and a wrapped path/command.
DETAIL_PANE_FRACTION = 0.42
# The detail pane is clamped to this column band so it is neither uselessly
# narrow on a 90-column terminal nor wastefully wide on an ultra-wide one.
DETAIL_PANE_MIN_COLS = 32
DETAIL_PANE_MAX_COLS = 60


def use_two_pane(width: int) -> bool:
    """True when the terminal is wide enough for the two-pane command center,
    False when the single-column fallback should be used instead. The one
    place the width->layout decision is made, so both the renderer and the test
    read the same rule rather than restating the threshold."""
    return width >= TWO_PANE_MIN_COLS


def detail_pane_cols(width: int) -> int:
    """Columns to give the detail pane in two-pane mode, from the total terminal
    `width`. A fixed FRACTION, clamped to a sensible band, then bounded so
    the left rail always keeps at least as much room as the detail. Pure, so the
    split arithmetic is tested without curses. Only meaningful when
    use_two_pane(width) is True; callers gate on that."""
    raw = int(width * DETAIL_PANE_FRACTION)
    clamped = max(DETAIL_PANE_MIN_COLS, min(raw, DETAIL_PANE_MAX_COLS))
    # Never let the detail pane take more than half: the list is the main view.
    return min(clamped, width // 2)


def collapse_home(path: str, home: str | None = None) -> str:
    """Collapse a leading home-directory prefix to `~`, so a cwd reads as
    `~/projects/app` rather than `/Users/alex/projects/app`. Only
    an exact home prefix at a path boundary is collapsed; an unrelated path that
    merely starts with the same characters is left alone. `home` is injectable
    for tests; it defaults to the real home directory. An empty path stays
    empty."""
    if not path:
        return ""
    base = home if home is not None else os.path.expanduser("~")
    if not base:
        return path
    base = base.rstrip("/")
    if path == base:
        return "~"
    if path.startswith(base + "/"):
        return "~" + path[len(base):]
    return path


def project_name(cwd: str, home: str | None = None) -> str:
    """The scannable project name for a cwd: its last path segment, so
    `/Users/x/projects/my_app` reads as
    `my_app` in a group header. The filesystem root is shown as
    `/`, and an empty cwd (resume-from-anywhere) as `(no workspace)`. Pure, so
    the grouping label is tested without a terminal."""
    if not cwd:
        return "(no workspace)"
    collapsed = cwd.rstrip("/")
    if not collapsed:
        return "/"
    if collapsed == "~":
        return "~"
    return collapsed.rsplit("/", 1)[-1] or "/"


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp like `2026-10-16T17:01:37.186Z` into an
    aware UTC datetime, or None if it is empty or unparseable. A trailing `Z`
    is normalized to `+00:00` because datetime.fromisoformat did not accept `Z`
    before Python 3.11, and the tool supports 3.10+ (CONTRIBUTING). A naive
    result (no offset in the string) is assumed UTC, matching how the provider
    stamps `updatedAt`."""
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def humanize_time(ts: str, now: _dt.datetime | None = None) -> str:
    """A short, human relative time for a session's `updatedAt`: `now`,
    `2m`, `3h`, `yesterday`, a weekday for the last week (`Mon`), then a
    `M>Mon DD` calendar date, and the year for anything older. Replaces the raw
    ISO string the old UI showed. `now` is injectable so the mapping is tested
    deterministically; it defaults to the current UTC time. An empty or
    unparseable timestamp returns an empty string rather than a guess, so a
    missing time is simply blank rather than a wrong `now`."""
    when = _parse_iso(ts)
    if when is None:
        return ""
    current = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    delta = current - when
    secs = delta.total_seconds()
    if secs < 0:
        # A clock skew or a future stamp: do not invent "in 3m", just show now.
        return "now"
    if secs < 60:
        return "now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m"
    hours = int(secs // 3600)
    if hours < 24:
        return f"{hours}h"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return when.strftime("%a")  # weekday name, e.g. Mon
    if when.year == current.year:
        return when.strftime("%b %d").replace(" 0", " ")  # e.g. Oct 3
    return when.strftime("%b %d %Y").replace(" 0", " ")


def status_dot(status: str) -> str:
    """A single-glyph status marker for a session row. A running/active
    session gets a filled dot, everything else an open dot, so the eye can pick
    out live work in a long list. The exact status vocabulary comes from the
    provider; this treats any status containing `run` or `active` as live and is
    otherwise conservative (open dot), because an unknown status must not read
    as running."""
    s = (status or "").strip().lower()
    if "run" in s or "active" in s:
        return "\u25cf"  # ● filled
    return "\u25cb"  # ○ open


@dataclass(frozen=True)
class SessionGroup:
    """A set of rows that share a cwd, with the display label for the group
    header. Pure data; the renderer draws it. `name` is the scannable
    project name, `path` the home-collapsed cwd shown dimmed beside it."""
    name: str
    path: str
    cwd: str
    rows: tuple[Row, ...]


def group_rows_by_cwd(rows: list[Row], home: str | None = None) -> list[SessionGroup]:
    """Group rows under their cwd for the project-grouped list both layouts use. Groups appear in the order their first row appears in `rows`, and rows
    keep their order within a group, so the provider's recency ordering is
    preserved rather than re-sorted. `home` is injectable for the path collapse.
    Pure, so the grouping is tested without a terminal."""
    order: list[str] = []
    buckets: dict[str, list[Row]] = {}
    for row in rows:
        key = row.cwd
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    groups: list[SessionGroup] = []
    for key in order:
        groups.append(
            SessionGroup(
                name=project_name(key, home=home),
                path=collapse_home(key, home=home),
                cwd=key,
                rows=tuple(buckets[key]),
            )
        )
    return groups


def wrap_cols(text: str, width: int) -> list[str]:
    """Hard-wrap `text` into lines of at most `width` terminal columns, for the
    detail pane's cwd and resume command. Breaks at a space when one is
    available within the width, otherwise mid-token (a long path has no spaces),
    so a line never exceeds the pane. Control characters are neutralized by
    clip_line on each piece. An empty string yields no lines; width <= 0 yields
    none."""
    if width <= 0 or not text:
        return []
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if line_cols(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # The word alone may still exceed the width (a long path): hard-split it
        # by columns using clip_line repeatedly.
        while line_cols(word) > width:
            head = clip_line(word, width)
            if not head:
                break
            lines.append(head)
            word = word[len(head):]
        current = word
    if current:
        lines.append(current)
    return [clip_line(line, width) for line in lines]


def detail_lines(entry, width: int, now: _dt.datetime | None = None,
                 home: str | None = None) -> list[str]:
    """The right-pane detail text for the highlighted entry, as a list of
    already-clipped lines. For the new-session entry it describes what Enter
    will do; for a session row it shows status, relative + absolute time, label,
    auto-title, session id, wrapped cwd, and the exact resume command. Pure (no
    curses), so the panel's content is tested directly. `width` is the pane's
    column budget; `now`/`home` are injectable for deterministic tests."""
    if width <= 0:
        return []
    if isinstance(entry, NewSessionEntry):
        body = [
            "new session",
            "",
            "Enter starts a fresh session:",
            *wrap_cols(" ".join(new_session_argv()), width),
        ]
        return [clip_line(line, width) for line in body]

    rel = humanize_time(entry.updated_at, now=now)
    when = _parse_iso(entry.updated_at)
    abs_str = when.strftime("%Y-%m-%d %H:%M") if when else ""
    time_line = " ".join(x for x in (rel, f"({abs_str})" if abs_str else "") if x)
    label_line = entry.label if entry.label else "(no label - auto-title)"
    lines = [
        f"{status_dot(entry.status)} {entry.name}",
        "",
        f"status   {entry.status or 'unknown'}",
        f"updated  {time_line}" if time_line else "updated  unknown",
        f"label    {label_line}",
        f"title    {entry.title or '(none)'}",
        f"session  {entry.session_id}",
        "cwd",
        *["  " + line for line in wrap_cols(collapse_home(entry.cwd, home=home), max(1, width - 2))],
        "",
        "resume",
        *["  " + line for line in wrap_cols(" ".join(resume_argv(entry.session_id)), max(1, width - 2))],
    ]
    return [clip_line(line, width) for line in lines]


@dataclass(frozen=True)
class ListLine:
    """One drawable line of the left-rail list. Either a non-selectable
    group header (`entry_index is None`) or a selectable entry mapped back to
    its index in the flat `entries` list (so navigation stays on `entries`
    while the display is grouped). `text` is the already-formatted cell; `attr`
    is a hint ('header', 'row', or 'new') the renderer turns into a curses
    attribute. Pure data."""
    text: str
    attr: str
    entry_index: int | None


def row_cell(entry, width: int, two_pane: bool, now=None, home=None) -> str:
    """The compact one-line cell for a session row or the new-session entry in
    the left rail. In two-pane mode it is terse (dot + name + short time),
    because the detail pane carries the rest; in single-column mode it also
    carries the label/auto marker, status and the collapsed cwd, since there is
    no detail pane to show them. Pure, so both forms are tested without curses.
    `now`/`home` are injectable for deterministic tests."""
    if isinstance(entry, NewSessionEntry):
        return clip_line(entry.label_text, width)
    dot = status_dot(entry.status)
    rel = humanize_time(entry.updated_at, now=now)
    if two_pane:
        base = f"{dot} {entry.name}"
        if rel:
            # Right-align the short relative time within the cell.
            pad = width - line_cols(base) - line_cols(rel) - 1
            if pad >= 1:
                return clip_line(base + " " * pad + " " + rel, width)
        return clip_line(base, width)
    marker = "label" if entry.label else "auto "
    status = f" {{{entry.status}}}" if entry.status else ""
    tail = f"  [{marker}]{status}  {rel}" if rel else f"  [{marker}]{status}"
    path = collapse_home(entry.cwd, home=home)
    return clip_line(f"{dot} {entry.name}{tail}  {path}", width)


def build_list_lines(groups: list["SessionGroup"], entries: list, width: int,
                     two_pane: bool, now=None, home=None) -> list[ListLine]:
    """Build the grouped left-rail as a flat list of drawable ListLines.

    `entries` is the flat selectable list (new-session entry, then the filtered
    rows) that navigation indexes into; `groups` is those same rows grouped by
    cwd for display. The result interleaves a non-selectable header before each
    group's rows, and keeps the new-session entry (entries index 0) as the first
    selectable line, so the display is grouped while selection stays on
    `entries`. Pure, so the interleaving and the entry->index mapping are tested
    without a terminal. A row that is not present in `entries` (should not
    happen, but defended) is skipped rather than mapped to a wrong index.

    Formats EVERY slot, so it is kept for tests and any caller that wants the
    whole list at once. The renderer uses build_list_slots + format_slot so it
    only formats the visible window; do not call this per frame on a large
    list."""
    slots = build_list_slots(groups, entries)
    return [format_slot(slot, width, two_pane, now=now, home=home) for slot in slots]


@dataclass(frozen=True)
class ListSlot:
    """A cheap, unformatted descriptor of one left-rail line. Carries only
    what the renderer needs to decide layout and selection: the kind
    ('new' | 'header' | 'row'), the selectable entry index (None for a header),
    the entry object (for a 'new'/'row' slot) and the group (for a 'header').
    Building these for the whole list is O(rows) with no string work; the
    expensive per-cell formatting is deferred to format_slot for the visible
    slice only, so a 6651-row list no longer formats every row per keystroke."""
    kind: str
    entry_index: int | None
    entry: object | None
    group: object | None


def build_list_slots(groups: list["SessionGroup"], entries: list) -> list[ListSlot]:
    """Build the interleaved left-rail STRUCTURE without formatting any text. Same ordering as build_list_lines (new-session entry, then each group
    as a header followed by its rows), but each slot is a cheap descriptor, so
    this is safe to call every frame on a large list. Pure. format_slot turns a
    slot into a drawn line, and the renderer calls it only for visible slots."""
    index_of = {id(entry): i for i, entry in enumerate(entries)}
    slots: list[ListSlot] = []
    if entries and isinstance(entries[0], NewSessionEntry):
        slots.append(ListSlot(kind="new", entry_index=0,
                              entry=entries[0], group=None))
    for group in groups:
        slots.append(ListSlot(kind="header", entry_index=None,
                              entry=None, group=group))
        for row in group.rows:
            idx = index_of.get(id(row))
            if idx is None:
                continue
            slots.append(ListSlot(kind="row", entry_index=idx,
                                  entry=row, group=group))
    return slots


def format_slot(slot: ListSlot, width: int, two_pane: bool,
                now=None, home=None) -> ListLine:
    """Format one ListSlot into a drawable ListLine. The expensive step
    (row_cell / header text), called only for the slots actually on screen.
    Pure, so both the formatting and the slot->line mapping are tested."""
    if slot.kind == "header":
        group = slot.group
        header = group.name
        if group.path and group.path != group.name:
            header = f"{group.name}  {group.path}"
        return ListLine(text=clip_line(header, width), attr="header",
                        entry_index=None)
    attr = "new" if slot.kind == "new" else "row"
    return ListLine(
        text=row_cell(slot.entry, width, two_pane, now=now, home=home),
        attr=attr,
        entry_index=slot.entry_index,
    )


def first_selectable_slot(slots: list[ListSlot]) -> int:
    """The index into `slots` of the first selectable (non-header) slot, or 0. The slot-level analogue of first_selectable_line, for the renderer's
    scroll anchoring on the unformatted structure. Pure."""
    for i, slot in enumerate(slots):
        if slot.entry_index is not None:
            return i
    return 0


def slot_index_for_entry(slots: list[ListSlot], entry_index: int) -> int | None:
    """The `slots` index whose selectable entry is `entry_index`, or None.
    The slot-level analogue of line_index_for_entry, so the renderer finds the
    selected row's position in the structure without formatting. Pure."""
    for i, slot in enumerate(slots):
        if slot.entry_index == entry_index:
            return i
    return None


def first_selectable_line(lines: list[ListLine]) -> int:
    """The index into `lines` of the first selectable (non-header) line, or 0
    if there is none. Used to anchor scrolling. Pure."""
    for i, line in enumerate(lines):
        if line.entry_index is not None:
            return i
    return 0


def line_index_for_entry(lines: list[ListLine], entry_index: int) -> int | None:
    """The `lines` index whose selectable entry is `entry_index`, or None.
    Lets the renderer find where the highlighted entry sits among the headers,
    so it can keep it on screen. Pure."""
    for i, line in enumerate(lines):
        if line.entry_index == entry_index:
            return i
    return None


# ---------------------------------------------------------------------------
# Data load (subprocess to kiro_sessions.py; injectable runner for tests)
# ---------------------------------------------------------------------------


def _kiro_sessions_script() -> str:
    """The sibling kiro_sessions.py, resolved next to this file so the launcher
    works regardless of cwd."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiro_sessions.py")


def _list_timeout() -> float:
    """Outer subprocess timeout for the `kiro_sessions.py list` call. The
    provider owns the real enumeration deadline and honors KIRO_SESSIONS_TIMEOUT
    (default 60s); the launcher must not impose a SHORTER deadline that would
    kill a slow-but-legal enumeration the operator deliberately allowed (a
    hardcoded 120s once overrode a configured 180s). So we
    read the same variable and add headroom, so the provider's own timeout fires
    first with its own clean message. The buffer also covers interpreter startup
    on a cold store."""
    raw = os.environ.get("KIRO_SESSIONS_TIMEOUT")
    provider = 60.0
    if raw:
        try:
            provider = float(raw)
        except ValueError:
            # Let the provider itself reject a bad value with its own message;
            # here just fall back to a safe outer bound.
            provider = 60.0
    return provider + 30.0


def load_rows(all_cwds: bool, runner=None) -> tuple[list[Row], str]:
    """Load sessions by shelling out to `kiro_sessions.py list --json`.

    Returns `(rows, warnings)`: the projected rows, and any stderr text the
    provider emitted on a clean load (for example a skipped corrupt label
    file). The caller decides when to show `warnings`.

    `runner` is injected by tests; by default it runs the real script with the
    current interpreter. A non-zero exit, a timeout, or unparseable output
    RAISES SystemExit rather than returning an empty list, so a broken load
    never reads as "no sessions" (the same invariant kiro_sessions.py holds for
    its own enumeration)."""
    if runner is None:
        cmd = [sys.executable, _kiro_sessions_script(), "list", "--json"]
        if all_cwds:
            cmd.append("--all-cwds")
        timeout = _list_timeout()

        def _default_runner(_cmd=cmd, _t=timeout):  # noqa: ANN001
            return subprocess.run(_cmd, capture_output=True, text=True, timeout=_t)

        runner = _default_runner

    try:
        proc = runner()
    except subprocess.TimeoutExpired as exc:
        # The outer bound fired. This is the launcher's safety net, not the
        # normal deadline (the provider's own KIRO_SESSIONS_TIMEOUT fires
        # first). Surface it as a clean exit, not a traceback, since run()
        # calls this inside the picker loop.
        raise SystemExit(
            f"error: `kiro_sessions.py list` did not finish within {exc.timeout:.0f}s. "
            "Raise KIRO_SESSIONS_TIMEOUT if your session store is very large."
        )
    except OSError as exc:
        # e.g. too many open files, or the interpreter/script cannot be
        # executed. Normalize to SystemExit so a reload triggered from inside
        # the picker (a label edit) shows a banner rather than a traceback
        #.
        raise SystemExit(f"error: could not run `kiro_sessions.py list`: {exc}")
    if proc.returncode != 0:
        raise SystemExit(
            f"error: `kiro_sessions.py list` exited {proc.returncode}: "
            f"{tail_stderr(proc.stderr)}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: could not parse kiro_sessions.py output ({exc}).")
    if not isinstance(payload, list):
        raise SystemExit(
            "error: kiro_sessions.py did not return a list of sessions "
            f"(got {type(payload).__name__})."
        )
    # Even on a clean (exit 0) load, kiro_sessions.py may have warned on stderr
    # about a corrupt label file it skipped. Return that so the caller can
    # surface it: swallowing it would hide a lost label behind a silent
    # "no match". stdout carries the JSON,
    # so warnings never pollute the parsed data.
    warnings = (proc.stderr or "").strip()
    return rows_from_json(payload), warnings


def _import_kiro_sessions():
    """Import the sibling kiro_sessions.py as a module, so the launcher can
    enumerate IN-PROCESS instead of spawning a second Python interpreter.

    Resolved by path next to this file (not by name on sys.path), so it works
    regardless of cwd and regardless of whether the repo is importable. Cached
    on the function so repeated loads (every label edit reloads) import once.
    The module is the same code the subprocess path runs, so the data contract
    is identical; only the process boundary is removed."""
    cached = getattr(_import_kiro_sessions, "_mod", None)
    if cached is not None:
        return cached
    import importlib.util

    path = _kiro_sessions_script()
    spec = importlib.util.spec_from_file_location("kiro_sessions", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load the session provider at {path}.")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("kiro_sessions", mod)
    spec.loader.exec_module(mod)
    _import_kiro_sessions._mod = mod
    return mod


def load_rows_inprocess(all_cwds: bool) -> tuple[list[Row], str]:
    """Load sessions by calling kiro_sessions in-process, the default fast
    path. Returns the same `(rows, warnings)` contract as load_rows(): the
    projected rows, and any provider warning text (e.g. a skipped corrupt label
    file) captured from the provider's stderr.

    This removes the second-interpreter startup and the JSON round-trip over a
    pipe that the subprocess path pays. It still goes through the provider's own
    `list_sessions`/`load_labels`/`_display_rows`, so the rows are identical to
    the `--json` output and the provider's "raise, never empty-on-failure"
    error contract is preserved (list_sessions raises SystemExit on a broken
    enumeration). Provider warnings are printed to stderr by load_labels, so
    they are captured by redirecting stderr for the duration of the call."""
    import contextlib
    import io

    sessions_mod = _import_kiro_sessions()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            sessions = sessions_mod.list_sessions(all_cwds=all_cwds)
            labels = sessions_mod.load_labels()
            payload = sessions_mod._display_rows(sessions, labels)
    except SystemExit:
        # The provider's own clean failure (bad enumeration, unreadable store):
        # re-raise so the caller surfaces it exactly as the subprocess path did.
        raise
    except OSError as exc:
        raise SystemExit(f"error: could not enumerate sessions: {exc}")
    warnings = buf.getvalue().strip()
    return rows_from_json(payload), warnings


def write_label(session_id: str, label: str, runner=None) -> None:
    """Set or remove a session's label through kiro_sessions.py.

    An empty/whitespace `label` REMOVES the label (`label <id> --remove`), so an
    inline editor submitted empty on a labeled row reverts it to Kiro's
    auto-title; a non-empty label is written. The provider owns validation and
    the single-file-per-session store, so this never touches the
    store directly. A non-zero exit RAISES SystemExit carrying the provider's
    own stderr, which the picker shows in its banner rather than crashing
    (mirroring the load_rows failure contract).

    `runner` is injected by tests; by default it runs the real script with the
    current interpreter."""
    if not session_id or not session_id.strip():
        raise SystemExit("error: a non-empty session id is required to label a session.")
    script = _kiro_sessions_script()
    if label and label.strip():
        cmd = label_argv(script, session_id, label)
    else:
        cmd = remove_label_argv(script, session_id)

    if runner is None:
        def _default_runner(_cmd=cmd):  # noqa: ANN001
            return subprocess.run(_cmd, capture_output=True, text=True, timeout=30)

        runner = _default_runner

    try:
        proc = runner()
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"error: `kiro_sessions.py label` did not finish within {exc.timeout:.0f}s."
        )
    except OSError as exc:
        # e.g. the interpreter or script path cannot be executed. Surface it as
        # SystemExit so the picker shows it in the banner rather than tearing
        # the UI down with a traceback.
        raise SystemExit(f"error: could not run `kiro_sessions.py label`: {exc}")
    if proc.returncode != 0:
        raise SystemExit(
            f"error: labeling failed: {tail_stderr(proc.stderr)}"
        )


def _ensure_kiro_cli() -> str:
    """Resolve kiro-cli to an ABSOLUTE path on PATH, or fail clearly.

    Returns the absolute path so a caller can exec it AFTER a chdir without
    re-resolving against PATH from the new directory. A relative PATH entry
    (e.g. "bin") would otherwise let execvp find a different kiro-cli, or none,
    once we chdir into the session's workspace."""
    found = shutil.which("kiro-cli")
    if found is None:
        raise SystemExit(
            "error: kiro-cli not found on PATH; cannot resume or start a session."
        )
    return os.path.abspath(found)


def resume_blocker(row: Row) -> str | None:
    """Return a human-readable reason the row cannot be resumed in place, or
    None if it can. Pure (no chdir/exec) so the decision is unit-testable: the
    one blocking case is a recorded cwd that no longer exists on disk, which
    must refuse rather than resume from the wrong directory. An empty cwd is allowed (resume from wherever the launcher is)."""
    if row.cwd and not os.path.isdir(row.cwd):
        return (
            f"the session's workspace {row.cwd!r} no longer exists, so it cannot "
            "be resumed in place"
        )
    return None


def exec_resume(row: Row) -> None:
    """Replace this process with the resumed session, from the row's cwd.

    os.execv by ABSOLUTE path (not execvp) so no wrapper process lingers and so
    the chdir below cannot change which binary runs: kiro-cli is resolved
    before the chdir. The cwd change
    happens because sessions are keyed by working directory; a recorded cwd
    that no longer exists is refused (see resume_blocker). Never returns on
    success."""
    kiro = _ensure_kiro_cli()
    argv = resume_argv(row.session_id)
    blocker = resume_blocker(row)
    if blocker:
        raise SystemExit(
            f"error: {blocker}. Recreate or restore that directory, or resume "
            "it manually with:\n  " + " ".join(argv)
        )
    if row.cwd:
        os.chdir(row.cwd)
    os.execv(kiro, argv)


def exec_new_session() -> None:
    """Replace this process with a fresh v3 session in the current cwd."""
    kiro = _ensure_kiro_cli()
    os.execv(kiro, new_session_argv())


# ---------------------------------------------------------------------------
# Color. The decision to use color is a pure predicate so it is tested;
# the actual pair setup needs a real terminal and lives in the curses section.
# ---------------------------------------------------------------------------

# Logical color roles the renderer asks for by name, so the curses section maps
# each to a pair number once and the draw code never hardcodes a pair id. The
# values are just the role names; _ColorMap (set up at runtime) turns a role
# into a curses attribute, falling back to a plain attribute when color is off.
COLOR_ROLES = (
    "header",    # group header (project name + path)
    "dot_run",   # a running/active session's status dot
    "dot_idle",  # an idle/other session's status dot
    "marker",    # the [label]/[auto] marker in single-column mode
    "time",      # the relative time, dimmed
    "field",     # detail-pane field labels (status/updated/...)
    "divider",   # the two-pane vertical divider
    "new",       # the "+ new session" entry
)

# Kiro CLI's brand colors, read from the kiro-cli app bundle on 2026-10-16,
# so the launcher reads as part of the same tool family rather than a generic
# ANSI scheme:
#   - violet  #7B5CFA  the Kiro logo accent (CLI help renders it as magenta)
#   - green   #2ecc71  Kiro's success / active color
#   - grey     a0a0b8  Kiro's muted secondary text
#   - red     #e74c3c  Kiro's error / denial color (not used here yet)
# The 256-color INDEX is the nearest xterm-256 cube/grey entry to each hex;
# computed once, not at runtime. The BASIC fallback is the nearest of the 8
# standard ANSI colors, used when the terminal has fewer than 256 colors.
KIRO_VIOLET_256 = 99     # ~#7B5CFA
KIRO_GREEN_256 = 41      # ~#2ecc71
KIRO_GREY_256 = 248      # ~#a0a0b8


def role_colors(color_depth: int) -> dict:
    """Choose each role's foreground color, aligned to Kiro's brand family.

    `color_depth` is the terminal's color count (curses.COLORS). With 256 or
    more colors each brand role uses the nearest xterm-256 index to Kiro's hex;
    otherwise it uses the nearest of the 8 basic ANSI colors (named here by the
    standard curses index, so the curses module is not needed to test this).
    Returns `{role: fg_index}`. Pure, so the depth->color mapping is unit-tested
    without a terminal; _build_color_map turns these into curses pairs and owns
    the monochrome fallback when color is off entirely.

    Basic ANSI indices used in the fallback (curses.COLOR_* equal these):
    1=red, 2=green, 4=blue, 5=magenta, 6=cyan. Violet maps to magenta (its
    nearest basic analogue, and what Kiro's own CLI help uses); grey maps to
    blue, the closest cool muted tone the 8-color set offers."""
    if color_depth >= 256:
        violet, green, grey = KIRO_VIOLET_256, KIRO_GREEN_256, KIRO_GREY_256
    else:
        # Nearest basic ANSI: violet->magenta(5), green->green(2), grey->blue(4).
        violet, green, grey = 5, 2, 4
    return {
        "header": violet,    # brand accent structures the view
        "dot_run": green,    # Kiro's active/success color
        "dot_idle": grey,    # muted
        "marker": violet,    # brand accent on the label marker
        "time": grey,        # muted secondary text (Kiro's #a0a0b8)
        "field": violet,     # detail field labels in the brand accent
        "divider": grey,     # muted structural line
        "new": green,        # starting fresh = Kiro's active/success color
    }


def should_use_color(has_colors: bool, env: dict | None = None) -> bool:
    """Whether to use color. True only when the terminal reports color
    support AND color is not disabled by the environment. Honors the de-facto
    NO_COLOR standard (any non-empty value disables color) and an explicit
    KIRO_LAUNCH_COLOR=0/1 override that wins over both. Pure, so the policy is
    tested without a terminal; the caller passes curses.has_colors()."""
    e = env if env is not None else os.environ
    override = e.get("KIRO_LAUNCH_COLOR")
    if override is not None:
        if override.strip() in ("0", "false", "no", "off"):
            return False
        if override.strip() in ("1", "true", "yes", "on"):
            return bool(has_colors)
    if e.get("NO_COLOR"):
        # The NO_COLOR convention: presence of any non-empty value disables it.
        return False
    return bool(has_colors)


# ---------------------------------------------------------------------------
# curses UI (thin shell over the pure logic; not unit-tested)
# ---------------------------------------------------------------------------


def _build_color_map(curses_mod, use_color: bool) -> dict:
    """Map each logical COLOR_ROLE to a curses attribute. When `use_color`
    is False (or color init fails) every role maps to a sensible monochrome
    attribute, so the renderer draws exactly as it did before color existed.
    Needs a real terminal (curses.start_color), so it is not unit-tested; the
    policy that decides `use_color` is the pure should_use_color()."""
    # Monochrome fallbacks: what each role rendered as before color.
    mono = {
        "header": curses_mod.A_DIM | curses_mod.A_BOLD,
        "dot_run": curses_mod.A_BOLD,
        "dot_idle": curses_mod.A_DIM,
        "marker": curses_mod.A_DIM,
        "time": curses_mod.A_DIM,
        "field": curses_mod.A_DIM,
        "divider": curses_mod.A_DIM,
        "new": curses_mod.A_BOLD,
    }
    if not use_color:
        return mono
    try:
        curses_mod.start_color()
        curses_mod.use_default_colors()
    except curses_mod.error:
        return mono
    # Role -> foreground color, aligned to Kiro's brand family. The pure
    # role_colors() picks the nearest 256-color index to Kiro's hex when the
    # terminal supports 256 colors, else the nearest basic ANSI color. -1 as the
    # background (use_default_colors) keeps the terminal theme's own backdrop.
    depth = getattr(curses_mod, "COLORS", 8) or 8
    palette = role_colors(depth)
    attrs: dict = {}
    for i, role in enumerate(COLOR_ROLES, start=1):
        try:
            curses_mod.init_pair(i, palette[role], -1)
            attr = curses_mod.color_pair(i)
            # Bold the brand-accent roles so the violet reads as a deliberate
            # accent rather than dim text; the muted roles stay un-bold.
            if role in ("header", "new", "marker", "field"):
                attr |= curses_mod.A_BOLD
            attrs[role] = attr
        except curses_mod.error:
            attrs[role] = mono[role]
    return attrs


def _run_picker(rows: list[Row], all_cwds: bool, warning: str = ""):
    """Run the curses picker. Returns a ("resume", Row), ("new", None), or
    ("quit", None) decision. Kept free of any launching side effect so the
    caller owns the exec and the function stays a pure UI loop. `warning`, if
    given, is shown in the header so a provider warning (e.g. a skipped corrupt
    label file) is visible INSIDE the alternate screen, where a plain stderr
    print would have been hidden."""
    import curses

    def inner(stdscr):
        # curs_set raises on a terminal without cursor-visibility control
        # (e.g. vt100); it is an optional nicety, so treat it as best effort
        #.
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        stdscr.keypad(True)
        # Color map: one setup, then roles -> attributes. should_use_color
        # is the pure policy; _build_color_map does the terminal-dependent pair
        # init and falls back to the monochrome attributes when color is off or
        # init fails, so the UI looks exactly as before on a no-color terminal.
        colors = _build_color_map(curses, should_use_color(curses.has_colors()))
        query = ""
        selected = 0
        scope_all = all_cwds
        current = rows
        notice = ""  # transient in-UI message (e.g. a blocked resume reason)

        def recompute():
            # Land selection on the first real row after the query changes, so
            # the "type to filter, press Enter" habit resumes the top match
            # rather than starting a new session. With no matches, land on the new-session entry (the
            # only entry). Navigation keys change `selected` directly and do not
            # call recompute, so moving up/down does not snap back.
            nonlocal current, selected
            current = filter_rows(rows, query)
            selected = initial_selection(len(current))

        def safe_addstr(y, x, text, attr=curses.A_NORMAL):
            # curses raises if you write at or past the last column/row, which
            # is how a small terminal crashed the picker.
            # Sanitize control chars and clip to the remaining columns, then
            # swallow the boundary error that still occurs writing the very
            # last cell.
            height, width = stdscr.getmaxyx()
            if y < 0 or y >= height or x >= width:
                return
            clipped = clip_line(text, width - 1 - x)
            try:
                stdscr.addstr(y, x, clipped, attr)
            except curses.error:
                pass

        def _prompt_line(scr, prompt, seed=""):
            # A minimal one-line text editor drawn on the bottom row: shows
            # `prompt` + the current buffer, takes printable characters and
            # backspace, returns the buffer on Enter or None on Esc.
            # Kept here (not unit-tested) because it drives curses directly; the
            # write it feeds (write_label) and the argv builders ARE tested.
            buf = seed
            while True:
                # Re-read the terminal size every iteration: it can shrink while
                # the editor is open, and a cached row/col would make the move
                # below raise an uncaught curses.error that killed the picker
                #.
                height, width = scr.getmaxyx()
                y = max(0, height - 1)
                line = prompt_display(prompt, buf, max(0, width - 1))
                try:
                    scr.move(y, 0)
                    scr.clrtoeol()
                    scr.addstr(y, 0, line)
                except curses.error:
                    pass
                scr.refresh()
                try:
                    ch = scr.get_wch()
                except curses.error:
                    continue
                if isinstance(ch, str):
                    if ch == "\x1b":            # esc: cancel
                        return None
                    if ch in ("\n", "\r"):     # enter: accept
                        return buf
                    if ch in ("\x7f", "\b"):   # backspace
                        buf = buf[:-1]
                    elif ch.isprintable():
                        buf += ch
                else:
                    if ch == curses.KEY_ENTER:  # enter decoded as a special key
                        return buf
                    if ch == curses.KEY_BACKSPACE:
                        buf = buf[:-1]

        def _select_entry():
            # Act on the highlighted entry. Nothing is selectable unless it
            # clipped to visible text at this width: in a terminal too small to
            # render the selected entry, Enter must do nothing, not silently
            # start or resume something unseen. The new-session entry starts a fresh session; a session row
            # resumes, but only when its cwd still exists. Returns a decision tuple to return from the loop, or
            # None to stay in the picker.
            nonlocal notice
            if not selected_visible:
                return None
            entry = entries[selected]
            if isinstance(entry, NewSessionEntry):
                return ("new", None)
            blocker = resume_blocker(entry)
            if blocker:
                notice = blocker
                return None
            return ("resume", entry)

        def _edit_label(ents, idx):
            # Inline label editor. Only a session row can be
            # labeled; the new-session entry has nothing to name. Seeds the
            # editor with the current label, writes through kiro_sessions.py on
            # Enter (empty submission removes the label so the row reverts to
            # the auto-title), does nothing on Esc. A provider failure is
            # returned as a notice string rather than raised, so the picker
            # stays up (mirrors the stale-cwd handling). On a successful write
            # it RE-FETCHES rows from the provider (the labels live there, so
            # the in-memory rows are stale), CLEARS the query so the renamed row
            # is not filtered out of view,
            # and KEEPS the reload's warnings in the banner. A reload failure AFTER a successful write is
            # reported distinctly, so the user is never left with the old name
            # and only a vague error.
            # Returns a notice or None.
            nonlocal rows, warning, query, selected
            entry = ents[idx]
            if isinstance(entry, NewSessionEntry):
                return "the new-session entry cannot be labeled"
            seed = entry.label or ""
            new_label = _prompt_line(stdscr, f"label for {entry.name!r}: ", seed)
            # plan_label_edit owns the Esc / empty-noop / write decision purely,
            # so it is unit-tested.
            action, text = plan_label_edit(bool(entry.label), new_label)
            if action in ("cancel", "noop"):
                return None
            try:
                write_label(entry.session_id, text)
            except SystemExit as exc:
                # The write itself failed: nothing changed, show why.
                return str(exc)
            # The write succeeded. Reload so the new name shows; a reload
            # failure here is a SEPARATE condition (the label IS saved).
            try:
                refreshed, refreshed_warning = load_rows_inprocess(all_cwds=scope_all)
            except SystemExit as exc:
                return f"label saved, but reloading the list failed: {exc}"
            rows = refreshed
            warning = refreshed_warning
            query = ""  # show the renamed row rather than filtering it out
            recompute()
            # Keep the edited session highlighted after the reload, rather than
            # snapping to the top row and scrolling the renamed row off-screen
            # in a long list. If the row is gone (e.g. filtered by a scope change) the
            # initial_selection default from recompute() stands.
            found = entry_index_for_session(current, entry.session_id)
            if found is not None:
                selected = found
            if text.strip():
                return f'labeled -> "{text.strip()}"'
            return "label removed"

        recompute()
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            # entries = the new-session entry followed by the filtered rows, so
            # selection, clamping and rendering all operate on one unified list
            #. selected indexes into entries.
            entries = picker_entries(current)
            selected = clamp_selection(selected, len(entries))
            scope = "all" if scope_all else "current"
            two_pane = use_two_pane(width)
            count_txt = f"{len(current)} session" + ("" if len(current) == 1 else "s")
            header = f"kiro launch  [scope: {scope}]  {count_txt}"
            safe_addstr(0, 0, header, curses.A_BOLD)
            # Terse legend; kept within ~72 columns so it does not clip on an
            # 80-column terminal. ^e edits the
            # highlighted session's label, ^g starts a new session.
            safe_addstr(1, 0,
                        "enter select  ^n/^p move  ^e label  ^g new  tab scope  esc quit")
            safe_addstr(2, 0, f"search: {query}")

            # Row 3 shows a transient notice (e.g. a blocked-resume reason set
            # by the last Enter) if present, otherwise a provider warning
            # (skipped corrupt label file), inside the alternate screen so it is
            # actually seen; the list starts one row lower when either is shown
            #.
            # Banner: a provider warning (persists across reloads) and a
            # transient notice (success or an error from the last action) can
            # BOTH be present. A single clipped line always sacrificed one of
            # them, and swapping which one lost just moved the bug. So give
            # each its OWN row when both exist: the
            # warning on row 3, the notice on row 4, the list below. Neither can
            # clip the other.
            list_top = 4
            if warning:
                safe_addstr(3, 0, "! " + warning, curses.A_BOLD)
                list_top = 5
            if notice:
                safe_addstr(list_top, 0, "> " + notice, curses.A_BOLD)
                list_top += 1

            # Two-pane command center on a wide terminal, single-column
            # fallback below the width threshold. In two-pane mode the left rail
            # takes `list_cols` and the detail pane the rest, split by a vertical
            # divider; in single-column mode the list spans the full width and
            # there is no detail pane. The width->layout decision is the pure
            # use_two_pane()/detail_pane_cols() pair, so the split is tested.
            if two_pane:
                detail_cols = detail_pane_cols(width)
                divider_col = width - detail_cols - 1
                list_cols = divider_col
            else:
                detail_cols = 0
                divider_col = None
                list_cols = width

            # The grouped left-rail STRUCTURE, built cheaply from the filtered
            # rows grouped by cwd. build_list_slots does no string work, so
            # this is O(rows) with no per-cell formatting even at --all scope;
            # only the visible window is formatted below. Navigation still runs
            # on `entries`; each slot maps back to its entries index (or None
            # for a header).
            groups = group_rows_by_cwd(current)
            slots = build_list_slots(groups, entries)
            # The slot that carries the currently selected entry, so scrolling
            # keeps it on screen even with headers interleaved.
            sel_slot = slot_index_for_entry(slots, selected)
            if sel_slot is None:
                sel_slot = first_selectable_slot(slots)

            visible = max(0, height - list_top - 1)
            selected_visible = False
            if visible == 0 or list_cols <= 1:
                safe_addstr(min(height - 1, 2), 0, "(terminal too small)")
            else:
                # Scroll so the selected slot is within the visible window, then
                # FORMAT ONLY that window: at most a screenful of row_cell
                # calls per frame, regardless of how many thousands of sessions
                # the filter left in `current`.
                start = max(0, sel_slot - visible + 1) if sel_slot >= visible else 0
                for i in range(start, min(len(slots), start + visible)):
                    slot = slots[i]
                    line = format_slot(slot, max(1, list_cols - 1), two_pane)
                    y = list_top + (i - start)
                    if slot.kind == "header":
                        safe_addstr(y, 0, line.text, colors["header"])
                        continue
                    is_sel = slot.entry_index == selected
                    if is_sel:
                        # Reverse video for the selected row reads clearly in
                        # any theme; draw the whole cell reversed.
                        safe_addstr(y, 0, line.text, curses.A_REVERSE)
                    else:
                        base = colors["new"] if slot.kind == "new" else curses.A_NORMAL
                        safe_addstr(y, 0, line.text, base)
                        # Overlay the colored status dot (column 0) for a real
                        # session row, so running vs idle is pickable at a
                        # glance. The dot is the first character of the cell;
                        # redraw just it in its role color on top of the cell.
                        if slot.kind == "row" and slot.entry is not None and line.text:
                            dot = line.text[0]
                            role = ("dot_run"
                                    if dot == status_dot("running")
                                    else "dot_idle")
                            safe_addstr(y, 0, dot, colors[role])
                    if is_sel and clip_line(line.text, list_cols - 1):
                        selected_visible = True
                if not current:
                    safe_addstr(min(height - 1, list_top + 1), 0,
                                "(no matching sessions; enter starts a new one)")

            # The detail pane: a vertical divider then the highlighted
            # entry's full detail, drawn to the right of the list. Only in
            # two-pane mode; the pure detail_lines() builds the content so the
            # panel is tested without curses.
            if two_pane and divider_col is not None and divider_col >= 1:
                for y in range(list_top, height - 1):
                    safe_addstr(y, divider_col, "\u2502", colors["divider"])  # │
                detail_x = divider_col + 2
                entry = entries[selected] if 0 <= selected < len(entries) else None
                if entry is not None:
                    dlines = detail_lines(entry, max(1, detail_cols - 1))
                    for i, dline in enumerate(dlines):
                        y = list_top + i
                        if y >= height - 1:
                            break
                        if i == 0:
                            attr = colors["new"]  # the title line, highlighted
                        elif dline and dline[0] not in (" ", "\u25cf", "\u25cb") \
                                and dline.split()[0] in (
                                    "status", "updated", "label", "title",
                                    "session", "cwd", "resume"):
                            attr = colors["field"]  # a field-label line
                        else:
                            attr = curses.A_NORMAL
                        safe_addstr(y, detail_x, dline, attr)

            stdscr.refresh()

            # get_wch returns a str for a typed character (including non-ASCII)
            # or an int for a special key, so Unicode labels are searchable
            #. It can raise on a resize/interrupt;
            # treat that as "redraw".
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue

            if isinstance(key, str):
                if key == "\x1b":  # esc
                    return ("quit", None)
                if key in ("\n", "\r"):
                    decision = _select_entry()
                    if decision is not None:
                        return decision
                elif key == "\x05":  # ctrl-e: edit the highlighted label
                    # Gated on selected_visible: in a terminal too small to
                    # render the selected row, editing (and especially removing)
                    # a label the user cannot see must not proceed. Mirrors the _select_entry gate.
                    if selected_visible:
                        notice = _edit_label(entries, selected) or ""
                elif key == "\x07":  # ctrl-g: new session (kept as a shortcut)
                    # New-session is also a selectable row, but the
                    # ctrl-g shortcut is retained so its meaning does not change
                    # from.
                    return ("new", None)
                elif key == "\x0e":  # ctrl-n: down (emacs convention)
                    notice = ""
                    selected = clamp_selection(selected + 1, len(entries))
                elif key == "\x10":  # ctrl-p: up (emacs convention)
                    notice = ""
                    selected = max(0, selected - 1)
                elif key == "\t":
                    scope_all = not scope_all
                    return ("rescope", scope_all)
                elif key in ("\x7f", "\b"):  # backspace / delete
                    notice = ""
                    query = query[:-1]
                    recompute()
                elif key.isprintable():
                    notice = ""
                    query += key
                    recompute()
            else:  # special key (int)
                if key in (curses.KEY_ENTER,):
                    decision = _select_entry()
                    if decision is not None:
                        return decision
                elif key == curses.KEY_UP:
                    notice = ""
                    selected = max(0, selected - 1)
                elif key == curses.KEY_DOWN:
                    # clamp_selection keeps the index in range; on an empty list
                    # it returns 0 rather than driving selected negative, which
                    # would index from the end on the next non-empty frame.
                    notice = ""
                    selected = clamp_selection(selected + 1, len(entries))
                elif key == curses.KEY_BACKSPACE:
                    notice = ""
                    query = query[:-1]
                    recompute()

    return curses.wrapper(inner)


def run(all_cwds: bool) -> int:
    """Load, pick, and launch. The picker can ask to re-scope (tab), which
    reloads with the other cwd setting; a resume or new-session decision execs
    and never returns."""
    scope_all = all_cwds
    while True:
        try:
            rows, warnings = load_rows_inprocess(all_cwds=scope_all)
        except SystemExit as exc:
            # A load failure (kiro-cli error, timeout, unparseable output) must
            # not crash the whole launcher mid-session, which is what an
            # uncaught SystemExit from a rescope reload would do. Report it and quit cleanly with the
            # load's own exit status.
            print(str(exc), file=sys.stderr)
            return 1
        # The warning is passed INTO the picker and shown in its header, not
        # printed to stderr: curses switches to an alternate screen that would
        # hide a prior stderr line, so the user would never see it.
        decision, payload = _run_picker(rows, scope_all, warning=warnings)
        if decision == "quit":
            return 0
        if decision == "rescope":
            scope_all = bool(payload)
            continue
        if decision == "new":
            exec_new_session()  # never returns
        if decision == "resume":
            exec_resume(payload)  # never returns
        return 0  # defensive; exec paths do not fall through


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiro_launch",
        description=(
            "Find and resume a labeled Kiro session from a curses picker "
            ". Wraps kiro_sessions.py; shows your labels over Kiro's "
            "auto-titles."
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Start scoped to every workspace, not just the current directory "
        "(toggle in the UI with tab). The default is the current directory, "
        "which is the handful of sessions for the project you are sitting in.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Current directory by default: you launch from the project you are working
    # in, so its small, scannable session list is what you want first; --all
    # (or tab in the UI) widens to every workspace for cross-project hunting.
    # A heavy user can hold thousands of sessions across 150+ cwds, so an
    # all-cwds default would face you with the whole pile every time (scoped to
    # how the launcher is actually used).
    try:
        return run(all_cwds=args.all)
    except KeyboardInterrupt:
        # curses.wrapper restores the terminal but re-raises KeyboardInterrupt,
        # which would otherwise dump a traceback for an ordinary Ctrl-C quit
        #. 130 is the conventional
        # "terminated by SIGINT" exit status.
        return 130


if __name__ == "__main__":
    sys.exit(main())
