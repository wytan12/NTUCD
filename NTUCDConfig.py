print("Bot starting...")

import csv
import json
import aiohttp
import gspread
from io import StringIO
from telegram import Update, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from oauth2client.service_account import ServiceAccountCredentials
import os
from dotenv import load_dotenv

load_dotenv()

# === CONFIGURATION ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = "NTUCD AY25/26 Timeline"
SHEET_TAB_NAME = "PERFORMANCE List"

# === Conversation states ===
DATE, EVENT, LOCATION = range(3)

# Thread access configuration
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 4
TOPIC_MEDIA_IDS = [25, 75]
TOPIC_BLOCKED_ID = 5

# Add exempt thread IDs here:
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS

# Track topic initialization
initialized_topics = set()
pending_questions = {}

# === Google Sheets Setup ===
# def get_gspread_sheet():
#     creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
#     scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
#     creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
#     client = gspread.authorize(creds)
#     return client.open(SHEET_NAME).worksheet(SHEET_TAB_NAME)
def get_gspread_sheet():
    # creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
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
    if msg.text and msg.text.startswith("/"):
        return
    if thread_id is None and not user_is_admin:
        await msg.delete()
    elif thread_id == TOPIC_VOTING_ID and not user_is_admin:
        if msg.poll or not (msg.reply_to_message and msg.reply_to_message.poll):
            await msg.delete()
    elif thread_id in TOPIC_MEDIA_IDS and not user_is_admin:
        if not (msg.photo or msg.video or (msg.document and msg.document.mime_type.startswith(("image/", "video/")))):
            await msg.delete()
    elif thread_id == TOPIC_BLOCKED_ID and not user_is_admin:
        await msg.delete()

# === Handle PERF/EVENT/OTHERS selection ===
async def topic_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, selection, thread_id = query.data.split("|")
    thread_id = int(thread_id)

    # Delete init prompt
    prompt_id = context.chat_data.pop(f"init_prompt_{thread_id}", None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=prompt_id)
        except:
            pass

    if selection == "PERF":
        context.user_data["thread_id"] = thread_id
        prompt = await query.message.chat.send_message(
            "\U0001F4C5 Please enter the *Performance Date* (e.g. 12 MAR 2025):",
            parse_mode="Markdown", message_thread_id=thread_id
        )
        pending_questions["date"] = prompt.message_id
        return DATE

    await query.message.edit_text(f"Topic marked as {selection}. No further action.")
    return ConversationHandler.END

# === Conversation steps ===
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=pending_questions.pop("date", None))
    except:
        pass
    context.user_data["date"] = update.message.text.strip()
    prompt = await update.effective_chat.send_message(
        "\U0001F4CD Please enter the *Event* name:",
        parse_mode="Markdown", message_thread_id=context.user_data["thread_id"]
    )
    pending_questions["event"] = prompt.message_id
    return EVENT

async def get_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=pending_questions.pop("event", None))
    except:
        pass
    context.user_data["event"] = update.message.text.strip()
    prompt = await update.effective_chat.send_message(
        "\U0001F4CC Please enter the *Location*:",
        parse_mode="Markdown", message_thread_id=context.user_data["thread_id"]
    )
    pending_questions["location"] = prompt.message_id
    return LOCATION

async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=pending_questions.pop("location", None))
    except:
        pass
    thread_id = context.user_data["thread_id"]
    date = context.user_data["date"]
    event = context.user_data["event"]
    location = update.message.text.strip()

    sheet = get_gspread_sheet()
    sheet.append_row([thread_id, date, event, location])

    template = (
        f"\U0001F4E2 *Performance Summary*\n\n"
        f"\U0001F4CD *Event:* {event}\n"
        f"\U0001F4C5 *Date:* {date}\n"
        f"\U0001F4CC *Location:* {location}\n\n"
        f"Stay tuned for updates and indicate interest if applicable!"
    )
    msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
    await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    return ConversationHandler.END

# === /threadid & /remind ===
async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.is_topic_message:
        await msg.reply_text(f"\U0001F9F5 This topic's thread ID is: {msg.message_thread_id}", parse_mode="Markdown")
    else:
        await msg.reply_text("\u2757 This command must be used *inside a topic*.", parse_mode="Markdown")

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.is_topic_message:
        return
    thread_id = msg.message_thread_id
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
            await msg.reply_text(template, parse_mode="Markdown")
            return
    await msg.reply_text("\u274C This thread has not been registered.", parse_mode="Markdown")

# === Setup Bot ===
app = ApplicationBuilder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|")],
    states={
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_event)],
        LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    allow_reentry=True
)

app.add_handler(CommandHandler("threadid", thread_id_command))
app.add_handler(CommandHandler("remind", remind_command))
app.add_handler(conv_handler)
app.add_handler(CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|"))
app.add_handler(MessageHandler(filters.ALL, handle_message))

print("Bot is running...")
app.run_polling()