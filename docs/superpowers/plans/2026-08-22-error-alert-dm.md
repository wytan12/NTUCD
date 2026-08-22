# Error Alert DM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `[ERROR]` / `[WARN]` line the bot prints arrives as a throttled Telegram DM to the maintainer alone, identifying which user hit the problem.

**Architecture:** A tee wrapper replaces `sys.stdout`/`sys.stderr` at startup, passing all output through unchanged while enqueuing lines that match a level prefix. A background thread drains the queue, applies three layers of throttling, and POSTs to Telegram with `requests` — deliberately independent of PTB's `Bot` object and event loop, so it survives a wedged loop and ports unchanged to a second bot.

**Tech Stack:** Python 3, `requests` (already in requirements), `threading`, `queue`. Tests use `pytest` (local dev only — NOT added to `requirements.txt`, so Heroku never installs it).

**Spec:** `docs/superpowers/specs/2026-08-22-error-alert-dm-design.md`

## Global Constraints

- `services/alerts.py` MUST NOT import anything from this project (`config`, `services.*`, `handlers.*`, `utils.*`) and MUST NOT import `telegram`. It reads `os.environ` directly and duck-types any update object. This is what makes it copy-pasteable into bot #2.
- Never block the caller. `print()` is called from inside async handlers; the tee only enqueues.
- The alerter must never break the bot. Every failure path degrades to "print one line to the real stdout and continue".
- Heroku log output must remain a superset of today's — existing lines byte-identical, new `[ERROR]` lines added only in Task 7.
- No new entries in `requirements.txt` (`requests==2.32.3` is already present). `requirements.txt` is UTF-16 encoded — do not rewrite it.
- Timezone for display is Asia/Singapore, matching the rest of the bot.
- Env var contract (exact names): `ALERT_BOT_TOKEN`, `ALERT_CHAT_ID`, `ALERT_LEVELS`, `ALERT_SOURCE`, `ALERT_IGNORE`.

### Spec addenda discovered during planning

1. **`[transient]` collision.** `main.py:89` prints transient network noise as
   `[ERROR][transient] ...`. That matches the `[ERROR]` prefix but is exactly the
   self-healing noise the code deliberately quiets. Hence `ALERT_IGNORE` (default
   `[transient]`): comma-separated substrings that veto a line even when its
   prefix matches.

2. **Coverage gap.** An audit of all 179 `except` blocks found **20** that report
   a failure to the user (`edit_message_text("❌ Failed…")`) but never print it —
   so they reach neither Heroku nor the alerter. These are the true blind spot
   and are fixed in Task 7. A further 26 blocks swallow silently, but 15 are
   `except ValueError: continue` around sheet-cell parsing (normal control flow
   for a blank cell) and the rest are `except BadRequest: pass` around edits of
   already-gone messages. Those are deliberately left alone as noise.

---

### Task 1: Module scaffold and signature extraction

**Files:**
- Create: `services/alerts.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_alerts.py`
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: nothing
- Produces: `services.alerts._signature(line: str) -> str`

- [ ] **Step 1: Create the dev requirements file**

```bash
printf 'pytest==8.3.2\n' > requirements-dev.txt
python -m pip install -r requirements-dev.txt
```

Deliberately separate from `requirements.txt` so Heroku does not install pytest
into the worker dyno.

- [ ] **Step 2: Write the failing test**

Create `tests/__init__.py` as an empty file, then create `tests/test_alerts.py`:

```python
from services.alerts import _signature


def test_signature_collapses_digits():
    a = _signature("[ERROR] Sheet write failed for row 47")
    b = _signature("[ERROR] Sheet write failed for row 912")
    assert a == b


def test_signature_separates_different_messages():
    a = _signature("[ERROR] Sheet write failed for row 47")
    b = _signature("[ERROR] Telegram send failed for row 47")
    assert a != b


def test_signature_is_bounded():
    assert len(_signature("[WARN] " + "x" * 5000)) <= 200
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.alerts'`

- [ ] **Step 4: Create the module with the minimal implementation**

Create `services/alerts.py`:

```python
"""Standalone Telegram error-alert forwarder.

Drop-in for any Python Telegram bot. Imports NOTHING from the host project and
nothing from `telegram` — configuration comes from environment variables only,
so this file can be copied into another bot unchanged.

    ALERT_BOT_TOKEN  sender bot token. Falls back to BOT_TOKEN when unset.
    ALERT_CHAT_ID    REQUIRED. The maintainer's Telegram user id.
    ALERT_LEVELS     comma-separated prefixes to alert on. Default "ERROR,WARN".
    ALERT_SOURCE     label naming which bot is alerting. Default "BOT".
    ALERT_IGNORE     comma-separated substrings that veto a line. Default "[transient]".

SETUP GOTCHA: Telegram forbids a bot from DMing a user who has never messaged
it. If you point ALERT_BOT_TOKEN at a NEW bot, you must press Start on that bot
once, or every alert fails with 403.

Usage:
    from services.alerts import install_alerts
    install_alerts()   # once, as early in startup as possible

Self-test:
    python -m services.alerts
"""

import re

_SIGNATURE_MAX = 200
_DIGITS_RE = re.compile(r"\d+")


def _signature(line):
    """Reduce a log line to a stable identity for dedupe.

    Digits are collapsed so the same failure against different rows, user ids or
    timestamps groups into one alert instead of hundreds.
    """
    collapsed = _DIGITS_RE.sub("#", line.strip())
    return collapsed[:_SIGNATURE_MAX]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: PASS — 3 passed

- [ ] **Step 6: Commit**

```bash
git add services/alerts.py tests/__init__.py tests/test_alerts.py requirements-dev.txt
git commit -m "feat(alerts): add module scaffold and log-line signature"
```

---

### Task 2: Throttle — dedupe, hourly cap, window summaries

**Files:**
- Modify: `services/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `_signature(line) -> str`
- Produces:
  - `Throttle(now_fn=time.monotonic, dedupe_window=900.0, hourly_cap=20)`
  - `Throttle.admit(sig: str, line: str) -> str` returning `"SEND"` | `"SUPPRESS"` | `"MUTE"`
  - `Throttle.due_summaries() -> list[tuple[str, int]]` of `(sample_line, suppressed_count)`
  - `Throttle.muted_report() -> tuple[int, int] | None` returning `(sent, suppressed)` once per mute period

`"MUTE"` is returned exactly once, on the transition into the muted state; every
subsequent admit while muted returns `"SUPPRESS"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
from services.alerts import Throttle


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_first_of_a_signature_sends():
    th = Throttle(now_fn=FakeClock())
    assert th.admit("sig-a", "[ERROR] boom") == "SEND"


def test_repeat_within_window_is_suppressed():
    th = Throttle(now_fn=FakeClock())
    th.admit("sig-a", "[ERROR] boom")
    assert th.admit("sig-a", "[ERROR] boom") == "SUPPRESS"
    assert th.admit("sig-a", "[ERROR] boom") == "SUPPRESS"


def test_different_signature_sends_independently():
    th = Throttle(now_fn=FakeClock())
    th.admit("sig-a", "[ERROR] boom")
    assert th.admit("sig-b", "[ERROR] other") == "SEND"


def test_window_expiry_yields_a_summary_with_the_count():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    for _ in range(47):
        th.admit("sig-a", "[ERROR] boom")
    assert th.due_summaries() == []
    clock.advance(901)
    assert th.due_summaries() == [("[ERROR] boom", 47)]
    assert th.due_summaries() == []


def test_window_expiry_with_no_suppression_yields_nothing():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    clock.advance(901)
    assert th.due_summaries() == []


def test_signature_resends_after_its_window_expires():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    clock.advance(901)
    th.due_summaries()
    assert th.admit("sig-a", "[ERROR] boom") == "SEND"


def test_hourly_cap_mutes_once_then_suppresses():
    th = Throttle(now_fn=FakeClock(), dedupe_window=900.0, hourly_cap=3)
    assert th.admit("s1", "x") == "SEND"
    assert th.admit("s2", "x") == "SEND"
    assert th.admit("s3", "x") == "SEND"
    assert th.admit("s4", "x") == "MUTE"
    assert th.admit("s5", "x") == "SUPPRESS"


def test_muted_report_returns_counts_once():
    th = Throttle(now_fn=FakeClock(), dedupe_window=900.0, hourly_cap=2)
    th.admit("s1", "x")
    th.admit("s2", "x")
    th.admit("s3", "x")
    th.admit("s4", "x")
    assert th.muted_report() == (2, 2)
    assert th.muted_report() is None


def test_cap_resets_after_an_hour():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0, hourly_cap=2)
    th.admit("s1", "x")
    th.admit("s2", "x")
    assert th.admit("s3", "x") == "MUTE"
    clock.advance(3601)
    assert th.admit("s4", "x") == "SEND"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ImportError: cannot import name 'Throttle'`

- [ ] **Step 3: Implement Throttle**

In `services/alerts.py`, extend the imports at the top to include `time` and
`deque`, then append the class:

```python
import time
from collections import deque

DEDUPE_WINDOW_SECONDS = 900.0
HOURLY_CAP = 20
_RATE_PERIOD_SECONDS = 3600.0


class Throttle:
    """Decides whether a given alert may be sent right now.

    Three layers, in order:
      1. signature dedupe  — one send per signature per dedupe_window
      2. hourly rate cap   — a hard ceiling on total sends per hour
      3. window summaries  — a trailing "N more" count when a window closes

    Not thread-safe by itself; the worker thread is its only caller.
    """

    def __init__(self, now_fn=time.monotonic, dedupe_window=DEDUPE_WINDOW_SECONDS,
                 hourly_cap=HOURLY_CAP):
        self._now = now_fn
        self._dedupe_window = dedupe_window
        self._hourly_cap = hourly_cap
        self._windows = {}          # sig -> [opened_at, suppressed, sample_line]
        self._sent_at = deque()     # monotonic timestamps of real sends
        self._muted = False
        self._muted_suppressed = 0
        self._muted_sent = 0
        self._pending_report = None

    def _prune_rate(self, now):
        cutoff = now - _RATE_PERIOD_SECONDS
        while self._sent_at and self._sent_at[0] < cutoff:
            self._sent_at.popleft()

    def admit(self, sig, line):
        now = self._now()
        self._prune_rate(now)

        if self._muted and len(self._sent_at) < self._hourly_cap:
            # The hour rolled far enough to free capacity again.
            self._muted = False
            self._muted_suppressed = 0
            self._muted_sent = 0

        window = self._windows.get(sig)
        if window is not None and (now - window[0]) < self._dedupe_window:
            window[1] += 1
            return "SUPPRESS"

        if self._muted:
            self._muted_suppressed += 1
            return "SUPPRESS"

        if len(self._sent_at) >= self._hourly_cap:
            self._muted = True
            self._muted_sent = len(self._sent_at)
            self._muted_suppressed = 1
            self._pending_report = None
            return "MUTE"

        self._windows[sig] = [now, 0, line]
        self._sent_at.append(now)
        return "SEND"

    def due_summaries(self):
        """Return (sample_line, suppressed_count) for windows that just closed.

        Only windows that actually suppressed something are reported; a closed
        window with a zero count is simply forgotten.
        """
        now = self._now()
        out = []
        for sig in list(self._windows):
            opened_at, suppressed, sample = self._windows[sig]
            if (now - opened_at) >= self._dedupe_window:
                del self._windows[sig]
                if suppressed:
                    out.append((sample, suppressed))
        return out

    def muted_report(self):
        """Return (sent, suppressed) exactly once per mute period."""
        if not self._muted:
            return None
        if self._pending_report is not None:
            return None
        self._pending_report = (self._muted_sent, self._muted_suppressed)
        return self._pending_report
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: PASS — 12 passed

- [ ] **Step 5: Commit**

```bash
git add services/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): add three-layer throttle with dedupe and hourly cap"
```

---

### Task 3: Message formatting and user identification

**Files:**
- Modify: `services/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `format_alert(level: str, source: str, message: str, when: datetime.datetime, count: int = 0, context: str | None = None, frames: str | None = None) -> str`
  - `format_mute_notice(source: str, sent: int, suppressed: int, when: datetime.datetime) -> str`
  - `who(update) -> str` — duck-typed, returns `"@handle (12345)"`, `"12345"`, or `"unknown user"`
  - `TELEGRAM_MAX_CHARS = 4096`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
import datetime

from services.alerts import (
    TELEGRAM_MAX_CHARS,
    format_alert,
    format_mute_notice,
    who,
)

WHEN = datetime.datetime(2026, 8, 22, 9, 14)


def test_format_puts_level_source_and_time_on_the_first_line():
    out = format_alert("ERROR", "NTUFD", "[ERROR] boom", WHEN)
    first = out.splitlines()[0]
    assert "ERROR" in first
    assert "NTUFD" in first
    assert "09:14" in first


def test_format_uses_distinct_emoji_per_level():
    assert format_alert("ERROR", "N", "x", WHEN).startswith("\U0001F534")
    assert format_alert("WARN", "N", "x", WHEN).startswith("\U0001F7E0")


def test_format_includes_the_message_body():
    out = format_alert("ERROR", "NTUFD", "[ERROR] Sheet write failed", WHEN)
    assert "Sheet write failed" in out


def test_format_escapes_html_so_telegram_does_not_reject_it():
    out = format_alert("ERROR", "NTUFD", "[ERROR] <b>x</b> & co", WHEN)
    assert "&lt;b&gt;" in out
    assert "&amp;" in out


def test_format_adds_a_count_line_only_when_suppressed():
    assert "47 more" in format_alert("ERROR", "N", "x", WHEN, count=47)
    assert "more" not in format_alert("ERROR", "N", "x", WHEN, count=0)


def test_format_includes_context_and_frames_when_given():
    out = format_alert("ERROR", "N", "x", WHEN,
                       context="user 123", frames="a.py:1 in f")
    assert "user 123" in out
    assert "a.py:1 in f" in out


def test_format_truncates_to_the_telegram_limit():
    out = format_alert("ERROR", "N", "y" * 9000, WHEN)
    assert len(out) <= TELEGRAM_MAX_CHARS
    assert "truncated" in out


def test_mute_notice_names_both_counts():
    out = format_mute_notice("NTUFD", 20, 312, WHEN)
    assert "20" in out and "312" in out and "NTUFD" in out


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeUpdate:
    def __init__(self, user):
        self.effective_user = user


def test_who_renders_handle_and_id():
    assert who(FakeUpdate(FakeUser(1505249420, "weiyin"))) == "@weiyin (1505249420)"


def test_who_falls_back_to_the_bare_id():
    assert who(FakeUpdate(FakeUser(1505249420))) == "1505249420"


def test_who_never_raises_on_a_junk_update():
    assert who(None) == "unknown user"
    assert who(object()) == "unknown user"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_alert'`

- [ ] **Step 3: Implement the formatters and `who`**

Add `html` to the imports in `services/alerts.py`, then append:

```python
import html

TELEGRAM_MAX_CHARS = 4096
_TRUNCATION_MARKER = "\n\n… truncated"

# Colour-coded circles beat text prefixes for at-a-glance triage in a scrolling
# DM thread, which is how these are actually read.
_LEVEL_EMOJI = {"ERROR": "\U0001F534", "WARN": "\U0001F7E0", "INFO": "\U0001F535"}


def who(update):
    """Identify the user behind an update, for 'who hit this?' in an alert.

    Duck-typed on purpose: this module never imports `telegram`, so it stays
    portable. Returns "unknown user" rather than raising, because an alerter
    that crashes while describing a crash is worse than a vague alert.
    """
    try:
        user = getattr(update, "effective_user", None)
        if user is None:
            return "unknown user"
        uid = getattr(user, "id", None)
        if uid is None:
            return "unknown user"
        handle = getattr(user, "username", None)
        return "@{} ({})".format(handle, uid) if handle else str(uid)
    except Exception:
        return "unknown user"


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def format_alert(level, source, message, when, count=0, context=None, frames=None):
    """Build the Telegram HTML body for one alert.

    The first line carries level, source and time so the alert is triageable
    straight from a lock-screen notification, without opening the chat.
    """
    emoji = _LEVEL_EMOJI.get(level, "⚠️")
    head = "{} <b>{}</b> · {} · {}".format(
        emoji, html.escape(level), html.escape(source), when.strftime("%H:%M")
    )
    parts = [head, "<code>{}</code>".format(html.escape(message.strip()))]
    if count:
        parts.append("↳ <b>{} more</b> in the last 15 min".format(count))
    if frames:
        parts.append("<pre>{}</pre>".format(html.escape(frames)))
    if context:
        parts.append(html.escape(context))
    return _clip("\n".join(parts), TELEGRAM_MAX_CHARS)


def format_mute_notice(source, sent, suppressed, when):
    """Deliberately blunt: says both that something is wrong AND that the alert
    stream can no longer be trusted, so go read the real logs."""
    return (
        "\U0001F507 <b>{} muted</b> — {} alerts sent this hour, "
        "<b>{} suppressed</b>.\nCheck the server logs for the full picture."
    ).format(html.escape(source), sent, suppressed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: PASS — 23 passed

- [ ] **Step 5: Commit**

```bash
git add services/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): add message formatting and user identification"
```

---

### Task 4: The stdout/stderr tee

**Files:**
- Modify: `services/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `AlertTee(stream, prefixes: tuple[str, ...], ignore: tuple[str, ...], emit)`
  - `AlertTee.write(text) -> int`, `.flush()`, `.isatty()`, `.fileno()`, `.writelines(lines)`
  - `_SENDING` — a `threading.local()` whose truthy `.active` attribute suppresses capture (the recursion guard)

`emit` is called as `emit(level, line)` where `level` is the matched prefix
without brackets and `line` is the stripped log line.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
import io

from services.alerts import _SENDING, AlertTee


def make_tee():
    sink = io.StringIO()
    seen = []
    tee = AlertTee(sink, ("ERROR", "WARN"), ("[transient]",),
                   lambda level, line: seen.append((level, line)))
    return tee, sink, seen


def test_tee_passes_everything_through_unchanged():
    tee, sink, _ = make_tee()
    tee.write("[ERROR] boom\n")
    tee.write("ordinary output\n")
    assert sink.getvalue() == "[ERROR] boom\nordinary output\n"


def test_tee_emits_matching_lines():
    tee, _, seen = make_tee()
    tee.write("[ERROR] boom\n")
    assert seen == [("ERROR", "[ERROR] boom")]


def test_tee_ignores_non_matching_lines():
    tee, _, seen = make_tee()
    tee.write("[INFO] hello\n")
    tee.write("bare line\n")
    assert seen == []


def test_tee_honours_the_ignore_list():
    tee, _, seen = make_tee()
    tee.write("[ERROR][transient] NetworkError: read timeout\n")
    assert seen == []


def test_tee_reassembles_lines_split_across_writes():
    tee, _, seen = make_tee()
    tee.write("[WARN] split ")
    tee.write("message\n")
    assert seen == [("WARN", "[WARN] split message")]


def test_tee_handles_several_lines_in_one_write():
    tee, _, seen = make_tee()
    tee.write("[ERROR] one\n[WARN] two\n")
    assert seen == [("ERROR", "[ERROR] one"), ("WARN", "[WARN] two")]


def test_tee_does_not_capture_while_the_sending_guard_is_set():
    tee, sink, seen = make_tee()
    _SENDING.active = True
    try:
        tee.write("[ERROR] boom\n")
    finally:
        _SENDING.active = False
    assert seen == []
    assert sink.getvalue() == "[ERROR] boom\n"


def test_tee_never_lets_an_emit_failure_escape():
    sink = io.StringIO()

    def exploding_emit(level, line):
        raise RuntimeError("emit is broken")

    tee = AlertTee(sink, ("ERROR",), (), exploding_emit)
    tee.write("[ERROR] boom\n")
    assert sink.getvalue() == "[ERROR] boom\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ImportError: cannot import name '_SENDING'`

- [ ] **Step 3: Implement AlertTee**

Add `threading` to the imports in `services/alerts.py`, then append:

```python
import threading

# Set while the worker is sending. Anything printed inside that window bypasses
# capture entirely, so a failing alert can never trigger another alert.
_SENDING = threading.local()


class AlertTee:
    """Passthrough stream wrapper that also reports matching log lines.

    Every byte still reaches the wrapped stream, so server logs are unchanged.
    Lines are buffered because print() issues the text and the newline as two
    separate write() calls.

    Methods are declared explicitly rather than proxied via __getattr__, so a
    missing attribute fails loudly here instead of recursing.
    """

    def __init__(self, stream, prefixes, ignore, emit):
        self._stream = stream
        self._prefixes = tuple(prefixes)
        self._ignore = tuple(ignore)
        self._emit = emit
        self._buffer = ""

    def write(self, text):
        written = self._stream.write(text)
        if getattr(_SENDING, "active", False):
            return written
        try:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._consider(line)
        except Exception:
            # A broken alerter must never break the program it is watching.
            self._buffer = ""
        return written

    def _consider(self, line):
        stripped = line.strip()
        if not stripped:
            return
        for needle in self._ignore:
            if needle in stripped:
                return
        for prefix in self._prefixes:
            if stripped.startswith("[{}]".format(prefix)):
                try:
                    self._emit(prefix, stripped)
                except Exception:
                    pass
                return

    def flush(self):
        self._stream.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: PASS — 31 passed

- [ ] **Step 5: Commit**

```bash
git add services/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): add passthrough stdout tee with recursion guard"
```

---

### Task 5: Sender, worker thread, and install_alerts

**Files:**
- Modify: `services/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `_signature`, `Throttle`, `format_alert`, `format_mute_notice`, `AlertTee`, `_SENDING`
- Produces:
  - `alert(message: str, level: str = "ERROR", context: str | None = None, frames: str | None = None) -> None`
  - `install_alerts() -> bool` (True when active, False when it no-opped)
  - `_deliver(token, chat_id, text, session=None) -> tuple[bool, bool]` returning `(ok, permanent_failure)`
  - `COALESCE_SECONDS = 3.0`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
import services.alerts as alerts_mod
from services.alerts import _deliver


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "body"


class FakeSession:
    def __init__(self, status_code):
        self._status_code = status_code
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeResponse(self._status_code)


def test_deliver_reports_success():
    session = FakeSession(200)
    assert _deliver("tok", "42", "hi", session=session) == (True, False)
    assert "tok" in session.calls[0][0]
    assert session.calls[0][1]["chat_id"] == "42"


def test_deliver_marks_403_as_permanent():
    assert _deliver("tok", "42", "hi", session=FakeSession(403)) == (False, True)


def test_deliver_marks_401_as_permanent():
    assert _deliver("tok", "42", "hi", session=FakeSession(401)) == (False, True)


def test_deliver_marks_500_as_retryable():
    assert _deliver("tok", "42", "hi", session=FakeSession(500)) == (False, False)


def test_deliver_survives_a_thrown_transport_error():
    class Exploding:
        def post(self, url, json=None, timeout=None):
            raise OSError("network down")

    assert _deliver("tok", "42", "hi", session=Exploding()) == (False, False)


def test_install_noops_without_a_chat_id(monkeypatch):
    monkeypatch.delenv("ALERT_CHAT_ID", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "tok")
    monkeypatch.setattr(alerts_mod, "_installed", False)
    assert alerts_mod.install_alerts() is False


def test_install_noops_without_any_token(monkeypatch):
    monkeypatch.setenv("ALERT_CHAT_ID", "42")
    monkeypatch.delenv("ALERT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(alerts_mod, "_installed", False)
    assert alerts_mod.install_alerts() is False


def test_alert_before_install_is_a_silent_noop():
    alerts_mod.alert("[ERROR] nobody is listening")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ImportError: cannot import name '_deliver'`

- [ ] **Step 3: Implement the sender, worker and installer**

Add `datetime`, `os`, `queue`, `sys` to the imports in `services/alerts.py`,
then append:

```python
import datetime
import os
import queue
import sys

COALESCE_SECONDS = 3.0
QUEUE_MAX = 1000
_RETRY_DELAYS = (1.0, 3.0)
_HTTP_TIMEOUT = 10.0
_SGT = datetime.timezone(datetime.timedelta(hours=8))

_queue = None
_throttle = None
_installed = False
_disabled = False
_real_stdout = sys.__stdout__
_token = None
_chat_id = None
_source = "BOT"


def _log(text):
    """Write to the ORIGINAL stdout, bypassing the tee entirely."""
    try:
        _real_stdout.write(text + "\n")
        _real_stdout.flush()
    except Exception:
        pass


def _deliver(token, chat_id, text, session=None):
    """POST one message. Returns (ok, permanent_failure).

    A permanent failure (bad token, or the user never pressed Start) is never
    worth retrying, so the caller disables alerting entirely instead of
    hammering Telegram for the life of the process.
    """
    if session is None:
        import requests

        session = requests
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = session.post(url, json=payload, timeout=_HTTP_TIMEOUT)
    except Exception:
        return (False, False)
    if response.status_code == 200:
        return (True, False)
    if response.status_code in (400, 401, 403, 404):
        return (False, True)
    return (False, False)


def _send_with_retry(text):
    """Send, retrying only transient failures. Sets the recursion guard."""
    global _disabled
    _SENDING.active = True
    try:
        for delay in (0.0,) + _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            ok, permanent = _deliver(_token, _chat_id, text)
            if ok:
                return True
            if permanent:
                _disabled = True
                _log("[alerts] permanent send failure (bad token, or you have "
                     "never pressed Start on the alert bot). Alerting disabled.")
                return False
        return False
    finally:
        _SENDING.active = False


def _enqueue(level, line, context=None, frames=None):
    if _queue is None or _disabled:
        return
    try:
        _queue.put_nowait((level, line, context, frames))
    except queue.Full:
        # Dropping a diagnostic beats blocking a handler mid-request.
        pass


def alert(message, level="ERROR", context=None, frames=None):
    """Explicitly raise an alert with richer context than a bare log line."""
    _enqueue(level, message, context, frames)


def _worker():
    """Drain the queue forever: coalesce a burst, throttle, then send."""
    while True:
        try:
            batch = [_queue.get()]
            time.sleep(COALESCE_SECONDS)
            while True:
                try:
                    batch.append(_queue.get_nowait())
                except queue.Empty:
                    break

            for level, line, context, frames in batch:
                if _disabled:
                    break
                decision = _throttle.admit(_signature(line), line)
                if decision == "SUPPRESS":
                    continue
                if decision == "MUTE":
                    report = _throttle.muted_report()
                    if report:
                        _send_with_retry(
                            format_mute_notice(_source, report[0], report[1],
                                               datetime.datetime.now(_SGT))
                        )
                    continue
                _send_with_retry(
                    format_alert(level, _source, line,
                                 datetime.datetime.now(_SGT),
                                 context=context, frames=frames)
                )

            for sample, count in _throttle.due_summaries():
                if _disabled:
                    break
                _send_with_retry(
                    format_alert("ERROR", _source, sample,
                                 datetime.datetime.now(_SGT), count=count)
                )
        except Exception as exc:
            # The worker must survive anything; a dead thread means silent
            # blindness, the exact failure this feature exists to fix.
            _log("[alerts] worker error: {}: {}".format(type(exc).__name__, exc))
            time.sleep(1.0)


def install_alerts():
    """Swap in the tee and start the worker. Safe to call more than once.

    Returns True when alerting is active, False when it deliberately no-opped
    (missing configuration), which is the normal case for local development.
    """
    global _queue, _throttle, _installed, _token, _chat_id, _source

    if _installed:
        return True

    _token = os.environ.get("ALERT_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    _chat_id = os.environ.get("ALERT_CHAT_ID")
    _source = os.environ.get("ALERT_SOURCE", "BOT")

    if not _chat_id or not _token:
        _log("[alerts] ALERT_CHAT_ID or a bot token is unset — alerting is off.")
        return False

    levels = tuple(
        part.strip().upper()
        for part in os.environ.get("ALERT_LEVELS", "ERROR,WARN").split(",")
        if part.strip()
    )
    ignore = tuple(
        part.strip()
        for part in os.environ.get("ALERT_IGNORE", "[transient]").split(",")
        if part.strip()
    )

    _queue = queue.Queue(maxsize=QUEUE_MAX)
    _throttle = Throttle()

    sys.stdout = AlertTee(sys.stdout, levels, ignore, _enqueue)
    sys.stderr = AlertTee(sys.stderr, levels, ignore, _enqueue)

    threading.Thread(target=_worker, name="alert-worker", daemon=True).start()
    _installed = True
    _log("[alerts] active — levels={} source={}".format(",".join(levels), _source))
    return True


if __name__ == "__main__":
    # Self-test: verifies the token and chat id WITHOUT deploying anything.
    if install_alerts():
        print("[ERROR] alerts self-test — if you can read this in Telegram, it works.")
        time.sleep(COALESCE_SECONDS + 5.0)
        print("[alerts] self-test finished.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_alerts.py -v`
Expected: PASS — 39 passed

- [ ] **Step 5: Verify the module imports cleanly standalone**

Run: `python -c "import services.alerts; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add services/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): add Telegram sender, worker thread and installer"
```

---

### Task 6: Wire into the bot

**Files:**
- Modify: `main.py` (import block near line 42; `global_error_handler` near line 92; `main()` near line 214)

**Interfaces:**
- Consumes: `install_alerts()`, `alert(message, level, context, frames)`, `who(update)`
- Produces: nothing

Wiring goes in `main()` rather than `on_startup`, because `on_startup` is a PTB
`post_init` hook that runs only after the application is built — startup failures
before that point would go unreported. `main()` is the true first line of the
process.

- [ ] **Step 1: Add the import**

In `main.py`, after the existing `from config import ...` line (line 42), add:

```python
from services.alerts import alert, install_alerts, who
```

- [ ] **Step 2: Install as the first statement of main()**

In `main.py`, change:

```python
def main():
    print("Bot starting...")
```

to:

```python
def main():
    # Route [ERROR]/[WARN] log lines to the maintainer's DM. No-ops without
    # ALERT_CHAT_ID, so local runs are unaffected. See services/alerts.py.
    install_alerts()
    print("Bot starting...")
```

- [ ] **Step 3: Enrich crash alerts with the update context**

In `main.py`, inside `global_error_handler`, replace these two lines:

```python
    print(f"[ERROR] Unhandled exception while processing an update: {type(err).__name__}: {err}")
    traceback.print_exception(type(err), err, err.__traceback__)
```

with:

```python
    summary = f"[ERROR] Unhandled exception while processing an update: {type(err).__name__}: {err}"
    print(summary)
    traceback.print_exception(type(err), err, err.__traceback__)

    # The tee already alerted on the printed line above, but without the who and
    # the what. Re-raising it here with the update context is what turns
    # "something broke" into "I can go reproduce it". The throttle's signature
    # dedupe collapses the two into one DM.
    frames = None
    if err.__traceback__:
        frames = "".join(traceback.format_tb(err.__traceback__)[-3:]).strip()
    bits = [f"👤 {who(update)}"]
    if isinstance(update, Update) and update.callback_query and update.callback_query.data:
        bits.append(f"🧩 {update.callback_query.data}")
    alert(summary, level="ERROR", context=" · ".join(bits), frames=frames)
```

- [ ] **Step 4: Verify the bot still imports**

Run: `python -c "import main; print('ok')"`
Expected: `ok` (or a pre-existing config error unrelated to alerts — if so,
confirm the traceback does not mention `services.alerts`)

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — 39 passed

- [ ] **Step 6: Local end-to-end verification**

`ALERT_CHAT_ID` is the maintainer's own Telegram user id.

```bash
ALERT_BOT_TOKEN=<the NTUFD bot token> ALERT_CHAT_ID=<your telegram user id> ALERT_SOURCE=NTUFD python -m services.alerts
```

Expected: one DM reading `🔴 ERROR · NTUFD · HH:MM` followed by the self-test
line. If nothing arrives, confirm you have DMed that bot at least once.

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat(alerts): route error and warning logs to the maintainer DM"
```

---

### Task 7: Close the 20-block coverage gap

**Files:**
- Modify: `handlers/admin_handlers.py` (lines 120, 362, 375, 445, 621, 634, 709)
- Modify: `handlers/attendance_handlers.py` (lines 347, 379, 414, 516)
- Modify: `handlers/modify_handlers.py`, `handlers/private_handlers.py`, `handlers/poll_handlers.py`, `handlers/welcome_tea_handlers.py` (remaining sites found by the audit command below)

**Interfaces:**
- Consumes: `who(update)` from `services/alerts.py`
- Produces: nothing

These blocks currently tell the admin "❌ Failed…" on screen and log nothing, so
the failure reaches neither Heroku nor the maintainer. Each gets one added
`print` line. No control flow changes.

- [ ] **Step 1: Re-run the audit to get the current line numbers**

```bash
for f in $(find . -name "*.py" -not -path "./venv/*"); do awk -v F="$f" '/except/{ln=NR; getline nxt; if (nxt ~ /edit_message_text|reply_text|answer\(/ && nxt !~ /print/) printf "%s:%d\n", F, ln}' "$f"; done
```

Expected: 20 `file:line` pairs. Work through them in the order printed.

- [ ] **Step 2: Add the import to each file you touch**

At the top of every handler file modified in this task, add:

```python
from services.alerts import who
```

- [ ] **Step 3: Add one print per block**

For each site, insert a `print` immediately after the `except` line, keeping the
existing user-facing message untouched. Example, at
`handlers/attendance_handlers.py:347`:

```python
        except Exception as e:
            print(f"[ERROR] attendance: failed to load attendees — {who(update)}: {e}")
            await query.edit_message_text(f"❌ Failed to load attendees: {e}")
```

Rules for the message text:
- start with `[ERROR]`, then a short area tag (`attendance:`, `remind:`,
  `announce:`, `modify:`, `perf:`) so signatures group sensibly;
- describe the operation, not the exception class;
- end with `— {who(update)}: {e}`.

If the enclosing function has no `update` in scope but has `query`, use a
lightweight shim instead:

```python
print(f"[ERROR] announce: broadcast failed — @{query.from_user.username} ({query.from_user.id}): {exc}")
```

- [ ] **Step 4: Verify every touched file still imports**

```bash
python -c "import handlers.admin_handlers, handlers.attendance_handlers, handlers.modify_handlers, handlers.private_handlers, handlers.poll_handlers, handlers.welcome_tea_handlers; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Confirm the gap is closed**

Re-run the Step 1 audit command.
Expected: no output — every such block now prints.

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — 39 passed

- [ ] **Step 7: Commit**

```bash
git add handlers/
git commit -m "fix(logging): log the 20 failures that were only shown to the user"
```

- [ ] **Step 8: Deploy configuration (manual, by the maintainer)**

`ALERT_BOT_TOKEN` is deliberately left unset, so alerts come from the existing
bot and nothing new has to be created:

```bash
heroku config:set ALERT_CHAT_ID=<your telegram user id> ALERT_SOURCE=NTUFD
```

---

## Verification

After Task 7, confirm all of the following:

1. `python -m pytest tests/ -v` — 39 passed.
2. Heroku logs after deploy still contain every `[ERROR]`/`[WARN]` line exactly as before, plus the 20 new ones.
3. A deliberate error (tap a stale dashboard button) produces exactly ONE DM, not two, despite both the tee and `global_error_handler` reporting it.
4. That DM names the user who triggered it.
5. `[ERROR][transient]` network noise produces NO DM.
6. Removing the `install_alerts()` line from `main()` fully restores previous alerting behaviour (the Task 7 log lines remain, which is desirable on its own).
