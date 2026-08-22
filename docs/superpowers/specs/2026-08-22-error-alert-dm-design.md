# Error Alert DM — Design

**Date:** 2026-08-22
**Status:** Approved
**Branch:** cleanup-version

## Problem

Silent failures in the NTUFD bot are only discoverable by reading Heroku's log
stream. Nobody does that routinely, so failures go unnoticed — most notably the
19–20 Aug 2026 onboarding failures, where Google 503s discarded a verified
member's form data with no visible signal.

The signal already exists: the codebase makes **179 `print()` calls**, of which
**38 are `[ERROR]`** and **62 are `[WARN]`**. The `logging` module is not used
anywhere (0 usages). The problem is delivery, not instrumentation.

## Goal

Any `[ERROR]` or `[WARN]` line printed anywhere in the bot arrives as a Telegram
DM to the maintainer alone — never to the group, never fanned out to other
committee members.

## Non-goals

- Replacing Heroku logs. They remain complete and unchanged.
- Migrating 179 call sites to the `logging` module.
- Changing the existing MAIN-admin alert DMs (join-request notices in
  `verification_handlers.py:161`), which are operational and must stay fanned out.
- Publishing a pip package. Deferred until a third consumer exists.

## Approach: tee `stdout`/`stderr`

At startup, replace `sys.stdout` / `sys.stderr` with a passthrough wrapper that:

1. writes every line to the real stream (Heroku output byte-identical to today), and
2. if the line starts with a configured level prefix, enqueues an alert.

**Rejected alternatives:**

- *Migrate to `logging` + custom Handler.* Idiomatic, but a 179-site diff across
  every file for no additional alerting capability over the tee. Unacceptable
  regression risk in a bot running live member onboarding.
- *Explicit `alert()` calls at chosen sites.* Precise, but only fires where
  someone remembered to add a call — reproducing the exact blind spot that caused
  the 19–20 Aug incident.

An `alert()` helper is still provided for future high-context call sites.

## Architecture

One new file, one changed line.

```
services/alerts.py   ← new, self-contained, no project imports
main.py              ← +1 line in on_startup
```

`services/alerts.py` deliberately imports nothing from this project and reads its
own `os.environ`. This is what makes it copy-pasteable into bot #2 unchanged.
Env vars are NOT added to `config.py`, which would re-couple it.

Components:

- **`AlertTee`** — stream wrapper; matches level prefixes, enqueues, never blocks.
- **Worker thread** — owns the queue, applies throttling, POSTs to Telegram via
  `requests`. Deliberately NOT the PTB `Bot` object: no dependency on the bot's
  event loop, so it still delivers if PTB is wedged, and it ports cleanly.
- **`install_alerts()`** — called once at startup; swaps streams, starts worker.
- **`alert(msg)`** — explicit helper for richer future payloads.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ALERT_BOT_TOKEN` | falls back to `BOT_TOKEN` | sender identity |
| `ALERT_CHAT_ID` | *(required)* | maintainer's Telegram user id |
| `ALERT_LEVELS` | `ERROR,WARN` | prefixes that trigger an alert |
| `ALERT_SOURCE` | `NTUFD` | label identifying which bot is alerting |

If `ALERT_CHAT_ID` is missing, `install_alerts()` prints one line and no-ops, so
local runs and fresh checkouts behave exactly as today.

Shipping with `ALERT_BOT_TOKEN` unset means **nothing new to create or deploy**.
Sending via the main token does not conflict with PTB polling — Telegram only
rejects duplicate `getUpdates`, not concurrent `sendMessage`. Switching to a
separate alert bot later is a config-var change with **no code change**.

A separate alert bot requires **no additional dyno** and therefore no additional
Heroku cost: it never polls, so no process runs for it.

## Queue and throttle

Flow: `print()` → prefix match → `queue.put()` (non-blocking) → worker → throttle
→ POST.

Queue is **bounded (1000)**. When full, new items are dropped rather than
blocking — freezing a handler mid-onboarding is worse than losing a diagnostic.

Three throttle layers:

1. **Signature dedupe.** Strip digits/ids/timestamps to form a signature.
   Repeats within a 15-min window increment a counter instead of sending. On
   window close, one `↳ N more` line. A 503 storm across 200 members becomes one
   DM plus a count.
2. **Global rate cap.** Max 20 DMs/hour. On hit, one "muted, N suppressed"
   message, then silence until the hour rolls.
3. **Coalescing window.** ~3s delay so a burst from one failed operation arrives
   grouped rather than as five pings.

**Known tradeoff:** dedupe can mask a second, distinct problem sharing a
signature with a noisy first. Mitigation is the `↳ N more` counter — an
implausibly high count is the cue to read the full Heroku logs.

**Recursion guard (mandatory).** A thread-local flag is set while the worker
sends. Anything printed inside that window bypasses the tee, so a failing alert
can never trigger another alert.

## Message format

First line must be readable from a lock-screen notification: level, source, time.
Message body starts on line two. Telegram HTML parse mode. Truncated to 4096
chars with a marker.

```
🟠 WARN · NTUFD · 14:32
[WARN] Skipped row 47 in MEMBER INFO — no Tele ID
```

```
🔴 ERROR · NTUFD · 09:14
[ERROR] Unhandled exception while processing an update: BadRequest: Message is not modified

attendance_handlers.py:312 in _update_attendance_bubble
telegram/_bot.py:2847 in edit_message_text

👤 user 1505249420 · 🧩 ATTD_PAGE|2
```

```
🔴 ERROR · NTUFD · 09:14
[ERROR] Google Sheets read failed: 503 Service Unavailable
↳ 47 more in the last 15 min
```

```
🔇 NTUFD muted — 20 alerts sent this hour, 312 suppressed.
Resuming at 10:00. Check Heroku logs for the full picture.
```

Decisions: last **three** traceback frames only (the top of a PTB traceback is
always library plumbing). User id and callback data are pulled from the `update`
object when available — they turn "something broke" into "I can reproduce it".
Colour-coded circles over text prefixes, for at-a-glance triage in a scrolling
thread. Source label present from day one so bot #2 does not require a retrofit.
Time is `HH:MM` SGT only; Telegram already stamps the message.

## Failure handling

The alerter must never break the bot.

| Failure | Behaviour |
|---|---|
| Telegram 5xx / unreachable | few retries with backoff, then drop and continue |
| `429 RetryAfter` | honour stated wait, one retry (mirrors `_paced_send`) |
| `401` / `403` (bad token, never pressed Start) | permanent — print one line to real stdout, disable for process lifetime |
| Unexpected worker exception | caught, printed to real stdout, worker survives |
| Missing config | `install_alerts()` no-ops with one printed line |

**Setup gotcha:** Telegram forbids a bot from DMing a user who never messaged it.
Not an issue while `ALERT_BOT_TOKEN` is unset. On switching to a separate alert
bot, the maintainer **must press Start on it once** or every alert 403s.
Documented in the module docstring.

## Testing

`python -m services.alerts` self-test: sends one alert and exits, so the token
and chat id can be verified **locally** before any deploy.

## Rollback

Delete the one line in `on_startup`. The tee is the only integration point;
everything else in the codebase is untouched.
