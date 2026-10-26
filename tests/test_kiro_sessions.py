"""Tests for kiro_sessions.py: the sidecar label index.

Synthetic fixtures only. Session enumeration is exercised through an injected
runner, never a real kiro-cli call, so the suite is hermetic and does not depend
on the developer's own Kiro session store.
"""

import importlib.util
import json
import os
import sys as _sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

COVERS = ["kiro_sessions.py"]

# Scripts live at the repo root (one level up from tests/).
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "kiro_sessions.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kiro_sessions", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ks = _load_module()


def _runner_returning(stdout: str, returncode: int = 0, stderr: str = ""):
    """Build a `runner` the module's list_sessions accepts: a zero-arg callable
    returning an object with returncode/stdout/stderr."""
    def runner():
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
    return runner


def _envelope(cwd, sessions):
    return {"cwd": cwd, "sessions": sessions, "complete": True}


def _session(sid, title="New Session", updated="2026-10-01T00:00:00Z", status="idle"):
    return {
        "sessionId": sid,
        "source": "v3",
        "title": title,
        "updatedAt": updated,
        "executionTarget": "local",
        "status": status,
    }


class LabelStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        # The store is a DIRECTORY of per-session files. Point at a nested path
        # that does not exist yet, to prove the first write creates it.
        self.dir = Path(self._tmp.name) / "nested" / "kiro-session-labels"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_store_reads_as_empty_not_error(self):
        """A first run has no store dir yet; that must be an empty map, not a
        crash, or `label` could never create it."""
        self.assertEqual(ks.load_labels(self.dir), {})

    def test_set_then_get_roundtrips_and_persists(self):
        ks.set_label("sess_a", "Murmur HUD work", self.dir)
        labels = ks.load_labels(self.dir)
        self.assertEqual(ks.get_label("sess_a", labels), "Murmur HUD work")
        # One per-session file was created, parent dirs made.
        f = self.dir / "sess_a.json"
        self.assertTrue(f.exists())
        on_disk = json.loads(f.read_text())
        self.assertEqual(on_disk["label"], "Murmur HUD work")
        self.assertEqual(on_disk["sessionId"], "sess_a")
        self.assertIn("updatedAt", on_disk)

    def test_each_session_is_its_own_file(self):
        """The core of option A: different sessions never share a file, so two
        machines labeling different sessions cannot collide on sync."""
        ks.set_label("sess_a", "alpha", self.dir)
        ks.set_label("sess_b", "beta", self.dir)
        files = sorted(p.name for p in self.dir.glob("*.json"))
        self.assertEqual(files, ["sess_a.json", "sess_b.json"])

    def test_set_overwrites_existing_label(self):
        ks.set_label("sess_a", "first", self.dir)
        ks.set_label("sess_a", "second", self.dir)
        self.assertEqual(ks.get_label("sess_a", ks.load_labels(self.dir)), "second")

    def test_remove_reports_whether_a_label_existed(self):
        ks.set_label("sess_a", "x", self.dir)
        self.assertTrue(ks.remove_label("sess_a", self.dir))
        self.assertFalse(ks.remove_label("sess_a", self.dir))
        self.assertIsNone(ks.get_label("sess_a", ks.load_labels(self.dir)))

    def test_one_corrupt_file_is_skipped_not_fatal(self):
        """A single unreadable file in a synced directory must not blind the
        reader to every other label."""
        ks.set_label("sess_good", "fine", self.dir)
        (self.dir / "sess_bad.json").write_text("{ not json", encoding="utf-8")
        (self.dir / "sess_wrongshape.json").write_text(json.dumps(["a", "list"]), encoding="utf-8")
        labels = ks.load_labels(self.dir)
        self.assertEqual(labels.get("sess_good"), "fine")
        self.assertNotIn("sess_bad", labels)
        self.assertNotIn("sess_wrongshape", labels)

    def test_parser_limit_files_are_skipped_not_fatal(self):
        """json raises bare ValueError (huge integer) and RecursionError (deep
        nesting), outside JSONDecodeError. One such file must still only skip
        itself, leaving valid labels searchable."""
        self.dir.mkdir(parents=True, exist_ok=True)
        ks.set_label("sess_good", "fine", self.dir)
        # A >4300-digit integer: json.loads raises ValueError, not JSONDecodeError.
        (self.dir / "sess_hugeint.json").write_text(
            '{"label": ' + "9" * 4400 + "}", encoding="utf-8")
        # Deeply nested arrays: json.loads raises RecursionError.
        (self.dir / "sess_deep.json").write_text(
            "[" * 20000 + "]" * 20000, encoding="utf-8")
        labels = ks.load_labels(self.dir)
        self.assertEqual(labels.get("sess_good"), "fine")
        self.assertNotIn("sess_hugeint", labels)
        self.assertNotIn("sess_deep", labels)

    def test_store_path_that_is_a_file_is_refused(self):
        """If the override points at a plain file instead of a directory, say so
        rather than silently misbehaving."""
        f = Path(self._tmp.name) / "not-a-dir"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            ks.load_labels(f)
        self.assertIn("not a directory", str(cm.exception))

    def test_filename_is_the_session_key(self):
        """The filename is the sole key, used by read, write, and remove alike,
        so there is no split where a file is findable but not relabelable. A hand-renamed file is simply keyed
        by its new stem."""
        ks.set_label("sess_real", "the label", self.dir)
        (self.dir / "sess_real.json").rename(self.dir / "renamed.json")
        labels = ks.load_labels(self.dir)
        # Keyed by the new filename stem, not the stale sessionId field inside.
        self.assertEqual(labels.get("renamed"), "the label")
        self.assertNotIn("sess_real", labels)

    def test_set_is_atomic_leaving_no_tmp_behind(self):
        ks.set_label("sess_a", "x", self.dir)
        leftovers = list(self.dir.glob("*.tmp")) + list(self.dir.glob(".*.tmp"))
        self.assertEqual(leftovers, [], f"atomic write left a temp file: {leftovers}")

    def test_label_file_is_written_owner_only_0600(self):
        """Labels can name private work and the store lives in a synced vault,
        so each file must be owner-only. A fresh per-session
        file has no prior mode to inherit, closing the 0644 window the review
        flagged for the single-blob design."""
        import stat
        ks.set_label("sess_a", "x", self.dir)
        mode = stat.S_IMODE((self.dir / "sess_a.json").stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_relabel_tightens_an_existing_loose_mode_file(self):
        """Re-labeling a session whose file was somehow 0644 must end at 0600,
        and the private bytes must never land in a 0644 file. The write creates
        a fresh 0o600 temp and replaces, so the loose mode is never inherited."""
        import stat
        ks.set_label("sess_a", "first", self.dir)
        os.chmod(self.dir / "sess_a.json", 0o644)
        ks.set_label("sess_a", "second", self.dir)
        mode = stat.S_IMODE((self.dir / "sess_a.json").stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0o600 after relabel, got {oct(mode)}")
        self.assertEqual(ks.get_label("sess_a", ks.load_labels(self.dir)), "second")

    def test_empty_session_id_or_label_is_rejected(self):
        with self.assertRaises(SystemExit):
            ks.set_label("", "name", self.dir)
        with self.assertRaises(SystemExit):
            ks.set_label("sess_a", "   ", self.dir)

    def test_path_traversal_session_id_is_refused(self):
        """A hostile or malformed id must never write outside the store (defense
        in depth; real Kiro ids are safe, but this never trusts that)."""
        for bad in ("../escape", "a/b", "..", ".", r"a\b", "with\x00nul", ".hidden"):
            with self.assertRaises(SystemExit):
                ks.set_label(bad, "x", self.dir)

    def test_symlinked_label_file_is_refused_not_followed(self):
        """A symlink in the store must not redirect a write or remove to another
        session's file."""
        ks.set_label("sess_b", "real b", self.dir)
        # Plant sess_a.json as a symlink to sess_b.json.
        (self.dir / "sess_a.json").symlink_to(self.dir / "sess_b.json")
        # Writing sess_a must not clobber sess_b through the symlink.
        with self.assertRaises(SystemExit):
            ks.set_label("sess_a", "attacker", self.dir)
        # Removing sess_a must not delete sess_b through the symlink.
        with self.assertRaises(SystemExit):
            ks.remove_label("sess_a", self.dir)
        self.assertEqual(ks.get_label("sess_b", ks.load_labels(self.dir)), "real b")
        # The symlink itself is skipped on read, not followed.
        self.assertNotIn("sess_a", ks.load_labels(self.dir))

    def test_unreadable_store_dir_raises_not_empty(self):
        """An unreadable store directory must surface, not read as an empty
        store that hides existing labels.
        glob() swallows the dir-read error on 3.13; scandir does not."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        ks.set_label("sess_a", "x", self.dir)
        os.chmod(self.dir, 0o000)
        try:
            with self.assertRaises(SystemExit) as cm:
                ks.load_labels(self.dir)
            self.assertIn("cannot read label store", str(cm.exception))
        finally:
            os.chmod(self.dir, 0o700)  # restore so tearDown can clean up

    def test_two_sequential_labels_both_survive(self):
        ks.set_label("sess_a", "alpha", self.dir)
        ks.set_label("sess_b", "beta", self.dir)
        labels = ks.load_labels(self.dir)
        self.assertEqual(ks.get_label("sess_a", labels), "alpha")
        self.assertEqual(ks.get_label("sess_b", labels), "beta")

    def test_concurrent_labels_do_not_lose_updates(self):
        """Threads racing on set_label must all survive. With one file per
        session there is no shared file to clobber, which is exactly why option
        A removes the cross-machine loss the single blob had."""
        import threading
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait()
                ks.set_label(f"sess_{i}", f"label-{i}", self.dir)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"workers errored: {errors}")
        labels = ks.load_labels(self.dir)
        for i in range(n):
            self.assertEqual(
                ks.get_label(f"sess_{i}", labels), f"label-{i}",
                f"sess_{i} label was lost to a race",
            )


class ListSessionsTests(unittest.TestCase):
    def test_flattens_envelopes_and_tags_each_session_with_its_cwd(self):
        out = json.dumps([
            _envelope("/work/a", [_session("sess_1")]),
            _envelope("/work/b", [_session("sess_2"), _session("sess_3")]),
        ])
        sessions = ks.list_sessions(all_cwds=True, runner=_runner_returning(out))
        self.assertEqual(len(sessions), 3)
        by_id = {s["sessionId"]: s for s in sessions}
        self.assertEqual(by_id["sess_1"]["cwd"], "/work/a")
        self.assertEqual(by_id["sess_3"]["cwd"], "/work/b")

    def test_nonzero_exit_is_an_error_not_empty(self):
        """A broken enumeration must never read as 'no sessions' - that would
        make `find` silently miss everything."""
        runner = _runner_returning("", returncode=2, stderr="boom")
        with self.assertRaises(SystemExit) as cm:
            ks.list_sessions(all_cwds=False, runner=runner)
        self.assertIn("exited 2", str(cm.exception))

    def test_unparseable_output_is_an_error(self):
        runner = _runner_returning("not json at all")
        with self.assertRaises(SystemExit) as cm:
            ks.list_sessions(all_cwds=False, runner=runner)
        self.assertIn("could not parse", str(cm.exception))

    def test_empty_cwd_envelope_yields_no_sessions(self):
        out = json.dumps([_envelope("/fresh/worktree", [])])
        self.assertEqual(ks.list_sessions(all_cwds=False, runner=_runner_returning(out)), [])

    def test_malformed_envelope_missing_sessions_is_rejected(self):
        """A missing 'sessions' key is a broken envelope, not an empty cwd;
        defaulting it to [] would mask the breakage."""
        out = json.dumps([{"cwd": "/work", "complete": True}])
        with self.assertRaises(SystemExit) as cm:
            ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
        self.assertIn("malformed envelope", str(cm.exception))

    def test_incomplete_listing_is_rejected(self):
        out = json.dumps([{"cwd": "/work", "sessions": [], "complete": False}])
        with self.assertRaises(SystemExit) as cm:
            ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
        self.assertIn("complete listing", str(cm.exception))

    def test_missing_or_falsey_complete_is_rejected(self):
        """complete must be explicitly true; missing, null, and 0 all leave
        completeness unestablished."""
        for env in (
            {"cwd": "/w", "sessions": []},            # missing
            {"cwd": "/w", "sessions": [], "complete": None},
            {"cwd": "/w", "sessions": [], "complete": 0},
            {"cwd": "/w", "sessions": [], "complete": "true"},  # string, not bool
        ):
            out = json.dumps([env])
            with self.assertRaises(SystemExit) as cm:
                ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
            self.assertIn("complete listing", str(cm.exception))

    def test_non_list_output_is_rejected(self):
        out = json.dumps({"not": "a list"})
        with self.assertRaises(SystemExit) as cm:
            ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
        self.assertIn("list of envelopes", str(cm.exception))

    def test_non_list_sessions_value_is_rejected(self):
        """`"sessions": {}` or `""` passes the key check but is not iterable as
        sessions; it must be rejected, not read as empty."""
        for bad in ({}, "", 5):
            out = json.dumps([{"cwd": "/work", "sessions": bad, "complete": True}])
            with self.assertRaises(SystemExit) as cm:
                ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
            self.assertIn("non-list 'sessions'", str(cm.exception))

    def test_session_without_id_is_rejected(self):
        """A session with no usable sessionId cannot be resumed; reject rather
        than emit `--resume-id ` with nothing after it. Covers missing, empty,
        and whitespace-only ids."""
        for bad in ({"title": "x", "updatedAt": "t"},   # missing
                    {"sessionId": "", "title": "x"},     # empty
                    {"sessionId": "   ", "title": "x"}):  # whitespace-only
            out = json.dumps([_envelope("/work", [bad])])
            with self.assertRaises(SystemExit) as cm:
                ks.list_sessions(all_cwds=False, runner=_runner_returning(out))
            self.assertIn("sessionId", str(cm.exception))

    def test_timeout_override_rejects_non_numeric(self):
        """KIRO_SESSIONS_TIMEOUT must be a number; a bad value fails loudly
        rather than silently falling back."""
        import os as _os
        prev = _os.environ.get("KIRO_SESSIONS_TIMEOUT")
        _os.environ["KIRO_SESSIONS_TIMEOUT"] = "not-a-number"
        try:
            with self.assertRaises(SystemExit):
                ks._list_timeout()
        finally:
            if prev is None:
                _os.environ.pop("KIRO_SESSIONS_TIMEOUT", None)
            else:
                _os.environ["KIRO_SESSIONS_TIMEOUT"] = prev

    def test_timeout_override_accepts_numeric(self):
        import os as _os
        prev = _os.environ.get("KIRO_SESSIONS_TIMEOUT")
        _os.environ["KIRO_SESSIONS_TIMEOUT"] = "120"
        try:
            self.assertEqual(ks._list_timeout(), 120.0)
        finally:
            if prev is None:
                _os.environ.pop("KIRO_SESSIONS_TIMEOUT", None)
            else:
                _os.environ["KIRO_SESSIONS_TIMEOUT"] = prev


class FindAndListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "kiro-session-labels"
        # Two sessions: one labeled, one not.
        ks.set_label("sess_hud", "Murmur HUD reposition", self.dir)
        self.sessions = [
            _session("sess_hud", title="New Session", updated="2026-10-01T10:00:00Z"),
            _session("sess_sync", title="Check GitHub Repos For Updates",
                     updated="2026-10-02T10:00:00Z"),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self):
        labels = ks.load_labels(self.dir)
        return ks._display_rows(self.sessions, labels)

    def test_label_wins_over_auto_title_in_display_name(self):
        rows = {r["sessionId"]: r for r in self._rows()}
        self.assertEqual(rows["sess_hud"]["name"], "Murmur HUD reposition")
        self.assertEqual(rows["sess_hud"]["label"], "Murmur HUD reposition")
        # Unlabeled session falls back to Kiro's auto-title.
        self.assertEqual(rows["sess_sync"]["name"], "Check GitHub Repos For Updates")
        self.assertIsNone(rows["sess_sync"]["label"])

    def test_rows_sorted_most_recent_first(self):
        rows = self._rows()
        self.assertEqual(rows[0]["sessionId"], "sess_sync")  # 10-02 newer than 10-01

    def test_each_row_carries_a_runnable_resume_command(self):
        rows = {r["sessionId"]: r for r in self._rows()}
        self.assertEqual(
            rows["sess_hud"]["resume"], "kiro-cli chat --resume-id sess_hud"
        )

    def test_find_matches_label_substring_case_insensitively(self):
        rows = {r["sessionId"]: r for r in self._rows()}
        q = "murmur"
        matched = [r for r in rows.values()
                   if q in (r["label"] or "").lower() or q in r["title"].lower()]
        self.assertEqual([r["sessionId"] for r in matched], ["sess_hud"])

    def test_find_matches_auto_title_even_without_a_label(self):
        rows = list(self._rows())
        q = "github"
        matched = [r for r in rows
                   if q in (r["label"] or "").lower() or q in r["title"].lower()]
        self.assertEqual([r["sessionId"] for r in matched], ["sess_sync"])


if __name__ == "__main__":
    unittest.main()
