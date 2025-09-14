import csv, re
import json
import gspread
from io import StringIO
from telegram import Update, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, constants, BotCommandScopeDefault, BotCommandScopeChatAdministrators, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters, PollAnswerHandler, Application, ChatJoinRequestHandler, ChatMemberHandler
)
from oauth2client.service_account import ServiceAccountCredentials
import os
from datetime import date, timedelta, datetime, time
import asyncio
import threading
from telegram.error import BadRequest
import pytz
from functools import wraps
from telegram.constants import ParseMode

sg_tz = pytz.timezone("Asia/Singapore") 

# === CONFIGURATION ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = "NTUCD AY25/26 Timeline"
SHEET_TAB_NAME = "PERFORMANCE List"
ATTENDANCE_TAB = "ATTENDANCE List"
CHAT_ID = -1002590844000 # Main group Chat ID
# CHAT_ID = -1002614985856 # Debug group Chat ID

# State for conversation handler
ASK_MATRIC = 1234

# === Conversation states ===
DATE, EVENT, LOCATION = range(3)
MODIFY_FIELD, MODIFY_VALUE = range(3, 5) 

# Add column names used in your Google Sheet
SHEET_COLUMNS = ["THREAD ID", "EVENT", "PROPOSED DATE | TIME", "LOCATION", "PERFORMANCE INFO", "CONFIRMED DATE | TIME", "STATUS"]

# Thread access configuration, for main group
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 4
TOPIC_MEDIA_IDS = [25, 75]
TOPIC_BLOCKED_ID = 5 # score

# Thread access configuration, for debug group
# GENERAL_TOPIC_ID = None
# TOPIC_VOTING_ID = 5
# TOPIC_MEDIA_IDS = [6,7]
# TOPIC_ENHANCED_IDS = [10,11,13,18]
# TOPIC_BLOCKED_ID = 8 # score

# Add exempt thread IDs here: # threadid 11 is for chat (For Main Group)
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS
# Add exempt thread IDs here: # threadid 11 is for chat (For Debug Group)
# EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS + TOPIC_ENHANCED_IDS

# Track topic initialization
initialized_topics = set()
OTHERS_THREAD_IDS = set()
pending_questions = {}
# Track all active polls: poll_id -> type
active_polls = {}  # Example: {"123456789": "training", "987654321": "interest"}
# For each type of poll, track answers if needed
yes_voters = set()
interest_votes = {}  # poll_id -> {user_id: [option_indices]}
pending_users = {} # pending user join request

# === GUI COMMANDS ===
# Step 1: Define admin-only commands
admin_commands = [
    BotCommand("start", "Just to test the bot and grab chat ID"),
    BotCommand("threadid", "To obtain the thread ID of the current topic"),
    BotCommand("poll", "Create attendance poll in Attendance Topic"),
    BotCommand("modify", "Modify performance summary details"),
    BotCommand("remind", "Remind about performance"),
    BotCommand("confirmation", "Confirm performance details"),
]

# Step 2: Function to register them for admins only
async def set_admin_commands(application):
    await application.bot.set_my_commands(
        commands=admin_commands,
        scope=BotCommandScopeChatAdministrators(chat_id=CHAT_ID)  # Your group ID
    )

# Step 3: Optional - clear commands for regular users
async def clear_global_commands(application):
    await application.bot.set_my_commands([], scope=BotCommandScopeDefault())
    
# Step 4: Run both in startup
async def on_startup(application):
    await set_admin_commands(application)
    await clear_global_commands(application)

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

def get_attendance_ws():
    return get_gspread_sheet(ATTENDANCE_TAB)

def _find_or_create_header_rows(ws):
    """Ensure row 1 = 'Tele Poll ID', row 2 = 'Training Date'."""
    values = ws.get_all_values()
    # Make sure we have at least 2 rows
    if len(values) < 2:
        # Ensure at least 2 rows exist (append blanks if needed)
        need = 2 - len(values)
        for _ in range(need):
            ws.append_row([""])
        values = ws.get_all_values()

    # Set labels if missing
    if not values or not values[0] or (values[0][0] or "").strip() != "Tele Poll ID":
        ws.update_acell("A1", "Tele Poll ID")
    values = ws.get_all_values()  # refresh
    if len(values) < 2 or not values[1] or (values[1][0] or "").strip() != "Training Date":
        ws.update_acell("A2", "Training Date")

def _date_label_from_display(date_str: str) -> str:
    """
    Your send_poll stores date like 'September 16, 2025'.
    Attendance headers use short 'd/m' like '16/9'.
    Convert to 'd/m' (no leading zeros).
    """
    date_str = (date_str or "").strip()
    # Already looks like d/m? just return
    if "/" in date_str and date_str.count("/") == 1 and all(p.isdigit() for p in date_str.split("/")):
        return date_str

    # Try common formats
    for fmt in ("%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            d, m = dt.day, dt.month
            return f"{d}/{m}"
        except Exception:
            continue

    # Fallback: keep original (won't match headers unless identical)
    return date_str

def _get_existing_header_row(ws, row_idx: int) -> list[str]:
    """Returns the full row values (1-based row index)."""
    return ws.row_values(row_idx)

def _find_or_create_date_column(ws, short_label: str) -> int:
    """
    Finds the column index where row 2 ('Training Date') equals short_label.
    If not found, appends at the next empty column.
    """
    header_row = _get_existing_header_row(ws, 2)  # row 2 = Training Date row
    # Ensure at least col A exists
    if not header_row:
        header_row = ["Training Date"]
        ws.update_acell("A2", "Training Date")

    # Find existing column
    for idx, val in enumerate(header_row, start=1):
        if (val or "").strip().lower() == short_label.strip().lower():
            return idx

    # Not found -> create at next column
    next_col = len(header_row) + 1
    ws.update_cell(2, next_col, short_label)  # put header text
    return next_col

def append_poll(record: dict):
    """
    Writes poll_id horizontally under the date column in 'Attendance List'.
    Layout:
      Row 1: 'Tele Poll ID' | [poll id under matching date col]
      Row 2: 'Training Date' | [date labels across]
    """
    ws = get_attendance_ws()
    _find_or_create_header_rows(ws)

    # Ensure A1 / A2 labels correct (idempotent)
    ws.update_acell("A1", "Tele Poll ID")
    ws.update_acell("A2", "Training Date")

    poll_id = str(record["poll_id"])
    date_display = record.get("date", "")  # e.g. 'September 16, 2025' or '16/9'
    date_short = _date_label_from_display(date_display)

    # Find/create the date column under 'Training Date'
    col = _find_or_create_date_column(ws, date_short)

    # Put the poll id in row 1 at that column
    ws.update_cell(1, col, poll_id)

# # === REMINDER ===
# async def send_reminder(bot, chat_id, thread_id):
#     next_tuesday = get_next_tuesday()
#     try:
#         await bot.send_message(
#             chat_id=chat_id,
#             message_thread_id=thread_id,
#             text=f"Reminder: There's training tomorrow {next_tuesday.strftime('%B %d, %Y')}."
#         )
#     except Exception as e:
#         print(f"[ERROR] Failed to send reminder: {e}")

# === REMINDER (sheet-driven) ===
async def send_reminder(bot, chat_id, thread_id):
    try:
        ws = get_attendance_ws()
        _find_or_create_header_rows(ws)

        # Row 1: poll IDs; Row 2: date labels
        poll_row = ws.row_values(1)
        date_row = ws.row_values(2)

        # Normalize lengths
        max_len = max(len(poll_row), len(date_row))
        if len(poll_row) < max_len:
            poll_row += [""] * (max_len - len(poll_row))
        if len(date_row) < max_len:
            date_row += [""] * (max_len - len(date_row))

        # Find last column (from right) where a poll id exists
        last_col = None
        for idx in range(max_len, 0, -1):
            if idx == 1:
                # skip column A (labels) unless you also store IDs there
                if (poll_row[0] or "").strip().isdigit():
                    last_col = 1
                break
            if (poll_row[idx - 1] or "").strip():
                last_col = idx
                break

        # Compute the date to announce
        if last_col and last_col > 1:
            date_label_short = (date_row[last_col - 1] or "").strip()  # e.g. '16/9'
            # Format nicely for chat: '16 Sep 2025' if possible; if only dd/mm, keep it
            pretty_date = date_label_short
            # Try to map dd/mm to a full date this/next year (optional):
            try:
                d, m = [int(x) for x in date_label_short.split("/")]
                # Heuristic: if today's month > m, assume next year; else this year
                today = datetime.now(sg_tz)
                year = today.year + (1 if today.month > m else 0)
                dt = datetime(year, m, d, tzinfo=sg_tz)
                pretty_date = dt.strftime("%d %b %Y")
            except Exception:
                pass
        else:
            # Fallback to computed next Tuesday
            pretty_date = get_next_tuesday().strftime("%d %b %Y")

        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"Reminder: There's training tomorrow {pretty_date}."
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")
        
# === POLL SEND ===
@admin_only
async def send_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    tues_date = next_tuesday.strftime('%B %d, %Y')

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Are you joining the training on {tues_date}?",
        options=["Yes", "No"],
        is_anonymous=False,
        message_thread_id=thread_id
    )

    # ✅ Register the poll type
    active_polls[msg.poll.id] = "training"
    yes_voters.clear()  # reset voters for the new training poll

    append_poll({
        "poll_id": msg.poll.id,
        "message_id": msg.message_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "date": tues_date
    })
    # Schedule reminder
    threading.Timer(delay_sec, lambda: asyncio.run(send_reminder(context.bot, chat_id, thread_id))).start()

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
def get_gspread_sheet(tab_name=SHEET_TAB_NAME):
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    # creds_dict = GOOGLE_CREDENTIALS_JSON
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).worksheet(tab_name)

# Google Sheet for Welcome Tea responses 
def get_gspread_sheet_welcome_tea(tab_name = "Form Responses 1"):
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open("NTUCD Welcome Tea Registration 2025 (Responses)").worksheet(tab_name)

def matric_valid(matric_number: str) -> bool:
    sheet = get_gspread_sheet_welcome_tea()
    rows = sheet.get_all_records()  # Each row is a dict

    for row in rows:
        matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
        attendance = str(row.get("Attendance", "")).strip()

        if matric_in_row == matric_number.strip().upper():
            # ✅ Attendance must be exactly '1'
            return attendance == "1"

    return False
def append_to_others_list(thread_id):
    try:
        sheet = get_gspread_sheet("OTHERS List")
        sheet.append_row([thread_id])
        print(f"[INFO] Thread ID {thread_id} written to OTHERS List.")
        OTHERS_THREAD_IDS.add(thread_id)  # Also track in memory
    except Exception as e:
        print(f"[ERROR] Failed to write Thread ID {thread_id} to OTHERS List: {e}")

# === Admin check ===
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]

# === Restriction handler ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat  # ✅ always handy!
    thread_id = msg.message_thread_id
    user_is_admin = await is_admin(update, context)

    print(f"[DEBUG] handle_message called by user {user.id} in thread {thread_id}")
    print(f"[DEBUG] user_is_admin: {user_is_admin}")

    if thread_id in EXEMPTED_THREAD_IDS or thread_id in OTHERS_THREAD_IDS:
        pass
    else:
        if msg.is_topic_message and thread_id not in initialized_topics:
            initialized_topics.add(thread_id)

            # testing start
            sheet = get_gspread_sheet()
            records = sheet.get_all_records()
            for idx, row in enumerate(records):
                if str(row["THREAD ID"]) == str(thread_id):
                    return
            # testing end

            if user_is_admin:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("PERF", callback_data=f"topic_type|PERF|{thread_id}")],
                    [InlineKeyboardButton("OTHERS", callback_data=f"topic_type|OTHERS|{thread_id}")]
                ])
                prompt = await chat.send_message(
                    "What's the topic for? PERF or OTHERS?",
                    reply_markup=keyboard,
                    message_thread_id=thread_id
                )
                context.chat_data[f"init_prompt_{thread_id}"] = prompt.message_id

            # ✅ SAFE DELETE
            if chat.type in ["group", "supergroup"]:
                await msg.delete()
            else:
                print(f"[DEBUG] Not a group — skip delete.")
            return

    # === THREAD MESSAGE RESTRICTIONS ===
    # restrict all actions except for admin users
    if not user_is_admin:

        # Restrict all command messages (e.g., /command)
        if msg.text and msg.text.startswith("/"):
            print(f"[DEBUG] User {user.id} sent command in thread {thread_id}. Deleting.")
            if chat.type in ["group", "supergroup"]:
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to delete command message: {e}")
            else:
                print(f"[DEBUG] Not a group — skip delete.")

        # About NTUCD — block all messages
        if thread_id is None:
            print(f"[DEBUG] Message in ABOUT NTUCD. Deleting.")
            if chat.type in ["group", "supergroup"]:
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to delete message in ABOUT thread: {e}")
            else:
                print(f"[DEBUG] Not a group — skip delete.")

        # Voting Topic — only allow replies to polls
        elif thread_id == TOPIC_VOTING_ID:
            if msg.poll or not (msg.reply_to_message and msg.reply_to_message.poll):
                print(f"[DEBUG] Message not a poll or poll reply in VOTING. Deleting.")
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to delete message in VOTING: {e}")

        elif thread_id in TOPIC_MEDIA_IDS:
            is_photo = msg.photo is not None
            is_video = msg.video is not None
            is_valid_doc = (
                msg.document is not None and
                getattr(msg.document, "mime_type", "").startswith(("image/", "video/"))
            )

            is_gif = msg.animation is not None
            is_sticker = msg.sticker is not None
            is_voice = msg.voice is not None
            is_audio = msg.audio is not None
            is_text = msg.text is not None
            is_contact = msg.contact is not None
            is_location = msg.location is not None
            is_venue = msg.venue is not None
            is_poll = msg.poll is not None
            is_video_note = msg.video_note is not None
            
            # is_web_app = getattr(msg, "web_app_data", None) is not None
            # is_via_bot = msg.via_bot is not None

            if is_gif or is_sticker or is_voice or is_audio or is_contact or is_location or is_venue or is_poll or is_video_note:
                print(f"[DEBUG] ❌ msg in MEDIA thread {thread_id}. Deleting.")
                await msg.delete()
            # elif is_web_app or is_via_bot:
            #     print(f"[DEBUG] ❌ Web App or Telebubble message in MEDIA thread {thread_id}. Deleting.")
            #     await msg.delete()
            elif is_text:
                print(f"[DEBUG] ❌ Text message in MEDIA thread {thread_id}. Deleting.")
                await msg.delete()
            elif is_valid_doc:
                print(f"[ALLOWED] ✅ Valid document in MEDIA thread {thread_id}")
            elif msg.document:
                print(f"[DEBUG] ❌ Invalid document type: {msg.document.mime_type} in MEDIA thread {thread_id}. Deleting.")
                await msg.delete()
            elif not (is_photo or is_video or is_valid_doc):
                print(f"[DEBUG] ❌ Non-media message in MEDIA thread {thread_id}. Deleting.")
                print(f"[INFO] Message type details: {msg}")
                await msg.delete()
            else:
                print(f"[DEBUG] Valid media message{msg} in MEDIA thread {thread_id}.")
                print(f"[ALLOWED] ✅ Valid media message in MEDIA thread {thread_id}")

        # BLOCKED Topic — block all messages
        elif thread_id == TOPIC_BLOCKED_ID:
            print(f"[DEBUG] User {user.id} tried to send message in BLOCKED thread {thread_id}. Deleting.")
            print(f"[INFO] Deleted message type: {msg}")
            try:
                await msg.delete()
            except Exception as e:
                print(f"[ERROR] Failed to delete in BLOCKED topic: {e}")

    # === Fallback log for all messages
    else:
        print(f"[INFO] Message from admin in thread {thread_id}: allowed.")

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
    
    elif selection == "OTHERS":
        append_to_others_list(thread_id)
    
    await query.message.edit_text(f"Topic marked as {selection}. No further action.")
    return ConversationHandler.END

# Handle button selection
async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) != 3:
        return
    _, thread_id, action = parts
    thread_id = int(thread_id)

    # 🧹 Delete previous ACCEPT/REJECT prompt
    msg_id = context.chat_data.pop(f"confirm_prompt_{thread_id}", None)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=msg_id)
        except:
            pass

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_number, row_data = None, None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2
            row_data = row
            break
    if not row_data:
        return

    if action == "CANCEL":
        # Unpin summary (if exists), and send back Performance Opportunity
        # old_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
        # if old_msg_id:
        #     try:
        #         await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=old_msg_id)
        #     except:
        #         pass
        return

    if action == "REJECT":
        # === Update status in GSheet ===
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            if str(row["THREAD ID"]) == str(thread_id):
                row_index = idx + 2
                sheet.update_cell(row_index, 7, "REJECTED")
                break

        # === Send rejection message
        await query.message.chat.send_message("❌ Performance rejected. This topic will now be closed.", message_thread_id=thread_id)

        # ✅ Delay + delete topic
        await delete_topic_with_delay(context, chat_id=query.message.chat.id, thread_id=thread_id)
    
    if action == "ACCEPT":
        all_dates = row_data["PROPOSED DATE | TIME"].splitlines()
        context.chat_data[f"final_row_number_{thread_id}"] = row_number
        context.chat_data[f"final_all_dates_{thread_id}"] = all_dates
        context.chat_data[f"selected_dates_{thread_id}"] = []

        buttons = [[InlineKeyboardButton(text=d, callback_data=f"FINALDATE|{thread_id}|{i}")]
                   for i, d in enumerate(all_dates)]
        buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data=f"FINALDATE|{thread_id}|CONFIRM")])
        prompt = await query.message.chat.send_message(
            "🗓️ Select final date(s) to confirm:",
            reply_markup=InlineKeyboardMarkup(buttons),
            message_thread_id=thread_id
        )
        context.chat_data[f"final_prompt_{thread_id}"] = prompt.message_id

# Handle final date selection
async def final_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    if len(data) != 3:
        return

    _, thread_id, selection = data
    thread_id = int(thread_id)

    print(f"[DEBUG] Callback received for thread_id={thread_id}, selection={selection}")
    print(f"[DEBUG] chat_data keys: {list(context.chat_data.keys())}")

    row_number = context.chat_data.get(f"final_row_number_{thread_id}")
    all_dates = context.chat_data.get(f"final_all_dates_{thread_id}", [])
    selected = context.chat_data.setdefault(f"selected_dates_{thread_id}", [])

    print(f"[DEBUG] row_number={row_number}")
    print(f"[DEBUG] all_dates={all_dates}")
    print(f"[DEBUG] currently selected={selected}")

    if selection == "CONFIRM":
        if not selected:
            return await query.message.reply_text("⚠️ Please select at least one date before confirming.")

        # === Cleanup UI messages ===
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
            print("[DEBUG] Deleted final selection message.")
        except Exception as e:
            print(f"[WARNING] Failed to delete final date selection message: {e}")

        try:
            msg_id = context.chat_data.pop("confirm_prompt_msg_id", None)
            if msg_id:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
                print(f"[DEBUG] Deleted ACCEPT/REJECT message: {msg_id}")
        except Exception as e:
            print(f"[WARNING] Failed to delete ACCEPT/REJECT message: {e}")

        # === Update sheet ===
        value = "\n".join([all_dates[i] for i in sorted(map(int, selected))])
        sheet = get_gspread_sheet()
        sheet.update_cell(row_number, 6, value)  # Column F = 6
        sheet.update_cell(row_number, 7, "ACCEPTED")  # Column G = 7

        # === Refresh row ===
        updated_row = sheet.row_values(row_number)
        while len(updated_row) < 7:
            updated_row += [""] * (7 - len(updated_row))

        event, proposed, location, info, confirmed, status = updated_row[1:7]

        # === Format confirmed date
        date_lines = []
        for line in confirmed.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                dt = datetime.strptime(line, "%d %b %Y | %I:%M%p")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
            except:
                formatted = f"• {line}"
            date_lines.append(formatted)

        # === Format summary
        template = (
            f"📢 *Performance Summary*\n\n"
            f"📍 *Event*\n"
            f"• {event}\n\n"
            f"📅 *Confirmed Date | Time*\n"
            f"{chr(10).join(date_lines)}\n\n"
            f"📌 *Location*\n"
            f"• {location}\n\n"
            f"📝 *Performance Information:*\n"
            f"{info.strip()}"
        )

        # === Unpin previous summary (if any), then pin new summary
        try:
            old_summary_id = context.chat_data.pop(f"summary_msg_{thread_id}", None)
            if old_summary_id:
                await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=old_summary_id)
                print(f"[DEBUG] Unpinned old summary: {old_summary_id}")
        except Exception as e:
            print(f"[WARNING] Failed to unpin previous summary: {e}")

        # ✅ Send new summary
        msg = await query.message.chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=query.message.chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id

        return

    # === Handle toggling checkboxes
    if selection not in selected:
        selected.append(selection)
    else:
        selected.remove(selection)

    new_buttons = []
    for i, d in enumerate(all_dates):
        is_selected = str(i) in selected
        label = f"✅ {d}" if is_selected else d
        new_buttons.append([InlineKeyboardButton(text=label, callback_data=f"FINALDATE|{thread_id}|{i}")])
    new_buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data=f"FINALDATE|{thread_id}|CONFIRM")])

    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))


def parse_flexible_date(date_str: str) -> str:
    original = date_str.strip()
    lower = original.lower()

    # Normalize dot-separated times (e.g., 2.30 → 2:30)
    lower = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', lower)

    # Normalize 4-digit shorthand times (e.g., 1430 → 14:30)
    lower = re.sub(r'\b(\d{4})\b', lambda m: f"{m.group(1)[:2]}:{m.group(1)[2:]}", lower)

    # Normalize 3-4 digit shorthand times like 830am → 8:30 am
    lower = re.sub(r'\b(\d{1,2})(\d{2})(am|pm)\b', r'\1:\2 \3', lower)

    # Normalize AM/PM time (e.g., 2pm → 2:00 pm, 2:30pm → 2:30 pm)
    lower = re.sub(r'(\d{1,2}:\d{2})(am|pm)\b', r'\1 \2', lower)
    lower = re.sub(r'\b(\d{1,2})(am|pm)\b', r'\1:00 \2', lower)

    # Match formats like: 24jun25 2:30pm, 24june2025, 24jun 2pm, 24june26
    pattern = re.match(r"(\d{1,2})[\s-]?([a-zA-Z]{3,9})[\s-]?(\d{2,4})?(?:\s+(\d{1,2}:\d{2}(?:\s?(?:am|pm))?))?$", lower)
    if not pattern:
        raise ValueError(f"❌ Invalid date format: '{original}'\n👉 Use formats like '24jun25' or '24jun25 2:30pm'.")

    day, month, year, time_part = pattern.groups()

    # Default year to current year if not given
    if not year:
        year = str(datetime.now().year)
    elif len(year) == 2:
        year = "20" + year

    # Build datetime string
    date_time_str = f"{day} {month} {year}"
    if time_part:
        date_time_str += f" {time_part}"

        # Try valid time formats (first 24h, then 12h)
        for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M", "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p"):
            try:
                dt = datetime.strptime(date_time_str, fmt)
                time_str = dt.strftime("%I:%M%p").lstrip("0").lower()
                return f"{dt.strftime('%d %b %Y').upper()} | {time_str}"
            except ValueError:
                continue

        raise ValueError(f"❌ Time format is invalid: '{time_part}'\n👉 Use formats like 2pm, 2:30pm, 1430")

    # No time part → just parse date
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(f"{day} {month} {year}", fmt)
            return dt.strftime("%d %b %Y").upper()
        except ValueError:
            continue

    raise ValueError(f"❌ Date format is invalid: '{original}'\n👉 Use formats like '24jun25', not numeric months.")


def parse_and_format_dates(dates_str):
    parts = [p.strip() for p in dates_str.split(",")]
    results = []
    invalid_parts = []

    for part in parts:
        if not part:
            invalid_parts.append("(empty)")
            continue
        try:
            formatted = parse_flexible_date(part)
            results.append(formatted)
        except ValueError:
            invalid_parts.append(part)

    if invalid_parts:
        if len(parts) == 1:
            # 🟥 Single entry error
            raise ValueError(
                "❌ Invalid *date/time* format.\n"
                "👉 Use formats like:\n"
                "- `23aug25 8:30pm`\n"
                "- `23aug 1430`\n"
                "- `23aug25`\n"
                "- `23aug`\n"
                "\n⚠️ Make sure your input uses letters for month (e.g. `aug`, not `08`)."
            )
        else:
            # 🟥 Multiple entry error
            raise ValueError(
                "❌ One or more *date/time* entries are invalid.\n"
                "👉 Use correct comma `,` between entries and formats like:\n"
                "- `23aug 8pm, 24aug 9pm`\n"
                "- `23aug25 1430, 24aug25`\n"
                "\n⚠️ Use *letter months*, not numeric (e.g. `aug`, not `08`)."
            )

    return results

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
        raw_date = update.message.text.strip()
        try:
            formatted_dates = parse_and_format_dates(raw_date)
            date = "\n".join(formatted_dates)
        except ValueError as e:
            # Clean up older messages if exist
            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                    except Exception as ex:
                        print(f"[WARNING] Failed to delete previous {key} message: {ex}")

            error_msg = await update.effective_chat.send_message(
                str(e),
                parse_mode="Markdown",
                message_thread_id=context.user_data["perf_temp"]["thread_id"]
            )
            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
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
                sheet.update(values=[[event, date, location, info, "", ""]], range_name=f"B{idx}:G{idx}")
                break

    # === Case 2: Full Input ===
    else:
        raw_text = update.message.text.strip()
        parts = [p.strip() for p in raw_text.split("//")]
        print(f"[DEBUG] Parsed parts: {len(parts)} - {parts}")

        thread_id = context.user_data.get("thread_id")  # ✅ Define this early

        if len(parts) < 3 or any(not p for p in parts[:3]):
            error_msg = await update.effective_chat.send_message(
            "❌ Invalid input format. Use:\n*Event // Date // Location // Info (optional)*\n\n"
            "Example:\n`NTU Welcome Tea // 23 JUN 2025 8:00pm // NYA // Formal wear required`",
            parse_mode="Markdown",
            message_thread_id=thread_id
        )

            # Clean up older messages if exist
            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                    except Exception as e:
                        print(f"[WARNING] Failed to delete previous {key} message: {e}")

            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
            return DATE

        event, date, location = parts[0], parts[1], parts[2]
        info = parts[3] if len(parts) >= 4 else ""
        thread_id = context.user_data.get("thread_id")

        # Try to format date
        try:
            raw_date = date
            formatted_dates = parse_and_format_dates(raw_date)
            date = "\n".join(formatted_dates)
        except ValueError as e:
            date = raw_date
            # Save temp and still append to sheet
            context.user_data["perf_temp"] = {
                "event": event,
                "location": location,
                "info": info,
                "thread_id": thread_id
            }
            try:
                sheet.append_row([thread_id, event, date, location, info, "", ""])
                print("[DEBUG] Row appended with invalid date")
            except Exception as e2:
                print(f"[ERROR] Failed to append row with invalid date: {e}")
 
            # Clean up previous errors
            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                    except Exception as ex:
                        print(f"[WARNING] Failed to delete previous {key} message: {ex}")

            # Show proper error message from parse_and_format_dates()
            error_msg = await update.effective_chat.send_message(
                str(e),
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
            return DATE

        # Valid date → Append to sheet
        try:
            sheet.append_row([thread_id, event, date, location, info, "", ""])
            print("[DEBUG] Row appended successfully")
        except Exception as e:
            print(f"[ERROR] Failed to append row: {e}")

    # Format multiline Date | Time block
    date_lines = []
    for entry in date.splitlines(): 
        entry = entry.strip()
        if not entry:
            continue
        try:
            dt = datetime.strptime(entry, "%d %b %Y %H%M")
            formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
            date_lines.append(formatted)
        except:
            date_lines.append(f"• {entry}")  # fallback if not parseable
    formatted_dates = "\n".join(date_lines)

    # Then build the final message
    template = (
        f"\U0001F4E2 *Performance Opportunity*\n\n"
        f"\U0001F4CD *Event*\n"
        f"• {event}\n\n"
        f"\U0001F4C5 *Date | Time*\n"
        f"{formatted_dates}\n\n"
        f"\U0001F4CC *Location*\n"
        f"• {location}\n\n"
        f"*Performance Information:*\n"
        f"{info.strip()}"
    )
    msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
    await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
    # Send interest poll (single/multi-choice)
    poll = await send_interest_poll(context.bot, update.effective_chat.id, thread_id, sheet)
    context.chat_data[f"interest_poll_msg_{thread_id}"] = poll["message_id"] if poll else None
    print(f"[DEBUG] Saved new poll message ID: {poll['message_id']}")

    return ConversationHandler.END

async def delete_topic_with_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int, delay_seconds: int = 5):
    await asyncio.sleep(delay_seconds)
    try:
        await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        print(f"[INFO] Topic {thread_id} deleted after delay.")
    except Exception as e:
        print(f"[ERROR] Failed to delete topic {thread_id}: {e}")

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

    return ConversationHandler.END

async def confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg.is_topic_message:
        return await msg.reply_text("⛔ This command must be used inside a topic thread.")

    thread_id = msg.message_thread_id
    if not await is_admin(update, context):
        return await msg.reply_text("⛔ Only admins can use this command.")

    # Delete the /confirmation command message
    try:
        await msg.delete()
    except:
        pass

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_number, row_data = None, None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2
            row_data = row
            break

    if not row_data:
        return await msg.reply_text("❌ This thread is not registered.")

    if row_data["STATUS"]:
        return await msg.reply_text(
            f"❌ This performance is already marked as `{row_data['STATUS']}`.",
            parse_mode="Markdown"
        )

    keyboard = [
        [InlineKeyboardButton("✅ ACCEPT", callback_data=f"CONFIRM|{thread_id}|ACCEPT")],
        [InlineKeyboardButton("❌ REJECT", callback_data=f"CONFIRM|{thread_id}|REJECT")],
        [InlineKeyboardButton("🚫 CANCEL", callback_data=f"CONFIRM|{thread_id}|CANCEL")]
    ]
    prompt = await chat.send_message(
        "🎯 Is this performance *ACCEPTED* or *REJECTED*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id
    )
    context.chat_data[f"confirm_prompt_{thread_id}"] = prompt.message_id

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
    chat_id = msg.chat_id
    thread_id = msg.message_thread_id

    print("[DEBUG] /remind triggered.")
    print(f"[DEBUG] Thread ID: {thread_id}")

    # Delete the command message
    try:
        await msg.delete()
        print("[DEBUG] Deleted /remind command message.")
    except Exception as e:
        print(f"[WARNING] Failed to delete /remind command message: {e}")

    # Guard clause: must be inside a topic
    if not msg.is_topic_message:
        print("[DEBUG] Not a topic message. Ignoring.")
        return

    # Guard clause: exempted thread
    if thread_id in EXEMPTED_THREAD_IDS:
        print("[DEBUG] Thread is exempted. Skipping.")
        return

    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()

        for row in records:
            if str(row["THREAD ID"]) == str(thread_id):
                status = row.get("STATUS", "").strip().upper()
                if status != "ACCEPTED":
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Reminder can only be used *after confirmation*.",
                        parse_mode="Markdown",
                        message_thread_id=thread_id
                    )
                    print("[DEBUG] Status not ACCEPTED. Reminder skipped.")
                    return

                # === Compose reminder message ===
                date_str = row.get("CONFIRMED DATE | TIME", "").strip()
                date_lines = "\n".join([f"• {d.strip()}" for d in date_str.splitlines() if d.strip()])
                template = (
                    f"📢 *Performance Reminder*\n\n"
                    f"📍 *Event*\n• {row['EVENT']}\n\n"
                    f"📅 *Date | Time*\n{date_lines}\n\n"
                    f"📌 *Location*\n• {row['LOCATION']}\n\n"
                    f"*📝 Final Preparation Notes*\n\n"
                    f"*👀 Glasses & Contact Lens*\n"
                    f"If you wear glasses, try your best to perform without them (e.g. wear contact lens). Default is *no glasses* on stage. Make sure you're comfortable before show day.\n\n"
                    f"*⬇️💪 Shave Your Armpits*\n"
                    f"We want the audience to focus on our performance, not our underarms 🪒😌 So please make sure to shave before the show!\n\n"
                    f"*🎽 Costume Tips*\n"
                    f"Our costumes are sleeveless and v-neck. Avoid wearing bright-colored bras (neon pink/yellow/rainbow 🌈). A black sports bra is best.\n\n"
                    f"*🦶 Barefoot Reminder*\n"
                    f"Everyone will be performing *barefoot*. Don't forget!\n\n"
                    f"*💇 Hair Tying*\n"
                    f"If you have long hair, please tie it up neatly. You can also ask someone to help if needed.\n\n"
                    f"*📺 Recap the Drum Score*\n"
                    f"Make sure to go through the performance videos again and recap the score before the show. Stay sharp!"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=template,
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )
                print("[DEBUG] Reminder message sent.")
                return

        # ❌ Not found
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ This thread has not been registered.",
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        print("[DEBUG] Thread ID not found in GSheet.")

    except Exception as e:
        print(f"[ERROR] Failed to execute /remind: {e}")
    
# Start modify process
async def start_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    # Always attempt to delete the command message
    try:
        await update.message.delete()
    except Exception as e:
        print(f"[DEBUG] Failed to delete /modify command: {e}")

    # Skip if not in topic
    if not msg.is_topic_message:
        return

    thread_id = msg.message_thread_id

    # Skip if not admin or in exempted thread
    if thread_id in EXEMPTED_THREAD_IDS or not await is_admin(update, context):
        return

    print(f"[DEBUG] /modify triggered by user {update.effective_user.id} in chat {msg.chat_id}, thread {thread_id}")
    
    if thread_id is None:
        return await msg.reply_text("⛔ This command must be used inside a topic thread.")

    context.user_data["modify_thread_id"] = thread_id
    
    # === Lookup status from sheet ===
    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row = next((r for r in records if str(r["THREAD ID"]) == str(thread_id)), None)

    if not row:
        await msg.reply_text("❌ This thread is not registered in the sheet.", message_thread_id=thread_id)
        return

    status = row.get("STATUS", "").strip().upper()
    print(f"[DEBUG] Status for thread {thread_id}: {status}")

    if status == "REJECTED":
        await msg.reply_text("❌ This performance is already REJECTED. You cannot modify it.", message_thread_id=thread_id)
        return

     # === Dynamically set modifiable fields ===
    if status == "":
        date_field = "PROPOSED DATE | TIME"
        modify_options = ["EVENT", date_field, "LOCATION", "PERFORMANCE INFO"]
    else:
        date_field = "CONFIRMED DATE | TIME"
        modify_options = ["EVENT", date_field, "LOCATION", "PERFORMANCE INFO", "STATUS"]

    # === Build keyboard dynamically from modify_options
    emoji_map = {
        "EVENT": "📍",
        "CONFIRMED DATE | TIME": "📅",
        "PROPOSED DATE | TIME": "📅",
        "LOCATION": "📌",
        "PERFORMANCE INFO": "📝",
        "STATUS": "📊",
    }

    # === Build inline keyboard ===
    keyboard = []
    for opt in modify_options:
        emoji = emoji_map.get(opt, "")
        keyboard.append([InlineKeyboardButton(f"{emoji} {opt.title()}", callback_data=f"MODIFY|{opt}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="MODIFY|CANCEL")])

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
    
    # === Show inline keyboard for selecting confirmed date ===
    if field == "CONFIRMED DATE | TIME":
        print("[DEBUG] User selected to modify CONFIRMED DATE | TIME")
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row["THREAD ID"]) == str(context.user_data["modify_thread_id"]):
                proposed = row.get("PROPOSED DATE | TIME", "").strip()
                proposed_dates = [d.strip() for d in proposed.splitlines() if d.strip()]
                if not proposed_dates:
                    await query.message.chat.send_message(
                        "⚠️ No proposed dates available to choose from.",
                        message_thread_id=context.user_data["modify_thread_id"]
                    )
                    return ConversationHandler.END

                # ✅ Init values
                context.user_data["proposed_dates"] = proposed_dates
                context.user_data["selected_date_indices"] = []  # <-- important
                thread_id = context.user_data["modify_thread_id"]

                # Build inline buttons using stringified index
                buttons = []
                for i, d in enumerate(proposed_dates):
                    label = d
                    buttons.append([
                        InlineKeyboardButton(label, callback_data=f"modify_date_selected|{i}")
                    ])

                buttons.append([
                    InlineKeyboardButton("✅ Confirm Selection", callback_data="modify_date_selected|CONFIRM")
                ])

                markup = InlineKeyboardMarkup(buttons)

                await query.message.chat.send_message(
                    "📅 Please choose the *confirmed date*, then press ✅ Confirm Selection:",
                    reply_markup=markup,
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )

                return ConversationHandler.END
    
    # === Handle STATUS change via inline buttons ===
    elif field == "STATUS":
        print("[DEBUG] User selected to modify STATUS")
        thread_id = context.user_data["modify_thread_id"]
        buttons = [
            [InlineKeyboardButton("❌ Reject Performance", callback_data="modify_status_selected|REJECTED")],
            [InlineKeyboardButton("↩️ Cancel", callback_data="modify_status_selected|CANCEL")]
        ]
        markup = InlineKeyboardMarkup(buttons)
        await query.message.chat.send_message(
            "🚦 Please select the new *STATUS*: ",
            reply_markup=markup,
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        return ConversationHandler.END
    
    prompt = await query.message.chat.send_message(
        f"✅ Got it! What is the new value for *{field}*?",
        parse_mode="Markdown",
        message_thread_id=context.user_data["modify_thread_id"]
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE

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

    # === Step 1: Find the row number ===
    row_number = None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2  # Header row + 1-based index
            break

    if row_number is None:
        print("[DEBUG] Thread ID not found in sheet")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This thread is not registered in the sheet."
        )
        return ConversationHandler.END
    
    status = row.get("STATUS", "").strip().upper()
    allowed_fields = []

    if status == "":
        allowed_fields = ["EVENT", "PROPOSED DATE | TIME", "LOCATION", "PERFORMANCE INFO"]
    elif status == "ACCEPTED":
        allowed_fields = ["EVENT", "CONFIRMED DATE | TIME", "LOCATION", "PERFORMANCE INFO", "STATUS"]
    else:
        await update.effective_chat.send_message("❌ This performance is already REJECTED. You cannot modify it.", message_thread_id=thread_id)
        return ConversationHandler.END

    if field not in allowed_fields:
        await update.effective_chat.send_message(f"⛔ You can’t modify *{field}* in the current status (*{status}*).", parse_mode="Markdown", message_thread_id=thread_id)
        return ConversationHandler.END

    try:
        if field in ["PROPOSED DATE | TIME", "CONFIRMED DATE | TIME"]:
            try:
                formatted = parse_and_format_dates(value)
                value = ", ".join(formatted)
                print(f"[DEBUG] Reformatted date value: {value}")
            except ValueError as e:
                error_msg = await update.effective_chat.send_message(
                    str(e),
                    parse_mode="Markdown",
                    message_thread_id=context.user_data["modify_thread_id"]
                )
                context.chat_data["modify_error_msg_id"] = error_msg.message_id
                context.chat_data["invalid_input_msg_id"] = update.message.message_id
                
                return MODIFY_VALUE
            
        for key in ["modify_error_msg_id", "invalid_input_msg_id"]:
            msg_id = context.chat_data.pop(key, None)
            if msg_id:
                try:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except Exception as ex:
                    print(f"[WARNING] Failed to delete previous {key} message: {ex}")

        # === Step 3: Delete the new input message ===
        try:
            await update.message.delete()
        except:
            pass

        # === Step 4: Update the sheet ===
        col_index = SHEET_COLUMNS.index(field) + 1
        if field == "PROPOSED DATE | TIME":
            value = "\n".join(value.split(", "))
            print(f"[DEBUG] Rewritten date value with newline: {repr(value)}")

        range_name = f"{chr(64 + col_index)}{row_number}"  # e.g., C5
        sheet.update(range_name=range_name, values=[[value]])
        print(f"[DEBUG] Sheet updated at row {row_number}, column {col_index}")

        # === Step 5: Clean up prompt messages ===
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                try:
                    await asyncio.sleep(0.3)
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except BadRequest as e:
                    print(f"[INFO] Prompt message {msg_id} already deleted or not found: {e}")
            context.chat_data.pop("modify_prompt_msg_ids")

        # === Step 6: Delete previous summary ===
        prev_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
        if prev_msg_id:
            await asyncio.sleep(0.3)
            try:
                await context.bot.unpin_chat_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
                print(f"[DEBUG] Deleted previous summary message: {prev_msg_id}")
            except Exception as e:
                print(f"[WARNING] Could not delete previous summary: {e}")

        # === Step 7: Prepare and send updated summary ===
        updated_row = sheet.row_values(row_number)
        while len(updated_row) < 5:
            updated_row.append("")
        status = updated_row[6] if len(updated_row) > 6 else ""
        summary_title = "Performance Summary" if status else "Performance Opportunity"

        date_lines = []
        for entry in updated_row[2].splitlines():
            entry = entry.strip()
            if not entry:
                continue
            try:
                dt = datetime.strptime(entry, "%d %b %Y %H%M")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                date_lines.append(formatted)
            except:
                date_lines.append(f"• {entry}")
        formatted_dates = "\n".join(date_lines)

        template = (
            f"📢 *{summary_title}*\n\n"
            f"📍 *Event*\n"
            f"• {updated_row[1]}\n\n"
            f"📅 *Date | Time*\n"
            f"{formatted_dates}\n\n"
            f"📌 *Location*\n"
            f"• {updated_row[3]}\n\n"
            f"📌 *Performance Information:*\n"
            f"{updated_row[4].strip()}"
        )

        msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
        print(f"[DEBUG] Pinned updated summary for thread {thread_id}")

        # === Step 8: Resend poll if date changed ===
        if field == "PROPOSED DATE | TIME":
            try:
                old_poll_msg_id = context.chat_data.get(f"interest_poll_msg_{thread_id}")
                if old_poll_msg_id:
                    try:
                        await asyncio.sleep(0.3)
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_poll_msg_id)
                        print(f"[DEBUG] Deleted previous interest poll: {old_poll_msg_id}")
                    except Exception as e:
                        print(f"[WARNING] Failed to delete old poll message {old_poll_msg_id}: {e}")

                poll_msg = await send_interest_poll(
                    bot=context.bot,
                    chat_id=update.effective_chat.id,
                    thread_id=thread_id,
                    sheet=sheet
                )

                if poll_msg:
                    context.chat_data[f"interest_poll_msg_{thread_id}"] = poll_msg["message_id"]
                    print(f"[DEBUG] Saved new poll message ID: {poll_msg['message_id']}")
            except Exception as e:
                print(f"[WARNING] Failed to send interest poll: {e}")

        return ConversationHandler.END

    except Exception as e:
        print(f"[ERROR] Failed to update sheet: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Failed to update: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    
async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, selected = query.data.split("|")
    thread_id = context.user_data.get("modify_thread_id")
    proposed_dates = context.user_data.get("proposed_dates", [])

    selected_indices = set(context.user_data.get("selected_date_indices", []))

    if selected == "CONFIRM":
        if not selected_indices:
            await query.message.reply_text(
                "⚠️ Please select at least one date before confirming.",
                message_thread_id=thread_id
            )
            return

        # ✅ Always follow original order from GSheet, not click order
        final_dates = []
        for i, raw in enumerate(proposed_dates):
            if str(i) not in selected_indices:
                continue

            raw = raw.strip()
            parsed = False
            for fmt in ("%d %b %Y | %I:%M%p", "%d %b %Y %H%M", "%d %b %Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    formatted = f"{dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                    final_dates.append(formatted)
                    parsed = True
                    break
                except Exception as e:
                    continue
            if not parsed:
                print(f"[WARNING] Failed to parse date '{raw}', storing as-is")
                final_dates.append(raw.upper())

        final_value = "\n".join(final_dates)

        # === Write to GSheet ===
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        row = None
        row_index = None
        for idx, r in enumerate(records):
            if str(r["THREAD ID"]) == str(thread_id):
                row = r
                row_index = idx + 2
                sheet.update_cell(row_index, 6, final_value)  # CONFIRMED DATE | TIME
                break

        # === Delete previous summary
        prev_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
        if prev_msg_id:
            try:
                await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=prev_msg_id)
                await context.bot.delete_message(chat_id=query.message.chat.id, message_id=prev_msg_id)
            except Exception as e:
                print(f"[WARNING] Failed to delete previous summary: {e}")

        # === Print new summary
        formatted_lines = [f"• {d}" for d in final_dates]
        template = (
            f"📢 *Performance Summary*\n\n"
            f"📍 *Event*\n• {row['EVENT']}\n\n"
            f"📅 *Date | Time*\n" +
            "\n".join(formatted_lines) + "\n\n"
            f"📌 *Location*\n• {row['LOCATION']}\n\n"
            f"📌 *Performance Information:*\n{row['PERFORMANCE INFO'].strip()}"
        )
        msg = await query.message.chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=query.message.chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id

        # Cleanup
        context.user_data.pop("proposed_dates", None)
        context.user_data.pop("selected_date_indices", None)
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
        except:
            pass
        return

    # === Toggle selection
    if selected in selected_indices:
        selected_indices.remove(selected)
    else:
        selected_indices.add(selected)
    context.user_data["selected_date_indices"] = selected_indices

    # === Redraw button UI
    buttons = []
    for i, d in enumerate(proposed_dates):
        label = f"✅ {d}" if str(i) in selected_indices else d
        buttons.append([InlineKeyboardButton(label, callback_data=f"modify_date_selected|{i}")])
    buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="modify_date_selected|CONFIRM")])
    markup = InlineKeyboardMarkup(buttons)

    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        print(f"[WARNING] Could not update buttons: {e}")

async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, selection = query.data.split("|")
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except:
            pass
        return

    if selection == "REJECTED":
        print(f"[DEBUG] Admin rejected performance in thread {thread_id}")
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            if str(row["THREAD ID"]) == str(thread_id):
                row_index = idx + 2
                sheet.update_cell(row_index, 7, "REJECTED")  # STATUS column
                break

        # Print cancellation notice
        try:
            await query.message.chat.send_message(
                "🚫 This performance has been *cancelled*. The topic will be deleted shortly.",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
        except:
            pass

        # Delay then delete topic
        await delete_topic_with_delay(context, chat_id=query.message.chat.id, thread_id=thread_id)
        return
    
# Join request 
async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    #FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header"

    print(f"Join request received from {user.first_name}")

    # Always send the form
    # First message
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"Hi {user.first_name}, Welcome!!🥁\n\n"
                "<b>NTU Chinese Drums' Welcome Tea Session</b>\n"
                "<b>Date:</b> 19th August 2025 (Tuesday)\n"
                "<b>Time:</b> 1830 – 2130 (GMT+8)\n"
                "<b>Venue:</b> <a href='https://goo.gl/maps/7yqc3EfYNE92'>Nanyang House Foyer</a>\n"
                "<b>Dress Code:</b> Comfortable & Casual (we generally go barefoot for our practices/performances, "
                "so preferably wear slippers – but covered shoes are fine too!)\n\n"
                "🍱 <b>Dinner is provided</b> and time is allocated to eat, so no need to dabao. You can come straight from class!\n\n"
                "<b>🗺 Video Guide to Nanyang House</b>\n"
                "▶️ <b>Red Bus (Hall 2) Video Guide:</b>\n"
                "<a href='https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link'>Watch here</a>\n"
                "▶️ <b>Blue Bus (Hall 6) Video Guide:</b>\n"
                "<a href='https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link'>Watch here</a>\n"
                "📄 <b>PDF Guide to Nanyang House:</b>\n"
                "<a href='https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link'>View PDF</a>\n\n"
            ),
            parse_mode=ParseMode.HTML
        )  

        # Send registration form link
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"🌸 To make our Welcome Tea run smoothly, we’d love it if you could fill in the sign-up form below 📝💛\n"
                f"<a href='https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header'>Registration Form</a>\n"
                f"(you can ignore this if you’ve already done so ✔️)\n\n"
                f"💬 If you have any queries, feel free to contact:\n"
                f"👉 Chairperson Brandon: @Brandonkjj\n"
                f"👉 Vice-chairperson Pip Hui: @pip_1218\n\n"
                f"👋 See you next Tuesday! 🎉"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print("Could not message user:", e)

    # ✅ Store their pending join request
    pending_users[user.id] = update.chat_join_request

async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in pending_users:
        await update.message.reply_text(
            "❌ You don't have a pending join request or it has already been approved. Please request to join the group first\n\n"
            "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
        )
        return ConversationHandler.END

    await update.message.reply_text("Please enter your NTU matriculation number (case sensitive) to verify. e.g U2512345F")
    return ASK_MATRIC

async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    matric = update.message.text.strip()

    if user_id not in pending_users:
        await update.message.reply_text(
            "❌ You don't have a pending join request or it has already been approved. Please request to join the group first.\n\n"
            "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
        )
        return ConversationHandler.END

    join_request = pending_users[user_id]

    if matric_valid(matric):
        await join_request.approve()
        update_user_id_in_sheet(matric, user_id)
        await update.message.reply_text(
            "✅ Matric number and attendance verified. You have been approved. Welcome!"
        )
        pending_users.pop(user_id, None)
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            "❌ We couldn't verify your matric number or attendance. Please check your entry and try again.\n\n"
            "🔁 Please enter your NTU matriculation number (case sensitive) again e.g U2512345F:\n\n"
            "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
        )
        # Stay in ASK_MATRIC state
        return ASK_MATRIC

def copy_user_to_timeline(welcome_row: dict, telegram_user_id: int):
    others_sheet = get_gspread_sheet("PERFORMER Info")  
    name = welcome_row.get("Your Full Name (according to matric card)", "").strip()
    nickname = welcome_row.get("What name or nickname do you prefer to be called? ", "").strip()

    # Compose a new row
    new_row = [name, nickname, str(telegram_user_id), "Join", ""]

    # Append to the PERFORMER Info List
    others_sheet.append_row(new_row, value_input_option="USER_ENTERED")
    print(f"[INFO] Copied to PERFORMER Info List: {new_row}")

def update_user_id_in_sheet(matric_number: str, telegram_user_id: int):
    sheet = get_gspread_sheet_welcome_tea()
    rows = sheet.get_all_records()

    for idx, row in enumerate(rows, start=2):  # +2 because get_all_records() skips header, rows start at index 2
        matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
        if matric_in_row == matric_number.strip().upper():
            # Assume there is a column named 'User ID'
            user_id_col = None
            header = sheet.row_values(1)
            for i, col_name in enumerate(header, start=1):
                if col_name.strip().lower() == "user id":
                    user_id_col = i
                    break

            if user_id_col:
                sheet.update_cell(idx, user_id_col, str(telegram_user_id))
                print(f"[INFO] User ID {telegram_user_id} saved for {matric_number} in row {idx}.")

                # ✅ Copy to timeline sheet — PERFORMER info
                copy_user_to_timeline(row, telegram_user_id)

            else:
                print("[ERROR] 'User ID' column not found in sheet.")
            return

    print("[WARN] Matric number not found when trying to update User ID.")

# Avoid double entries    
def user_already_in_timeline(user_id: int) -> bool:
    others_sheet = get_gspread_sheet("PERFORMER Info")
    all_rows = others_sheet.get_all_records()
    for row in all_rows:
        if str(row.get("User ID", "")).strip() == str(user_id):
            return True
    return False

# Manually adding new member
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        user_id = member.id
        name = member.first_name or member.last_name

        print(f"[INFO] ✅ New member joined: {name} ({user_id})")
        print(f"[DEBUG] Telegram user object: is_bot={member.is_bot}, full_name={member.full_name}")

        if user_already_in_timeline(user_id):
            print(f"[INFO] User ID {user_id} already exists in timeline — skipping fallback insert.")
        else:
            # Fallback row → use name for both name & nickname
            fallback_row = {
                "Your Full Name (according to matric card)": name,
                "What name or nickname do you prefer to be called? ": name
            }
            copy_user_to_timeline(fallback_row, user_id)

        # await update.effective_chat.send_message(
        #     f"👋 Welcome, {name}!"
        # )
        
async def handle_member_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_change = update.chat_member
    old_status = status_change.old_chat_member.status
    new_status = status_change.new_chat_member.status
    user = status_change.new_chat_member.user
    
    print(f"[DEBUG] Status change for {user.full_name} ({user.id}): {old_status} ➝ {new_status}")

    # ✅ Detect first join
    if old_status == "left" and new_status == "member":
        print(f"[INFO] 🎉 User {user.full_name} ({user.id}) has joined the group for the first time.")
        
        if not user_already_in_timeline(user.id):
            fallback_row = {
                "Your Full Name (according to matric card)": user.full_name,
                "What name or nickname do you prefer to be called? ": user.full_name
            }
            copy_user_to_timeline(fallback_row, user.id)
        else:
            print(f"[INFO] User {user.id} already exists in sheet — skip adding.")
    
    # ✅ Detect leave (either voluntarily or kicked)
    elif old_status in ("member", "administrator") and new_status in ("left", "kicked"):
        print(f"[INFO] 🚪 User {user.full_name} ({user.id}) has left the group.")
        success = mark_user_left_in_sheet(user.id)
        print(f"[INFO] Marked as 'Left' in sheet: {success}")
        
def mark_user_left_in_sheet(user_id: int) -> bool:
    sheet = get_gspread_sheet("PERFORMER Info")
    records = sheet.get_all_records()
    header = sheet.row_values(1)

    # Get column indexes
    user_id_col = header.index("User ID") + 1
    status_col = header.index("Status") + 1 if "Status" in header else None
    leave_date_col = header.index("Leave Date") + 1 if "Leave Date" in header else None

    # If any expected column is missing, return False
    if not (user_id_col and status_col and leave_date_col):
        print("[ERROR] Missing required columns: 'User ID', 'Status', or 'Leave Date'")
        return False

    for idx, row in enumerate(records, start=2):  # +2: header + 1-based index
        if str(row.get("User ID", "")).strip() == str(user_id):
            # Update 'Status' to 'Left'
            sheet.update_cell(idx, status_col, "Left")
            # Update 'Leave Date' to now
            leave_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.update_cell(idx, leave_date_col, leave_time)
            return True

    print(f"[WARN] User ID {user_id} not found in sheet.")
    return False

# === Setup Bot ===
def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
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
    
    # New verify handler
    verify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", start_verification)],
        states={
            ASK_MATRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_matric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("threadid", thread_id_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("confirmation", confirmation))  
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|"))
    app.add_handler(CallbackQueryHandler(confirmation_callback, pattern="^CONFIRM\\|"))
    app.add_handler(CallbackQueryHandler(final_date_selection, pattern="^FINALDATE\\|"))
    app.add_handler(CallbackQueryHandler(handle_modify_status_selection, pattern="^modify_status_selected\\|"))
    app.add_handler(modify_conv_handler)
    app.add_handler(CallbackQueryHandler(handle_modify_date_selection, pattern='^modify_date_selected\\|'))
    app.add_handler(CommandHandler("poll", send_poll_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(verify_conv_handler)
    app.add_handler(ChatMemberHandler(handle_member_status, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    
    print("Bot is running...")
    
    try:
        others_sheet = get_gspread_sheet("OTHERS List")
        rows = others_sheet.col_values(1)
        OTHERS_THREAD_IDS.update({int(r.strip()) for r in rows if r.strip().isdigit()})
        print(f"[INFO] Loaded {len(OTHERS_THREAD_IDS)} OTHERS thread IDs.")
    except Exception as e:
        print(f"[ERROR] Failed to load OTHERS List: {e}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise



