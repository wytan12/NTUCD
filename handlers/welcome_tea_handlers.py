from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    WELCOME_TEA_DETAILS_DAYS_BEFORE,
    WELCOME_TEA_DETAILS_TIME,
    WELCOME_TEA_EVENT_DATE,
    WELCOME_TEA_GROUP_INVITE_LINK,
    WELCOME_TEA_INVITE_DAYS_BEFORE,
    WELCOME_TEA_INVITE_TIME,
    WELCOME_TEA_REMINDER_DAYS_BEFORE,
    WELCOME_TEA_REMINDER_TIME,
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

WELCOME_TEA_MESSAGE = (
    "🎉 **Thank you for scanning the Welcome Tea QR code!**\n\n"
    "We've successfully recorded your information for the Welcome Tea event.\n\n"
    "We will disseminate more information nearer to the Welcome Tea event.\n\n"
    "_If you have any questions, feel free to reach out to the chairpersons!_"
)

WELCOME_TEA_DETAILS_TEXT = (
    "Hi! Welcome!!🥁\n\n"
    "<b>NTU Chinese Drums' Welcome Tea Session</b>\n"
    "<b>Date:</b> 19th August 2025 (Tuesday)\n"
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
    "Please confirm whether you will be attending so we can prepare food and group access properly."
)

CONFIRMED_REPLY = "Thank you for confirming your attendance. We will add you to the group on Sunday"
REJECTED_REPLY = "Thank you so much for your interest, we hope to see you again!! ❤️"


def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data="WELCOME_TEA_CONFIRM"),
            InlineKeyboardButton("Reject", callback_data="WELCOME_TEA_REJECT"),
        ]
    ])


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


async def handle_welcome_tea_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist a user's Confirm/Reject button response."""
    query = update.callback_query
    await query.answer()

    if query.data == "WELCOME_TEA_CONFIRM":
        status = WELCOME_TEA_STATUS_ATTEND
        reply = CONFIRMED_REPLY
    elif query.data == "WELCOME_TEA_REJECT":
        status = WELCOME_TEA_STATUS_REJECT
        reply = REJECTED_REPLY
    else:
        await query.message.reply_text("Unknown Welcome Tea response. Please try again.")
        return

    user_id = query.from_user.id
    if not update_welcome_tea_status(user_id, status):
        await query.message.reply_text(
            "I could not find your Welcome Tea registration. Please scan the QR code again."
        )
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(reply)


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


async def send_welcome_tea_invite_job(context: ContextTypes.DEFAULT_TYPE):
    """Sunday automation: send invite link to Attend and Not Confirm users."""
    if not WELCOME_TEA_GROUP_INVITE_LINK:
        print("[WELCOME TEA][WARN] Invite job skipped: WELCOME_TEA_GROUP_INVITE_LINK is empty.")
        return

    recipients = get_welcome_tea_recipients({
        WELCOME_TEA_STATUS_ATTEND,
        WELCOME_TEA_STATUS_NOT_CONFIRM,
    })
    sent = 0
    for row in recipients:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "Welcome Tea is coming up! Please join the Telegram group here:\n\n"
                    f"{WELCOME_TEA_GROUP_INVITE_LINK}"
                ),
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Invite DM failed for {row['user_id']}: {e}")
    print(f"[WELCOME TEA] Invite broadcast complete. Sent: {sent}")


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
            "welcome_tea_invite",
            _scheduled_datetime(WELCOME_TEA_INVITE_DAYS_BEFORE, WELCOME_TEA_INVITE_TIME),
            send_welcome_tea_invite_job,
        ),
    ]

    for name, when, callback in jobs:
        if when <= now:
            print(f"[WELCOME TEA] Skipping {name}; scheduled time is in the past: {when}")
            continue
        application.job_queue.run_once(callback, when=when, name=name)
        print(f"[WELCOME TEA] Scheduled {name} at {when}")
