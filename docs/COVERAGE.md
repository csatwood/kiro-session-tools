# Coverage report

Line coverage was measured with the standard library only: `trace.Trace(count=1)` wraps a `python3 -m unittest discover -s tests` run (249 tests), and executable lines come from `trace._find_executable_linenos`; a line counts as hit if it executed at least once. After PR #8, `tests/test_kiro_relocate.py` provides a `load_tests` hook so unittest discovery collects all relocate tests (no separate fixture shim).

This refresh reflects `main` at the post–PR #8 tree (sessions CLI coverage from PR #7, launch edge paths from PR #6, relocate discovery from PR #8). Figures are absolute current coverage, not a before/after delta.

## Summary

| Module | Executable | Hit | Coverage |
|---|---|---|---|
| kiro_sessions.py | 274 | 259 | 94.5% |
| kiro_relocate.py | 135 | 132 | 97.8% |
| kiro_launch.py | 810 | 554 | 68.4% |

## kiro_sessions.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 274 | 259 | 94.5% |

Functions with uncovered lines:

- `_session_file` (line 97): 2 of 16 lines uncovered
- `_write_label_file` (line 133): 2 of 20 lines uncovered
- `remove_label` (line 251): 1 of 8 lines uncovered
- `_kiro_bin` (line 276): 1 of 6 lines uncovered
- `_list_timeout` (line 291): 1 of 9 lines uncovered
- `list_sessions` (line 306): 6 of 53 lines uncovered
- `runner` (line 319): 3 of 3 lines uncovered

## kiro_relocate.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 135 | 132 | 97.8% |

Functions with uncovered lines:

- `main` (line 126): 1 of 64 lines uncovered

## kiro_launch.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 810 | 554 | 68.4% |

The curses picker loop (`_run_picker` and its nested helpers) is intentionally untested: it needs a real terminal. Pure helpers the loop calls remain covered.

Functions with uncovered lines:

- `collapse_home` (line 432): 1 of 12 lines uncovered
- `project_name` (line 452): 1 of 9 lines uncovered
- `humanize_time` (line 489): 1 of 27 lines uncovered
- `build_list_slots` (line 726): 1 of 16 lines uncovered
- `_build_color_map` (line 1150): 29 of 30 lines uncovered
- `_run_picker` (line 1195): 221 of 222 lines uncovered
- `inner` (line 1204): 219 of 219 lines uncovered
- `recompute` (line 1224): 3 of 3 lines uncovered
- `safe_addstr` (line 1234): 9 of 9 lines uncovered
- `_prompt_line` (line 1249): 30 of 30 lines uncovered
- `_select_entry` (line 1290): 11 of 11 lines uncovered
- `_edit_label` (line 1309): 27 of 27 lines uncovered
