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
