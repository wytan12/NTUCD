import threading
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    PollAnswerHandler
)
from datetime import date, timedelta, datetime, time
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ─── CONFIG ──────────────────────────────────────────────────────────────
# BOT_TOKEN = "8185202906:AAGgqlHycUkA26NC4fOvz_K7MA1ZVG6Owtw"
ALLOWED_THREAD_ID = 4

active_poll_id = None
yes_voters = set()
ALLOWED_CHAT_ID = None  

# ─── /start ──────────────────────────────────────────────────────────────
def start(update: Update, context: CallbackContext):
    chat = update.effective_chat
    thread_id = getattr(update.effective_message, "message_thread_id", "N/A")
    update.message.reply_text(
        f"👋 Hi!"
    )
    print(f"[DEBUG] Chat ID: {chat.id}, Thread ID: {thread_id}")
    
def get_next_tuesday(today=None):
    if today is None:
        today = date.today()
    days_ahead = (1 - today.weekday() + 7) % 7  # 1 = Tuesday
    if days_ahead == 0:
        days_ahead = 7  # If today is Tuesday, get the *next* Tuesday
    return today + timedelta(days=days_ahead)

def get_next_monday_8pm(now=None):
    if now is None:
        now = datetime.now()
    
    # this_monday_8pm = datetime.combine(
    #     now - timedelta(days=now.weekday()),  # Monday of current week
    #     time(20, 0)  # 8:00 PM
    # )
    
    this_sunday = now + timedelta(days=(6 - now.weekday()))
    this_monday_8pm = datetime.combine(this_sunday, time(18, 00)) 

    # If current time is after this Monday 8PM, schedule for next Monday 8PM
    if now >= this_monday_8pm:
        next_monday_8pm = this_monday_8pm + timedelta(days=7)
    else:
        next_monday_8pm = this_monday_8pm

    return next_monday_8pm

# ─── /poll entry ────────────────────────────────────────────────────────
def poll_entry(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    if thread_id != ALLOWED_THREAD_ID:
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Command used in thread {thread_id}, deleted.")
        except Exception as e:
            print(f"[ERROR] Failed to delete message in wrong thread: {e}")
        return  

    # Check admin first
    if not is_user_admin(update, context):
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Non-admin {update.effective_user.id} tried to use /poll.")
        except Exception as e:
            print(f"[DELETE ERROR] Failed to delete command: {e}")
        return
    
    chat_id   = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    try:
        context.bot.delete_message(chat_id, update.message.message_id)
    except: pass

    keyboard = [
        [
            InlineKeyboardButton("4 h", callback_data="14400"),
            InlineKeyboardButton("8 h", callback_data="28800"),
        ],
        [
            InlineKeyboardButton("12 h", callback_data="43200"),
            InlineKeyboardButton("24 h", callback_data="86400"),
        ],
    ]
    
    msg = context.bot.send_message(
        chat_id,
        "⏳ *When should I send the reminder?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id,
    )

    context.user_data["chooser_msg_id"] = msg.message_id
    context.user_data["thread_id"]      = thread_id
    context.user_data["chat_id"]        = chat_id
    
    def auto_delete():
        try:
            context.bot.delete_message(chat_id, msg.message_id)
            print(f"[AUTO DELETE] Reminder option picker expired and removed.")
        except Exception as e:
            print(f"[AUTO DELETE ERROR] {e}")

    threading.Timer(60, auto_delete).start()

# ─── Reminder Function ───────────────────────────────────────────────────
def send_reminder(bot, chat_id, thread_id):
    next_tuesday = get_next_tuesday()
    try:
        bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"Reminder: There's training tomorrow {next_tuesday.strftime('%B %d, %Y')}. If you haven't voted yet, please do so!"
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")

# ─── Helper Function ─────────────────────────────────────────────────────
def is_user_admin(update: Update, context: CallbackContext) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    member = context.bot.get_chat_member(chat_id, user_id)
    print(f"[DEBUG] User {user_id} is member: {member.status}")
    return member.status in ['administrator', 'creator']


# ─── /poll ───────────────────────────────────────────────────────────────
def send_poll_handler(update: Update, context: CallbackContext):
    
    next_tuesday = get_next_tuesday()
    now = datetime.now()
    reminder_time = get_next_monday_8pm(now)
    delay_sec = (reminder_time - now).total_seconds()
    
    global active_poll_id, ALLOWED_CHAT_ID
    # query      = update.callback_query
    # delay_sec  = int(query.data)              # comes straight from button
    print(f"[DEBUG] Delay seconds: {delay_sec}")
    # chat_id    = context.user_data["chat_id"]
    # thread_id  = context.user_data["thread_id"]
    
    chat_id = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)
    
    if thread_id != ALLOWED_THREAD_ID:
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Command used in thread {thread_id}, deleted.")
        except Exception as e:
            print(f"[ERROR] Failed to delete message in wrong thread: {e}")
        return  

    # Check admin first
    if not is_user_admin(update, context):
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Non-admin {update.effective_user.id} tried to use /poll.")
        except Exception as e:
            print(f"[DELETE ERROR] Failed to delete command: {e}")
        return
    
    # Acknowledge button press immediately
    # query.answer()

    try:
        context.bot.delete_message(chat_id, update.message.message_id)
    except: pass
    
    if thread_id == ALLOWED_THREAD_ID:
        msg = context.bot.send_poll(
            chat_id=chat_id,
            question=f"Are you joining the training on {next_tuesday.strftime('%B %d, %Y')}?",
            options=["Yes", "No"],
            is_anonymous=False,
            message_thread_id=thread_id
        )
        active_poll_id = msg.poll.id
        ALLOWED_CHAT_ID = chat_id
        print(f"[DEBUG] Poll sent. ID: {active_poll_id}")

        # Schedule reminder 
        timer = threading.Timer(delay_sec, send_reminder, args=(context.bot, chat_id, thread_id))
        timer.start()

    else:
        update.message.reply_text("This command only works in thread 4.")

# ─── Handle Votes ────────────────────────────────────────────────────────
def handle_poll_answer(update: Update, context: CallbackContext):
    global active_poll_id, yes_voters

    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    selected_options = update.poll_answer.option_ids

    if poll_id != active_poll_id:
        return  # Ignore unrelated polls

    if 0 in selected_options:
        if user.id not in yes_voters:
            yes_voters.add(user.id)
            print(f"[DEBUG] {user.full_name} voted YES")

            # try:
            #     context.bot.send_message(
            #         chat_id=user.id,
            #         text="Thanks for confirming you'll join the training!"
            #     )
            # except Exception as e:
            #     print(f"[ERROR] Couldn't message {user.username or user.id}: {e}")

# ─── Main ────────────────────────────────────────────────────────────────
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Register handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("poll", send_poll_handler))
    dp.add_handler(PollAnswerHandler(handle_poll_answer))

    print("Bot started. Listening for updates...")
    updater.start_polling()
    updater.idle()

# ─── Entry ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    main()
