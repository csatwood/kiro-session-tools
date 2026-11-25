"""Edge-path tests for kiro_launch.py: write_label failure modes,
exec_new_session, and the in-process provider import (load + cache + failure).

Everything that would replace the process (os.execv) or spawn a child is
patched, so the suite never execs kiro-cli or shells out.
"""

import importlib.util
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

COVERS = ["kiro_launch.py"]

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "kiro_launch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kiro_launch", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kiro_launch"] = mod
    spec.loader.exec_module(mod)
    return mod


kl = _load_module()


class WriteLabelFailureTests(unittest.TestCase):
    def test_timeout_becomes_clean_systemexit_naming_the_deadline(self):
        def runner():
            raise subprocess.TimeoutExpired(cmd="x", timeout=30)

        with self.assertRaises(SystemExit) as cm:
            kl.write_label("sess_1", "my label", runner=runner)
        msg = str(cm.exception)
        self.assertIn("did not finish within 30s", msg)

    def test_oserror_becomes_clean_systemexit_not_traceback(self):
        def runner():
            raise PermissionError("not executable")

        with self.assertRaises(SystemExit) as cm:
            kl.write_label("sess_1", "my label", runner=runner)
        msg = str(cm.exception)
        self.assertIn("could not run", msg)
        self.assertIn("not executable", msg)

    def test_whitespace_session_id_raises_before_running(self):
        called = []
        with self.assertRaises(SystemExit):
            kl.write_label("   ", "x", runner=lambda: called.append(1))
        self.assertEqual(called, [])


class ExecNewSessionTests(unittest.TestCase):
    def test_execs_new_session_argv_with_absolute_kiro_path(self):
        with mock.patch.object(kl.shutil, "which", return_value="/abs/kiro-cli"), \
                mock.patch.object(kl.os, "execv") as execv:
            kl.exec_new_session()
        execv.assert_called_once_with("/abs/kiro-cli", kl.new_session_argv())

    def test_missing_kiro_cli_refuses_without_exec(self):
        with mock.patch.object(kl.shutil, "which", return_value=None), \
                mock.patch.object(kl.os, "execv") as execv:
            with self.assertRaises(SystemExit) as cm:
                kl.exec_new_session()
        self.assertIn("kiro-cli not found", str(cm.exception))
        execv.assert_not_called()


class ImportKiroSessionsTests(unittest.TestCase):
    def setUp(self):
        # Preserve and clear the function-attribute cache and sys.modules entry
        # so these tests neither depend on nor leak import state.
        self._had_cache = hasattr(kl._import_kiro_sessions, "_mod")
        self._old_cache = getattr(kl._import_kiro_sessions, "_mod", None)
        if self._had_cache:
            del kl._import_kiro_sessions._mod
        self._had_mod = "kiro_sessions" in sys.modules
        self._old_mod = sys.modules.get("kiro_sessions")
        sys.modules.pop("kiro_sessions", None)

    def tearDown(self):
        if hasattr(kl._import_kiro_sessions, "_mod"):
            del kl._import_kiro_sessions._mod
        if self._had_cache:
            kl._import_kiro_sessions._mod = self._old_cache
        sys.modules.pop("kiro_sessions", None)
        if self._had_mod:
            sys.modules["kiro_sessions"] = self._old_mod

    def test_loads_sibling_provider_and_caches_it(self):
        first = kl._import_kiro_sessions()
        self.assertTrue(callable(first.list_sessions))
        self.assertEqual(Path(first.__file__).resolve(), REPO_ROOT / "kiro_sessions.py")
        # Second call returns the cached module without re-executing the file.
        with mock.patch.object(importlib.util, "spec_from_file_location") as sp:
            second = kl._import_kiro_sessions()
        self.assertIs(second, first)
        sp.assert_not_called()

    def test_unloadable_provider_is_a_clean_systemexit(self):
        with mock.patch.object(importlib.util, "spec_from_file_location", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                kl._import_kiro_sessions()
        self.assertIn("cannot load the session provider", str(cm.exception))
        # A failed import must not poison the cache.
        self.assertFalse(hasattr(kl._import_kiro_sessions, "_mod"))

    def test_spec_without_loader_is_a_clean_systemexit(self):
        spec = types.SimpleNamespace(loader=None)
        with mock.patch.object(importlib.util, "spec_from_file_location", return_value=spec):
            with self.assertRaises(SystemExit):
                kl._import_kiro_sessions()


if __name__ == "__main__":
    unittest.main()
