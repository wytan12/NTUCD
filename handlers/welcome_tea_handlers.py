from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    WELCOME_TEA_GROUP_CHAT_ID,
    WELCOME_TEA_STATUS_ATTEND,
    WELCOME_TEA_STATUS_NOT_CONFIRM,
    WELCOME_TEA_STATUS_REJECT,
    sg_tz,
)
from services.google_sheets import (
    append_welcome_tea_id,
    get_welcome_tea_settings,
    get_welcome_tea_recipients,
    update_welcome_tea_status,
    mark_welcome_tea_setting_sent,
)
from utils.constants import welcome_tea_join_chats, welcome_tea_pending_requests

WELCOME_TEA_MESSAGE = (
    "🎉 <b>Yay! Thank you so much for your interest in NTU Festive Drums!</b>\n\n"
    "We've got your details safely recorded, and we can't wait to meet you! 🥁 We'll be sending out more exciting details as we get closer to the event, so keep an eye out. 👀\n"
    "If you haven't yet, please take a quick moment to fill out our <a href='{signup_form_link}'>Welcome Tea Registration form</a>. 📝\n"
    "<i>(If you've already filled this in, you're all set! ✅)</i>\n\n"
    "<i>Got any questions? Don't hesitate to drop a message to our friendly NTUFD Chairperson, @ma_ning (Ma Ning), or Vice-Chairperson, @jurikawazu (Juri), right here on Telegram! We'd love to help! 💬❤️</i>"
)

WELCOME_TEA_DETAILS_TEXT = (
    "Hello there! We are so excited to see you soon! 🥁✨\n\n"
    "<b>NTU Festive Drums' Welcome Tea Session</b>\n"
    "📅 <b>Date:</b> {event_date}\n"
    "⏰ <b>Time:</b> 1830 - 2130 (GMT+8)\n"
    "📍 <b>Venue:</b> <a href='https://maps.app.goo.gl/VHDueGBZ6AyHNjdx5'>Nanyang House Foyer</a>\n"
    "👕 <b>Dress Code:</b> Comfortable & Casual! (We generally go barefoot during practice, so slippers or sandals are highly recommended—but covered shoes are totally fine too!)\n\n"
    "🍱 <b>Dinner is on us!</b> Time is allocated for eating, so no need to dabao. Feel free to come straight from class! 🏃💨\n\n"
    "<b>How to get to Nanyang House</b>\n"
    "▶️ <a href='https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link'>Red Bus (Hall 2) Video Guide</a>\n"
    "▶️ <a href='https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link'>Blue Bus (Hall 6) Video Guide</a>\n"
    "📄 <a href='https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link'>PDF Route Guide</a>\n\n"
    "<i>Please let us know if you'll be joining us by clicking one of the buttons below! 👇</i>"
)

WELCOME_TEA_REMINDER_TEXT = (
    "🥁 *Just a gentle little reminder!*\n\n"
    "Our Welcome Tea is coming up, and we'd love to know if you can make it. Please let us know by confirming the attendance—we hope to see you there! 😊✨"
)

CONFIRMED_REPLY = "Awesome! 🎉 Thanks for confirming. We'll be adding you to the Welcome Tea group chat soon, so keep an eye out for that! 🥁"
REJECTED_REPLY = (
    "Aww, what a bummer! 🥺 But thank you so much for your interest in our club! ❤️\n\n"
    "If your schedule clears up and you change your mind, the door is always open. Just drop a message to our Chairpersons @ma_ning (Ma Ning) or @jurikawazu (Juri) and we'll gladly add you in! Have a great semester ahead! ✨"
)

WELCOME_TEA_DETAILS_MESSAGE_KEY = "welcome_tea_details_messages"
WELCOME_TEA_SCHEDULER_INTERVAL_SECONDS = 30


def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="WELCOME_TEA_CONFIRM"),
            InlineKeyboardButton("❌ Reject", callback_data="WELCOME_TEA_REJECT"),
        ]
    ])


def _welcome_tea_message() -> str:
    settings = get_welcome_tea_settings()
    return WELCOME_TEA_MESSAGE.format(signup_form_link=settings["signup_form_link"])


def _welcome_tea_followup_text() -> str:
    settings = get_welcome_tea_settings()
    return (
        "🎉 <b>Thank you for coming to our Welcome Tea!</b> We really hope you had a fantastic time with us! 🥁✨\n\n"
        "Ready to make some noise and officially join the NTUFD family? 🤩\n"
        f"👉 <a href='{settings['main_group_welcome_tea_invite_link']}'>Click here to request to join our Main Group!</a>\n\n"
        "<i>⚠️ Important: After requesting to join, please check your private messages. The bot will send you a quick verification message to get you fully approved!</i>"
    )


async def handle_welcome_tea_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle QR code scans from /start welcome_tea."""
    user = update.effective_user
    user_id = user.id
    username = user.full_name or ""

    success = append_welcome_tea_id(user_id, username)

    if success:
        await update.message.reply_text(_welcome_tea_message(), parse_mode=ParseMode.HTML)
        print(f"[INFO] Welcome Tea registration successful for user {user_id} (@{username})")
    else:
        error_msg = (
            "❌ We encountered an issue saving your information. "
            "Please try again or contact an admin."
        )
        await update.message.reply_text(error_msg)
        print(f"[ERROR] Failed to register Welcome Tea user {user_id}")


async def handle_welcome_tea_join_request(join_request, context: ContextTypes.DEFAULT_TYPE):
    """Capture a Welcome Tea join request and leave it pending for Sunday."""
    user = join_request.from_user
    username = user.full_name or ""

    welcome_tea_pending_requests[user.id] = join_request
    welcome_tea_join_chats[user.id] = join_request.chat.id
    context.application.bot_data["welcome_tea_group_chat_id"] = join_request.chat.id

    success = append_welcome_tea_id(user.id, username)
    if not success:
        print(f"[WELCOME TEA][ERROR] Failed to register join request for {user.id}.")

    try:
        await context.bot.send_message(
            chat_id=getattr(join_request, "user_chat_id", None) or user.id,
            text=_welcome_tea_message(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not DM QR confirmation to {user.id}: {e}")


async def handle_welcome_tea_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist a user's Confirm/Reject button response."""
    query = update.callback_query

    if _welcome_tea_approval_closed():
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Could not remove expired response buttons: {e}")
        await query.answer(
            "Welcome Tea group admission has already been processed, so this response is closed.",
            show_alert=True,
        )
        return

    if query.data == "WELCOME_TEA_CONFIRM":
        status = WELCOME_TEA_STATUS_ATTEND
        reply = CONFIRMED_REPLY
    elif query.data == "WELCOME_TEA_REJECT":
        status = WELCOME_TEA_STATUS_REJECT
        reply = REJECTED_REPLY
    else:
        await query.answer("Unknown Welcome Tea response. Please try again.", show_alert=True)
        return

    user_id = query.from_user.id
    if not update_welcome_tea_status(user_id, status):
        await query.answer(
            "I could not find your Welcome Tea registration. Please submit the join request again.",
            show_alert=True,
        )
        return

    if status == WELCOME_TEA_STATUS_REJECT:
        await _decline_welcome_tea_join_request(context.bot, user_id, context)

    try:
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=reply,
        )
        await query.answer("Response recorded.")
        print(f"[WELCOME TEA] Sent {status} confirmation reply to {user_id}.")
    except Exception as e:
        # Join requests only grant temporary DM access. If it has expired, the
        # callback alert still confirms the response without raising an error.
        await query.answer(reply, show_alert=True)
        print(f"[WELCOME TEA][WARN] Could not DM response to {user_id}; showed callback alert instead: {e}")

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not remove response buttons for {user_id}: {e}")


async def _send_welcome_tea_details(bot, user_id: int):
    # 1. Fetch live settings from Google Sheets
    settings = get_welcome_tea_settings()
    event_date_obj = settings.get("event_date")
    
    # 2. Format the date beautifully (e.g. "18 August 2026 (Tuesday)")
    if event_date_obj:
        # %d = Day, %B = Full Month Name, %Y = Year, %A = Full Day Name
        formatted_date = event_date_obj.strftime("%d %B %Y (%A)")
        
        # Optional: Strip leading zero for single-digit days (e.g., "08" -> "8")
        if formatted_date.startswith("0"):
            formatted_date = formatted_date[1:]
    else:
        formatted_date = "TBA" # Fallback just in case the sheet cell is empty
        
    # 3. Inject the formatted date into the text template
    final_text = WELCOME_TEA_DETAILS_TEXT.format(event_date=formatted_date)
    
    return await bot.send_message(
        chat_id=user_id,
        text=final_text,
        parse_mode=ParseMode.HTML,
        reply_markup=_confirmation_keyboard(),
        disable_web_page_preview=True,
    )


async def _send_welcome_tea_reminder(bot, user_id: int):
    await bot.send_message(
        chat_id=user_id,
        text=WELCOME_TEA_REMINDER_TEXT,
        parse_mode=ParseMode.MARKDOWN,
    )


async def send_welcome_tea_details_job(context: ContextTypes.DEFAULT_TYPE):
    """Friday automation: send Welcome Tea details to Not Confirm users."""
    recipients = get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
    sent = 0
    for row in recipients:
        try:
            message = await _send_welcome_tea_details(context.bot, row["user_id"])
            _remember_welcome_tea_details_message(context, row["user_id"], message.message_id)
            sent += 1
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Details DM failed for {row['user_id']}: {e}")
    print(f"[WELCOME TEA] Details broadcast complete. Sent: {sent}")


async def send_welcome_tea_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Saturday automation: remind users still marked Not Confirm."""
    recipients = get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
    sent = 0
    for row in recipients:
        try:
            await _send_welcome_tea_reminder(context.bot, row["user_id"])
            sent += 1
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Reminder DM failed for {row['user_id']}: {e}")
    print(f"[WELCOME TEA] Reminder broadcast complete. Sent: {sent}")


async def process_welcome_tea_join_requests_job(context: ContextTypes.DEFAULT_TYPE):
    """Sunday automation: approve Attend/Not Confirm and reject Reject users."""
    await _remove_pending_confirmation_buttons(context)

    approve_targets = get_welcome_tea_recipients({
        WELCOME_TEA_STATUS_ATTEND,
        WELCOME_TEA_STATUS_NOT_CONFIRM,
    })
    reject_targets = get_welcome_tea_recipients({WELCOME_TEA_STATUS_REJECT})

    approved = 0
    declined = 0
    for row in approve_targets:
        if await _approve_welcome_tea_join_request(context.bot, row["user_id"], context):
            approved += 1
    for row in reject_targets:
        if await _decline_welcome_tea_join_request(context.bot, row["user_id"], context):
            declined += 1

    print(f"[WELCOME TEA] Join-request processing complete. Approved: {approved}, declined: {declined}")


async def send_post_welcome_tea_followup_job(context: ContextTypes.DEFAULT_TYPE):
    """Send the thank-you message and main-group link in the Welcome Tea group."""
    try:
        await context.bot.send_message(
            chat_id=WELCOME_TEA_GROUP_CHAT_ID,
            text=_welcome_tea_followup_text(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        print("[WELCOME TEA] Post-event main-group invitation sent.")
    except Exception as e:
        print(f"[WELCOME TEA][ERROR] Post-event invitation failed: {e}")


def _welcome_tea_chat_id(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    if user_id in welcome_tea_join_chats:
        return welcome_tea_join_chats[user_id]
    request = welcome_tea_pending_requests.get(user_id)
    if request:
        return request.chat.id
    return context.application.bot_data.get("welcome_tea_group_chat_id") or WELCOME_TEA_GROUP_CHAT_ID


def _remember_welcome_tea_details_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int):
    messages = context.application.bot_data.setdefault(WELCOME_TEA_DETAILS_MESSAGE_KEY, {})
    messages.setdefault(str(user_id), []).append(message_id)


async def _remove_pending_confirmation_buttons(context: ContextTypes.DEFAULT_TYPE):
    messages = context.application.bot_data.get(WELCOME_TEA_DETAILS_MESSAGE_KEY, {})
    removed = 0
    for chat_id, message_ids in list(messages.items()):
        for message_id in message_ids:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    reply_markup=None,
                )
                removed += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Could not remove expired buttons for {chat_id}/{message_id}: {e}")
    context.application.bot_data[WELCOME_TEA_DETAILS_MESSAGE_KEY] = {}
    print(f"[WELCOME TEA] Removed confirmation buttons from {removed} detail messages.")


async def _approve_welcome_tea_join_request(bot, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    request = welcome_tea_pending_requests.get(user_id)
    try:
        if request:
            await request.approve()
        else:
            await bot.approve_chat_join_request(chat_id=_welcome_tea_chat_id(user_id, context), user_id=user_id)
        welcome_tea_pending_requests.pop(user_id, None)
        welcome_tea_join_chats.pop(user_id, None)
        return True
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not approve join request for {user_id}: {e}")
        return False


async def _decline_welcome_tea_join_request(bot, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    request = welcome_tea_pending_requests.get(user_id)
    try:
        if request:
            await request.decline()
        else:
            await bot.decline_chat_join_request(chat_id=_welcome_tea_chat_id(user_id, context), user_id=user_id)
        welcome_tea_pending_requests.pop(user_id, None)
        welcome_tea_join_chats.pop(user_id, None)
        return True
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not decline join request for {user_id}: {e}")
        return False


def _welcome_tea_approval_closed() -> bool:
    approval_at = get_welcome_tea_settings()["approval_at"]
    return bool(approval_at and datetime.now(sg_tz) >= approval_at)


async def welcome_tea_scheduler_tick(context: ContextTypes.DEFAULT_TYPE):
    """Poll sheet settings and run due Welcome Tea jobs once per configured datetime."""
    settings = get_welcome_tea_settings()
    if not settings:
        return
        
    now = datetime.now(sg_tz)

    # Map the jobs to our newly formatted settings dictionary
    jobs = [
        ("details", settings.get("details_send_at"), settings.get("details_send_status"), settings.get("details_send_row"), send_welcome_tea_details_job),
        ("reminder", settings.get("reminder_send_at"), settings.get("reminder_send_status"), settings.get("reminder_send_row"), send_welcome_tea_reminder_job),
        ("approval", settings.get("approval_at"), settings.get("approval_status"), settings.get("approval_row"), process_welcome_tea_join_requests_job),
        ("followup", settings.get("followup_send_at"), settings.get("followup_send_status"), settings.get("followup_send_row"), send_post_welcome_tea_followup_job),
    ]

    for name, when, status, row_num, callback in jobs:
        # 1. 🛡️ THE BULLETPROOF CHECK: If the sheet says SENT, immediately skip!
        if status and str(status).strip().upper() == "SENT":
            continue

        # Skip if the date/time is blank or invalid in the sheet
        if when is None:
            continue 

        # 2. 🎯 TRIGGER ACTION: Has the clock passed the target time?
        if now >= when:
            print(f"[WELCOME TEA] Running sheet-driven {name} job scheduled at {when}")
            try:
                # Run the actual broadcast
                await callback(context)
                
                # 3. 📝 LOCK IT IN: Write "SENT" to the Google Sheet permanently
                if row_num:
                    mark_welcome_tea_setting_sent(row_num)
                    
            except Exception as e:
                print(f"[WELCOME TEA][ERROR] Job {name} failed: {e}")


def schedule_welcome_tea_jobs(application):
    """Register the sheet-driven Welcome Tea automation checker."""
    if not application.job_queue:
        print("[WELCOME TEA][WARN] JobQueue unavailable; Welcome Tea jobs not scheduled.")
        return

    application.job_queue.run_repeating(
        welcome_tea_scheduler_tick,
        interval=WELCOME_TEA_SCHEDULER_INTERVAL_SECONDS,
        first=0,
        name="welcome_tea_sheet_settings_checker",
    )
    print(
        "[WELCOME TEA] Sheet-driven automation checker scheduled "
        f"every {WELCOME_TEA_SCHEDULER_INTERVAL_SECONDS} seconds."
    )
