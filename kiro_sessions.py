#!/usr/bin/env python3
"""Findable Kiro session resume via a sidecar label index.

## Why this exists

`kiro-cli chat --resume-picker` lists sessions by an auto-generated `title`
Kiro derives from the conversation. Many read as "New Session" or a truncated
first prompt, so picking the right one from a long list (149 cwds, 100+
sessions in some) is slow and error-prone. Kiro has no supported way to set or
pin a memorable name: editing the `title` in a session's own `session.json`
shows up in the picker, but resuming and sending a message REGENERATES it from
the new content (measured 2026-09-30: a marker became the next prompt). There
is no title-lock field in Kiro's schema.

So the label lives OUTSIDE Kiro's store, in a sidecar JSON file Kiro never
reads or writes, keyed by the stable `sessionId`. Kiro's title regeneration
cannot touch it. This file is both the library and the CLI; subcommands are the
interface (`label`, `find`, `list`), so no separate wrapper scripts are needed.

## Data contract (verified against Kiro 2.25.0, 2026-10-02)

`kiro-cli chat --list-sessions --format json` returns a list of envelopes, one
per cwd, each `{cwd, sessions, complete}`. Each session is
`{sessionId, source, title, updatedAt, executionTarget, status}`. Without
`--all-cwds` only the current cwd's envelope is returned. `sessionId` is the key
passed to `--resume-id`. Session enumeration goes through this command, never by
scraping the TUI or reading the sqlite/jsonl store directly, so a Kiro storage
change does not silently break this.

## The label store: one file per session

`~/.kiro/session-labels/<sessionId>.json`, a DIRECTORY of tiny per-session
files next to Kiro's own store. Overridable with `KIRO_SESSION_LABELS_PATH`
(the directory; used by tests). The store is LOCAL to the machine, kept beside
Kiro's own store rather than in any synced personal directory.

One file per session is deliberate and is what lets labels survive WITHOUT
silent loss. The per-file layout also makes cross-machine replication safe if
the store ever lives in a synced directory: a single shared JSON blob fails
that, because a sync that resolves a plain-file conflict by last-modified-wins
with no merge would overwrite one machine's whole dictionary and silently drop
a label. With one file per session, two machines labeling different sessions
touch different files and never collide. That design is kept even though the
store is LOCAL today (`~/.kiro/session-labels`, no sync): it costs nothing, and
it leaves the door open to a future synced or shared store without a redesign.

The FILENAME is the session id and the sole key. `set_label`/`remove_label`
address a session only by its `<id>.json` path, and `load_labels` keys reads on
the filename too, so there is never a file that `find` can see but relabel or
remove cannot reach.

Each file:

    {"schemaVersion": 1, "sessionId": "<id>", "label": "<text>", "updatedAt": "<iso8601>"}

The `sessionId` field inside is informational only (it records what the file
was written for); the filename is authoritative.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1

# Default label store (a DIRECTORY of per-session files) under the user's Kiro
# directory, next to Kiro's own session store. The label metadata lives
# beside Kiro's store rather than in any synced personal directory. Env
# override exists for
# tests and for a machine whose store lives elsewhere.
_DEFAULT_LABELS = Path.home() / ".kiro" / "session-labels"


def labels_dir() -> Path:
    override = os.environ.get("KIRO_SESSION_LABELS_PATH")
    return Path(override).expanduser() if override else _DEFAULT_LABELS


# ---------------------------------------------------------------------------
# Label store: one file per session (see module docstring for why)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    # Second precision, UTC, trailing Z. A conventional machine-written
    # timestamp format; the exact format is not load-bearing.
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_file(session_id: str, directory: Path) -> Path:
    """Resolve the per-session file path, refusing any id that would escape the
    labels directory. SessionIds in practice are `sess_<uuid>`, `cli_<uuid>` or
    bare hex, but this never trusts that: an id with a slash, `..`, a NUL, or
    any other path separator is rejected rather than sanitized, because a
    silently rewritten id would label the wrong session or write outside the
    store. The resolved path must sit directly inside the labels directory."""
    if "/" in session_id or "\\" in session_id or "\x00" in session_id:
        raise SystemExit(
            f"error: session id {session_id!r} contains a path separator; refusing it."
        )
    if session_id in (".", "..") or session_id.startswith("."):
        raise SystemExit(
            f"error: session id {session_id!r} is not a valid file name; refusing it."
        )
    # Build the path WITHOUT resolving symlinks. Path.resolve() would follow a
    # symlink planted in the store (e.g. sess_a.json -> sess_b.json), so a
    # write or remove addressed to one id could hit another session's file.
    # The directory itself is resolved
    # once to anchor the containment check; the leaf is never dereferenced.
    base = directory.resolve()
    candidate = base / f"{session_id}.json"
    if candidate.parent != base:
        raise SystemExit(
            f"error: session id {session_id!r} would write outside the label store; refusing it."
        )
    # A symlink at the leaf is refused outright: resolving it is exactly the
    # redirect we are guarding against, and a label file is never legitimately
    # a symlink.
    if candidate.is_symlink():
        raise SystemExit(
            f"error: label path for {session_id!r} is a symlink; refusing to follow it."
        )
    return candidate


def _write_label_file(session_id: str, label: str, directory: Path) -> None:
    """Write one per-session label file atomically and owner-only.

    Each session is an independent file, so two machines labeling DIFFERENT
    sessions never touch the same path and Obsidian Sync has nothing to resolve
    (spec: labels survive across machines). No cross-session lock is
    needed; the atomic replace makes concurrent writes to the SAME session
    cleanly last-writer-wins rather than corrupting.

    The temp file is created 0o600 via os.open BEFORE any bytes are written,
    rather than via ael_concurrency.atomic_write_text + a later chmod. That
    helper copies an existing target's mode onto the temp file, so re-labeling
    a session whose file was somehow 0644 would expose the new private bytes at
    0644 for the window before the chmod.
    Creating owner-only from the start closes that window for both the new-file
    and the overwrite case. The unique mkstemp name also avoids the shared-temp
    collision from the earlier single-blob design."""
    directory.mkdir(parents=True, exist_ok=True)
    target = _session_file(session_id, directory)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session_id,
        "label": label,
        "updatedAt": _now_iso(),
    }
    body = json.dumps(payload, indent=2) + "\n"
    # mkstemp creates the temp file 0o600 by construction, in the same dir so
    # os.replace is an atomic rename rather than a cross-filesystem copy.
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_labels(directory: Path | None = None) -> dict[str, str]:
    """Return `{sessionId: label}` by reading every per-session file.

    A missing directory is an empty store (first `label` creates it). An
    individual unreadable or malformed file is SKIPPED with a stderr warning,
    not fatal: one corrupt file in a synced directory must not blind `find` to
    every other label. This is the per-file analogue of the old
    tolerate-a-bad-entry behavior."""
    d = directory or labels_dir()
    if not d.exists():
        return {}
    if not d.is_dir():
        raise SystemExit(
            f"error: label store {d} exists but is not a directory. "
            "The store is one JSON file per session under this path."
        )
    # List the directory through os.scandir so a read failure (e.g. the dir
    # chmod'd 000) RAISES rather than being swallowed. Path.glob() suppresses
    # the directory-read error on Python 3.13, which would turn an unreadable
    # store into a silent empty result and hide a real matching label.
    try:
        entries = sorted(
            (e for e in os.scandir(d) if e.name.endswith(".json")),
            key=lambda e: e.name,
        )
    except OSError as exc:
        raise SystemExit(
            f"error: cannot read label store {d} ({exc}). "
            "Refusing to report an empty store when the directory is unreadable."
        )
    labels: dict[str, str] = {}
    for entry in entries:
        f = Path(entry.path)
        # A symlinked label file is skipped, matching the write/remove side's
        # refusal to follow one: a label
        # file is never legitimately a symlink.
        if entry.is_symlink():
            print(f"warning: skipping symlinked label file {f.name}", file=sys.stderr)
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            # Any failure reading or parsing ONE file must skip only that file,
            # never abort the whole read: the invariant is that one bad file
            # cannot blind `find` to every other label. The anticipated cases
            # are JSONDecodeError/UnicodeDecodeError/OSError, but json also
            # raises bare ValueError (a >4300-digit integer) and RecursionError
            # (deeply nested input), so catch broadly rather than enumerate and
            # miss one.
            print(f"warning: skipping unreadable label file {f.name} ({exc})", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"warning: skipping malformed label file {f.name}", file=sys.stderr)
            continue
        # The FILENAME is the session id, and it is the sole key. `set_label`
        # and `remove_label` address a session only by its canonical
        # `<id>.json` path, so keying reads on anything else (such as a
        # `sessionId` field that disagrees with the filename after a manual
        # rename) would let a file be visible to `find` yet unreachable by
        # relabel/remove. One key, one path.
        sid = f.stem
        label = data.get("label")
        if isinstance(label, str) and label:
            labels[sid] = label
    return labels


def set_label(session_id: str, label: str, directory: Path | None = None) -> None:
    # An empty id would create an unreachable, unresumable entry. A label
    # stripped to nothing is equally useless.
    if not session_id or not session_id.strip():
        raise SystemExit("error: a non-empty session id is required.")
    if not label or not label.strip():
        raise SystemExit("error: a non-empty label is required.")
    _write_label_file(session_id, label, directory or labels_dir())


def remove_label(session_id: str, directory: Path | None = None) -> bool:
    """Return True if a label was removed, False if there was none."""
    if not session_id or not session_id.strip():
        raise SystemExit("error: a non-empty session id is required.")
    target = _session_file(session_id, directory or labels_dir())
    if target.exists():
        target.unlink()
        return True
    return False


def get_label(session_id: str, labels: dict[str, str]) -> str | None:
    """Return the label for a session from a `{sessionId: label}` map, or None."""
    return labels.get(session_id)


# ---------------------------------------------------------------------------
# Kiro session enumeration
# ---------------------------------------------------------------------------


class KiroUnavailable(SystemExit):
    pass


def _kiro_bin() -> str:
    # shutil.which resolves PATH exactly as the user's own shell does when they
    # type `kiro-cli`. Pinning an absolute path was considered but rejected on
    # evidence: if PATH held a malicious `kiro-cli`,
    # every Kiro invocation is already compromised, not just this tool, and a
    # hard-coded path would break the normal ~/.local/bin/kiro-cli symlink.
    found = shutil.which("kiro-cli")
    if not found:
        raise KiroUnavailable(
            "error: kiro-cli not found on PATH. This tool enumerates sessions "
            "via `kiro-cli chat --list-sessions`."
        )
    return found


def _list_timeout() -> float:
    """Enumeration timeout, overridable for a slow or very large session store
. 60s is ample for a JSON list on a
    normal store; the override exists for the pathological case."""
    raw = os.environ.get("KIRO_SESSIONS_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            raise SystemExit(
                f"error: KIRO_SESSIONS_TIMEOUT must be a number of seconds, got {raw!r}."
            )
    return 60.0


def list_sessions(all_cwds: bool, runner=None) -> list[dict]:
    """Return a flat list of session dicts, each carrying its own `cwd` so a
    caller can build the resume command and show where the session lives.

    `runner` is injected by tests; by default it shells out to kiro-cli. A
    non-zero exit or unparseable output is an error, not an empty list, so a
    broken enumeration never reads as "no sessions"."""
    if runner is None:
        cmd = [_kiro_bin(), "chat", "--list-sessions", "--format", "json"]
        if all_cwds:
            cmd.append("--all-cwds")
        timeout = _list_timeout()

        def runner(_cmd=cmd, _t=timeout):  # noqa: ANN001
            return subprocess.run(
                _cmd, capture_output=True, text=True, timeout=_t
            )

    proc = runner()
    if proc.returncode != 0:
        raise SystemExit(
            f"error: `kiro-cli chat --list-sessions` exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:400]}"
        )
    try:
        envelopes = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: could not parse --list-sessions output ({exc}).")

    sessions: list[dict] = []
    if not isinstance(envelopes, list):
        raise SystemExit(
            "error: --list-sessions did not return a list of envelopes "
            f"(got {type(envelopes).__name__}); refusing to treat it as 'no sessions'."
        )
    for env in envelopes:
        if not isinstance(env, dict) or "sessions" not in env:
            # A missing `sessions` key is a malformed envelope, not an empty
            # workspace. Defaulting it to [] would turn a broken enumeration
            # into a silent "no matches".
            raise SystemExit(
                "error: --list-sessions returned a malformed envelope "
                f"(no 'sessions' field): {str(env)[:200]}"
            )
        if env.get("complete") is not True:
            # Require an explicit `true`. A missing field, null, or 0 all leave
            # completeness unestablished, so treating them as complete would let
            # a partial listing read as a clean, complete result.
            raise SystemExit(
                f"error: --list-sessions did not confirm a complete listing for cwd "
                f"{env.get('cwd', '?')!r} (complete={env.get('complete')!r}); "
                "retry rather than trust a possibly partial set."
            )
        raw_sessions = env.get("sessions")
        if not isinstance(raw_sessions, list):
            # `"sessions": {}` or `""` passes the key check above but is not a
            # list; iterating it would silently yield nothing (a dict yields
            # keys) and read as "no sessions".
            raise SystemExit(
                "error: --list-sessions envelope has a non-list 'sessions' "
                f"({type(raw_sessions).__name__}) for cwd {env.get('cwd', '?')!r}."
            )
        cwd = env.get("cwd", "")
        for s in raw_sessions:
            sid = s.get("sessionId") if isinstance(s, dict) else None
            if not isinstance(sid, str) or not sid.strip():
                # A session with no usable id cannot be resumed; a resume
                # command built from it would be `--resume-id ` with nothing
                # (or only whitespace) after it. A
                # whitespace-only id is as unusable as an empty one, so strip
                # before the check. Reject rather than emit an unusable row.
                raise SystemExit(
                    "error: --list-sessions returned a session with no usable "
                    f"sessionId for cwd {env.get('cwd', '?')!r}: {str(s)[:200]}"
                )
            s = dict(s)
            s["cwd"] = cwd
            sessions.append(s)
    return sessions


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _resume_cmd(session_id: str) -> str:
    return f"kiro-cli chat --resume-id {session_id}"


def _display_rows(sessions: list[dict], labels: dict[str, str]) -> list[dict]:
    """Merge the chosen label over Kiro's auto-title. `name` is what a human
    scans; `title` is kept so a search can still match the auto-title."""
    rows = []
    for s in sessions:
        sid = s.get("sessionId", "")
        label = get_label(sid, labels)
        rows.append(
            {
                "sessionId": sid,
                "label": label,
                "title": s.get("title", ""),
                "name": label or s.get("title", ""),
                "cwd": s.get("cwd", ""),
                "updatedAt": s.get("updatedAt", ""),
                "status": s.get("status", ""),
                "resume": _resume_cmd(sid),
            }
        )
    # Most-recent first: that is the order a resume search usually wants.
    rows.sort(key=lambda r: r["updatedAt"], reverse=True)
    return rows


def _print_rows(rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("(no matching sessions)")
        return
    for r in rows:
        marker = "label" if r["label"] else "auto "
        name = r["name"] or "(untitled)"
        print(f"{name}")
        print(f"    [{marker}] {r['updatedAt']}  {r['cwd']}")
        print(f"    {r['resume']}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_label(args: argparse.Namespace) -> int:
    if args.remove:
        removed = remove_label(args.session_id)
        print(
            f"removed label for {args.session_id}"
            if removed
            else f"no label was set for {args.session_id}"
        )
        return 0
    if not args.label:
        raise SystemExit("error: a label is required (or pass --remove).")
    set_label(args.session_id, args.label)
    print(f'labeled {args.session_id} -> "{args.label}"')
    print(f"    {_resume_cmd(args.session_id)}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    labels = load_labels()
    sessions = list_sessions(all_cwds=args.all_cwds)
    rows = _display_rows(sessions, labels)
    q = args.query.lower()
    matched = [
        r
        for r in rows
        if q in (r["label"] or "").lower()
        or q in r["title"].lower()
        or q in r["sessionId"].lower()
    ]
    _print_rows(matched, args.json)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    labels = load_labels()
    sessions = list_sessions(all_cwds=args.all_cwds)
    rows = _display_rows(sessions, labels)
    if args.labeled_only:
        rows = [r for r in rows if r["label"]]
    _print_rows(rows, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiro_sessions",
        description=(
            "Findable Kiro session resume via a sidecar label index. "
            "Labels live outside Kiro's store, so title regeneration cannot wipe them."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("label", help="Set or remove a memorable label for a session id")
    pl.add_argument("session_id", help="Kiro sessionId (from `find`/`list` or --list-sessions)")
    pl.add_argument("label", nargs="?", help="Memorable name to attach")
    pl.add_argument("--remove", action="store_true", help="Remove the label for this session")
    pl.set_defaults(func=cmd_label)

    pf = sub.add_parser("find", help="Search labels and auto-titles; print resume commands")
    pf.add_argument("query", help="Case-insensitive substring to match on label, title, or id")
    pf.add_argument(
        "--all-cwds",
        action="store_true",
        help="Search every workspace, not just the current directory",
    )
    pf.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    pf.set_defaults(func=cmd_find)

    ps = sub.add_parser("list", help="List sessions with labels merged over auto-titles")
    ps.add_argument(
        "--all-cwds",
        action="store_true",
        help="List every workspace, not just the current directory",
    )
    ps.add_argument(
        "--labeled-only", action="store_true", help="Only show sessions that have a label"
    )
    ps.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    ps.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
