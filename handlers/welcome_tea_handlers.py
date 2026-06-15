from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    MAIN_GROUP_WELCOME_TEA_INVITE_LINK,
    WELCOME_TEA_DETAILS_DAYS_BEFORE,
    WELCOME_TEA_DETAILS_TIME,
    WELCOME_TEA_EVENT_DATE,
    WELCOME_TEA_FOLLOWUP_DAYS_AFTER,
    WELCOME_TEA_FOLLOWUP_TIME,
    WELCOME_TEA_GROUP_CHAT_ID,
    WELCOME_TEA_APPROVAL_DAYS_BEFORE,
    WELCOME_TEA_APPROVAL_TIME,
    WELCOME_TEA_REMINDER_DAYS_BEFORE,
    WELCOME_TEA_REMINDER_TIME,
    WELCOME_TEA_SIGNUP_FORM_LINK,
    WELCOME_TEA_STATUS_ATTEND,
    WELCOME_TEA_STATUS_NOT_CONFIRM,
    WELCOME_TEA_STATUS_REJECT,
    sg_tz,
)
from services.google_sheets import (
    append_welcome_tea_id,
    get_welcome_tea_recipients,
    update_welcome_tea_status,
)
from utils.constants import welcome_tea_join_chats, welcome_tea_pending_requests

WELCOME_TEA_MESSAGE = (
    "🎉 **Thank you for scanning the Welcome Tea QR code!**\n\n"
    "We've successfully recorded your information for the Welcome Tea event.\n\n"
    "We will disseminate more information nearer to the Welcome Tea event.\n\n"
    "Please fill in this Welcome Tea Registration form if you have not done so:\n"
    f"{WELCOME_TEA_SIGNUP_FORM_LINK}\n"
    "_(Please ignore this if you have already filled in the form.)_\n\n"
    "_If you have any questions, feel free to reach out to the chairpersons!_"
)

WELCOME_TEA_DETAILS_TEXT = (
    "Hi! Welcome!!🥁\n\n"
    "<b>NTU Festive Drums' Welcome Tea Session</b>\n"
    "<b>Date:</b> 18th August 2026 (Tuesday)\n"
    "<b>Time:</b> 1830 - 2130 (GMT+8)\n"
    "<b>Venue:</b> <a href='https://goo.gl/maps/7yqc3EfYNE92'>Nanyang House Foyer</a>\n"
    "<b>Dress Code:</b> Comfortable & Casual (we generally go barefoot for our practices/performances, "
    "so preferably wear slippers - but covered shoes are fine too!)\n\n"
    "🍱 <b>Dinner is provided</b> and time is allocated to eat, so no need to dabao. "
    "You can come straight from class!\n\n"
    "<b>🗺 Video Guide to Nanyang House</b>\n"
    "▶️ <b>Red Bus (Hall 2) Video Guide:</b>\n"
    "<a href='https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link'>Watch here</a>\n"
    "▶️ <b>Blue Bus (Hall 6) Video Guide:</b>\n"
    "<a href='https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link'>Watch here</a>\n"
    "📄 <b>PDF Guide to Nanyang House:</b>\n"
    "<a href='https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link'>View PDF</a>\n\n"
    "Please confirm whether you will be joining us below."
)

WELCOME_TEA_REMINDER_TEXT = (
    "🥁 Gentle reminder for Welcome Tea!\n\n"
    "Please let us know if you'll be attending. We look forward to hearing from you!"
)

CONFIRMED_REPLY = "Thank you for confirming your attendance. We will add you to the group on Sunday!"
REJECTED_REPLY = "Thank you so much for your interest, we hope to see you again!! ❤️"


def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data="WELCOME_TEA_CONFIRM"),
            InlineKeyboardButton("Reject", callback_data="WELCOME_TEA_REJECT"),
        ]
    ])


WELCOME_TEA_FOLLOWUP_TEXT = (
    "Thank you for coming to NTUFD Welcome Tea! We hope you had a great time.\n\n"
    "Ready to join us? Request to join the NTUFD main Telegram group here:\n"
    f"{MAIN_GROUP_WELCOME_TEA_INVITE_LINK}\n\n"
    "After requesting to join, check your private messages from the bot and "
    "complete /verification with your matriculation number."
)


async def handle_welcome_tea_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle QR code scans from /start welcome_tea."""
    user = update.effective_user
    user_id = user.id
    username = user.username or ""

    success = append_welcome_tea_id(user_id, username)

    if success:
        await update.message.reply_text(WELCOME_TEA_MESSAGE, parse_mode="Markdown")
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
    username = user.username or ""

    welcome_tea_pending_requests[user.id] = join_request
    welcome_tea_join_chats[user.id] = join_request.chat.id
    context.application.bot_data["welcome_tea_group_chat_id"] = join_request.chat.id

    success = append_welcome_tea_id(user.id, username)
    if not success:
        print(f"[WELCOME TEA][ERROR] Failed to register join request for {user.id}.")

    try:
        await context.bot.send_message(
            chat_id=getattr(join_request, "user_chat_id", None) or user.id,
            text=WELCOME_TEA_MESSAGE,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not DM QR confirmation to {user.id}: {e}")


async def handle_welcome_tea_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist a user's Confirm/Reject button response."""
    query = update.callback_query

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
    await bot.send_message(
        chat_id=user_id,
        text=WELCOME_TEA_DETAILS_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=_confirmation_keyboard(),
        disable_web_page_preview=True,
    )


async def _send_welcome_tea_reminder(bot, user_id: int):
    await bot.send_message(
        chat_id=user_id,
        text=WELCOME_TEA_REMINDER_TEXT,
    )


async def send_welcome_tea_details_job(context: ContextTypes.DEFAULT_TYPE):
    """Friday automation: send Welcome Tea details to Not Confirm users."""
    recipients = get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
    sent = 0
    for row in recipients:
        try:
            await _send_welcome_tea_details(context.bot, row["user_id"])
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
            text=WELCOME_TEA_FOLLOWUP_TEXT,
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


def _scheduled_datetime(days_before: int, target_time):
    target_date = WELCOME_TEA_EVENT_DATE - timedelta(days=days_before)
    combined = datetime.combine(target_date, target_time)
    if combined.tzinfo is None:
        return sg_tz.localize(combined)
    return combined.astimezone(sg_tz)


def schedule_welcome_tea_jobs(application):
    """Register one-off Welcome Tea automation jobs from config.py."""
    if not application.job_queue:
        print("[WELCOME TEA][WARN] JobQueue unavailable; Welcome Tea jobs not scheduled.")
        return

    now = datetime.now(sg_tz)
    jobs = [
        (
            "welcome_tea_details",
            _scheduled_datetime(WELCOME_TEA_DETAILS_DAYS_BEFORE, WELCOME_TEA_DETAILS_TIME),
            send_welcome_tea_details_job,
        ),
        (
            "welcome_tea_reminder",
            _scheduled_datetime(WELCOME_TEA_REMINDER_DAYS_BEFORE, WELCOME_TEA_REMINDER_TIME),
            send_welcome_tea_reminder_job,
        ),
        (
            "welcome_tea_approval",
            _scheduled_datetime(WELCOME_TEA_APPROVAL_DAYS_BEFORE, WELCOME_TEA_APPROVAL_TIME),
            process_welcome_tea_join_requests_job,
        ),
        (
            "welcome_tea_followup",
            sg_tz.localize(datetime.combine(
                WELCOME_TEA_EVENT_DATE + timedelta(days=WELCOME_TEA_FOLLOWUP_DAYS_AFTER),
                WELCOME_TEA_FOLLOWUP_TIME,
            )),
            send_post_welcome_tea_followup_job,
        ),
    ]

    for name, when, callback in jobs:
        if when <= now:
            print(f"[WELCOME TEA] Skipping {name}; scheduled time is in the past: {when}")
            continue
        application.job_queue.run_once(callback, when=when, name=name)
        print(f"[WELCOME TEA] Scheduled {name} at {when}")
