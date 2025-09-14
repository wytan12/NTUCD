from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import admin_only, is_admin
from utils.helpers import get_next_tuesday, get_next_monday_8pm
from utils.constants import active_polls, yes_voters, interest_votes
from services.google_sheets import append_poll, get_gspread_sheet
from config import sg_tz, TOPIC_VOTING_ID, SHEET_COLUMNS
from datetime import datetime
import threading
import asyncio
import re

@admin_only
async def send_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /poll command"""
    from handlers.admin_handlers import send_reminder
    
    print("[DEBUG] send_poll_handler triggered")
    next_tuesday = get_next_tuesday()
    now = datetime.now(sg_tz)
    reminder_time = get_next_monday_8pm(now)
    delay_sec = (reminder_time - now).total_seconds()

    global yes_voters
    chat_id = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    if thread_id != TOPIC_VOTING_ID:
        try:
            await context.bot.delete_message(
                chat_id=chat_id, 
                message_id=update.message.message_id
            )
        except Exception as e:
            print(f"[ERROR] Failed to delete message in wrong thread: {e}")
        return

    if not await is_admin(update, context):
        try:
            await context.bot.delete_message(
                chat_id=chat_id, 
                message_id=update.message.message_id
            )
        except Exception as e:
            print(f"[DELETE ERROR] {e}")
        return

    try:
        await context.bot.delete_message(chat_id, update.message.message_id)
    except:
        pass
    
    tues_date = next_tuesday.strftime('%B %d, %Y')

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Are you joining the training on {tues_date}?",
        options=["Yes", "No"],
        is_anonymous=False,
        message_thread_id=thread_id
    )

    active_polls[msg.poll.id] = "training"
    yes_voters.clear()

    append_poll({
        "poll_id": msg.poll.id,
        "message_id": msg.message_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "date": tues_date
    })
    
    threading.Timer(
        delay_sec, 
        lambda: asyncio.run(send_reminder(context.bot, chat_id, thread_id))
    ).start()

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answers"""
    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    selected_options = update.poll_answer.option_ids

    poll_type = active_polls.get(poll_id)

    if poll_type == "training":
        if 0 in selected_options and user.id not in yes_voters:
            yes_voters.add(user.id)
            print(f"[DEBUG] {user.full_name} voted YES for training")
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

        # Extract date string from column 3 (index 2)
        raw_date_str = matched_row[2].strip() if len(matched_row) > 1 else ''
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