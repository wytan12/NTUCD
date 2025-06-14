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
yes_voters = set()

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

    global active_poll_id, ALLOWED_CHAT_ID
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

    if thread_id == TOPIC_VOTING_ID:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Are you joining the training on {next_tuesday.strftime('%B %d, %Y')}?",
            options=["Yes", "No"],
            is_anonymous=False,
            message_thread_id=thread_id
        )
        active_poll_id = msg.poll.id
        ALLOWED_CHAT_ID = chat_id
        threading.Timer(delay_sec, lambda: asyncio.run(send_reminder(context.bot, chat_id, thread_id))).start()

# === POLL ANSWER ===
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_poll_id, yes_voters
    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    selected_options = update.poll_answer.option_ids

    if poll_id != active_poll_id:
        return

    if 0 in selected_options and user.id not in yes_voters:
        yes_voters.add(user.id)
        print(f"[DEBUG] {user.full_name} voted YES")

# === Google Sheets Setup ===
def get_gspread_sheet():
    creds_dict = GOOGLE_CREDENTIALS_JSON
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
    # if not user_is_admin:
    #     if thread_id in EXEMPTED_THREAD_IDS:
    #         return
    #     elif msg.text and msg.text.startswith("/"):
    #         await msg.delete()
    #         return
    #         #await msg.delete()
    # if thread_id is None and not user_is_admin:
    #     await msg.delete()
    # elif thread_id == TOPIC_VOTING_ID and not user_is_admin:
    #     if msg.poll or not (msg.reply_to_message and msg.reply_to_message.poll):
    #         await msg.delete()
    # elif thread_id in TOPIC_MEDIA_IDS and not user_is_admin:
    #     if not (msg.photo or msg.video or (msg.document and msg.document.mime_type.startswith(("image/", "video/")))):
    #         await msg.delete()
    # elif thread_id == TOPIC_BLOCKED_ID and not user_is_admin:
    #     await msg.delete()

async def send_interest_poll(bot, chat_id, thread_id):
    try:
        msg = await bot.send_poll(
            chat_id=chat_id,
            message_thread_id=thread_id,
            question="Are you interested in this performance?",
            options=["Yes", "No"],
            is_anonymous=False
        )
        print(f"[DEBUG] Sent interest poll in thread {thread_id}")
        return msg.poll.id  # Return poll ID if needed
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

    if selection in ["PERF", "EVENT"]:
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

# === Conversation steps ===
async def parse_perf_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await update.message.delete()
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

    raw_text = update.message.text.strip()
    parts = [p.strip() for p in raw_text.split("//")]

    if len(parts) < 3:
        # Invalid: show error, stay in same state
        error_msg = await update.effective_chat.send_message(
            "\u274C Invalid format. Please use:\n*Event // Date // Location // Info (optional)*",
            parse_mode="Markdown",
            message_thread_id=context.user_data.get("thread_id")
        )
        context.chat_data["last_error"] = error_msg.message_id
        
        return DATE  # <-- Stay in the same state!

    # Cleanup old error if any
    if "last_error" in context.chat_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.chat_data.pop("last_error")
            )
        except:
            pass
        
    event = parts[0]
    date = parts[1]
    location = parts[2]
    info = parts[3] if len(parts) >= 4 else ""
    thread_id = context.user_data.get("thread_id")

    sheet = get_gspread_sheet()
    sheet.append_row([thread_id, event, date, location, info])
    
    template = (
        f"\U0001F4E2 *Performance Summary*\n\n"
        f"\U0001F4CD *Event:* {event}\n"
        f"\U0001F4C5 *Date:* {date}\n"
        f"\U0001F4CC *Location:* {location}\n"
        f"\n{info.strip()}"
    )
    msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
    # Store summary message ID
    context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id

    # Pin summary
    await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)

    # Send and store interest poll
    poll_msg = await context.bot.send_poll(
        chat_id=update.effective_chat.id,
        message_thread_id=thread_id,
        question="Are you interested in this performance?",
        options=["Yes", "No"],
        is_anonymous=False
    )
    context.chat_data[f"interest_poll_msg_{thread_id}"] = poll_msg.message_id
    # await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    # await send_interest_poll(context.bot, update.effective_chat.id, thread_id)
    return ConversationHandler.END

# async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await update.message.delete()
#     return ConversationHandler.END

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