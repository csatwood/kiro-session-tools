# kiro_session_tools

Tooling to make Kiro sessions findable and resumable by a label you choose,
over the model-generated auto-titles Kiro's native `--sessions` dashboard shows.

## Compatibility

Verified against Kiro CLI 2.25.0. This tooling reads Kiro's session store
layout (sessions keyed by `sha256(absolute_cwd)[:16]`), which is observed
behavior, not a documented, stable API. A future Kiro release could change it
and break the store-level features (notably `kiro_relocate.py`). The label
layer and picker degrade gracefully; relocate is the most layout-dependent.

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

## Relocating sessions after a repo move

Kiro keys its session store by `sha256(absolute_cwd)[:16]`. If you move a
project directory on disk, its sessions keep existing but stop showing up in
`ks` / `kiro-cli --list-sessions` from the new path (they are still resumable
by id). `kiro_relocate.py` re-indexes them to the new path:

```sh
python3 kiro_relocate.py --old /old/path/to/repo --new /new/path/to/repo --dry-run
python3 kiro_relocate.py --old /old/path/to/repo --new /new/path/to/repo
```

**Back up `~/.kiro` before a real run.** This edits Kiro's own session store.
It is guarded (dry-run by default, refuses to clobber an existing target,
refuses a relative path or a `--new` inside `--old`, writes atomically), but a
backup is the right habit before any in-place store edit:

```sh
cp -a ~/.kiro/sessions ~/.kiro/sessions.bak && cp -a ~/.kiro/session-index ~/.kiro/session-index.bak
```

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
