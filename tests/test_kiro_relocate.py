"""Tests for kiro_relocate: hash, path rewrite, and index repoint on fixtures.

No reliance on a real ~/.kiro: KIRO_STORE_ROOT points every store access at a
tmp dir. Covers the three things that can silently corrupt a relocate: the hash
must match Kiro's, the session.json workspace paths must be rewritten, and the
new index must list the moved sessions.
"""
import json
import os
import hashlib
from pathlib import Path

import importlib.util

MOD = Path(__file__).resolve().parent.parent / "kiro_relocate.py"
spec = importlib.util.spec_from_file_location("kiro_relocate", MOD)
kr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kr)


def _mk_session(root: Path, cwd: str, sess_name: str, at_ms: int):
    """Create a fixture session under the hash of cwd, plus its index line."""
    h = hashlib.sha256(cwd.encode()).hexdigest()[:16]
    sdir = root / "sessions" / h / sess_name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "session.json").write_text(json.dumps({
        "id": sess_name, "title": "t",
        "workspacePaths": [cwd], "rootPaths": [cwd],
    }))
    idx = root / "session-index"
    idx.mkdir(parents=True, exist_ok=True)
    (idx / f"{h}.jsonl").open("a").write(
        json.dumps({"op": "add", "sessionPath": f"{h}/{sess_name}", "at": at_ms}) + "\n")
    return h


def test_cwd_hash_matches_known_kiro_value():
    # Pinned values: sha256(path)[:16] for two fixed example paths, so a change
    # to the hashing would fail loudly.
    assert kr.cwd_hash("/Users/alex/projects/app-one") == "0d7e4b313db63582"
    assert kr.cwd_hash("/Users/alex/projects/app-two") == "67fd55f89816f453"


def test_plan_finds_sessions_under_old_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoA"
    _mk_session(tmp_path, old, "sess_1", 1000)
    _mk_session(tmp_path, old + "/sub", "sess_2", 2000)  # nested cwd under old
    _mk_session(tmp_path, "/work/other", "sess_3", 3000)  # unrelated
    moves = kr.plan(old, "/work/moved/repoA")
    found = {m[0].name for m in moves}
    assert found == {"sess_1", "sess_2"}
    # nested path is remapped preserving its suffix
    nwps = {m[4] for m in moves}
    assert "/work/moved/repoA" in nwps
    assert "/work/moved/repoA/sub" in nwps


def test_execute_moves_dir_rewrites_json_and_indexes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoB"
    new = "/work/moved/repoB"
    _mk_session(tmp_path, old, "sess_x", 5000)

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()

    new_hash = kr.cwd_hash(new)
    moved = tmp_path / "sessions" / new_hash / "sess_x"
    assert moved.is_dir(), "session dir should move under the new-path hash"
    # workspace paths rewritten
    d = json.loads((moved / "session.json").read_text())
    assert d["workspacePaths"] == [new]
    assert d["rootPaths"] == [new]
    # new index lists it
    idx = (tmp_path / "session-index" / f"{new_hash}.jsonl").read_text()
    assert f"{new_hash}/sess_x" in idx


def test_refuses_to_clobber_existing_target(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoC"
    new = "/work/moved/repoC"
    _mk_session(tmp_path, old, "sess_y", 6000)
    # pre-create the target so the move must refuse
    new_hash = kr.cwd_hash(new)
    (tmp_path / "sessions" / new_hash / "sess_y").mkdir(parents=True)

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()
    # the original must still be present (not clobbered/removed)
    old_hash = kr.cwd_hash(old)
    assert (tmp_path / "sessions" / old_hash / "sess_y").is_dir()


def test_remove_old_only_after_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoD"
    new = "/work/moved/repoD"
    _mk_session(tmp_path, old, "sess_z", 7000)
    old_hash = kr.cwd_hash(old)

    import sys
    monkeypatch.setattr(sys, "argv",
                        ["kiro_relocate", "--old", old, "--new", new, "--remove-old"])
    kr.main()
    # old index removed, old dir emptied and removed
    assert not (tmp_path / "session-index" / f"{old_hash}.jsonl").exists()
    assert not (tmp_path / "sessions" / old_hash).exists()


def test_remove_old_keeps_a_clobber_skipped_session_sharing_the_old_hash(tmp_path, monkeypatch):
    """Regression for the CRITICAL finding: two sessions share an old hash, one
    is clobber-skipped, and --remove-old must NOT delete the old index/dir, or
    the skipped session is orphaned from ks/--list-sessions."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoE"
    new = "/work/moved/repoE"
    old_hash = kr.cwd_hash(old)
    new_hash = kr.cwd_hash(new)
    # Two sessions under the same old hash.
    _mk_session(tmp_path, old, "sess_keep", 8000)
    _mk_session(tmp_path, old, "sess_move", 8100)
    # Pre-create the NEW target for sess_keep only, so its move is skipped.
    (tmp_path / "sessions" / new_hash / "sess_keep").mkdir(parents=True)

    import sys
    monkeypatch.setattr(sys, "argv",
                        ["kiro_relocate", "--old", old, "--new", new, "--remove-old"])
    kr.main()

    # sess_move relocated; sess_keep stayed under the old hash (clobber skip).
    assert (tmp_path / "sessions" / new_hash / "sess_move").is_dir()
    assert (tmp_path / "sessions" / old_hash / "sess_keep").is_dir()
    # The old index MUST survive, because sess_keep still lives under old_hash.
    old_idx = tmp_path / "session-index" / f"{old_hash}.jsonl"
    assert old_idx.exists(), "old index deleted -> sess_keep orphaned"
    assert f"{old_hash}/sess_keep" in old_idx.read_text()


def test_append_index_creates_missing_index_dir(tmp_path, monkeypatch):
    """Regression for the MEDIUM finding: the session-index directory may not
    exist on a fresh install; append_index must create it, not raise."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoF"
    new = "/work/moved/repoF"
    # Build a session by hand WITHOUT creating the session-index directory.
    old_hash = kr.cwd_hash(old)
    sdir = tmp_path / "sessions" / old_hash / "sess_fresh"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps(
        {"id": "sess_fresh", "workspacePaths": [old], "rootPaths": [old]}))
    assert not (tmp_path / "session-index").exists()

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()  # must not raise FileNotFoundError

    new_hash = kr.cwd_hash(new)
    new_idx = tmp_path / "session-index" / f"{new_hash}.jsonl"
    assert new_idx.exists()
    assert f"{new_hash}/sess_fresh" in new_idx.read_text()


def test_rewrite_repoints_every_path_under_the_old_root(tmp_path, monkeypatch):
    """Regression for the LOW finding: a session with a secondary path under the
    old root (e.g. a worktree) has every such entry rewritten, not just the
    first; entries outside the old root are left untouched."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoG"
    new = "/work/moved/repoG"
    old_hash = kr.cwd_hash(old)
    sdir = tmp_path / "sessions" / old_hash / "sess_wt"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({
        "id": "sess_wt",
        "workspacePaths": [old, old + "/.worktrees/x", "/elsewhere/keep"],
        "rootPaths": [old],
    }))
    (tmp_path / "session-index").mkdir(parents=True, exist_ok=True)

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()

    new_hash = kr.cwd_hash(new)
    d = json.loads((tmp_path / "sessions" / new_hash / "sess_wt" / "session.json").read_text())
    assert d["workspacePaths"] == [new, new + "/.worktrees/x", "/elsewhere/keep"]
    assert d["rootPaths"] == [new]


def test_clobber_skip_returns_nonzero_exit(tmp_path, monkeypatch):
    """A clobber-skip is a partial failure and must surface in the exit code,
    not be masked by a 0 return (second-round review finding)."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoH"
    new = "/work/moved/repoH"
    _mk_session(tmp_path, old, "sess_c", 9000)
    new_hash = kr.cwd_hash(new)
    (tmp_path / "sessions" / new_hash / "sess_c").mkdir(parents=True)  # force skip

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    rc = kr.main()
    assert rc == 2, "a clobber-skip must return a non-zero exit"


def test_dry_run_reports_clobber_conflict(tmp_path, monkeypatch, capsys):
    """Dry-run must preview the clobber conflict and return non-zero, so the
    plan reflects what a real run would skip (second-round review finding)."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoI"
    new = "/work/moved/repoI"
    _mk_session(tmp_path, old, "sess_d", 9100)
    new_hash = kr.cwd_hash(new)
    (tmp_path / "sessions" / new_hash / "sess_d").mkdir(parents=True)

    import sys
    monkeypatch.setattr(sys, "argv",
                        ["kiro_relocate", "--old", old, "--new", new, "--dry-run"])
    rc = kr.main()
    out = capsys.readouterr().out
    assert "target exists" in out, "dry-run should preview the clobber conflict"
    assert rc == 2
    # dry-run must not actually move anything
    old_hash = kr.cwd_hash(old)
    assert (tmp_path / "sessions" / old_hash / "sess_d").is_dir()


def test_rewrite_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    """session.json is written via a temp-file-then-replace, so no .tmp sibling
    survives and the content is valid JSON (second-round review finding)."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoJ"
    new = "/work/moved/repoJ"
    _mk_session(tmp_path, old, "sess_e", 9200)

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()

    new_hash = kr.cwd_hash(new)
    sdir = tmp_path / "sessions" / new_hash / "sess_e"
    assert (sdir / "session.json").is_file()
    assert not (sdir / "session.json.tmp").exists(), "temp file must be renamed away"
    json.loads((sdir / "session.json").read_text())  # valid JSON, no raise


def test_refuses_new_inside_old(tmp_path, monkeypatch, capsys):
    """--new under --old would re-migrate already-moved sessions on a re-run
    (round-3 finding); refuse before planning."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work"
    new = "/work/moved"
    _mk_session(tmp_path, "/work/repoA", "sess_sub", 9300)

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    rc = kr.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "refusing" in err
    # nothing moved
    assert (tmp_path / "sessions" / kr.cwd_hash("/work/repoA") / "sess_sub").is_dir()


def test_old_index_at_returns_latest_timestamp(tmp_path, monkeypatch):
    """A session opened repeatedly has several index lines; the migrated entry
    must carry the most recent `at`, not the first (round-3 finding)."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    old = "/work/repoK"
    new = "/work/moved/repoK"
    old_hash = kr.cwd_hash(old)
    sdir = tmp_path / "sessions" / old_hash / "sess_multi"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps(
        {"id": "sess_multi", "workspacePaths": [old], "rootPaths": [old]}))
    idx = tmp_path / "session-index"
    idx.mkdir(parents=True, exist_ok=True)
    # three opens, ascending timestamps
    with (idx / f"{old_hash}.jsonl").open("a") as f:
        for at in (1000, 2000, 3000):
            f.write(json.dumps({"op": "add", "sessionPath": f"{old_hash}/sess_multi", "at": at}) + "\n")

    assert kr.old_index_at(old_hash, "sess_multi") == 3000

    import sys
    monkeypatch.setattr(sys, "argv", ["kiro_relocate", "--old", old, "--new", new])
    kr.main()
    new_hash = kr.cwd_hash(new)
    new_idx = (idx / f"{new_hash}.jsonl").read_text()
    assert '"at": 3000' in new_idx


def test_refuses_relative_paths(tmp_path, monkeypatch, capsys):
    """A relative --new would move sessions under a hash Kiro never queries,
    so they vanish silently; a relative --old matches nothing. Refuse both."""
    monkeypatch.setenv("KIRO_STORE_ROOT", str(tmp_path))
    _mk_session(tmp_path, "/work/repoL", "sess_rel", 9400)
    import sys

    # relative --new
    monkeypatch.setattr(sys, "argv",
                        ["kiro_relocate", "--old", "/work/repoL", "--new", "work/moved"])
    rc = kr.main()
    assert rc == 2
    assert "absolute" in capsys.readouterr().err

    # relative --old
    monkeypatch.setattr(sys, "argv",
                        ["kiro_relocate", "--old", "work/repoL", "--new", "/work/moved"])
    rc = kr.main()
    assert rc == 2
    assert "absolute" in capsys.readouterr().err

    # the session was never moved
    assert (tmp_path / "sessions" / kr.cwd_hash("/work/repoL") / "sess_rel").is_dir()
