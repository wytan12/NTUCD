from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import admin_only, is_admin
from utils.constants import active_polls, yes_voters, interest_votes
from services.google_sheets import (
    set_attendance, is_training_poll_id, get_gspread_sheet
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
        present = 0 in selected_options  # option 0 = "Yes"
        try:
            ok, info = set_attendance(poll_id, user.id, present)
            if ok:
                print(f"[ATTD] {info}: {'present' if present else 'absent'} (poll {poll_id})")
            else:
                print(f"[ATTD][WARN] {info}")
        except Exception as e:
            print(f"[ATTD][ERROR] Failed to record attendance: {e}")

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


async def auto_poll_check(context: ContextTypes.DEFAULT_TYPE):
    """Daily job (runs 20:00 SGT). Sends a training poll for any date defined in
    the Attendance sheet that is exactly 2 days away and not yet polled.

    For Tuesday training this fires the poll on Sunday 8pm. The poll id is written
    into that date's column (row 1), and a reminder is scheduled for 10pm the day
    before training.
    """
    from datetime import datetime, timedelta, time as dtime
    from config import (CHAT_ID, ATT_FIRST_DATE_COL, ATT_POLL_ROW, ATT_DATE_ROW)
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
        if d - today != timedelta(days=2) or already_polled:
            continue

        try:
            msg = await context.bot.send_poll(
                chat_id=CHAT_ID,
                question=f"Are you joining the training on {date_str}?",
                options=["Yes", "No"],
                is_anonymous=False,
                message_thread_id=TOPIC_VOTING_ID,
            )
            active_polls[msg.poll.id] = "training"
            yes_voters.clear()
            ws.update_cell(ATT_POLL_ROW, col, str(msg.poll.id))
            print(f"[AUTO-POLL] Sent poll for {date_str} (col {col}), id {msg.poll.id}")

            from handlers.admin_handlers import send_reminder
            now = datetime.now(sg_tz)
            reminder_dt = sg_tz.localize(datetime.combine(d - timedelta(days=1), dtime(22, 0)))
            delay = (reminder_dt - now).total_seconds()
            if delay > 0:
                threading.Timer(
                    delay,
                    lambda: asyncio.run(send_reminder(context.bot, CHAT_ID, TOPIC_VOTING_ID))
                ).start()
        except Exception as e:
            print(f"[AUTO-POLL][ERROR] Failed to send poll for {date_str}: {e}")