from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import admin_only, is_admin
from utils.constants import active_polls, yes_voters, interest_votes
from services.google_sheets import (
    set_attendance, is_training_poll_id, get_gspread_sheet, invalidate_sheet_cache,
    format_training_poll_ref,
)
from config import sg_tz, TOPIC_VOTING_ID, SHEET_COLUMNS
from datetime import datetime
import threading
import asyncio
import re

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answers"""
    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    selected_options = update.poll_answer.option_ids

    poll_type = active_polls.get(poll_id)

    # Survive restarts: if the poll id is recorded in the attendance sheet,
    # treat it as a training poll even though active_polls was cleared.
    if poll_type is None and is_training_poll_id(poll_id):
        poll_type = "training"
        active_polls[poll_id] = "training"

    if poll_type == "training":
        # set_attendance maps the picked options onto every date column the
        # poll covers: option 0 = "yes" on a single-date poll, one option per
        # date on a multi-date poll (the index is stored in each column's ref).
        try:
            ok, info = set_attendance(poll_id, user.id, selected_options)
            if ok:
                print(f"[ATTD] {info} (poll {poll_id}, options {list(selected_options)})")
            else:
                print(f"[ATTD][WARN] {info}")
        except Exception as e:
            print(f"[ATTD][ERROR] Failed to record attendance: {e}")

        present = 0 in selected_options
        if present:
            yes_voters.add(user.id)
        else:
            yes_voters.discard(user.id)
    elif poll_type == "interest":
        interest_votes[poll_id][user.id] = selected_options
        print(f"[DEBUG] {user.full_name} voted for interest poll: {selected_options}")
    else:
        print(f"[WARN] Received answer for unknown poll ID {poll_id}")
        
async def send_interest_poll(bot, chat_id, thread_id, sheet):
    """Send interest poll for a performance"""
    global active_polls, interest_votes
    try:
        # Get all rows in the sheet
        all_rows = sheet.get_all_values()

        # Find the row where column 1 matches the thread_id
        matched_row = None
        for row in all_rows:
            if row and str(row[0]).strip() == str(thread_id):
                matched_row = row
                break

        if not matched_row:
            print(f"[WARN] No matching row for thread_id {thread_id}")
            return None

        # ✅ Check STATUS before sending poll
        status_col_index = SHEET_COLUMNS.index("STATUS")
        if len(matched_row) > status_col_index and matched_row[status_col_index].strip():
            print(f"[SKIPPED] Interest poll not sent. STATUS is {matched_row[status_col_index]}")
            return None

        # Extract PERF DATE | TIME (index 4 in new schema)
        perf_date_col = SHEET_COLUMNS.index("PERF DATE | TIME")
        raw_date_str = matched_row[perf_date_col].strip() if len(matched_row) > perf_date_col else ''
        if not raw_date_str:
            print(f"[WARN] No date data for thread_id {thread_id}")
            return None

        # Split by comma and clean
        dates = [d.strip() for d in re.split(r'[\n,]', raw_date_str) if d.strip()]

        # Send poll based on number of dates
        if len(dates) == 1:
            msg = await bot.send_poll(
                chat_id=chat_id,
                message_thread_id=thread_id,
                question=f"Are you interested in the performance on {dates[0]}?",
                options=["Yes", "No"],
                is_anonymous=False
            )
        elif len(dates) > 1:
            msg = await bot.send_poll(
                chat_id=chat_id,
                message_thread_id=thread_id,
                question="Which dates are you interested in?",
                options=dates,
                allows_multiple_answers=True,
                is_anonymous=False
            )
        else:
            print(f"[WARN] No valid dates after parsing for thread_id {thread_id}")
            return None
        
         # ✅ Register poll as 'interest'
        active_polls[msg.poll.id] = "interest"
        interest_votes[msg.poll.id] = {}

        print(f"[DEBUG] Sent interest poll for thread {thread_id}")
        return {
            "message_id": msg.message_id,
            "poll_id": msg.poll.id
        }

    except Exception as e:
        print(f"[ERROR] Failed to send interest poll: {e}")
        return None


# Telegram allows at most 10 poll options; one is the "can't make any" escape.
MAX_POLL_DATES = 9
CANT_MAKE_ANY_OPTION = "❌ Can't make any"


async def post_training_poll(bot, ws, col: int, date_str: str, d):
    """Single-date wrapper over `post_training_polls` (used by auto_poll_check)."""
    return await post_training_polls(bot, ws, [(col, date_str, d)])


async def post_training_polls(bot, ws, items):
    """Post ONE training poll covering `items` = [(col, date_str, date), ...],
    store its ref in row 1 of every covered column, and schedule a 10pm
    night-before reminder per date.

    - One date → the usual yes/no poll; option 0 must stay "yes".
    - Several → a multi-answer poll with one option per date (soonest first)
      plus a trailing "can't make any". Each column's ref carries its option
      index (`poll_id|message_id|n`), so a vote for 2 Oct and 8 Oct marks "1"
      in exactly those two columns.

    Once row 1 holds a ref, `auto_poll_check` treats the date as polled and
    never posts it again — so an early poll replaces the scheduled one.
    """
    from datetime import timedelta, time as dtime
    from gspread.utils import rowcol_to_a1
    from config import CHAT_ID, ATTENDANCE_TAB, ATT_POLL_ROW
    from handlers.admin_handlers import send_reminder

    items = sorted(items, key=lambda it: it[2])
    if not items:
        raise ValueError("no dates to poll")
    if len(items) > MAX_POLL_DATES:
        raise ValueError(f"a poll can cover at most {MAX_POLL_DATES} dates")

    if len(items) == 1:
        _col, date_str, _d = items[0]
        msg = await bot.send_poll(
            chat_id=CHAT_ID,
            question=f"🥁 Training on {date_str}, you in? 🔥",
            options=["✅ Let's drum!", "🙅 Not this time"],  # option 0 must stay = "yes"
            is_anonymous=False,
            message_thread_id=TOPIC_VOTING_ID,
        )
        refs = [(items[0][0], format_training_poll_ref(msg.poll.id, msg.message_id))]
    else:
        msg = await bot.send_poll(
            chat_id=CHAT_ID,
            question="🥁 Which training dates can we count you in for? Pick all that apply 🔥",
            options=[f"✅ {d:%a} {d.day} {d:%b}" for _c, _s, d in items] + [CANT_MAKE_ANY_OPTION],
            allows_multiple_answers=True,
            is_anonymous=False,
            message_thread_id=TOPIC_VOTING_ID,
        )
        refs = [(col, format_training_poll_ref(msg.poll.id, msg.message_id, option=i))
                for i, (col, _s, _d) in enumerate(items)]

    active_polls[msg.poll.id] = "training"
    yes_voters.clear()
    ws.batch_update(
        [{"range": rowcol_to_a1(ATT_POLL_ROW, col), "values": [[ref]]} for col, ref in refs],
        value_input_option="USER_ENTERED",
    )
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)
    labels = ", ".join(s for _c, s, _d in items)
    print(f"[POLL] Sent poll for {labels} (cols {[c for c, _r in refs]}), id {msg.poll.id}")

    now = datetime.now(sg_tz)
    for _col, date_str, d in items:
        reminder_dt = sg_tz.localize(datetime.combine(d - timedelta(days=1), dtime(22, 0)))
        delay = (reminder_dt - now).total_seconds()
        if delay > 0:
            pretty = d.strftime("%d %b %Y")
            threading.Timer(
                delay,
                lambda pretty=pretty: asyncio.run(send_reminder(bot, CHAT_ID, TOPIC_VOTING_ID, pretty))
            ).start()
    return msg


async def auto_poll_check(context: ContextTypes.DEFAULT_TYPE):
    """Daily job (runs 09:00 SGT). Sends a training poll for any date defined in
    the Attendance sheet that is **4 days away or sooner** (but not in the past)
    and not yet polled.

    For Tuesday training this normally fires the poll on the previous Friday 9am.
    Using "≤ 4 days" rather than "exactly 4 days" means a date that gets moved
    *inside* the 4-day window — e.g. the admin edits the sheet to bring a date
    from 7 days to 1 day away — is still caught on the next daily run instead of
    being skipped forever. The poll id is written into that date's column (row 1),
    and a reminder is scheduled for 10pm the day before training.

    A date an admin already polled early via "Send Poll Now" has a row-1 ref, so
    it counts as already polled and is skipped here.
    """
    from config import ATT_FIRST_DATE_COL, ATT_POLL_ROW, ATT_DATE_ROW
    from services.google_sheets import get_attendance_ws, parse_sheet_date

    try:
        ws = get_attendance_ws()
        row1 = ws.row_values(ATT_POLL_ROW)
        row2 = ws.row_values(ATT_DATE_ROW)
    except Exception as e:
        print(f"[AUTO-POLL][ERROR] Failed to read attendance sheet: {e}")
        return

    today = datetime.now(sg_tz).date()
    for col in range(ATT_FIRST_DATE_COL, len(row2) + 1):
        date_str = (row2[col - 1] or "").strip()
        if not date_str:
            continue
        d = parse_sheet_date(date_str)
        if not d:
            print(f"[AUTO-POLL][WARN] Could not parse date '{date_str}' at col {col}")
            continue

        already_polled = col - 1 < len(row1) and (row1[col - 1] or "").strip()
        # Poll when the date is 4 days away or sooner (down to today), but never
        # for past dates or dates already polled. "≤ 4 days" (instead of "== 4")
        # catches dates moved inside the window by a manual sheet edit.
        days_away = (d - today).days
        if already_polled or not (0 <= days_away <= 4):
            continue

        try:
            await post_training_poll(context.bot, ws, col, date_str, d)
        except Exception as e:
            print(f"[AUTO-POLL][ERROR] Failed to send poll for {date_str}: {e}")
