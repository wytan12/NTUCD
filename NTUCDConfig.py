import csv
import json
import gspread
from io import StringIO
from telegram import Update, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters, PollAnswerHandler,
)
from oauth2client.service_account import ServiceAccountCredentials
import os
from datetime import date, timedelta, datetime, time
import asyncio
import threading
from telegram.error import BadRequest
import pytz
from functools import wraps

sg_tz = pytz.timezone("Asia/Singapore") 

# === CONFIGURATION ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = "NTUCD AY25/26 Timeline"
SHEET_TAB_NAME = "PERFORMANCE List"

# === Conversation states ===
DATE, EVENT, LOCATION = range(3)
MODIFY_FIELD, MODIFY_VALUE = range(3, 5) 

# Add column names used in your Google Sheet
SHEET_COLUMNS = ["THREAD ID", "EVENT", "DATE", "LOCATION", "PERFORMANCE INFO"]

# Thread access configuration
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 4
TOPIC_MEDIA_IDS = [25, 75]
TOPIC_BLOCKED_ID = 5 # score

# Add exempt thread IDs here: # threadid 11 is for chat
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS

# Track topic initialization
initialized_topics = set()
pending_questions = {}
# Track all active polls: poll_id -> type
active_polls = {}  # Example: {"123456789": "training", "987654321": "interest"}
# For each type of poll, track answers if needed
yes_voters = set()
interest_votes = {}  # poll_id -> {user_id: [option_indices]}

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
            except Exception as e:
                print(f"[DELETE ERROR] {e}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# === START ===
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):    
    chat = update.effective_chat
    thread_id = getattr(update.effective_message, "message_thread_id", "N/A")
    await update.message.reply_text("👋 Hi!")
    print(f"[DEBUG] Chat ID: {chat.id}, Thread ID: {thread_id}")
    
# === TIME HELPERS ===
def get_next_tuesday(today=None):
    if today is None:
        today = date.today()
    days_ahead = (1 - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)

def get_next_monday_8pm(now=None):
    if now is None:
        now = datetime.now(sg_tz)
    days_until_monday = (7 - now.weekday()) % 7
    next_monday = now + timedelta(days=days_until_monday)
    naive_dt = datetime.combine(next_monday, time(22, 0))
    this_monday_8pm = sg_tz.localize(naive_dt)
    if now >= this_monday_8pm:
        return this_monday_8pm + timedelta(days=7)
    return this_monday_8pm

# === REMINDER ===
async def send_reminder(bot, chat_id, thread_id):
    next_tuesday = get_next_tuesday()
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"Reminder: There's training tomorrow {next_tuesday.strftime('%B %d, %Y')}."
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")
        
# === POLL SEND ===
@admin_only
async def send_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    next_tuesday = get_next_tuesday()
    now = datetime.now(sg_tz)
    reminder_time = get_next_monday_8pm(now)
    delay_sec = (reminder_time - now).total_seconds()

    global yes_voters
    chat_id = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    if thread_id != TOPIC_VOTING_ID:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception as e:
            print(f"[ERROR] Failed to delete message in wrong thread: {e}")
        return

    if not await is_admin(update, context):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception as e:
            print(f"[DELETE ERROR] {e}")
        return

    try:
        await context.bot.delete_message(chat_id, update.message.message_id)
    except:
        pass

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Are you joining the training on {next_tuesday.strftime('%B %d, %Y')}?",
        options=["Yes", "No"],
        is_anonymous=False,
        message_thread_id=thread_id
    )

    # ✅ Register the poll type
    active_polls[msg.poll.id] = "training"
    yes_voters.clear()  # reset voters for the new training poll

    # Schedule reminder
    threading.Timer(delay_sec, lambda: asyncio.run(send_reminder(context.bot, chat_id, thread_id))).start()

# === POLL ANSWER ===
# async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     global active_poll_id, yes_voters
#     poll_id = update.poll_answer.poll_id
#     user = update.poll_answer.user
#     selected_options = update.poll_answer.option_ids

#     if poll_id != active_poll_id:
#         return

#     if 0 in selected_options and user.id not in yes_voters:
#         yes_voters.add(user.id)
#         print(f"[DEBUG] {user.full_name} voted YES")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# === Google Sheets Setup ===
def get_gspread_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    # creds_dict = GOOGLE_CREDENTIALS_JSON
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).worksheet(SHEET_TAB_NAME)

# === Admin check ===
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]

# === Restriction handler ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    thread_id = msg.message_thread_id
    user_is_admin = await is_admin(update, context)

    # === EXEMPT specific thread IDs from auto prompt ===
    if thread_id in EXEMPTED_THREAD_IDS:
        # No prompt, just proceed with restrictions below
        pass
    else:
        # === AUTO INITIATION WHEN NEW TOPIC STARTS ===
        if msg.is_topic_message and thread_id not in initialized_topics:
            initialized_topics.add(thread_id)
            if user_is_admin:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("PERF", callback_data=f"topic_type|PERF|{thread_id}")],
                    [InlineKeyboardButton("EVENT", callback_data=f"topic_type|EVENT|{thread_id}")],
                    [InlineKeyboardButton("OTHERS", callback_data=f"topic_type|OTHERS|{thread_id}")]
                ])
                prompt = await update.effective_chat.send_message(
                    "What's the topic for? PERF, EVENT, or OTHERS?",
                    reply_markup=keyboard,
                    message_thread_id=thread_id
                )
                context.chat_data[f"init_prompt_{thread_id}"] = prompt.message_id
            await msg.delete()
            return

    # === THREAD MESSAGE RESTRICTIONS ===
    # restrict all actions except for admin users
    if not user_is_admin:
        if msg.text.startswith("/"):
            print(f"[DEBUG] User {update.effective_user.id} tried to send command in exempted thread {thread_id}. Deleting message.")
            await msg.delete()
        if thread_id is None: # AboutNTUCD
            await msg.delete()
        elif thread_id == TOPIC_VOTING_ID:
            if msg.poll or not (msg.reply_to_message and msg.reply_to_message.poll):
                await msg.delete()
        elif thread_id in TOPIC_MEDIA_IDS:
            if not (msg.photo or msg.video or (msg.document and msg.document.mime_type.startswith(("image/", "video/")))):
                await msg.delete()
        elif thread_id == TOPIC_BLOCKED_ID:
            print(f"[DEBUG] User {update.effective_user.id} tried to send message in blocked thread {thread_id}. Deleting message.")
            await msg.delete()

async def send_interest_poll(bot, chat_id, thread_id, sheet):
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

        # Extract date string from column 3 (index 2)
        raw_date_str = matched_row[2].strip() if len(matched_row) > 1 else ''
        if not raw_date_str:
            print(f"[WARN] No date data for thread_id {thread_id}")
            return None

        # Split by comma and clean
        dates = [d.strip() for d in raw_date_str.split(',') if d.strip()]

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
        interest_votes[msg.poll.id] = {}  # Initialize vote tracking

        print(f"[DEBUG] Sent interest poll for thread {thread_id}")
        return msg.poll.id

    except Exception as e:
        print(f"[ERROR] Failed to send interest poll: {e}")
        return None

# === Handle PERF/EVENT/OTHERS selection ===
async def topic_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, selection, thread_id = query.data.split("|")
    thread_id = int(thread_id)

    # Delete the initial prompt if it exists
    prompt_id = context.chat_data.pop(f"init_prompt_{thread_id}", None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=prompt_id)
        except Exception as e:
            print(f"[DELETE ERROR] {e}")

    context.user_data["thread_id"] = thread_id
    context.user_data["topic_type"] = selection

    if selection == "PERF":
        prompt = await query.message.chat.send_message(
            "\U0001F4DD Please enter: *Event // Date // Location // Info (If any)*",
            parse_mode="Markdown", message_thread_id=thread_id
        )
        pending_questions["perf_input"] = prompt.message_id
        return DATE

    elif selection == "DATE_ONLY":
        prompt = await query.message.chat.send_message(
            "\U0001F4C5 Please enter the *Performance Date* (e.g. 12 MAR 2025):",
            parse_mode="Markdown", message_thread_id=thread_id
        )
        pending_questions["date"] = prompt.message_id
        return DATE

    await query.message.edit_text(f"Topic marked as {selection}. No further action.")
    return ConversationHandler.END

# === Dates Format ===
def parse_and_format_dates(dates_str):
    accepted_formats = [
        "%d %b %Y", "%d %B %Y",     # 23 Jun 2025, 23 June 2025
        "%d%b%Y", "%d%B%Y",         # 23Jun2025, 23June2025
        "%d %b,%Y", "%d%b,%Y",      # 23 Jun,2025
        "%d %b", "%d%b",            # 23 Jun, 23Jun (assume current year)
    ]
    result = []
    for part in dates_str.split(","):
        part = part.strip()
        parsed = None
        for fmt in accepted_formats:
            try:
                parsed = datetime.strptime(part, fmt)
                # If year is missing, assume current year
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=datetime.now().year)
                break
            except ValueError:
                continue
        if not parsed:
            raise ValueError(f"Invalid date format: {part}")
        result.append(parsed.strftime("%d %b %Y").upper())  # e.g., 23 JUN 2025
        print(f"[DEBUG] Parsed date: {result[-1]} from input: {part}")
    return result
# === Conversation steps ===
def are_valid_dates(dates_str):
    try:
        parse_and_format_dates(dates_str)
        return True
    except ValueError:
        return False

# === Conversation steps ===
async def parse_perf_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    sheet = get_gspread_sheet()

    try:
        if "perf_input" in pending_questions:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=pending_questions.pop("perf_input")
            )
    except Exception as e:
        print(f"[WARNING] Failed to delete perf input prompt: {e}")
    
    # Delete any previously shown error message from the bot
    try:
        if "last_error" in context.chat_data:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.chat_data.pop("last_error")
            )
    except Exception as e:
        print(f"[WARNING] Failed to delete previous error message: {e}")

    # === Case 1: Retrying Date Only ===
    if "perf_temp" in context.user_data:
        date = update.message.text.strip()
        try:
            formatted_dates = parse_and_format_dates(date)
            date = ", ".join(formatted_dates)
        except ValueError:
            error_msg = await update.effective_chat.send_message(
                "❌ Invalid *date* format. Use:\n`DD MMM YYYY` (e.g. `23 JUN 2025`)",
                parse_mode="Markdown",
                message_thread_id=context.user_data["perf_temp"]["thread_id"]
            )
            context.chat_data["last_error"] = error_msg.message_id
            return DATE

        # Restore previous info
        temp = context.user_data.pop("perf_temp")
        event = temp["event"]
        location = temp["location"]
        info = temp["info"]
        thread_id = temp["thread_id"]

        # Update sheet
        all_rows = sheet.get_all_values()
        for idx, row in enumerate(all_rows, start=1):
            if row and str(row[0]) == str(thread_id):
                sheet.update(values=[[event, date, location, info]], range_name=f"B{idx}:E{idx}")
                break

    # === Case 2: Full Input ===
    else:
        raw_text = update.message.text.strip()
        parts = [p.strip() for p in raw_text.split("//")]
        print(f"[DEBUG] Parsed parts: {len(parts)} - {parts}")

        if len(parts) < 3 or any(not p for p in parts[:3]):
            error_msg = await update.effective_chat.send_message(
                "❌ Invalid format. Use:\n*Event // Date // Location // Info (optional)*",
                parse_mode="Markdown",
                message_thread_id=context.user_data.get("thread_id")
            )
            context.chat_data["last_error"] = error_msg.message_id
            return DATE

        event, date, location = parts[0], parts[1], parts[2]
        info = parts[3] if len(parts) >= 4 else ""
        thread_id = context.user_data.get("thread_id")

        if not are_valid_dates(date):
            try:
                sheet.append_row([thread_id, event, date, location, info])
                print("[DEBUG] Row appended successfully")
            except Exception as e:
                print(f"[ERROR] Failed to append row: {e}")
            print(f"[DEBUG] Temporarily saved invalid date row for thread {thread_id}")

            context.user_data["perf_temp"] = {
                "event": event,
                "location": location,
                "info": info,
                "thread_id": thread_id
            }
 
            error_msg = await update.effective_chat.send_message(
                "❌ Invalid *date* format. Use:\n`DD MMM YYYY` (e.g. `23 JUN 2025`)",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
            context.chat_data["last_error"] = error_msg.message_id
            return DATE

        try:
            sheet.append_row([thread_id, event, date, location, info])
            print("[DEBUG] Row appended successfully")
        except Exception as e:
            print(f"[ERROR] Failed to append row: {e}")

    # ✅ Send performance summary and interest poll
    template = (
        f"\U0001F4E2 *Performance Summary*\n\n"
        f"\U0001F4CD *Event:* {event}\n"
        f"\U0001F4C5 *Date:* {date}\n"
        f"\U0001F4CC *Location:* {location}\n"
        f"\n{info.strip()}"
    )
    msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
    await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
    # Send interest poll (single/multi-choice)
    await send_interest_poll(context.bot, update.effective_chat.id, thread_id, sheet)
    # context.chat_data[f"interest_poll_msg_{thread_id}"] = poll_id

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("[DEBUG] Cancel triggered")
    chat = update.effective_chat

    # Clean up bot prompt messages
    try:
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                await chat.delete_message(msg_id)
            context.chat_data.pop("modify_prompt_msg_ids")
    except Exception as e:
        print(f"[WARNING] Failed to delete prompt messages on cancel: {e}")

    # Try to delete the command message issued by user (e.g. /modify or text input)
    try:
        await update.message.delete()
    except:
        pass

    # Delete previously pinned performance summary if any
    # try:
    #     pinned_msg = await chat.get_pinned_message()
    #     if pinned_msg:
    #         await pinned_msg.delete()
    # except Exception as e:
    #     print(f"[WARNING] Failed to delete pinned message on cancel: {e}")

    # await chat.send_message("❌ Modification cancelled.", message_thread_id=update.message.message_thread_id)
    return ConversationHandler.END

# === /threadid ===
@admin_only
async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    msg = update.effective_message
    if msg.is_topic_message:
        await msg.reply_text(f"\U0001F9F5 This topic's thread ID is: {msg.message_thread_id}", parse_mode="Markdown")
    else:
        await msg.reply_text("\u2757 This command must be used *inside a topic*.", parse_mode="Markdown")
    try:
        await update.message.delete()  # delete the command sent by user
    except Exception as e:
        print(f"[DEBUG] Failed to delete /threadid command: {e}")

# === /remind command ===
@admin_only
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = update.effective_message
    if not msg.is_topic_message:
        return
    thread_id = msg.message_thread_id
    if thread_id in EXEMPTED_THREAD_IDS or not await is_admin(update, context):
        return
    
    try:
        await update.message.delete()  # delete the command sent by user
    except Exception as e:
        print(f"[DEBUG] Failed to delete /remind command: {e}")

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    for row in records:
        if str(row['THREAD ID']) == str(thread_id):
            template = (
                f"\U0001F4E2 *Performance Reminder*\n\n"
                f"\U0001F4CD *Event:* {row['EVENT']}\n"
                f"\U0001F4C5 *Date:* {row['DATE']}\n"
                f"\U0001F4CC *Location:* {row['LOCATION']}\n\n"
                f"Please be punctual and check the group for updates."
            )
            await context.bot.send_message(
                chat_id=msg.chat.id,
                text=template,
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
            return

    await context.bot.send_message(
        chat_id=msg.chat.id,
        text="\u274C This thread has not been registered.",
        parse_mode="Markdown",
        message_thread_id=thread_id
    )
    
# Start modify process
async def start_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # try:
    #     await update.message.delete()  # delete the command sent by user
    # except Exception as e:
    #     print(f"[DEBUG] Failed to delete /modify command: {e}")
    msg = update.effective_message
    if not msg.is_topic_message:
        return
    thread_id = msg.message_thread_id
    if thread_id in EXEMPTED_THREAD_IDS or not await is_admin(update, context) :
        return
 
    try:
        await update.message.delete()  # delete the command sent by user
    except Exception as e:
        print(f"[DEBUG] Failed to delete /modify command: {e}")

    print(f"[DEBUG] /modify triggered by user {update.effective_user.id} in chat {msg.chat_id}, thread {thread_id}")

    if thread_id is None:
        return await msg.reply_text("⛔ This command must be used inside a topic thread.")

    context.user_data["modify_thread_id"] = thread_id
    keyboard = [
        [InlineKeyboardButton("📍 Event", callback_data="MODIFY|EVENT")],
        [InlineKeyboardButton("📅 Date", callback_data="MODIFY|DATE")],
        [InlineKeyboardButton("📌 Location", callback_data="MODIFY|LOCATION")],
        [InlineKeyboardButton("📝 Performance Info", callback_data="MODIFY|PERFORMANCE INFO")],
        [InlineKeyboardButton("❌ Cancel", callback_data="MODIFY|CANCEL")]
    ]
    prompt = await msg.chat.send_message(
        "✏️ What would you like to update?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_FIELD

# Ask what field to modify
async def get_modify_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, field = query.data.split("|", 1)
    context.user_data["modify_field"] = field

    # Clean up previous button prompt
    try:
        await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
    except:
        pass

    if field == "CANCEL":
        print("[DEBUG] User selected cancel button")
        return ConversationHandler.END
    context.user_data["modify_field"] = field
    prompt = await query.message.chat.send_message(
        f"✅ Got it! What is the new value for *{field}*?",
        parse_mode="Markdown",
        message_thread_id=context.user_data["modify_thread_id"]
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE

# Apply the new value to the sheet
async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    value = update.message.text.strip()
    if value.lower() == "/cancel":
        try:
            await update.message.delete()
        except:
            pass
        return ConversationHandler.END

    field = context.user_data["modify_field"]
    thread_id = context.user_data["modify_thread_id"]
    print(f"[DEBUG] Applying new value: {value} to field: {field} for thread ID: {thread_id}")
    
    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2  # Header row + 1-based index
            try:
                await update.message.delete()
            except:
                pass
            try:
                col_index = SHEET_COLUMNS.index(field) + 1
                print(f"[DEBUG] Updating sheet at row {row_number}, column {col_index} with value: {value}")
                sheet.update_cell(row_number, col_index, value)
                print(f"[DEBUG] Sheet updated at row {row_number}, column {col_index}")

                # Clean up prompt messages
                try:
                    if "modify_prompt_msg_ids" in context.chat_data:
                        for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                            try:
                                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                            except BadRequest as e:
                                print(f"[INFO] Prompt message {msg_id} already deleted or not found: {e}")
                        context.chat_data.pop("modify_prompt_msg_ids")
                except Exception as e:
                    print(f"[WARNING] Unexpected failure when deleting prompt messages: {e}")

                # Delete previous summary message if tracked
                prev_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
                if prev_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
                        print(f"[DEBUG] Deleted previous summary message: {prev_msg_id}")
                    except Exception as e:
                        print(f"[WARNING] Could not delete previous summary: {e}")

                # Prepare updated performance message
                updated_row = sheet.row_values(row_number)
                template = (
                    f"\U0001F4E2 *Performance Summary*\n\n"
                    f"\U0001F4CD *Event:* {updated_row[1]}\n"
                    f"\U0001F4C5 *Date:* {updated_row[2]}\n"
                    f"\U0001F4CC *Location:* {updated_row[3]}\n\n"
                    f"\n{updated_row[4]}"
                )

                # Send and pin new summary
                msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
                context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id  # Track new summary message

                await context.bot.pin_chat_message(
                    chat_id=update.effective_chat.id,
                    message_id=msg.message_id,  
                    disable_notification=True
                )

                print(f"[DEBUG] Pinned updated summary for thread {thread_id}")
                return ConversationHandler.END

            except Exception as e:
                print(f"[ERROR] Failed to update sheet: {e}")
                return await update.message.reply_text(f"\u274C Failed to update: `{e}`", parse_mode="Markdown")

    print("[DEBUG] Thread ID not found in sheet")
    return await update.message.reply_text("\u274C This thread is not registered in the sheet.")

# === Setup Bot ===
def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|")],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_perf_input)],
        },
        fallbacks=[],  # 🔧 This is required
    )
    
    # Add the new ConversationHandler
    modify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("modify", start_modify)],
        states={
            MODIFY_FIELD: [CallbackQueryHandler(get_modify_field_callback, pattern="^MODIFY\\|")],
            MODIFY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_modify_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("threadid", thread_id_command))
    app.add_handler(CommandHandler("remind", remind_command))  
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|"))
    app.add_handler(modify_conv_handler)
    app.add_handler(CommandHandler("poll", send_poll_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise