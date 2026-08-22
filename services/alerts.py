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
