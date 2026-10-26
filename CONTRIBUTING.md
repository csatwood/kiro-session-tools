# Contributing

Contributions are welcome. This is a small, dependency-light tool; please keep
it that way.

## Getting Started

1. Clone the repository:

   ```bash
   git clone https://github.com/csatwood/kiro-session-tools.git
   cd kiro-session-tools
   ```

2. Create a virtual environment (only `pytest` is needed, and only for tests):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   The tools themselves have no third-party runtime dependencies: Python 3.10+
   standard library plus `kiro-cli` on `PATH`.

## Running the tests

```bash
python3 -m pytest tests/
```

The curses UI (`kiro_launch.py`'s picker loop) needs a real terminal and is not
unit-tested. The pure logic it calls has full test coverage, with an injected
subprocess runner, so the suite never shells out to the real `kiro_sessions.py`
or `kiro-cli`.

## Conventions

- Keep the curses loop a thin shell over pure, tested helpers. A behavior worth
  asserting goes in a pure function the tests can call, not inside the loop.
- All session data and label writes route through `kiro_sessions.py`. The
  launcher never reads or writes the label store or Kiro's own store directly.
- Match the surrounding Python style. Snake_case module names (toolchain
  convention), four-space indent.
