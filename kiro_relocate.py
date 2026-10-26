#!/usr/bin/env python3
"""Relocate Kiro sessions after a workspace directory move.

Kiro keys its session store by sha256(absolute_cwd)[:16]. Moving a repo on disk
orphans its sessions: `ks` / `kiro-cli chat --list-sessions` no longer surface
them from the new path, though the data is intact and resumable by id.

This re-indexes sessions whose recorded workspacePath/rootPaths fell under an old
directory, to the new directory, so they list from the new path again.

Store layout (observed, schemaVersion 1 sessions, index v3):
  ~/.kiro/sessions/<hash>/<session-dir>/session.json   (hash = sha256(cwd)[:16])
  ~/.kiro/session-index/<hash>.jsonl                   (one {"op":"add","sessionPath":"<hash>/<sess>","at":ms} per line)
Session dirs are `sess_*` or `cli_*`. A per-hash `.index/` cache is NOT copied;
Kiro rebuilds it.

Safe by design: --dry-run prints the plan and changes nothing. Real runs move
data immediately, so pass --dry-run first to preview. Real runs refuse to
clobber an existing target session dir. Old-path index entries are left in
place unless --remove-old is passed, so the move can be validated first.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sys
from pathlib import Path


def kiro_root() -> Path:
    """The Kiro store root. Overridable with KIRO_STORE_ROOT so tests can point
    at a disposable fixture directory instead of the real ~/.kiro."""
    override = os.environ.get("KIRO_STORE_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".kiro"


def sessions_dir() -> Path:
    return kiro_root() / "sessions"


def index_dir() -> Path:
    return kiro_root() / "session-index"


def cwd_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def iter_session_jsons():
    yield from sessions_dir().glob("*/*/session.json")


def plan(old_root: str, new_root: str):
    """Return [(session_dir_path, old_hash, new_hash, old_wp, new_wp)] for every
    session whose workspacePath is old_root or under it."""
    old_root = old_root.rstrip("/")
    new_root = new_root.rstrip("/")
    moves = []
    for sj in iter_session_jsons():
        try:
            d = json.loads(sj.read_text())
        except Exception:
            continue
        wp = (d.get("workspacePaths") or d.get("rootPaths") or [""])[0]
        if not wp:
            continue
        if wp == old_root or wp.startswith(old_root + "/"):
            new_wp = new_root + wp[len(old_root):]
            moves.append((sj.parent, cwd_hash(wp), cwd_hash(new_wp), wp, new_wp))
    return moves


def rewrite_session_paths(session_json: Path, old_root: str, new_root: str):
    """Rewrite every workspacePaths/rootPaths entry that is old_root or under it
    to the corresponding path under new_root. Entries outside old_root are left
    unchanged, so a session with a secondary path under the old root (e.g. a
    worktree) is fully repointed, not just its primary path."""
    old_root = old_root.rstrip("/")
    new_root = new_root.rstrip("/")

    def repoint(p: str) -> str:
        if p == old_root or p.startswith(old_root + "/"):
            return new_root + p[len(old_root):]
        return p

    d = json.loads(session_json.read_text())
    for key in ("workspacePaths", "rootPaths"):
        if key in d and isinstance(d[key], list):
            d[key] = [repoint(p) for p in d[key]]
    # Atomic write: a crash mid-write must not corrupt session.json, whose dir
    # has already been moved. Write a sibling temp file, then os.replace (atomic
    # on the same filesystem) over the original.
    tmp = session_json.with_suffix(session_json.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    os.replace(tmp, session_json)


def append_index(new_hash: str, session_name: str, at_ms: int):
    line = json.dumps({"op": "add",
                       "sessionPath": f"{new_hash}/{session_name}",
                       "at": at_ms}) + "\n"
    index_dir().mkdir(parents=True, exist_ok=True)
    with (index_dir() / f"{new_hash}.jsonl").open("a") as f:
        f.write(line)


def old_index_at(old_hash: str, session_name: str) -> int:
    f = index_dir() / f"{old_hash}.jsonl"
    if not f.exists():
        return 0
    # Kiro appends one {"op":"add",...,"at":ms} per session open, so the LAST
    # matching line carries the most recent timestamp. Returning the first
    # would stamp the migrated entry with the oldest open, skewing a recency
    # sort in `ks` until Kiro next appends.
    latest = 0
    for ln in f.read_text().splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("sessionPath", "").endswith("/" + session_name):
            latest = o.get("at", latest)
    return latest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="old absolute workspace root")
    ap.add_argument("--new", required=True, help="new absolute workspace root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove-old", action="store_true",
                    help="after moving, delete the old-path index entries and empty dirs")
    args = ap.parse_args()

    # Both roots must be absolute. Kiro keys its store by sha256(absolute_cwd),
    # so a relative --new would move sessions under the hash of a relative
    # string that Kiro never queries: they vanish from `ks` with no error. A
    # relative --old simply matches nothing. Refuse both up front.
    if not Path(args.old).is_absolute() or not Path(args.new).is_absolute():
        print("refusing: --old and --new must be absolute paths.", file=sys.stderr)
        return 2

    # Guard against --new being --old or under it: plan() matches by path
    # prefix, so a re-run would re-match already-migrated sessions and move
    # them again (/new/new/...). Refuse before planning. The reverse (--old
    # under --new) is fine: migrated paths no longer match the old prefix.
    old_root = args.old.rstrip("/")
    new_root = args.new.rstrip("/")
    if new_root == old_root or new_root.startswith(old_root + "/"):
        print(f"refusing: --new ({args.new}) is --old ({args.old}) or inside it; "
              "a re-run would migrate already-moved sessions again.", file=sys.stderr)
        return 2

    moves = plan(args.old, args.new)
    if not moves:
        print("no sessions found under", args.old)
        return 0

    print(f"{len(moves)} session(s) to relocate: {args.old} -> {args.new}\n")
    moved_old_hashes: set[str] = set()
    skipped = 0
    for sdir, oh, nh, owp, nwp in moves:
        name = sdir.name
        tgt = sessions_dir() / nh / name
        action = "DRY" if args.dry_run else "MOVE"
        print(f"  [{action}] {name}")
        print(f"          {oh}/ -> {nh}/   (wp: {owp} -> {nwp})")
        # Report a clobber conflict on BOTH dry-run and real runs, so the
        # preview reflects what a real run would skip.
        if tgt.exists():
            print(f"          !! target exists, would skip to avoid clobber: {tgt}"
                  if args.dry_run else
                  f"          !! target exists, skipping to avoid clobber: {tgt}")
            skipped += 1
            continue
        if args.dry_run:
            continue
        (sessions_dir() / nh).mkdir(parents=True, exist_ok=True)
        shutil.move(str(sdir), str(tgt))
        rewrite_session_paths(tgt / "session.json", args.old, args.new)
        append_index(nh, name, old_index_at(oh, name))
        moved_old_hashes.add(oh)

    if args.remove_old and not args.dry_run:
        # Derive the cleanup set from sessions that ACTUALLY moved, never from
        # the full plan. A clobber-skipped session keeps its old-hash index
        # entry and dir; deleting them would orphan it from `ks`/--list-sessions
        # even though its data survives.
        for oh in sorted(moved_old_hashes):
            d = sessions_dir() / oh
            # Only touch the old hash if no session dirs remain under it, so a
            # skipped session sharing the hash keeps both its dir and its index.
            remaining = (
                d.exists()
                and any(c.name.startswith(("sess_", "cli_")) for c in d.iterdir())
            )
            if remaining:
                print(f"  kept old index {oh}.jsonl (sessions still under this hash)")
                continue
            idx = index_dir() / f"{oh}.jsonl"
            if idx.exists():
                idx.unlink()
                print(f"  removed old index {oh}.jsonl")
            if d.exists():
                shutil.rmtree(d)
                print(f"  removed emptied old session dir {oh}/")

    if skipped:
        # A clobber-skip is a partial failure: signal it in the exit code so a
        # caller (or a `ks relocate` wrapper) does not read success.
        verb = "would be skipped" if args.dry_run else "skipped"
        print(f"\n{skipped} session(s) {verb} (target already existed). Exit 2.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
