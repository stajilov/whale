# Structured Logger — Design Spec

**Date:** 2026-08-14
**Status:** Implemented
**Scope:** `logger.py` only — no changes to other modules.

## Purpose

Provide a single, easy-to-import structured JSON logger for the whale-harness CLI. All log lines are valid JSON objects, written to stdout and a rotating file, configured via the `LOG_LEVEL` environment variable.

## Goals

- One import: `from logger import logger`
- One call shape: `logger.info("msg", key="value")`
- JSON Lines output, one object per line, parseable by any JSON Lines consumer
- Zero new dependencies (uses Python stdlib `logging`)
- Never raise from a log call — failures degrade silently to stderr

## Non-Goals (YAGNI)

- Per-module loggers / hierarchical named loggers
- Remote sinks, log shipping, structured context binding across calls
- Async vs sync dispatch
- Log redaction / PII filtering
- Hot-reload of configuration
- Thread-local context (request IDs, etc.)

## Public API

```python
from logger import logger

logger.debug("starting", step=1)
logger.info("model call", model="openai/gpt-4o", tokens=123)
logger.warning("retry", attempt=2)
logger.error("failed", exc_info=True)   # or call inside an except block
logger.critical("shutdown", reason="oom")
```

Five level methods mirror `logging`: `debug`, `info`, `warning`, `error`, `critical`.

### Arguments

- First positional arg: human-readable message string. Supports `%`-style formatting with extra positional args, matching stdlib convention.
- Keyword args: merged as top-level keys in the JSON output.
- `exc_info=True` (or implicit when called inside an `except` block): adds `"exc_info": "<traceback string>"`.

## Log Line Shape

Every line is a single JSON object terminated by `\n`:

```json
{"ts":"2026-08-14T12:34:56.789Z","level":"INFO","logger":"whale","msg":"model call","session_id":"0d8c...","pid":1234,"hostname":"macbook.local","model":"openai/gpt-4o","tokens":123}
```

### Fields

| Field | Type | Source | Notes |
|---|---|---|---|
| `ts` | string | formatter | ISO 8601 UTC, millisecond precision, suffix `Z` |
| `level` | string | log record | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `logger` | string | constant | Always `"whale"` |
| `msg` | string | formatted record | The human message |
| `session_id` | string | module-level constant | UUID4 generated once at import |
| `pid` | int | module-level constant | `os.getpid()` at import |
| `hostname` | string | module-level constant | `socket.gethostname()` at import |
| `exc_info` | string | optional | Present only on error/critical with exception |
| _other_ | any | caller kwargs | Merged as top-level keys |

Reserved keys (`ts`, `level`, `logger`, `msg`, `session_id`, `pid`, `hostname`, `exc_info`) cannot be overridden by caller kwargs — if a caller passes `level="..."` it is silently ignored.

## Configuration

### Sources of configuration

- **`LOG_LEVEL` environment variable** — read once at module import.
  - Valid (case-insensitive): `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
  - Default: `INFO`.
  - Invalid value → log a single warning to stderr and use `INFO`.

No other configuration mechanisms. No CLI flags. No programmatic reconfigure.

### Handlers

1. **stdout** — `logging.StreamHandler(sys.stdout)`, JSON formatter.
2. **file** — `RotatingFileHandler` at `~/.whale/logs/whale.log`.
   - Created lazily on first log call (parent dir made with `mkdir(parents=True, exist_ok=True)`).
   - Rotation: 1 MB per file, 3 backups (`whale.log.1`, `whale.log.2`, `whale.log.3`).
   - Same JSON formatter as stdout.
   - If the file cannot be opened (permissions, disk full), fall back to stderr-only and write one warning to stderr.

### Logging must never raise

The root `whale` logger has a top-level try/except in a `logging.Handler.handleError` override (stdlib's default behavior is to swallow and write to `sys.stderr`). We additionally wrap the formatter to ensure any unexpected exception during format/emit is caught and reported to stderr.

## Architecture

`logger.py` contains four small units, all in one file:

1. **`_JsonFormatter(logging.Formatter)`** — converts a `LogRecord` into a JSON dict, handling `exc_info`, reserved keys, and kwargs from `record.__dict__["extra"]`.
2. **`_SafeHandler`** mixin — `handleError` that writes a single warning to stderr and never propagates.
3. **`_configure()`** — called once at import. Sets level, builds the two handlers, attaches formatter, prevents propagation.
4. **Module-level `logger = logging.getLogger("whale")`** — the public object.

The formatter reads caller kwargs from `record.__dict__` keys that are not stdlib `LogRecord` attributes. The convention: `logger.info("msg", foo=1)` translates to `logger.info("msg", extra={"foo": 1})` inside the module — but since callers use bare kwargs, we use a small helper `_log(level, msg, args, kwargs)` that pops kwargs into `extra`.

## Error Handling

| Failure | Behavior |
|---|---|
| Invalid `LOG_LEVEL` | Warn to stderr, use `INFO` |
| Cannot create `~/.whale/logs/` | Warn to stderr, drop file handler, keep stdout |
| Cannot open log file | Warn to stderr, drop file handler, keep stdout |
| JSON serialization of an extra value fails | Skip that key, continue with rest |
| Any handler emits an error | Caught by `handleError`, warn to stderr |

In every case, the calling code never sees an exception.

## Testing

Add `pytest` as a dev dependency. File: `tests/test_logger.py`.

Test cases:

1. Default level is `INFO` when `LOG_LEVEL` unset.
2. `LOG_LEVEL=DEBUG` enables debug records.
3. Invalid `LOG_LEVEL` falls back to `INFO` and writes a warning to stderr.
4. Output line is valid JSON.
5. Output line contains all core fields: `ts`, `level`, `logger`, `msg`, `session_id`, `pid`, `hostname`.
6. Caller kwargs appear as top-level keys.
7. Reserved keys from caller are ignored.
8. `exc_info=True` adds `exc_info` field.
9. Calling inside `except` block adds `exc_info` automatically.
10. Log file is created at `~/.whale/logs/whale.log` (use `tmp_path` monkeypatched home).
11. `LOG_LEVEL=CRITICAL` filters out `INFO` records.

Use `capsys` / `caplog` / monkeypatching of `LOG_LEVEL` and `Path.home()` to keep tests hermetic.

## Acceptance Criteria

- `from logger import logger; logger.info("hi")` writes one valid JSON line to stdout.
- The JSON line contains all core fields.
- Setting `LOG_LEVEL=DEBUG` in `.env` or shell enables debug output.
- No exception is ever raised from a `logger.*` call, even with pathological inputs.
- `pytest` passes; ruff passes (project uses ruff per cache dir).
- No new runtime dependencies in `pyproject.toml`.

## Out of Scope

Changes to `main.py`, `models.py`, README, or the existing project layout. Only `logger.py`, `tests/test_logger.py`, and the addition of `pytest` (dev-only) to `pyproject.toml`.