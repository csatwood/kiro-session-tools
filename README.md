# kiro_session_tools

Tooling to make Kiro sessions findable and resumable by a label you choose,
over the model-generated auto-titles Kiro's native `--sessions` dashboard shows.

## Components

- **`kiro_sessions.py`**: the label layer. It attaches a memorable label to a
  Kiro `sessionId`, stored outside Kiro's own store (which regenerates titles),
  and lists or searches sessions with labels merged over auto-titles. One JSON
  file per session under the label store.
  - `label <id> <text>` / `label <id> --remove`
  - `find <query> [--json] [--all-cwds]`
  - `list [--json] [--labeled-only] [--all-cwds]`
- **`kiro_launch.py`**: a stdlib-`curses` TUI front door over `kiro_sessions.py`.
  It lists sessions (label over auto-title), filters as you type, resumes the
  chosen session in place, labels or renames the highlighted session inline, and
  starts a new session as a selectable entry. On a wide terminal it draws a
  two-pane command center: a project-grouped left rail beside a detail pane for
  the highlighted session (full label, auto-title, wrapped cwd, exact + relative
  time, session id, status, and the exact resume command). Narrow terminals fall
  back to a single richer column with the marker, status, relative time and the
  home-collapsed cwd inline. Rows are colored (group headers, running/idle
  status dots, the label/auto marker, relative times) when the terminal
  supports it.

## Color

The launcher uses color when the terminal supports it, in Kiro CLI's own brand
family so the two read as one tool set: Kiro violet (`#7B5CFA`) for group
headers, the label marker and detail field labels; Kiro green (`#2ecc71`) for
running sessions and the new-session entry; Kiro's muted lavender-grey
(`#a0a0b8`) for idle sessions, relative times and the divider. On a 256-color
terminal these use the nearest xterm-256 indices; on an 8/16-color terminal
they fall back to the nearest basic ANSI colors. Set `NO_COLOR` (any value) to
turn color off, or `KIRO_LAUNCH_COLOR=0`/`1` to force it off or on. A terminal
without color support renders in the plain monochrome style.

## Label store

Labels live under `~/.kiro/session-labels` by default (one JSON file per
session). Override with `KIRO_SESSION_LABELS_PATH`.

## Install / use

Requires Python 3.10+ and `kiro-cli` on `PATH`. No third-party dependencies
(stdlib `curses` only).

```sh
python3 kiro_launch.py          # the picker, scoped to the current directory
python3 kiro_launch.py --all    # every workspace
python3 kiro_sessions.py list   # the underlying label/session list
```

Shell aliases (defined in the operator's personal shell profile, pointing at
this checkout):

- `ks`: open the launcher
- `kn`: run `kiro-cli chat --v3` to start fresh

## Keys in the launcher

`enter` select · `^n`/`^p` down/up · `^e` label the highlighted session ·
`^g` new session · `tab` toggle current/all scope · `esc` quit

## Tests

```sh
python3 -m pytest tests/
```

The curses UI itself is not unit-tested, because it needs a real terminal. All
the pure logic it calls is tested.

## License

MIT. See [LICENSE](LICENSE).
