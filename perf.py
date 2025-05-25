import threading
from datetime import date, timedelta, datetime, time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    CallbackQueryHandler,
    PollAnswerHandler,
)

# ─── CONFIG ──────────────────────────────────────────────────────────────

BOT_TOKEN = "8185202906:AAGgqlHycUkA26NC4fOvz_K7MA1ZVG6Owtw"
ALLOWED_THREAD_ID = 33

active_poll_id = None
yes_voters = set()
user_date_selections = {}

# ─── HELPERS ─────────────────────────────────────────────────────────────

def is_user_admin(update: Update, context: CallbackContext) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    member = context.bot.get_chat_member(chat_id, user_id)
    return member.status in ['administrator', 'creator']

def get_next_tuesday(today=None):
    if today is None:
        today = date.today()
    days_ahead = (1 - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)

def get_upcoming_date_buttons():
    today = date.today()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        btn_text = d.strftime("%b %d")  # e.g., "May 28"
        buttons.append(InlineKeyboardButton(btn_text, callback_data=f"date:{btn_text}"))
    return buttons

# ─── COMMAND: /pollsetup ─────────────────────────────────────────────────

def poll_setup_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    if thread_id != ALLOWED_THREAD_ID:
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Command used in thread {thread_id}, deleted.")
        except Exception as e:
            print(f"[ERROR] Failed to delete message in wrong thread: {e}")
        return  

    if not is_user_admin(update, context):
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            print(f"[BLOCKED] Non-admin {update.effective_user.id} tried to use /poll.")
        except Exception as e:
            print(f"[DELETE ERROR] Failed to delete command: {e}")
        return
    
    try:
        context.bot.delete_message(chat_id, update.message.message_id)
    except: pass

    user_date_selections[user_id] = []

    buttons = get_upcoming_date_buttons()
    rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("✅Confirm", callback_data="confirm")])

    context.bot.send_message(
        chat_id=chat_id,
        text="📅 Choose preferred training dates (you can select multiple):",
        reply_markup=InlineKeyboardMarkup(rows),
        message_thread_id=thread_id
    )

# ─── CALLBACK QUERY HANDLER ──────────────────────────────────────────────

def poll_calendar_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_date_selections:
        user_date_selections[user_id] = []

    if data.startswith("date:"):
        selected = data.split("date:")[1]
        if selected in user_date_selections[user_id]:
            user_date_selections[user_id].remove(selected)
            query.answer(f" Unselected {selected}")
        else:
            user_date_selections[user_id].append(selected)
            query.answer(f"Selected {selected}")

    elif data == "confirm":
        selections = user_date_selections.get(user_id, [])
        if len(selections) < 2:
            query.answer("⚠️Select at least 2 dates", show_alert=True)
            return

        chat_id = query.message.chat_id
        thread_id = getattr(query.message, "message_thread_id", None)
        question = "🗳 Which date do you prefer for training?"

        msg = context.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=selections,
            is_anonymous=False,
            allows_multiple_answers=True,
            message_thread_id=thread_id,
        )

        global active_poll_id
        active_poll_id = msg.poll.id
        query.edit_message_text("✅ Poll created successfully!")
        context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)

# ─── POLL VOTE TRACKER ───────────────────────────────────────────────────
user_votes = {}
def handle_poll_answer(update: Update, context: CallbackContext):
    global active_poll_id, user_votes

    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    selected_options = update.poll_answer.option_ids

    if poll_id != active_poll_id:
        return

    # Map selected option indices to actual date strings
    poll_message = update.poll_answer.poll_id  # for completeness, though poll options not exposed here
    selected_dates = [f"Option {i + 1}" for i in selected_options]  # you can improve this with a lookup if needed

    # Save the user's selected options
    user_votes[user.id] = selected_dates

# ─── /start ──────────────────────────────────────────────────────────────

def start(update: Update, context: CallbackContext):
    update.message.reply_text("👋 Use /pollsetup to create a date-selection poll!")

# ─── MAIN ────────────────────────────────────────────────────────────────

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("pollsetup", poll_setup_handler))
    dp.add_handler(CallbackQueryHandler(poll_calendar_callback))
    dp.add_handler(PollAnswerHandler(handle_poll_answer))

    print("🤖 Bot started. Listening for commands...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
