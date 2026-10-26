"""Tests for kiro_launch.py: the curses TUI session launcher.

The curses UI itself needs a real terminal and is not unit-tested. Everything
it calls is pure and IS tested here: row projection, token-AND filtering, the
resume/new argv builders, and the data load (through an injected runner, so the
suite never shells out to the real kiro_sessions.py or kiro-cli).
"""

import importlib.util
import json
import sys as _sys
import types
import unittest
from pathlib import Path

COVERS = ["kiro_launch.py"]

# Scripts live at the repo root (one level up from tests/).
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "kiro_launch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kiro_launch", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass can resolve its own module's
    # annotations (dataclasses looks the module up in sys.modules).
    _sys.modules["kiro_launch"] = mod
    spec.loader.exec_module(mod)
    return mod


kl = _load_module()


def _runner_returning(stdout: str, returncode: int = 0, stderr: str = ""):
    def runner():
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def _json_row(sid, name=None, title="New Session", label=None, cwd="/w",
              updated="2026-10-01T00:00:00Z", status="idle"):
    return {
        "sessionId": sid,
        "name": name if name is not None else (label or title),
        "title": title,
        "label": label,
        "cwd": cwd,
        "updatedAt": updated,
        "status": status,
        "resume": f"kiro-cli chat --resume-id {sid}",
    }


class RowProjectionTests(unittest.TestCase):
    def test_basic_projection(self):
        rows = kl.rows_from_json([_json_row("sess_a", label="Murmur HUD", title="New Session")])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.session_id, "sess_a")
        self.assertEqual(r.label, "Murmur HUD")
        self.assertEqual(r.cwd, "/w")

    def test_rows_without_usable_id_are_dropped(self):
        """A row with no resumable id must never appear as a choice."""
        payload = [
            _json_row("sess_ok", title="fine"),
            {"sessionId": "", "title": "empty id"},
            {"sessionId": "   ", "title": "whitespace id"},
            {"title": "missing id entirely"},
        ]
        rows = kl.rows_from_json(payload)
        self.assertEqual([r.session_id for r in rows], ["sess_ok"])

    def test_json_null_label_is_no_label_but_string_none_is_kept(self):
        """A JSON null label (the provider's signal for absence) becomes no
        label, so name falls back to the auto-title. A literal string "None"
        is NOT special-cased: the provider emits real null for absence, so a
        string "None" is a label the operator deliberately set and must be kept."""
        rows = kl.rows_from_json([
            _json_row("s1", name="New Session", title="New Session", label=None),
            _json_row("s2", name="New Session", title="New Session", label="None"),
        ])
        self.assertIsNone(rows[0].label)
        self.assertEqual(rows[1].label, "None")

    def test_name_falls_back_to_title_then_placeholder(self):
        rows = kl.rows_from_json([
            {"sessionId": "s1", "title": "Only a title"},
            {"sessionId": "s2"},  # no name, no title
        ])
        self.assertEqual(rows[0].name, "Only a title")
        self.assertEqual(rows[1].name, "(no title)")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.rows = kl.rows_from_json([
            _json_row("s1", label="Synthesia renewal work", title="Review API"),
            _json_row("s2", label=None, title="Check GitHub Repos For Updates"),
            _json_row("s3", label="Murmur HUD reposition", title="New Session"),
        ])

    def test_empty_query_returns_all(self):
        self.assertEqual(len(kl.filter_rows(self.rows, "")), 3)
        self.assertEqual(len(kl.filter_rows(self.rows, "   ")), 3)

    def test_matches_label(self):
        got = [r.session_id for r in kl.filter_rows(self.rows, "murmur")]
        self.assertEqual(got, ["s3"])

    def test_matches_auto_title_without_a_label(self):
        got = [r.session_id for r in kl.filter_rows(self.rows, "github")]
        self.assertEqual(got, ["s2"])

    def test_token_and_is_order_independent(self):
        """'renew synth' matches 'Synthesia renewal work' though the words are
        reversed: each token is an independent subsequence, AND-ed."""
        got = [r.session_id for r in kl.filter_rows(self.rows, "renew synth")]
        self.assertEqual(got, ["s1"])

    def test_fuzzy_subsequence_match(self):
        """'mmhd' matches 'Murmur HUD reposition' as a subsequence (the chars
        occur in order), which plain substring would miss (spec)."""
        got = [r.session_id for r in kl.filter_rows(self.rows, "mmhd")]
        self.assertEqual(got, ["s3"])

    def test_subsequence_respects_order(self):
        """'dhmm' must NOT match 'Murmur HUD': the characters are not in order."""
        got = [r.session_id for r in kl.filter_rows(self.rows, "dhmm")]
        self.assertNotIn("s3", got)

    def test_token_does_not_match_across_name_title_boundary(self):
        """A token must not match by spanning the name/title boundary: a row
        with name 'ab' and title 'cd' must NOT match 'bc', which a single joined
        haystack would have allowed."""
        rows = kl.rows_from_json([
            _json_row("s1", name="ab", title="cd", label="ab"),
        ])
        self.assertEqual(kl.filter_rows(rows, "bc"), [])
        # Each half still matches on its own.
        self.assertEqual([r.session_id for r in kl.filter_rows(rows, "ab")], ["s1"])
        self.assertEqual([r.session_id for r in kl.filter_rows(rows, "cd")], ["s1"])

    def test_matches_session_id(self):
        got = [r.session_id for r in kl.filter_rows(self.rows, "s2")]
        self.assertIn("s2", got)

    def test_id_match_is_prefix_only_not_substring(self):
        """A token matches a sessionId only as a PREFIX, never a substring in
        the middle, so a short token cannot incidentally match arbitrary hex
        anywhere in a UUID. Here 'bcd' is a
        substring of the id but not a prefix and must NOT match on the id; it is
        also not a subsequence of the name/title, so the row must not match at
        all."""
        rows = kl.rows_from_json([
            _json_row("sess_abcdef", label="Zzz", title="Qqq"),
        ])
        got = [r.session_id for r in kl.filter_rows(rows, "bcd")]
        self.assertEqual(got, [])
        # The same id DOES match on a real prefix.
        got2 = [r.session_id for r in kl.filter_rows(rows, "sess_ab")]
        self.assertEqual(got2, ["sess_abcdef"])

    def test_case_insensitive(self):
        self.assertEqual(
            [r.session_id for r in kl.filter_rows(self.rows, "MURMUR")], ["s3"]
        )

    def test_no_match_returns_empty(self):
        self.assertEqual(kl.filter_rows(self.rows, "zzz-nothing"), [])


class ArgvTests(unittest.TestCase):
    def test_resume_argv_is_argv_form_not_shell(self):
        self.assertEqual(
            kl.resume_argv("sess_a"),
            ["kiro-cli", "chat", "--resume-id", "sess_a"],
        )

    def test_resume_argv_rejects_empty(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                kl.resume_argv(bad)

    def test_resume_argv_does_not_shell_interpret_a_hostile_id(self):
        """argv form means a hostile id is one argument, never shell syntax."""
        argv = kl.resume_argv("x; rm -rf /")
        self.assertEqual(argv, ["kiro-cli", "chat", "--resume-id", "x; rm -rf /"])

    def test_new_session_argv_uses_v3(self):
        self.assertEqual(kl.new_session_argv(), ["kiro-cli", "chat", "--v3"])

    def test_ensure_kiro_cli_absolutizes_a_relative_which_result(self):
        """kiro-cli must be resolved to an absolute path before any chdir, so
        changing into a session's workspace cannot change which binary execs
. This feeds a RELATIVE which() result through
        the real os.path.abspath (no mock on abspath), so it would fail if
        _ensure_kiro_cli returned which()'s value unchanged."""
        import os as _os
        from unittest import mock
        with mock.patch.object(kl.shutil, "which", return_value="bin/kiro-cli"):
            got = kl._ensure_kiro_cli()
        self.assertTrue(_os.path.isabs(got), f"expected absolute, got {got!r}")
        self.assertEqual(got, _os.path.abspath("bin/kiro-cli"))

    def test_ensure_kiro_cli_raises_when_absent(self):
        from unittest import mock
        with mock.patch.object(kl.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                kl._ensure_kiro_cli()


class LabelArgvTests(unittest.TestCase):
    """The label-write argv builders. argv form, never shell, so a
    label with metacharacters is one argument; empty id refused."""

    def test_label_argv_shape(self):
        argv = kl.label_argv("/path/kiro_sessions.py", "sess_a", "Budget Q4")
        # A `--` terminator precedes the label so the provider's argparse cannot
        # read a label like "--remove" as a flag.
        self.assertEqual(
            argv[1:], ["/path/kiro_sessions.py", "label", "sess_a", "--", "Budget Q4"]
        )
        # argv[0] is the interpreter so the sibling script runs under the same python.
        self.assertEqual(argv[0], kl.sys.executable)

    def test_label_argv_terminator_protects_option_like_labels(self):
        """A label that looks like an option ("--remove", "-x") is placed after
        the `--` terminator, so the provider stores it as text instead of acting
        on it as a flag."""
        for hostile in ("--remove", "-x", "--help", "--all-cwds"):
            argv = kl.label_argv("/s.py", "sess_a", hostile)
            self.assertIn("--", argv)
            self.assertEqual(argv[-1], hostile)
            # The terminator comes before the label, never after it.
            self.assertLess(argv.index("--"), len(argv) - 1)
            self.assertEqual(argv[argv.index("--") + 1], hostile)

    def test_label_argv_does_not_shell_interpret_a_hostile_label(self):
        argv = kl.label_argv("/s.py", "sess_a", "x; rm -rf /")
        self.assertEqual(argv[-1], "x; rm -rf /")  # one argument, not shell syntax

    def test_label_argv_rejects_empty_id(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                kl.label_argv("/s.py", bad, "x")

    def test_remove_label_argv_shape(self):
        argv = kl.remove_label_argv("/s.py", "sess_a")
        self.assertEqual(argv[1:], ["/s.py", "label", "sess_a", "--remove"])

    def test_remove_label_argv_rejects_empty_id(self):
        with self.assertRaises(ValueError):
            kl.remove_label_argv("/s.py", "")


class WriteLabelTests(unittest.TestCase):
    """write_label routes through the provider: a non-empty label SETS (with a
    `--` terminator), an empty/whitespace label REMOVES, a provider failure
    raises SystemExit carrying the provider's stderr. subprocess.run
    is patched so the ACTUAL command built is inspected and no child runs."""

    def _capture_cmd(self, session_id, label):
        import subprocess as _sp
        from unittest import mock
        captured = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(_sp, "run", side_effect=fake_run):
            kl.write_label(session_id, label)  # runner=None -> default builds cmd
        return captured["cmd"]

    def test_non_empty_label_sets_with_terminator(self):
        cmd = self._capture_cmd("sess_a", "My Label")
        self.assertIn("label", cmd)
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-1], "My Label")
        self.assertNotIn("--remove", cmd)

    def test_empty_label_removes(self):
        for empty in ("", "   "):
            cmd = self._capture_cmd("sess_a", empty)
            self.assertIn("--remove", cmd)
            self.assertNotIn("My Label", cmd)

    def test_option_like_label_is_stored_not_acted_on(self):
        # "--remove" as a label must be written as text (after `--`), not run as
        # the remove flag.
        cmd = self._capture_cmd("sess_a", "--remove")
        term = cmd.index("--")
        self.assertEqual(cmd[term + 1], "--remove")  # the LABEL, after the terminator

    def test_provider_failure_raises_with_stderr(self):
        def failing_runner():
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom from provider")
        with self.assertRaises(SystemExit) as cm:
            kl.write_label("sess_a", "x", runner=failing_runner)
        self.assertIn("boom from provider", str(cm.exception))

    def test_empty_session_id_raises_before_running(self):
        calls = []

        def runner():
            calls.append(1)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with self.assertRaises(SystemExit):
            kl.write_label("", "x", runner=runner)
        self.assertEqual(calls, [])  # never ran the provider


class InitialSelectionTests(unittest.TestCase):
    """After the list is (re)filtered, selection lands on the first REAL row
    when there is one, so "type to filter, press Enter" resumes the top match
    instead of starting a new session; with no rows it lands on the new-session
    entry."""

    def test_lands_on_first_row_when_rows_exist(self):
        # entries are [new-session, row0, row1, ...]; index 1 is the first row.
        self.assertEqual(kl.initial_selection(3), 1)
        self.assertEqual(kl.initial_selection(1), 1)

    def test_lands_on_new_session_entry_when_no_rows(self):
        self.assertEqual(kl.initial_selection(0), 0)


class EntryIndexForSessionTests(unittest.TestCase):
    """entry_index_for_session maps a session id to its entries index (row
    index + 1, because the new-session entry is index 0), or None if absent. It
    is how a just-edited row stays highlighted after a reload."""

    def _rows(self, *ids):
        return kl.rows_from_json([_json_row(i) for i in ids])

    def test_first_row_is_entries_index_1(self):
        rows = self._rows("a", "b", "c")
        self.assertEqual(kl.entry_index_for_session(rows, "a"), 1)
        self.assertEqual(kl.entry_index_for_session(rows, "c"), 3)

    def test_absent_session_is_none(self):
        rows = self._rows("a", "b")
        self.assertIsNone(kl.entry_index_for_session(rows, "zzz"))

    def test_empty_rows_is_none(self):
        self.assertIsNone(kl.entry_index_for_session([], "a"))


class PlanLabelEditTests(unittest.TestCase):
    """plan_label_edit owns the Esc / empty-noop / write decision, so the edit
    branches are tested without curses."""

    def test_esc_cancels(self):
        self.assertEqual(kl.plan_label_edit(True, None), ("cancel", None))
        self.assertEqual(kl.plan_label_edit(False, None), ("cancel", None))

    def test_empty_on_unlabeled_row_is_noop(self):
        self.assertEqual(kl.plan_label_edit(False, ""), ("noop", None))
        self.assertEqual(kl.plan_label_edit(False, "   "), ("noop", None))

    def test_empty_on_labeled_row_writes_the_removal(self):
        # An empty submission on a LABELED row is a real action: remove it.
        action, text = kl.plan_label_edit(True, "")
        self.assertEqual(action, "write")
        self.assertEqual(text.strip(), "")

    def test_non_empty_writes(self):
        self.assertEqual(kl.plan_label_edit(False, "Budget Q4"), ("write", "Budget Q4"))
        self.assertEqual(kl.plan_label_edit(True, "Renamed"), ("write", "Renamed"))


class PromptDisplayTests(unittest.TestCase):
    """prompt_display budgets by DISPLAY WIDTH and shows the buffer tail, so a
    wide-char (CJK) prompt cannot hide the typed buffer."""

    def test_short_prompt_and_buffer_fit(self):
        self.assertEqual(kl.prompt_display("label: ", "abc", 80), "label: abc")

    def test_buffer_tail_shown_when_buffer_is_long(self):
        # The end of the buffer (where the cursor is) must stay visible.
        out = kl.prompt_display("l: ", "0123456789", 8)
        self.assertTrue(out.endswith("9"), f"expected tail visible, got {out!r}")
        self.assertLessEqual(kl.line_cols(out), 8)

    def test_wide_char_prompt_still_leaves_room_for_buffer(self):
        # 20 wide chars = 40 columns of prompt, but at 20 columns the prompt is
        # truncated so the ASCII buffer tail is still shown.
        wide_prompt = "\u65e5" * 20 + ": "
        out = kl.prompt_display(wide_prompt, "abc", 20)
        self.assertIn("c", out)  # the buffer is visible
        self.assertLessEqual(kl.line_cols(out), 20)

    def test_zero_width_budget_is_empty(self):
        self.assertEqual(kl.prompt_display("l: ", "abc", 0), "")


class TailStderrTests(unittest.TestCase):
    """tail_stderr returns the final exception line of provider stderr, where a
    Python traceback puts the cause, so a banner that clips from the left still
    shows the reason rather than stack frames."""

    def test_short_text_is_unchanged(self):
        self.assertEqual(kl.tail_stderr("boom"), "boom")

    def test_returns_the_final_exception_line_not_frames(self):
        trace = (
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in <module>\n"
            "    _mkstemp_inner(dir, prefix, suffix)\n"
            "PermissionError: [Errno 13] Permission denied: '/store'"
        )
        out = kl.tail_stderr(trace)
        self.assertEqual(out, "PermissionError: [Errno 13] Permission denied: '/store'")
        self.assertNotIn("_mkstemp_inner", out)

    def test_overlong_final_line_keeps_its_tail(self):
        long_line = "ValueError: " + ("x" * 500)
        out = kl.tail_stderr(long_line, limit=120)
        self.assertTrue(out.startswith("..."))
        self.assertLessEqual(len(out), 123)

    def test_empty_is_empty(self):
        self.assertEqual(kl.tail_stderr(""), "")
        self.assertEqual(kl.tail_stderr(None), "")


class PickerEntriesTests(unittest.TestCase):
    """picker_entries puts the new-session entry first, then the rows, and is
    never empty, so the launcher is always a front door."""

    def _rows(self, n):
        return kl.rows_from_json([_json_row(f"s{i}") for i in range(n)])

    def test_new_session_entry_is_first(self):
        entries = kl.picker_entries(self._rows(2))
        self.assertIsInstance(entries[0], kl.NewSessionEntry)
        self.assertEqual(len(entries), 3)  # 1 sentinel + 2 rows

    def test_present_even_with_no_rows(self):
        entries = kl.picker_entries([])
        self.assertEqual(len(entries), 1)
        self.assertIsInstance(entries[0], kl.NewSessionEntry)

    def test_rows_follow_the_entry_in_order(self):
        rows = self._rows(3)
        entries = kl.picker_entries(rows)
        self.assertEqual(entries[1:], rows)


class LoadRowsTests(unittest.TestCase):
    def test_loads_and_projects(self):
        out = json.dumps([_json_row("s1", label="x"), _json_row("s2")])
        rows, warnings = kl.load_rows(all_cwds=True, runner=_runner_returning(out))
        self.assertEqual([r.session_id for r in rows], ["s1", "s2"])
        self.assertEqual(warnings, "")

    def test_stderr_warning_is_returned_on_clean_load(self):
        """A clean (exit 0) load that carried a provider warning must surface
        it, not swallow it."""
        out = json.dumps([_json_row("s1")])
        runner = _runner_returning(out, returncode=0, stderr="warning: skipping bad.json")
        rows, warnings = kl.load_rows(all_cwds=False, runner=runner)
        self.assertEqual(len(rows), 1)
        self.assertIn("skipping bad.json", warnings)

    def test_nonzero_exit_raises_not_empty(self):
        runner = _runner_returning("", returncode=1, stderr="boom")
        with self.assertRaises(SystemExit) as cm:
            kl.load_rows(all_cwds=False, runner=runner)
        self.assertIn("exited 1", str(cm.exception))

    def test_unparseable_output_raises(self):
        with self.assertRaises(SystemExit) as cm:
            kl.load_rows(all_cwds=False, runner=_runner_returning("not json"))
        self.assertIn("could not parse", str(cm.exception))

    def test_non_list_payload_raises(self):
        with self.assertRaises(SystemExit) as cm:
            kl.load_rows(all_cwds=False, runner=_runner_returning(json.dumps({"a": 1})))
        self.assertIn("did not return a list", str(cm.exception))

    def test_empty_list_is_no_rows_not_error(self):
        rows, warnings = kl.load_rows(all_cwds=False, runner=_runner_returning("[]"))
        self.assertEqual(rows, [])
        self.assertEqual(warnings, "")

    def test_timeout_raises_clean_systemexit_not_traceback(self):
        """A provider that runs past the outer deadline must surface as a clean
        SystemExit, not an uncaught TimeoutExpired traceback: load_rows is
        called inside the picker loop, so a traceback would crash the whole
        launcher mid-session."""
        import subprocess as _sp

        def timing_out_runner():
            raise _sp.TimeoutExpired(cmd=["kiro_sessions.py", "list"], timeout=90.0)

        with self.assertRaises(SystemExit) as cm:
            kl.load_rows(all_cwds=False, runner=timing_out_runner)
        self.assertIn("did not finish", str(cm.exception))
        self.assertIn("KIRO_SESSIONS_TIMEOUT", str(cm.exception))

    def test_oserror_from_runner_is_normalized_to_systemexit(self):
        """A spawn failure (e.g. too many open files) during load must not
        escape as an OSError traceback: a label-edit reload calls load_rows
        inside the picker, so it must surface as a banner-able SystemExit."""
        def oserror_runner():
            raise OSError(24, "Too many open files")
        with self.assertRaises(SystemExit) as cm:
            kl.load_rows(all_cwds=False, runner=oserror_runner)
        self.assertIn("could not run", str(cm.exception))


class ListTimeoutTests(unittest.TestCase):
    """The launcher's outer subprocess timeout must never be SHORTER than the
    provider's own configured enumeration deadline, or it would kill a
    slow-but-legal enumeration the operator deliberately allowed via
    KIRO_SESSIONS_TIMEOUT."""

    def test_default_leaves_headroom_over_provider_default(self):
        from unittest import mock
        with mock.patch.dict(kl.os.environ, {}, clear=False):
            kl.os.environ.pop("KIRO_SESSIONS_TIMEOUT", None)
            self.assertGreater(kl._list_timeout(), 60.0)

    def test_honors_env_override_with_headroom(self):
        from unittest import mock
        with mock.patch.dict(kl.os.environ, {"KIRO_SESSIONS_TIMEOUT": "180"}):
            self.assertGreater(kl._list_timeout(), 180.0)

    def test_bad_env_value_falls_back_safely(self):
        from unittest import mock
        with mock.patch.dict(kl.os.environ, {"KIRO_SESSIONS_TIMEOUT": "not-a-number"}):
            # Does not raise here; the provider rejects a bad value with its own
            # message. The launcher just needs a safe outer bound.
            self.assertGreater(kl._list_timeout(), 0.0)


class DefaultRunnerCommandTests(unittest.TestCase):
    """The default runner builds the kiro_sessions.py command; --all-cwds must
    be present exactly when all_cwds is True. subprocess.run is patched so no
    real child process starts."""

    def _run_cmd_for(self, all_cwds: bool) -> list[str]:
        import subprocess as _sp
        from unittest import mock
        captured = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

        with mock.patch.object(_sp, "run", side_effect=fake_run):
            kl.load_rows(all_cwds=all_cwds)  # runner=None -> builds the default
        return captured["cmd"]

    def test_all_cwds_true_passes_the_flag(self):
        cmd = self._run_cmd_for(True)
        self.assertIn("--all-cwds", cmd)
        self.assertIn("list", cmd)

    def test_all_cwds_false_omits_the_flag(self):
        cmd = self._run_cmd_for(False)
        self.assertNotIn("--all-cwds", cmd)
        self.assertIn("list", cmd)


class ClipLineTests(unittest.TestCase):
    def test_clips_to_column_budget(self):
        self.assertEqual(kl.clip_line("abcdef", 3), "abc")

    def test_zero_or_negative_budget_is_empty(self):
        self.assertEqual(kl.clip_line("abc", 0), "")
        self.assertEqual(kl.clip_line("abc", -2), "")

    def test_control_chars_and_newlines_become_spaces(self):
        """A stray newline or tab (e.g. from a paste) must not break the
        one-row-per-session layout; control chars become a space."""
        self.assertEqual(kl.clip_line("a\nb", 10), "a b")
        self.assertEqual(kl.clip_line("a\tb", 10), "a b")
        self.assertNotIn("\n", kl.clip_line("First\nHidden", 20))

    def test_ascii_label_is_preserved(self):
        """The real input: operator-typed ASCII survives unchanged within the
        budget."""
        self.assertEqual(kl.clip_line("Budget review Q4 (draft)", 40),
                         "Budget review Q4 (draft)")

    def test_spaces_are_kept(self):
        self.assertEqual(kl.clip_line("a b c", 10), "a b c")

    def test_wide_chars_count_as_two_columns(self):
        """Kiro's auto-titles (shown for unlabeled sessions) are not operator
        ASCII and can contain CJK, so a wide char must consume two columns or
        it spills across result rows. Ten wide chars = 20 columns, so a
        10-column budget fits five."""
        wide = "\u65e5" * 10  # 日 x10
        self.assertEqual(len(kl.clip_line(wide, 10)), 5)

    def test_line_cols_counts_wide_as_two(self):
        self.assertEqual(kl.line_cols("ab"), 2)
        self.assertEqual(kl.line_cols("\u65e5\u65e5"), 4)


class ResumeBlockerTests(unittest.TestCase):
    def _row(self, cwd):
        return kl.Row(
            session_id="s1", name="n", title="t", label=None,
            cwd=cwd, updated_at="", status="",
        )

    def test_existing_cwd_is_launchable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(kl.resume_blocker(self._row(d)))

    def test_missing_cwd_is_blocked(self):
        reason = kl.resume_blocker(self._row("/no/such/dir/anywhere"))
        self.assertIsNotNone(reason)
        self.assertIn("no longer exists", reason)

    def test_empty_cwd_is_allowed(self):
        """No recorded cwd means resume from wherever the launcher is; allowed."""
        self.assertIsNone(kl.resume_blocker(self._row("")))


class ScopeDefaultTests(unittest.TestCase):
    def test_default_is_current_directory(self):
        """Default scope is the current directory; --all widens to every
        workspace (scoped to real use). Asserts END-TO-END: main() with
        no flag calls run(all_cwds=False), main(['--all']) calls
        run(all_cwds=True)."""
        from unittest import mock
        with mock.patch.object(kl, "run", return_value=0) as run_mock:
            kl.main([])
            run_mock.assert_called_once_with(all_cwds=False)
        with mock.patch.object(kl, "run", return_value=0) as run_mock:
            kl.main(["--all"])
            run_mock.assert_called_once_with(all_cwds=True)


class SelectionBoundsTests(unittest.TestCase):
    """Exercise the real clamp_selection helper, which the picker's three
    movement sites (reload, up, down) all call. Earlier these tests restated
    the clamp expression inline and so proved nothing about production code."""

    def test_empty_list_clamps_to_zero(self):
        # On an empty list, count-1 is -1; without the guard the index would go
        # negative and wrap to the end on the next non-empty frame.
        self.assertEqual(kl.clamp_selection(0, 0), 0)
        self.assertEqual(kl.clamp_selection(5, 0), 0)
        self.assertEqual(kl.clamp_selection(-3, 0), 0)

    def test_clamps_to_last_row(self):
        self.assertEqual(kl.clamp_selection(2, 3), 2)   # already last, stays
        self.assertEqual(kl.clamp_selection(9, 3), 2)   # past end, clamps down

    def test_in_range_is_unchanged(self):
        self.assertEqual(kl.clamp_selection(0, 3), 0)
        self.assertEqual(kl.clamp_selection(1, 3), 1)

    def test_negative_index_floors_to_zero(self):
        self.assertEqual(kl.clamp_selection(-1, 3), 0)


class ExecResumeOrderingTests(unittest.TestCase):
    """exec_resume must resolve kiro-cli BEFORE it chdirs, and must refuse to
    exec when the recorded cwd is gone.
    os.execv is patched so the test never actually replaces the process."""

    def _row(self, cwd):
        return kl.Row(
            session_id="s1", name="n", title="t", label=None,
            cwd=cwd, updated_at="", status="",
        )

    def test_which_resolves_before_chdir(self):
        """shutil.which must run before os.chdir: a relative PATH entry would
        otherwise resolve against the session's directory, not the launcher's."""
        import tempfile
        from unittest import mock
        calls = []
        with tempfile.TemporaryDirectory() as d:
            def record_which(_name):
                calls.append("which")
                return "/abs/kiro-cli"
            def record_chdir(_path):
                calls.append("chdir")
            def record_execv(_path, _argv):
                calls.append("execv")
            with mock.patch.object(kl.shutil, "which", side_effect=record_which), \
                 mock.patch.object(kl.os, "chdir", side_effect=record_chdir), \
                 mock.patch.object(kl.os, "execv", side_effect=record_execv), \
                 mock.patch.object(kl.os.path, "abspath", return_value="/abs/kiro-cli"):
                kl.exec_resume(self._row(d))
        self.assertEqual(calls, ["which", "chdir", "execv"])

    def test_missing_cwd_refuses_without_exec(self):
        """A recorded cwd that no longer exists must raise before any exec, so
        the session is never resumed from the wrong directory."""
        from unittest import mock
        with mock.patch.object(kl.shutil, "which", return_value="/abs/kiro-cli"), \
             mock.patch.object(kl.os.path, "abspath", return_value="/abs/kiro-cli"), \
             mock.patch.object(kl.os, "chdir") as chdir_mock, \
             mock.patch.object(kl.os, "execv") as execv_mock:
            with self.assertRaises(SystemExit):
                kl.exec_resume(self._row("/no/such/dir/anywhere"))
            chdir_mock.assert_not_called()
            execv_mock.assert_not_called()

    def test_empty_cwd_execs_without_chdir(self):
        """No recorded cwd means resume in place: exec, but no chdir."""
        from unittest import mock
        with mock.patch.object(kl.shutil, "which", return_value="/abs/kiro-cli"), \
             mock.patch.object(kl.os.path, "abspath", return_value="/abs/kiro-cli"), \
             mock.patch.object(kl.os, "chdir") as chdir_mock, \
             mock.patch.object(kl.os, "execv") as execv_mock:
            kl.exec_resume(self._row(""))
            chdir_mock.assert_not_called()
            execv_mock.assert_called_once()


class ResumeArgvContractTests(unittest.TestCase):
    """Tie kiro_launch.resume_argv to the ACTUAL resume command kiro_sessions.py
    produces, by importing its _resume_cmd and comparing, so the two scripts
    cannot drift."""

    def _load_sessions_module(self):
        import importlib.util
        sess_path = REPO_ROOT / "kiro_sessions.py"
        spec = importlib.util.spec_from_file_location("kiro_sessions", sess_path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["kiro_sessions"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_resume_argv_matches_sessions_resume_field(self):
        sessions = self._load_sessions_module()
        sid = "sess_x"
        provider_cmd = sessions._resume_cmd(sid)       # the string kiro_sessions emits
        launcher_argv = kl.resume_argv(sid)            # the argv the launcher execs
        self.assertEqual(" ".join(launcher_argv), provider_cmd)


# ---------------------------------------------------------------------------
# Command-center presentation helpers: pure, so the two-pane / single-column
# layout is tested without a real terminal.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402  (test-local import, grouped with the new tests)


class UseTwoPaneTests(unittest.TestCase):
    """The width->layout decision is one pure function, so the renderer and the
    test read the same threshold."""

    def test_wide_terminal_is_two_pane(self):
        self.assertTrue(kl.use_two_pane(kl.TWO_PANE_MIN_COLS))
        self.assertTrue(kl.use_two_pane(200))

    def test_narrow_terminal_falls_back(self):
        self.assertFalse(kl.use_two_pane(kl.TWO_PANE_MIN_COLS - 1))
        self.assertFalse(kl.use_two_pane(80))
        self.assertFalse(kl.use_two_pane(0))


class DetailPaneColsTests(unittest.TestCase):
    """The detail pane gets a clamped fraction of the width and never more than
    half, so the list stays the main view."""

    def test_never_more_than_half(self):
        for w in (90, 100, 140, 240):
            self.assertLessEqual(kl.detail_pane_cols(w), w // 2)

    def test_clamped_to_band(self):
        # Very wide: clamped to the max.
        self.assertEqual(kl.detail_pane_cols(1000), kl.DETAIL_PANE_MAX_COLS)
        # At the two-pane minimum: within the band and under half.
        got = kl.detail_pane_cols(kl.TWO_PANE_MIN_COLS)
        self.assertGreaterEqual(got, min(kl.DETAIL_PANE_MIN_COLS,
                                         kl.TWO_PANE_MIN_COLS // 2))
        self.assertLessEqual(got, kl.TWO_PANE_MIN_COLS // 2)


class CollapseHomeTests(unittest.TestCase):
    def test_exact_home_becomes_tilde(self):
        self.assertEqual(kl.collapse_home("/Users/x", home="/Users/x"), "~")

    def test_home_prefix_at_boundary(self):
        self.assertEqual(
            kl.collapse_home("/Users/x/projects/app", home="/Users/x"),
            "~/projects/app",
        )

    def test_similar_prefix_not_collapsed(self):
        # "/Users/xavier" must NOT be collapsed against home "/Users/x".
        self.assertEqual(
            kl.collapse_home("/Users/xavier/p", home="/Users/x"),
            "/Users/xavier/p",
        )

    def test_trailing_slash_on_home_is_handled(self):
        self.assertEqual(kl.collapse_home("/Users/x/p", home="/Users/x/"), "~/p")

    def test_empty_path_stays_empty(self):
        self.assertEqual(kl.collapse_home("", home="/Users/x"), "")

    def test_unrelated_path_unchanged(self):
        self.assertEqual(kl.collapse_home("/opt/tool", home="/Users/x"), "/opt/tool")


class ProjectNameTests(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(
            kl.project_name("/Users/x/Developer/Repositories/kiro_session_tools"),
            "kiro_session_tools",
        )

    def test_trailing_slash_ignored(self):
        self.assertEqual(kl.project_name("/a/b/proj/"), "proj")

    def test_root_is_slash(self):
        self.assertEqual(kl.project_name("/"), "/")

    def test_empty_is_no_workspace(self):
        self.assertEqual(kl.project_name(""), "(no workspace)")


class HumanizeTimeTests(unittest.TestCase):
    def setUp(self):
        self.now = _dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def _ts(self, dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_seconds_ago_is_now(self):
        t = self._ts(self.now - _dt.timedelta(seconds=5))
        self.assertEqual(kl.humanize_time(t, now=self.now), "now")

    def test_minutes(self):
        t = self._ts(self.now - _dt.timedelta(minutes=3))
        self.assertEqual(kl.humanize_time(t, now=self.now), "3m")

    def test_hours(self):
        t = self._ts(self.now - _dt.timedelta(hours=5))
        self.assertEqual(kl.humanize_time(t, now=self.now), "5h")

    def test_yesterday(self):
        t = self._ts(self.now - _dt.timedelta(days=1, hours=2))
        self.assertEqual(kl.humanize_time(t, now=self.now), "yesterday")

    def test_weekday_within_week(self):
        t = self._ts(self.now - _dt.timedelta(days=3))
        got = kl.humanize_time(t, now=self.now)
        self.assertIn(got, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))

    def test_calendar_date_same_year(self):
        t = self._ts(_dt.datetime(2026, 3, 2, 9, 0, 0, tzinfo=_dt.timezone.utc))
        self.assertEqual(kl.humanize_time(t, now=self.now), "Mar 2")

    def test_includes_year_when_older(self):
        t = self._ts(_dt.datetime(2024, 3, 2, 9, 0, 0, tzinfo=_dt.timezone.utc))
        self.assertEqual(kl.humanize_time(t, now=self.now), "Mar 2 2024")

    def test_z_suffix_parsed_on_py310(self):
        # fromisoformat did not accept a trailing Z before 3.11; the helper
        # normalizes it, so this must parse rather than return "".
        t = "2026-10-16T11:58:00Z"
        self.assertEqual(kl.humanize_time(t, now=self.now), "2m")

    def test_fractional_seconds_and_z(self):
        # The provider stamps milliseconds, e.g. 2026-10-16T17:01:37.186Z.
        # Use 2m so the .186s fraction cannot push the delta across a boundary;
        # this asserts the fractional+Z stamp PARSES, not the exact rounding.
        t = (self.now - _dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.186Z")
        self.assertEqual(kl.humanize_time(t, now=self.now), "1m")

    def test_empty_or_bad_is_blank(self):
        self.assertEqual(kl.humanize_time("", now=self.now), "")
        self.assertEqual(kl.humanize_time("not-a-date", now=self.now), "")

    def test_future_stamp_is_now_not_negative(self):
        t = self._ts(self.now + _dt.timedelta(minutes=10))
        self.assertEqual(kl.humanize_time(t, now=self.now), "now")


class StatusDotTests(unittest.TestCase):
    def test_running_is_filled(self):
        self.assertEqual(kl.status_dot("running"), "\u25cf")
        self.assertEqual(kl.status_dot("Active"), "\u25cf")

    def test_idle_and_unknown_are_open(self):
        self.assertEqual(kl.status_dot("idle"), "\u25cb")
        self.assertEqual(kl.status_dot(""), "\u25cb")
        self.assertEqual(kl.status_dot("whatever"), "\u25cb")


class GroupRowsByCwdTests(unittest.TestCase):
    def _rows(self, pairs):
        # pairs: list of (session_id, cwd)
        return kl.rows_from_json([_json_row(sid, cwd=cwd) for sid, cwd in pairs])

    def test_groups_preserve_first_appearance_order(self):
        rows = self._rows([("a", "/p1"), ("b", "/p2"), ("c", "/p1")])
        groups = kl.group_rows_by_cwd(rows, home="/home")
        self.assertEqual([g.cwd for g in groups], ["/p1", "/p2"])
        # /p1 keeps both its rows, in order.
        self.assertEqual([r.session_id for r in groups[0].rows], ["a", "c"])
        self.assertEqual([r.session_id for r in groups[1].rows], ["b"])

    def test_group_label_is_project_name_and_collapsed_path(self):
        rows = self._rows([("a", "/home/Developer/kiro_session_tools")])
        groups = kl.group_rows_by_cwd(rows, home="/home")
        self.assertEqual(groups[0].name, "kiro_session_tools")
        self.assertEqual(groups[0].path, "~/Developer/kiro_session_tools")

    def test_empty_rows_is_no_groups(self):
        self.assertEqual(kl.group_rows_by_cwd([], home="/home"), [])


class WrapColsTests(unittest.TestCase):
    def test_wraps_on_spaces(self):
        self.assertEqual(kl.wrap_cols("aa bb cc", 5), ["aa bb", "cc"])

    def test_hard_splits_a_long_token(self):
        # A long path has no spaces; it must still never exceed the width.
        out = kl.wrap_cols("/a/very/long/path/segment", 8)
        self.assertTrue(all(kl.line_cols(line) <= 8 for line in out))
        self.assertEqual("".join(out), "/a/very/long/path/segment")

    def test_empty_or_zero_width(self):
        self.assertEqual(kl.wrap_cols("", 10), [])
        self.assertEqual(kl.wrap_cols("abc", 0), [])


class DetailLinesTests(unittest.TestCase):
    def _row(self, **kw):
        return kl.rows_from_json([_json_row("sess_abc", **kw)])[0]

    def test_new_session_entry_describes_the_command(self):
        lines = kl.detail_lines(kl.NEW_SESSION, 60)
        joined = "\n".join(lines)
        self.assertIn("new session", joined)
        self.assertIn("--v3", joined)

    def test_session_detail_carries_the_key_fields(self):
        row = self._row(label="Budget review", title="auto title here",
                        cwd="/home/Developer/proj", status="running",
                        updated="2026-10-16T11:58:00Z")
        lines = kl.detail_lines(row, 60,
                                now=_dt.datetime(2026, 10, 16, 12, 0, 0,
                                                 tzinfo=_dt.timezone.utc),
                                home="/home")
        joined = "\n".join(lines)
        self.assertIn("Budget review", joined)      # label
        self.assertIn("auto title here", joined)    # auto-title
        self.assertIn("sess_abc", joined)           # session id
        self.assertIn("~/Developer/proj", joined)   # collapsed cwd
        self.assertIn("running", joined)            # status
        self.assertIn("2m", joined)                 # relative time
        self.assertIn("--resume-id sess_abc", joined)  # exact resume command

    def test_no_label_row_says_auto_title(self):
        row = self._row(label=None, title="Only a title")
        lines = kl.detail_lines(row, 60)
        self.assertTrue(any("no label" in line for line in lines))

    def test_every_line_fits_the_width(self):
        row = self._row(label="x" * 200, title="y" * 200, cwd="/" + "d/" * 100)
        lines = kl.detail_lines(row, 40)
        self.assertTrue(all(kl.line_cols(line) <= 40 for line in lines))

    def test_zero_width_is_empty(self):
        self.assertEqual(kl.detail_lines(kl.NEW_SESSION, 0), [])


class RowCellTests(unittest.TestCase):
    def _row(self, **kw):
        return kl.rows_from_json([_json_row("sess_1", **kw)])[0]

    def test_two_pane_cell_is_terse(self):
        row = self._row(label="My Label", status="running",
                        updated="2026-10-16T11:58:00Z")
        cell = kl.row_cell(row, 40, two_pane=True,
                           now=_dt.datetime(2026, 10, 16, 12, 0, 0,
                                            tzinfo=_dt.timezone.utc))
        self.assertIn("My Label", cell)
        self.assertIn("2m", cell)
        # The terse cell does not carry the cwd (the detail pane does).
        self.assertNotIn("/w", cell)
        self.assertLessEqual(kl.line_cols(cell), 40)

    def test_single_column_cell_carries_marker_and_path(self):
        row = self._row(label="My Label", status="idle", cwd="/home/proj",
                        updated="2026-10-16T11:58:00Z")
        cell = kl.row_cell(row, 120, two_pane=False,
                           now=_dt.datetime(2026, 10, 16, 12, 0, 0,
                                            tzinfo=_dt.timezone.utc),
                           home="/home")
        self.assertIn("My Label", cell)
        self.assertIn("[label]", cell)
        self.assertIn("~/proj", cell)

    def test_unlabeled_row_shows_auto_marker(self):
        row = self._row(label=None, title="New Session", cwd="/home/p")
        cell = kl.row_cell(row, 120, two_pane=False, home="/home")
        self.assertIn("[auto ]", cell)

    def test_new_session_entry_cell(self):
        cell = kl.row_cell(kl.NEW_SESSION, 40, two_pane=True)
        self.assertIn("new session", cell)

    def test_cell_never_exceeds_width(self):
        row = self._row(label="z" * 300, cwd="/" + "d/" * 80)
        for two in (True, False):
            cell = kl.row_cell(row, 30, two_pane=two)
            self.assertLessEqual(kl.line_cols(cell), 30)


class BuildListLinesTests(unittest.TestCase):
    def _rows(self, pairs):
        return kl.rows_from_json([_json_row(sid, cwd=cwd) for sid, cwd in pairs])

    def test_new_session_entry_leads_then_grouped_rows(self):
        rows = self._rows([("a", "/p1"), ("b", "/p1"), ("c", "/p2")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        lines = kl.build_list_lines(groups, entries, 80, two_pane=True)
        # First drawable line is the new-session entry (entries index 0).
        self.assertEqual(lines[0].attr, "new")
        self.assertEqual(lines[0].entry_index, 0)
        # Then a header, then the two /p1 rows, then a header, then the /p2 row.
        attrs = [ln.attr for ln in lines]
        self.assertEqual(attrs, ["new", "header", "row", "row", "header", "row"])

    def test_entry_indices_map_back_to_entries(self):
        rows = self._rows([("a", "/p1"), ("b", "/p2")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        lines = kl.build_list_lines(groups, entries, 80, two_pane=True)
        # Every selectable line points at the matching entry object.
        for ln in lines:
            if ln.entry_index is not None:
                self.assertIsInstance(ln.entry_index, int)
                self.assertTrue(0 <= ln.entry_index < len(entries))
        # The row labeled with session "b" maps to its entries index.
        b_idx = kl.entry_index_for_session(rows, "b")
        row_lines = [ln for ln in lines if ln.attr == "row"]
        self.assertIn(b_idx, [ln.entry_index for ln in row_lines])

    def test_headers_are_not_selectable(self):
        rows = self._rows([("a", "/p1")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        lines = kl.build_list_lines(groups, entries, 80, two_pane=True)
        headers = [ln for ln in lines if ln.attr == "header"]
        self.assertTrue(headers)
        self.assertTrue(all(ln.entry_index is None for ln in headers))

    def test_no_rows_still_has_new_session_line(self):
        entries = kl.picker_entries([])
        lines = kl.build_list_lines([], entries, 80, two_pane=True)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].attr, "new")


class FirstSelectableAndLineIndexTests(unittest.TestCase):
    def _lines(self):
        rows = kl.rows_from_json([
            _json_row("a", cwd="/p1"), _json_row("b", cwd="/p2"),
        ])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        return kl.build_list_lines(groups, entries, 80, two_pane=True), entries

    def test_first_selectable_is_the_new_session_line(self):
        lines, _ = self._lines()
        self.assertEqual(kl.first_selectable_line(lines), 0)

    def test_first_selectable_skips_leading_headers(self):
        # A hand-built list that starts with a header.
        lines = [
            kl.ListLine("hdr", "header", None),
            kl.ListLine("row", "row", 2),
        ]
        self.assertEqual(kl.first_selectable_line(lines), 1)

    def test_first_selectable_with_no_selectable_is_zero(self):
        lines = [kl.ListLine("hdr", "header", None)]
        self.assertEqual(kl.first_selectable_line(lines), 0)

    def test_line_index_for_entry_finds_the_row(self):
        lines, entries = self._lines()
        # entries index 2 is the second row (0 is new-session, 1 is first row).
        li = kl.line_index_for_entry(lines, 2)
        self.assertIsNotNone(li)
        self.assertEqual(lines[li].entry_index, 2)

    def test_line_index_for_absent_entry_is_none(self):
        lines, _ = self._lines()
        self.assertIsNone(kl.line_index_for_entry(lines, 999))


# ---------------------------------------------------------------------------
# color policy, lazy-render slots, and the in-process load.
# ---------------------------------------------------------------------------


class ShouldUseColorTests(unittest.TestCase):
    """The color decision is a pure predicate: color only when the terminal
    supports it and the environment does not disable it, with an explicit
    override that wins. Tested without a terminal; the caller passes
    curses.has_colors()."""

    def test_color_when_supported_and_unset(self):
        self.assertTrue(kl.should_use_color(True, env={}))

    def test_no_color_when_terminal_lacks_support(self):
        self.assertFalse(kl.should_use_color(False, env={}))

    def test_no_color_env_disables(self):
        self.assertFalse(kl.should_use_color(True, env={"NO_COLOR": "1"}))
        # Any non-empty value disables, per the NO_COLOR convention.
        self.assertFalse(kl.should_use_color(True, env={"NO_COLOR": "whatever"}))

    def test_empty_no_color_does_not_disable(self):
        # An empty NO_COLOR is not "set" under the convention.
        self.assertTrue(kl.should_use_color(True, env={"NO_COLOR": ""}))

    def test_explicit_off_override_wins_over_support(self):
        for off in ("0", "false", "no", "off"):
            self.assertFalse(kl.should_use_color(True, env={"KIRO_LAUNCH_COLOR": off}))

    def test_explicit_on_override_wins_over_no_color_env(self):
        self.assertTrue(kl.should_use_color(
            True, env={"NO_COLOR": "1", "KIRO_LAUNCH_COLOR": "1"}))

    def test_explicit_on_cannot_conjure_unsupported_color(self):
        # KIRO_LAUNCH_COLOR=1 still requires the terminal to actually support
        # color; it only overrides the NO_COLOR/auto policy, not reality.
        self.assertFalse(kl.should_use_color(False, env={"KIRO_LAUNCH_COLOR": "1"}))


class RoleColorsTests(unittest.TestCase):
    """The palette is aligned to Kiro's brand family: the depth->color
    choice is pure, so it is tested without a terminal."""

    def test_256_uses_kiro_brand_indices(self):
        colors = kl.role_colors(256)
        # Brand accent roles -> Kiro violet (nearest xterm-256 to #7B5CFA).
        for role in ("header", "marker", "field"):
            self.assertEqual(colors[role], kl.KIRO_VIOLET_256)
        # Active/new -> Kiro green (#2ecc71).
        self.assertEqual(colors["dot_run"], kl.KIRO_GREEN_256)
        self.assertEqual(colors["new"], kl.KIRO_GREEN_256)
        # Muted roles -> Kiro grey (#a0a0b8).
        for role in ("dot_idle", "time", "divider"):
            self.assertEqual(colors[role], kl.KIRO_GREY_256)

    def test_high_depth_also_uses_256_indices(self):
        # A truecolor terminal reports a large COLORS; still use the 256 band.
        self.assertEqual(kl.role_colors(16777216)["header"], kl.KIRO_VIOLET_256)

    def test_basic_depth_falls_back_to_nearest_ansi(self):
        colors = kl.role_colors(8)
        # Violet -> magenta(5), green -> green(2), grey -> blue(4).
        self.assertEqual(colors["header"], 5)
        self.assertEqual(colors["marker"], 5)
        self.assertEqual(colors["dot_run"], 2)
        self.assertEqual(colors["new"], 2)
        self.assertEqual(colors["dot_idle"], 4)
        self.assertEqual(colors["time"], 4)

    def test_16_color_terminal_uses_basic(self):
        # 16 colors is still below the 256 threshold -> basic ANSI.
        self.assertEqual(kl.role_colors(16)["header"], 5)

    def test_every_role_has_a_color_at_each_depth(self):
        for depth in (8, 16, 88, 256, 16777216):
            colors = kl.role_colors(depth)
            for role in kl.COLOR_ROLES:
                self.assertIn(role, colors)
                self.assertIsInstance(colors[role], int)


class BuildListSlotsTests(unittest.TestCase):
    """The cheap structural pass: same ordering as build_list_lines but no
    string formatting, so it is safe to call every frame on a huge list."""

    def _rows(self, pairs):
        return kl.rows_from_json([_json_row(sid, cwd=cwd) for sid, cwd in pairs])

    def test_order_matches_build_list_lines(self):
        rows = self._rows([("a", "/p1"), ("b", "/p1"), ("c", "/p2")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        slots = kl.build_list_slots(groups, entries)
        self.assertEqual([s.kind for s in slots],
                         ["new", "header", "row", "row", "header", "row"])

    def test_slot_entry_indices_match_lines(self):
        rows = self._rows([("a", "/p1"), ("b", "/p2")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        slots = kl.build_list_slots(groups, entries)
        lines = kl.build_list_lines(groups, entries, 80, two_pane=True)
        self.assertEqual([s.entry_index for s in slots],
                         [ln.entry_index for ln in lines])

    def test_headers_carry_group_and_no_entry_index(self):
        rows = self._rows([("a", "/home/proj")])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        slots = kl.build_list_slots(groups, entries)
        header = [s for s in slots if s.kind == "header"][0]
        self.assertIsNone(header.entry_index)
        self.assertIsNotNone(header.group)

    def test_no_rows_still_has_new_slot(self):
        entries = kl.picker_entries([])
        slots = kl.build_list_slots([], entries)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].kind, "new")


class FormatSlotTests(unittest.TestCase):
    """format_slot turns a cheap slot into the same drawable line
    build_list_lines would have produced for it, so lazy rendering is
    byte-equivalent to the eager path for the formatted window."""

    def _setup(self, two_pane):
        rows = kl.rows_from_json([
            _json_row("a", label="L", cwd="/home/p1", status="running"),
            _json_row("b", cwd="/home/p2"),
        ])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        return groups, entries

    def test_matches_build_list_lines_cell_for_cell(self):
        for two in (True, False):
            groups, entries = self._setup(two)
            now = _dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=_dt.timezone.utc)
            eager = kl.build_list_lines(groups, entries, 80, two, now=now,
                                        home="/home")
            slots = kl.build_list_slots(groups, entries)
            lazy = [kl.format_slot(s, 80, two, now=now, home="/home")
                    for s in slots]
            self.assertEqual([ln.text for ln in lazy], [ln.text for ln in eager])
            self.assertEqual([ln.attr for ln in lazy], [ln.attr for ln in eager])

    def test_header_slot_formats_name_and_path(self):
        groups, entries = self._setup(True)
        slots = kl.build_list_slots(groups, entries)
        header_slot = [s for s in slots if s.kind == "header"][0]
        line = kl.format_slot(header_slot, 80, two_pane=True)
        self.assertEqual(line.attr, "header")
        self.assertIn("p1", line.text)


class SlotNavigationTests(unittest.TestCase):
    def _slots(self):
        rows = kl.rows_from_json([
            _json_row("a", cwd="/p1"), _json_row("b", cwd="/p2"),
        ])
        entries = kl.picker_entries(rows)
        groups = kl.group_rows_by_cwd(rows, home="/home")
        return kl.build_list_slots(groups, entries)

    def test_first_selectable_slot_is_the_new_entry(self):
        self.assertEqual(kl.first_selectable_slot(self._slots()), 0)

    def test_first_selectable_slot_skips_leading_header(self):
        slots = [
            kl.ListSlot("header", None, None, object()),
            kl.ListSlot("row", 2, object(), object()),
        ]
        self.assertEqual(kl.first_selectable_slot(slots), 1)

    def test_first_selectable_slot_none_is_zero(self):
        slots = [kl.ListSlot("header", None, None, object())]
        self.assertEqual(kl.first_selectable_slot(slots), 0)

    def test_slot_index_for_entry_finds_row(self):
        slots = self._slots()
        idx = kl.slot_index_for_entry(slots, 2)
        self.assertIsNotNone(idx)
        self.assertEqual(slots[idx].entry_index, 2)

    def test_slot_index_for_absent_entry_is_none(self):
        self.assertIsNone(kl.slot_index_for_entry(self._slots(), 999))


class LoadRowsInProcessTests(unittest.TestCase):
    """The in-process load must return rows byte-equivalent to the
    subprocess --json path and preserve the provider's raise-on-failure
    contract. The provider module is stubbed so no real kiro-cli runs."""

    def _stub_provider(self, sessions, labels=None, warn=""):
        import types as _t

        def list_sessions(all_cwds):
            return sessions

        def load_labels():
            if warn:
                print(warn, file=_sys.stderr)
            return labels or {}

        def _display_rows(sess, labs):
            # Mirror the real provider's projection closely enough for the test.
            out = []
            for s in sess:
                sid = s["sessionId"]
                lab = labs.get(sid)
                out.append({
                    "sessionId": sid, "label": lab,
                    "title": s.get("title", ""),
                    "name": lab or s.get("title", ""),
                    "cwd": s.get("cwd", ""), "updatedAt": s.get("updatedAt", ""),
                    "status": s.get("status", ""),
                    "resume": f"kiro-cli chat --resume-id {sid}",
                })
            return out

        return _t.SimpleNamespace(
            list_sessions=list_sessions, load_labels=load_labels,
            _display_rows=_display_rows)

    def test_rows_projected_from_provider(self):
        from unittest import mock
        stub = self._stub_provider(
            [{"sessionId": "s1", "title": "T", "cwd": "/w",
              "updatedAt": "2026-10-01T00:00:00Z", "status": "idle"}],
            labels={"s1": "My Label"})
        with mock.patch.object(kl, "_import_kiro_sessions", return_value=stub):
            rows, warnings = kl.load_rows_inprocess(all_cwds=True)
        self.assertEqual([r.session_id for r in rows], ["s1"])
        self.assertEqual(rows[0].label, "My Label")
        self.assertEqual(warnings, "")

    def test_provider_warning_is_captured_from_stderr(self):
        from unittest import mock
        stub = self._stub_provider(
            [{"sessionId": "s1", "title": "T", "cwd": "/w",
              "updatedAt": "2026-10-01T00:00:00Z", "status": ""}],
            warn="warning: skipping bad.json")
        with mock.patch.object(kl, "_import_kiro_sessions", return_value=stub):
            rows, warnings = kl.load_rows_inprocess(all_cwds=False)
        self.assertEqual(len(rows), 1)
        self.assertIn("skipping bad.json", warnings)

    def test_provider_systemexit_propagates(self):
        from unittest import mock

        def boom(all_cwds):
            raise SystemExit("error: broken enumeration")
        import types as _t
        stub = _t.SimpleNamespace(
            list_sessions=boom, load_labels=lambda: {},
            _display_rows=lambda a, b: [])
        with mock.patch.object(kl, "_import_kiro_sessions", return_value=stub):
            with self.assertRaises(SystemExit) as cm:
                kl.load_rows_inprocess(all_cwds=False)
        self.assertIn("broken enumeration", str(cm.exception))

    def test_oserror_normalized_to_systemexit(self):
        from unittest import mock

        def oserr(all_cwds):
            raise OSError(24, "Too many open files")
        import types as _t
        stub = _t.SimpleNamespace(
            list_sessions=oserr, load_labels=lambda: {},
            _display_rows=lambda a, b: [])
        with mock.patch.object(kl, "_import_kiro_sessions", return_value=stub):
            with self.assertRaises(SystemExit) as cm:
                kl.load_rows_inprocess(all_cwds=False)
        self.assertIn("could not enumerate", str(cm.exception))


class InProcessMatchesSubprocessTests(unittest.TestCase):
    """A stronger contract check: for the same provider output, the in-process
    projection equals the subprocess --json projection row-for-row. Both
    go through rows_from_json, so this pins that the two load paths cannot
    drift in what they hand the renderer."""

    def test_byte_equivalent_rows(self):
        from unittest import mock
        sessions = [
            {"sessionId": "s1", "title": "A", "cwd": "/w1",
             "updatedAt": "2026-10-02T00:00:00Z", "status": "running"},
            {"sessionId": "s2", "title": "B", "cwd": "/w2",
             "updatedAt": "2026-10-01T00:00:00Z", "status": "idle"},
        ]
        # Subprocess path: feed the SAME projected JSON a provider would emit.
        projected = [
            {"sessionId": "s1", "label": None, "title": "A", "name": "A",
             "cwd": "/w1", "updatedAt": "2026-10-02T00:00:00Z",
             "status": "running", "resume": "kiro-cli chat --resume-id s1"},
            {"sessionId": "s2", "label": None, "title": "B", "name": "B",
             "cwd": "/w2", "updatedAt": "2026-10-01T00:00:00Z",
             "status": "idle", "resume": "kiro-cli chat --resume-id s2"},
        ]
        sub_rows, _ = kl.load_rows(
            all_cwds=True, runner=_runner_returning(json.dumps(projected)))
        stub = LoadRowsInProcessTests()._stub_provider(sessions)
        with mock.patch.object(kl, "_import_kiro_sessions", return_value=stub):
            ip_rows, _ = kl.load_rows_inprocess(all_cwds=True)
        self.assertEqual(
            [(r.session_id, r.name, r.cwd, r.status) for r in sub_rows],
            [(r.session_id, r.name, r.cwd, r.status) for r in ip_rows],
        )


if __name__ == "__main__":
    unittest.main()
