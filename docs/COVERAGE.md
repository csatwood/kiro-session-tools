# Coverage report

Line coverage was measured with the standard library only: `trace.Trace(count=1)` wraps the test run, and executable lines come from `trace._find_executable_linenos`; a line counts as hit if it executed at least once. `tests/test_kiro_relocate.py` is written for pytest (fixtures `tmp_path`, `monkeypatch`, `capsys`) and `python3 -m unittest discover` does not collect it, so the run used a ~30-line stdlib shim that supplies those three fixtures and calls each `test_*` function.

Test counts: unittest discovery finds 218 tests (none from `test_kiro_relocate.py`); the shim runs the 14 relocate tests separately.

## kiro_sessions.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 274 | 178 | 65.0% |

Functions with uncovered lines:

- `labels_dir` (line 81): 2 of 2 lines uncovered
- `_session_file` (line 97): 2 of 15 lines uncovered
- `_write_label_file` (line 133): 6 of 19 lines uncovered
- `remove_label` (line 251): 1 of 7 lines uncovered
- `_kiro_bin` (line 276): 5 of 5 lines uncovered
- `_list_timeout` (line 291): 1 of 8 lines uncovered
- `list_sessions` (line 306): 7 of 52 lines uncovered
- `runner` (line 319): 2 of 2 lines uncovered
- `_print_rows` (line 426): 12 of 12 lines uncovered
- `cmd_label` (line 446): 10 of 13 lines uncovered
- `cmd_find` (line 463): 12 of 12 lines uncovered
- `cmd_list` (line 479): 7 of 7 lines uncovered
- `build_parser` (line 489): 27 of 27 lines uncovered
- `main` (line 530): 2 of 2 lines uncovered

## kiro_relocate.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 132 | 122 | 92.4% |

Functions with uncovered lines:

- `plan` (line 50): 3 of 15 lines uncovered
- `old_index_at` (line 104): 2 of 12 lines uncovered
- `main` (line 123): 3 of 63 lines uncovered

## kiro_launch.py

| Total executable lines | Lines hit | Coverage |
|---|---|---|
| 810 | 534 | 65.9% |

Functions with uncovered lines:

- `collapse_home` (line 432): 1 of 11 lines uncovered
- `project_name` (line 452): 1 of 8 lines uncovered
- `humanize_time` (line 489): 1 of 26 lines uncovered
- `build_list_slots` (line 726): 1 of 15 lines uncovered
- `_import_kiro_sessions` (line 901): 13 of 13 lines uncovered
- `write_label` (line 959): 5 of 20 lines uncovered
- `exec_new_session` (line 1053): 2 of 2 lines uncovered
- `_build_color_map` (line 1150): 29 of 29 lines uncovered
- `_run_picker` (line 1195): 221 of 221 lines uncovered
- `inner` (line 1204): 218 of 218 lines uncovered
- `recompute` (line 1224): 2 of 2 lines uncovered
- `safe_addstr` (line 1234): 8 of 8 lines uncovered
- `_prompt_line` (line 1249): 29 of 29 lines uncovered
- `_select_entry` (line 1290): 10 of 10 lines uncovered
- `_edit_label` (line 1309): 26 of 26 lines uncovered
