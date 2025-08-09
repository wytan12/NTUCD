import threading
import asyncio
from datetime import datetime, date, timedelta, time
import pytz
from telegram import Update
from telegram.ext import ContextTypes

from config.settings import CHAT_ID, TOPIC_VOTING_ID
from utils.decorators import admin_only
from utils.tools import get_next_tuesday, get_next_monday_8pm

# Singapore timezone
sg_tz = pytz.timezone("Asia/Singapore")

# Global poll tracking
active_polls = {}  # poll_id -> type
yes_voters = set()
interest_votes = {}  # poll_id -> {user_id: [option_indices]}

class PollHandlers:

    @staticmethod
    @admin_only
    async def send_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create attendance poll in voting topic"""
        print("[DEBUG] send_poll_handler triggered")
        
        next_tuesday = get_next_tuesday()
        now = datetime.now(sg_tz)
        reminder_time = get_next_monday_8pm(now)
        delay_sec = (reminder_time - now).total_seconds()

        global yes_voters
        chat_id = update.effective_chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", None)

        # Must be in voting topic
        if thread_id != TOPIC_VOTING_ID:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            except Exception as e:
                print(f"[ERROR] Failed to delete message in wrong thread: {e}")
            return

        # Delete the command message
        try:
            await context.bot.delete_message(chat_id, update.message.message_id)
        except:
            pass

        # Send the training poll
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Are you joining the training on {next_tuesday.strftime('%B %d, %Y')}?",
            options=["Yes", "No"],
            is_anonymous=False,
            message_thread_id=thread_id
        )

        # Register the poll type
        active_polls[msg.poll.id] = "training"
        yes_voters.clear()  # Reset voters for the new training poll

        # Schedule reminder
        threading.Timer(
            delay_sec, 
            lambda: asyncio.run(PollHandlers._send_reminder(context.bot, chat_id, thread_id))
        ).start()

    @staticmethod
    async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle poll answer responses"""
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

    @staticmethod
    async def send_interest_poll(bot, chat_id, thread_id, sheet):
        """Send interest poll for performance dates"""
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

            # Check STATUS before sending poll
            from models.constants import SHEET_COLUMNS
            status_col_index = SHEET_COLUMNS.index("STATUS")
            if len(matched_row) > status_col_index and matched_row[status_col_index].strip():
                print(f"[SKIPPED] Interest poll not sent. STATUS is {matched_row[status_col_index]}")
                return None

            # Extract date string from column 3 (index 2)
            raw_date_str = matched_row[2].strip() if len(matched_row) > 2 else ''
            if not raw_date_str:
                print(f"[WARN] No date data for thread_id {thread_id}")
                return None

            # Split by comma and clean
            import re
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
            
            # Register poll as 'interest'
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

    # Private helper methods
    @staticmethod
    async def _send_reminder(bot, chat_id, thread_id):
        """Send training reminder"""
        next_tuesday = get_next_tuesday()
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"Reminder: There's training tomorrow {next_tuesday.strftime('%B %d, %Y')}."
            )
        except Exception as e:
            print(f"[ERROR] Failed to send reminder: {e}")