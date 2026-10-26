# Changelog

All notable changes to this project, most recent first.

## Unreleased

### Kiro-aligned palette
- `kiro_launch.py` (`ks`): re-tuned the color palette to Kiro CLI's brand
  family, so the launcher reads as part of the same tool set. Group
  headers, the label marker and detail field labels use Kiro violet
  (`#7B5CFA`); running sessions and the new-session entry use Kiro green
  (`#2ecc71`); idle sessions, relative times and the divider use Kiro's muted
  lavender-grey (`#a0a0b8`). Brand colors came from the kiro-cli app bundle.
  A new pure `role_colors(color_depth)` picks the nearest xterm-256 index on a
  256-color terminal and the nearest basic ANSI color below that, so the
  mapping is unit-tested; the monochrome fallback and the color on/off policy
  from the previous entry are unchanged.

### Color + performance
- `kiro_launch.py` (`ks`): added color and made it snappier.
  - **Color.** Group headers, status dots (green for running, blue for idle),
    the `[label]`/`[auto]` marker, relative times, detail-pane field labels and
    the divider are now colored. Color is used only when the terminal supports
    it and is not disabled; it honors `NO_COLOR` and an explicit
    `KIRO_LAUNCH_COLOR=0/1` override, and degrades to the previous monochrome
    attributes otherwise. The color decision is a pure, tested predicate.
  - **In-process load.** The launcher now imports `kiro_sessions` and
    enumerates in-process instead of spawning a second Python interpreter and
    parsing JSON over a pipe, removing that startup and round-trip cost. The
    rows are identical (same provider code) and the subprocess path is kept as
    the test seam.
  - **Lazy rendering.** The grouped list is built as a cheap unformatted
    structure and only the visible window of rows is formatted per frame.
    Measured on a 6687-session `--all` list, per-keystroke render dropped from
    ~247 ms to ~9 ms (about 29x), so typing no longer stutters.
  - All existing keybindings, the two-pane/fallback layout, and the
    `kiro_sessions.py`-only data path are preserved.

### Two-pane command-center layout
- `kiro_launch.py` (`ks`): refined the picker into a two-pane command-center
  layout. On a wide terminal the launcher shows a project-grouped
  left rail (status dot, label-over-title name, short relative time) beside a
  detail pane for the highlighted session (status, exact + relative time,
  label, auto-title, session id, wrapped cwd, and the exact resume command).
  Narrow terminals fall back to a single richer column that carries the
  label/auto marker, status, relative time and the home-collapsed cwd inline.
  The width->layout decision and all formatting (grouping by cwd, humanized
  relative time, `~`-collapsed paths, pane split, detail content) are pure,
  unit-tested functions, keeping the pure-logic / curses-shell split. All
  existing keybindings and behavior are preserved.

### Initial release
- `kiro_sessions.py`: a label layer over Kiro's session store. Attaches a
  memorable label to a Kiro `sessionId`, stored outside Kiro's own store (which
  regenerates titles), with one JSON file per session under
  `~/.kiro/session-labels` (override with `KIRO_SESSION_LABELS_PATH`).
- `kiro_launch.py` (`ks`): a stdlib-`curses` picker over the label layer to
  find, filter, resume, label and start Kiro sessions.
