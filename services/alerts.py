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

import datetime
import html
import os
import queue
import re
import sys
import threading
import time
from collections import deque

_SIGNATURE_MAX = 200
_DIGITS_RE = re.compile(r"\d+")


def _signature(line):
    """Reduce a log line to a stable identity for dedupe.

    Digits are collapsed so the same failure against different rows, user ids or
    timestamps groups into one alert instead of hundreds.
    """
    collapsed = _DIGITS_RE.sub("#", line.strip())
    return collapsed[:_SIGNATURE_MAX]


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
    emoji = _LEVEL_EMOJI.get(level, "\u26A0\uFE0F")
    head = "{} <b>{}</b> \u00b7 {} \u00b7 {}".format(
        emoji, html.escape(level), html.escape(source), when.strftime("%H:%M")
    )
    parts = [head, "<code>{}</code>".format(html.escape(message.strip()))]
    if count:
        parts.append("\u21b3 <b>{} more</b> in the last 15 min".format(count))
    if frames:
        parts.append("<pre>{}</pre>".format(html.escape(frames)))
    if context:
        parts.append(html.escape(context))
    return _clip("\n".join(parts), TELEGRAM_MAX_CHARS)


def format_mute_notice(source, sent, suppressed, when):
    """Deliberately blunt: says both that something is wrong AND that the alert
    stream can no longer be trusted, so go read the real logs."""
    return (
        "\U0001F507 <b>{} muted</b> \u2014 {} alerts sent this hour, "
        "<b>{} suppressed</b>.\nCheck the server logs for the full picture."
    ).format(html.escape(source), sent, suppressed)


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


def _pick_richest(batch):
    """Collapse a batch to one entry per signature, keeping the richest.

    A crash is reported twice on purpose: the tee sees the printed line, and
    `global_error_handler` re-raises it via alert() carrying the user and the
    traceback. Both share a signature, so without this the plain one — enqueued
    first, because print() runs first — would win and the context would be
    silently dropped. Order of first appearance is preserved.
    """
    def richness(entry):
        _level, _line, context, frames = entry
        return (1 if frames else 0) + (1 if context else 0)

    best = {}
    order = []
    for entry in batch:
        sig = _signature(entry[1])
        if sig not in best:
            best[sig] = entry
            order.append(sig)
        elif richness(entry) > richness(best[sig]):
            best[sig] = entry
    return [best[sig] for sig in order]


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

            for level, line, context, frames in _pick_richest(batch):
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
        _log("[alerts] ALERT_CHAT_ID or a bot token is unset - alerting is off.")
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
    _log("[alerts] active - levels={} source={}".format(",".join(levels), _source))
    return True


if __name__ == "__main__":
    # Self-test: verifies the token and chat id WITHOUT deploying anything.
    if install_alerts():
        print("[ERROR] alerts self-test - if you can read this in Telegram, it works.")
        time.sleep(COALESCE_SECONDS + 5.0)
        print("[alerts] self-test finished.")
