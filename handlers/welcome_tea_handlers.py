import asyncio
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import (
    WELCOME_TEA_GROUP_CHAT_ID,
    WELCOME_TEA_STATUS_ATTEND,
    WELCOME_TEA_STATUS_NOT_CONFIRM,
    WELCOME_TEA_STATUS_REJECT,
    WELCOME_TEA_STATUS_STILL_COMING,
    sg_tz,
)
from services.google_sheets import (
    append_welcome_tea_id,
    get_welcome_tea_settings,
    get_welcome_tea_recipients,
    update_welcome_tea_status,
    get_wt_user,
    update_wt_dietary,
    update_wt_attendance,
    update_wt_msg_id,
    mark_wt_user_col_sent,
    batch_update_wt_cells,
    get_wt_write_context,
)
from utils.constants import welcome_tea_join_chats, welcome_tea_pending_requests
from services.alerts import who

# ---------------------------------------------------------------------------
# Message constants
# ---------------------------------------------------------------------------

WELCOME_TEA_MESSAGE = (
    "🎉 <b>Yay! Thank you so much for your interest in NTU Festive Drums!</b>\n\n"
    "We've got your details safely recorded, and we can't wait to meet you! 🥁\n" 
    "We'll be sending out more exciting details as we get closer to the event, so keep an eye out. 👀\n"
    "If you haven't yet, please take a quick moment to fill out our <a href='{signup_form_link}'>Welcome Tea Registration form</a>. 📝\n"
    "<i>(If you've already filled this in, you're all set! ✅)</i>\n\n"
    "<i>Got any questions? Don't hesitate to drop a message to our friendly NTUFD Chairperson, @ma_ning (Ma Ning), or Vice-Chairperson, @jurikawazu (Juri), right here on Telegram! We'd love to help! 💬❤️</i>"
)

# WELCOME_TEA_DETAILS_TEXT = (
#     "Hello there! We are so excited to see you soon! 🥁✨\n\n"
#     "<b>NTU Festive Drums' Welcome Tea Session</b>\n"
#     "📅 <b>Date:</b> {event_date}\n"
#     "⏰ <b>Time:</b> 1830 - 2130 (GMT+8)\n"
#     "📍 <b>Venue:</b> <a href='https://maps.app.goo.gl/VHDueGBZ6AyHNjdx5'>Nanyang House Foyer</a>\n"
#     "👕 <b>Dress Code:</b> Comfortable & Casual! (We generally go barefoot during practice, so slippers or sandals are highly recommended—but covered shoes are totally fine too!)\n\n"
#     "🍱 <b>Dinner is on us!</b> Time is allocated for eating, so no need to dabao. Feel free to come straight from class! 🏃💨\n\n"
#     "<b>How to get to Nanyang House</b>\n"
#     "▶️ <a href='https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link'>Red Bus (Hall 2) Video Guide</a>\n"
#     "▶️ <a href='https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link'>Blue Bus (Hall 6) Video Guide</a>\n"
#     "📄 <a href='https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link'>PDF Route Guide</a>\n\n"
#     "<i>Please let us know if you'll be joining us by clicking one of the buttons below! 👇</i>"
# )

WELCOME_TEA_DETAILS_TEXT = (
    "Hello there! We are so excited to see you soon! 🥁✨\n\n"
    "<b>NTU Festive Drums' Welcome Tea Session</b>\n"
    "📅 <b>Date:</b> {event_date}\n"
    "⏰ <b>Time:</b> 1830 - 2130 (GMT+8)\n"
    "📍 <b>Venue:</b> <a href='https://maps.app.goo.gl/oM1RjKVcrUv5zCLQ8'>Nanyang Auditorium Foyer</a>\n"
    "👕 <b>Dress Code:</b> Comfortable & Casual! (We generally go barefoot during practice, so slippers or sandals are highly recommended—but covered shoes are totally fine too!)\n\n"
    "🍱 <b>Dinner is on us!</b> Time is allocated for eating, so no need to dabao. Feel free to come straight from class! 🏃💨\n\n"
    "<i>Please let us know if you'll be joining us by clicking one of the buttons below! 👇</i>"
)

WELCOME_TEA_REMINDER_TEXT = (
    "🥁 *Just a gentle little reminder!*\n\n"
    "Our Welcome Tea is coming up, and we'd love to know if you can make it. Please let us know by confirming the attendance—we hope to see you there! 😊✨"
)

PRE_CUTOFF_NUDGE_TEXT = (
    "⏰ <b>Last call! Welcome Tea is coming up very soon!</b>\n\n"
    "We noticed you haven't confirmed your attendance yet. If you're planning to join us, please let us know "
    "by tapping one of the buttons in our earlier message.\n\n"
    "We're finalising the event arrangements, so we'd appreciate your RSVP at your earliest convenience.\n\n"
    "Hope to see you there! 🥁✨"
)

CUTOFF_MSG_TEXT = (
    "⏰ <b>RSVP is now closed!</b>\n\n"
    "A big thank you to everyone who responded. We've completed our event preparations and can't wait to welcome you! 🥁✨\n\n"
    "If you've decided to join us after the RSVP deadline, we'd still be happy to have you! Just a small note: "
    "as our catering arrangements have already been finalised, please have your meal before coming, "
    "as we may not have enough refreshments for additional attendees.\n\n"
    "Let us know if you're still coming! 😊"
)

WTD_REMINDER_TEXT = (
    "🥁 <b>Welcome Tea starts tonight!</b>\n\n"
    "Just a friendly reminder — we're kicking off at <b>6:30 PM</b> at "
    "<b>Nanyang Auditorium Foyer</b>! 🎉\n\n"
    "🍱 Dinner is included — come hungry, no need to eat beforehand!\n\n"
    "See you soon! We can't wait to meet you! ❤️"
)

WTD_REMINDER_PENDING_TEXT = (
    "🥁 <b>Welcome Tea starts tonight 📣📣📣</b>\n\n"
    "Just a friendly reminder — we're kicking off at <b>6:30 PM</b> at "
    "<b>Nanyang Auditorium Foyer</b>! 🎉\n\n"
    "We noticed you haven't confirmed yet — if you're dropping by, we'd still love to see you! 😊 "
    "Please tap the <b>I'll Be There!!</b> or <b>Can't Make It</b> button in our earlier message to let us know.\n\n"
    "⚠️ <b>Heads up:</b> Please eat beforehand as we've already finalised our dinner headcount!"
)

DIETARY_QUESTION_TEXT = (
    "🍽️ <b>One quick question before we wrap up!</b>\n\n"
    "Do you have any <b>dietary restrictions</b> we should know about? "
    "(e.g. vegetarian, halal, nut allergy, etc.)\n\n"
    "<i>Reply with your dietary needs, or type <b>None</b> if you have no restrictions.</i>"
)

WELCOME_TEA_THANK_YOU_TEXT = (
    "🎉 <b>Thank you so much! We've noted your dietary preference.</b>\n\n"
    "We're so excited to have you at Welcome Tea! Keep an eye out for more updates. 🥁✨"
)

CONFIRMED_REPLY = "Awesome! 🎉 Thanks for confirming. We'll be adding you to the Welcome Tea group chat soon, so keep an eye out for that! 🥁"
REJECTED_REPLY = (
    "Aww, what a bummer! 🥺 But thank you so much for your interest in our club! ❤️\n"
    "If your schedule clears up and you change your mind, the door is always open. Just drop a message to our Chairpersons @ma_ning (Ma Ning) or @jurikawazu (Juri) and we'll gladly add you in! Have a great semester ahead! ✨"
)
STILL_COMING_REPLY = (
    "🎉 <b>Awesome! We're so excited to see you!</b>\n\n"
    "We can't wait to see you tonight! 🥁✨"
)
CANT_MAKE_IT_REPLY = (
    "No worries at all! 💙 We'll miss you, but we hope to see you at a future event! \n"
    "If you change your mind, feel free to reach out to our Chairpersons @ma_ning (Ma Ning) or @jurikawazu (Juri). ✨"
)

WELCOME_TEA_SCHEDULER_INTERVAL_SECONDS = 30
BROADCAST_SEND_DELAY_SECONDS = 0.05  # 50ms between sends → ~20 msg/sec (Telegram cap is 30/sec)

# SENT markers are flushed to the sheet every this many cells rather than once at
# the very end. A Heroku dyno restart mid-broadcast wipes anything still buffered
# in memory, and because `wt_jobs_sent` is only stamped after the job finishes,
# the scheduler would re-run the whole job and re-message everyone whose marker
# never landed. Flushing incrementally caps that exposure: at 2 cells per user,
# 40 cells ≈ 20 users, so a crash can duplicate at most ~20 messages instead of
# the entire list. Still only ~5 writes per 100 members — far under the 60/min quota.
BROADCAST_FLUSH_EVERY_CELLS = 40

# Broadcasts go out in batches with a pause between them. This is not about
# Telegram limits — it staggers when members RECEIVE the message, which staggers
# when they tap Confirm. Each Confirm costs ~4 Google reads and the quota is 60
# reads/min, so 100 people all receiving at once (and ~15 tapping in the same
# minute) would breach it. Spread over 20 minutes, the taps stay well under.
BROADCAST_BATCH_SIZE = 20
BROADCAST_BATCH_PAUSE_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="WELCOME_TEA_CONFIRM"),
            InlineKeyboardButton("❌ Reject", callback_data="WELCOME_TEA_REJECT"),
        ]
    ])


def _still_coming_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎉 I'll Be There!!", callback_data="WELCOME_TEA_STILL_COMING"),
            InlineKeyboardButton("😔 Can't Make It", callback_data="WELCOME_TEA_CANT_MAKE_IT"),
        ]
    ])


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _welcome_tea_message() -> str:
    settings = get_welcome_tea_settings()
    return WELCOME_TEA_MESSAGE.format(signup_form_link=settings.get("signup_form_link", ""))


def _welcome_tea_followup_text() -> str:
    settings = get_welcome_tea_settings()
    return (
        "🎉 <b>Thank you for coming to our Welcome Tea!</b> We really hope you had a fantastic time with us! 🥁✨\n\n"
        "Ready to make some noise and officially join the NTUFD family? 🤩\n"
        f"👉 <a href='{settings.get('main_group_welcome_tea_invite_link', '')}'>Click here to request to join our Main Group!</a>\n\n"
        "<i>⚠️ Important: After requesting to join, the bot will send you a quick verification message to get you fully approved!</i>"
    )


def _format_event_date(event_date_obj) -> str:
    if not event_date_obj:
        return "TBA"
    formatted = event_date_obj.strftime("%d %B %Y (%A)")
    return formatted.lstrip("0")


# ---------------------------------------------------------------------------
# Window detection
# ---------------------------------------------------------------------------

def _detect_wt_window(now: datetime, settings: dict) -> str:
    """Return the join window name based on current time vs schedule datetimes."""
    details_at = settings.get("details_send_at")
    reminder_at = settings.get("reminder_send_at")
    cutoff_at = settings.get("cutoff_at")
    final_cleanup_at = settings.get("final_cleanup_at")

    if not details_at:
        return "before_details"
    if now < details_at:
        return "before_details"
    if reminder_at and now < reminder_at:
        return "W1"
    if cutoff_at and now < cutoff_at:
        return "W2_W3"
    if final_cleanup_at and now < final_cleanup_at:
        return "W4"
    return "after"


# ---------------------------------------------------------------------------
# Core send helpers
# ---------------------------------------------------------------------------

async def _send_welcome_tea_details(bot, user_id: int):
    settings = get_welcome_tea_settings()
    final_text = WELCOME_TEA_DETAILS_TEXT.format(
        event_date=_format_event_date(settings.get("event_date"))
    )
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


def _welcome_tea_chat_id(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    if user_id in welcome_tea_join_chats:
        return welcome_tea_join_chats[user_id]
    request = welcome_tea_pending_requests.get(user_id)
    if request:
        return request.chat.id
    return context.application.bot_data.get("welcome_tea_group_chat_id") or WELCOME_TEA_GROUP_CHAT_ID


# ---------------------------------------------------------------------------
# Join request — window-aware catch-up
# ---------------------------------------------------------------------------

async def handle_welcome_tea_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle QR code scans from /start welcome_tea."""
    user = update.effective_user
    success = append_welcome_tea_id(user.id, user.full_name or "")
    if success:
        await update.message.reply_text(_welcome_tea_message(), parse_mode=ParseMode.HTML)
        print(f"[INFO] Welcome Tea registration successful for user {user.id} (@{user.username})")
    else:
        await update.message.reply_text(
            "❌ We encountered an issue saving your information. Please try again or contact an admin."
        )
        print(f"[ERROR] Failed to register Welcome Tea user {user.id}")


async def handle_welcome_tea_join_request(join_request, context: ContextTypes.DEFAULT_TYPE):
    """Capture a Welcome Tea join request and send window-appropriate catch-up messages."""
    user = join_request.from_user
    user_id = user.id
    username = user.full_name or ""

    welcome_tea_pending_requests[user_id] = join_request
    welcome_tea_join_chats[user_id] = join_request.chat.id
    context.application.bot_data["welcome_tea_group_chat_id"] = join_request.chat.id

    success = append_welcome_tea_id(user_id, username)
    if not success:
        print(f"[WELCOME TEA][ERROR] Failed to register join request for {user_id}.")

    # If the user already checked in via /attd (F=1), approve immediately and skip
    # all DMs — they came through the event-day flow and just need to enter the group.
    row = get_wt_user(user_id)
    if row and str(row.get("attendance", "")).strip() == "1":
        try:
            await join_request.approve()
            print(f"[WELCOME TEA] Auto-approved attendee {user_id} via RTJ link.")
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Auto-approve for attendee {user_id} failed: {e}")
        welcome_tea_pending_requests.pop(user_id, None)
        welcome_tea_join_chats.pop(user_id, None)
        return

    settings = get_welcome_tea_settings()
    now = datetime.now(sg_tz)
    window = _detect_wt_window(now, settings)

    dm_chat = getattr(join_request, "user_chat_id", None) or user_id

    # Always send Yay!! message
    try:
        await context.bot.send_message(
            chat_id=dm_chat,
            text=_welcome_tea_message(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not DM Yay!! to {user_id}: {e}")

    if window in ("W1", "W2_W3"):
        # Send Details + Accept/Reject buttons; store cutoff_msg_id in col G
        try:
            msg = await _send_welcome_tea_details(context.bot, dm_chat)
            update_wt_msg_id(user_id, 7, msg.message_id)  # G = Cutoff Msg ID
            mark_wt_user_col_sent(user_id, 9)              # I = Details Sent
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Details DM failed for {user_id}: {e}")

        if window == "W2_W3":
            # Late joiner catch-up: also send reminder
            try:
                await _send_welcome_tea_reminder(context.bot, dm_chat)
                mark_wt_user_col_sent(user_id, 10)          # J = Reminder Sent
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Reminder catch-up failed for {user_id}: {e}")

    elif window == "W4":
        # Cutoff already passed — send "I'll Be There / Can't Make It" DM
        try:
            msg = await context.bot.send_message(
                chat_id=dm_chat,
                text=CUTOFF_MSG_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=_still_coming_keyboard(),
            )
            update_wt_msg_id(user_id, 8, msg.message_id)  # H = Final Cutoff Msg ID
            mark_wt_user_col_sent(user_id, 13)             # M = Cutoff Sent
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Cutoff DM failed for {user_id}: {e}")


# ---------------------------------------------------------------------------
# Button callbacks — Confirm / Reject (W1–W3 buttons)
# ---------------------------------------------------------------------------

async def handle_welcome_tea_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ✅ Confirm / ❌ Reject button from the Details message."""
    query = update.callback_query
    user_id = query.from_user.id

    if query.data == "WELCOME_TEA_CONFIRM":
        status = WELCOME_TEA_STATUS_ATTEND
    elif query.data == "WELCOME_TEA_REJECT":
        status = WELCOME_TEA_STATUS_REJECT
    else:
        await query.answer("Unknown response. Please try again.", show_alert=True)
        return

    if not update_welcome_tea_status(user_id, status):
        await query.answer(
            "I could not find your Welcome Tea registration. Please submit the join request again.",
            show_alert=True,
        )
        return

    # Strip buttons from this message
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not remove buttons for {user_id}: {e}")

    if status == WELCOME_TEA_STATUS_REJECT:
        await _decline_welcome_tea_join_request(context.bot, user_id, context)
        try:
            await context.bot.send_message(chat_id=query.message.chat.id, text=REJECTED_REPLY)
            await query.answer("Response recorded.")
        except Exception as e:
            print(f"[WARN] welcome tea: reject reply fell back to popup - {who(update)}: {e}")
            await query.answer(REJECTED_REPLY, show_alert=True)
        print(f"[WELCOME TEA] {user_id} rejected.")
        return

    # Status == Attend: check if approval time has already passed → immediate approve
    settings = get_welcome_tea_settings()
    approval_at = settings.get("approval_at")
    now = datetime.now(sg_tz)
    if approval_at and now >= approval_at:
        approved = await _approve_welcome_tea_join_request(context.bot, user_id, context)
        if approved:
            mark_wt_user_col_sent(user_id, 11)  # K = Approval Sent

    # Send brief confirmation reply
    try:
        await context.bot.send_message(chat_id=query.message.chat.id, text=CONFIRMED_REPLY)
        await query.answer("Response recorded.")
    except Exception as e:
        print(f"[WARN] welcome tea: confirm reply fell back to popup - {who(update)}: {e}")
        await query.answer(CONFIRMED_REPLY, show_alert=True)

    # Trigger dietary question flow
    pending_dietary = context.application.bot_data.setdefault("wt_pending_dietary", {})
    existing_row = get_wt_user(user_id)
    if not (existing_row and existing_row.get("dietary")):
        pending_dietary[str(user_id)] = True
        try:
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=DIETARY_QUESTION_TEXT,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Could not ask dietary to {user_id}: {e}")

    print(f"[WELCOME TEA] {user_id} confirmed (Attend).")


# ---------------------------------------------------------------------------
# Button callbacks — I'll Be There / Can't Make It (W4 buttons)
# ---------------------------------------------------------------------------

async def handle_welcome_tea_still_coming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 🎉 I'll Be There!! button from the cutoff message."""
    query = update.callback_query
    user_id = query.from_user.id

    update_welcome_tea_status(user_id, WELCOME_TEA_STATUS_STILL_COMING)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not strip Still Coming buttons for {user_id}: {e}")

    try:
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=STILL_COMING_REPLY,
            parse_mode=ParseMode.HTML,
        )
        await query.answer("Response recorded.")
    except Exception as e:
        print(f"[WARN] welcome tea: still-coming reply fell back to popup - {who(update)}: {e}")
        await query.answer(STILL_COMING_REPLY, show_alert=True)

    print(f"[WELCOME TEA] {user_id} marked STILL_COMING.")


async def handle_welcome_tea_cant_make_it(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 😔 Can't Make It button from the cutoff message."""
    query = update.callback_query
    user_id = query.from_user.id

    update_welcome_tea_status(user_id, WELCOME_TEA_STATUS_REJECT)
    await _decline_welcome_tea_join_request(context.bot, user_id, context)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not strip Can't Make It buttons for {user_id}: {e}")

    try:
        await context.bot.send_message(chat_id=query.message.chat.id, text=CANT_MAKE_IT_REPLY)
        await query.answer("Response recorded.")
    except Exception as e:
        print(f"[WARN] welcome tea: can't-make-it reply fell back to popup - {who(update)}: {e}")
        await query.answer(CANT_MAKE_IT_REPLY, show_alert=True)

    print(f"[WELCOME TEA] {user_id} can't make it — declined.")


# ---------------------------------------------------------------------------
# Dietary reply handler (non-admin private message intercept)
# ---------------------------------------------------------------------------

async def handle_dietary_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercept dietary reply from WT members before the admin gate."""
    if not update.effective_message or not update.effective_message.text:
        return
    user_id = update.effective_user.id
    pending = context.application.bot_data.get("wt_pending_dietary", {})
    if str(user_id) not in pending:
        return  # Not in dietary capture mode — let other handlers run

    dietary = update.effective_message.text.strip()
    del pending[str(user_id)]
    context.application.bot_data["wt_pending_dietary"] = pending

    update_wt_dietary(user_id, dietary)

    try:
        await update.effective_message.reply_text(
            WELCOME_TEA_THANK_YOU_TEXT, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not send thank you to {user_id}: {e}")

    print(f"[WELCOME TEA] Dietary recorded for {user_id}: {dietary}")
    raise ApplicationHandlerStop  # Prevent admin gate from processing this message


# ---------------------------------------------------------------------------
# /attd WT day check-in (non-admin)
# ---------------------------------------------------------------------------

async def handle_attd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome Tea day /attd check-in for non-admin members."""
    from services.google_sheets import is_dashboard_admin

    user = update.effective_user
    user_id = user.id

    if is_dashboard_admin(user_id):
        await update.message.reply_text(
            "Use /start to open the admin dashboard for attendance management."
        )
        return

    settings = get_welcome_tea_settings()
    event_date = settings.get("event_date")
    if not event_date:
        return

    today = datetime.now(sg_tz).date()
    if today != event_date:
        return

    wt_link = settings.get("welcome_tea_join_request_link", "")
    row = get_wt_user(user_id)

    if row is None:
        # Walk-in not in the sheet at all — register and send direct join link
        append_welcome_tea_id(user_id, user.full_name or "", status="I'll still be Coming")
        update_wt_attendance(user_id)
        await update.message.reply_text(
            f"✅ Attendance lodged! Welcome, and enjoy the night! 🥁🎉\n\n"
            f"Please join our Welcome Tea group chat, tap the link below:\n{wt_link}"
        )
        return

    if row.get("attendance") == "1":
        await update.message.reply_text(
            "Your attendance has already been recorded! Enjoy the night! 🥁🎉"
        )
        return

    status = row.get("status", "")
    update_wt_attendance(user_id)

    if status in ("Confirm", "I'll still be Coming", "Last Min CMI"):
        await update.message.reply_text(
            "✅ Attendance lodged! Welcome, and enjoy the night! 🥁🎉"
        )

    elif status in ("Reject", "Waiting for reply"):
        update_welcome_tea_status(user_id, "I'll still be Coming")
        approved = await _approve_welcome_tea_join_request(context.bot, user_id, context)
        if approved:
            await update.message.reply_text(
                "✅ Attendance lodged! Welcome, and enjoy the night! 🥁🎉"
            )
        else:
            # No pending request (kicked earlier or never submitted) — send direct join link
            await update.message.reply_text(
                f"✅ Attendance lodged! Welcome!\n\n"
                f"Please join our Welcome Tea group chat, tap the link below:\n{wt_link}"
            )

    else:
        # Not Confirm or any unknown status — shouldn't occur but handle gracefully
        await update.message.reply_text(
            "✅ Attendance lodged! Welcome, and enjoy the night! 🥁🎉"
        )


# ---------------------------------------------------------------------------
# Scheduled job functions
# ---------------------------------------------------------------------------

def _flush_writes(writes, ctx, force=False):
    """Push buffered SENT markers to the sheet, in place.

    Called after every send; only actually writes once `BROADCAST_FLUSH_EVERY_CELLS`
    have accumulated, or when `force=True` (end of job). Clears `writes` on success
    so each cell is written exactly once. See BROADCAST_FLUSH_EVERY_CELLS for why
    this is incremental rather than a single flush at the end.
    """
    if not writes:
        return
    if not force and len(writes) < BROADCAST_FLUSH_EVERY_CELLS:
        return
    batch_update_wt_cells(list(writes), ctx=ctx)
    writes.clear()


async def _pace_batch(index, total, writes, ctx):
    """After each full batch — except the last — save markers and pause.

    `index` is the 0-based position in the recipient list, `total` its length.
    Flushing BEFORE the sleep is deliberate: a restart during a 5-minute pause
    would otherwise lose that batch's markers and re-message everyone in it.
    """
    done = index + 1
    if done % BROADCAST_BATCH_SIZE == 0 and done < total:
        _flush_writes(writes, ctx, force=True)
        print(f"[WELCOME TEA] {done}/{total} sent — pausing "
              f"{BROADCAST_BATCH_PAUSE_SECONDS}s before the next batch.")
        await asyncio.sleep(BROADCAST_BATCH_PAUSE_SECONDS)


async def _paced_send(make_coro):
    """Run a Telegram API call with rate-limit protection.

    Accepts a zero-argument callable (lambda) that returns a fresh coroutine each
    time it is called — this is required for retry to work correctly, because a
    coroutine that already raised an exception cannot be awaited a second time.

    Adds a 50 ms pause after every call (~20 msg/sec, well under Telegram's 30/sec cap).
    On a 429 RetryAfter response, waits the required seconds then retries once.

    Usage:
        await _paced_send(lambda: bot.send_message(chat_id=uid, text="hi"))
    """
    try:
        result = await make_coro()
        await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)
        return result
    except RetryAfter as e:
        wait = e.retry_after + 0.5
        print(f"[WELCOME TEA][RATE LIMIT] 429 — waiting {wait}s before retry")
        await asyncio.sleep(wait)
        result = await make_coro()  # fresh coroutine from the callable
        await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)
        return result


async def send_welcome_tea_details_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled: send Details + Accept/Reject to all Not Confirm users."""
    pending = [r for r in get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
               if r.get("details_sent") != "SENT"]
    sent = 0
    writes = []
    ctx = get_wt_write_context()  # 1 read, reused by every flush below
    try:
        for i, row in enumerate(pending):
            user_id = row["user_id"]
            try:
                msg = await _paced_send(lambda: _send_welcome_tea_details(context.bot, user_id))
                # Only queued after a successful send, so a failed DM is never marked SENT.
                writes.append((user_id, 7, msg.message_id))  # G = Cutoff Msg ID
                writes.append((user_id, 9, "SENT"))          # I = Details Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Details DM failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(pending), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)  # always flush the tail
    print(f"[WELCOME TEA] Details broadcast complete. Sent: {sent}")


async def send_welcome_tea_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled: send Reminder to all Not Confirm users."""
    pending = [r for r in get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
               if r.get("reminder_sent") != "SENT"]
    sent = 0
    writes = []
    ctx = get_wt_write_context()
    try:
        for i, row in enumerate(pending):
            user_id = row["user_id"]
            try:
                await _paced_send(lambda: _send_welcome_tea_reminder(context.bot, user_id))
                writes.append((user_id, 10, "SENT"))  # J = Reminder Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Reminder DM failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(pending), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)
    print(f"[WELCOME TEA] Reminder broadcast complete. Sent: {sent}")


async def process_welcome_tea_join_requests_job(context: ContextTypes.DEFAULT_TYPE):
    """Approval time: bulk-approve Attend users into WT group. Reject users are declined."""
    approve_targets = get_welcome_tea_recipients({WELCOME_TEA_STATUS_ATTEND})
    reject_targets = get_welcome_tea_recipients({WELCOME_TEA_STATUS_REJECT})

    approve_pending = [r for r in approve_targets if r.get("approval_sent") != "SENT"]

    approved = declined = 0
    writes = []
    ctx = get_wt_write_context()
    try:
        for i, row in enumerate(approve_pending):
            if await _approve_welcome_tea_join_request(context.bot, row["user_id"], context):
                writes.append((row["user_id"], 11, "SENT"))  # K = Approval Sent
                approved += 1
            await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)  # approve/decline are API calls too
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(approve_pending), writes, ctx)
        for row in reject_targets:
            if await _decline_welcome_tea_join_request(context.bot, row["user_id"], context):
                declined += 1
            await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)
    finally:
        _flush_writes(writes, ctx, force=True)

    print(f"[WELCOME TEA] Approval job complete. Approved: {approved}, Declined: {declined}")


async def send_pre_cutoff_nudge_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled: pre-cutoff nudge DM to all Not Confirm users."""
    pending = [r for r in get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
               if r.get("pre_cutoff_sent") != "SENT"]
    sent = 0
    writes = []
    ctx = get_wt_write_context()
    try:
        for i, row in enumerate(pending):
            user_id = row["user_id"]
            try:
                await _paced_send(lambda: context.bot.send_message(
                    chat_id=user_id,
                    text=PRE_CUTOFF_NUDGE_TEXT,
                    parse_mode=ParseMode.HTML,
                ))
                writes.append((user_id, 12, "SENT"))  # L = Pre-Cutoff Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Pre-cutoff nudge failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(pending), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)
    print(f"[WELCOME TEA] Pre-cutoff nudge complete. Sent: {sent}")


async def send_cutoff_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled: strip Accept/Reject buttons, send I'll Be There/Can't Make It DM."""
    pending = [r for r in get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
               if r.get("cutoff_sent") != "SENT"]
    sent = 0
    writes = []
    ctx = get_wt_write_context()
    try:
        for i, row in enumerate(pending):
            user_id = row["user_id"]

            # Strip existing details buttons via stored Cutoff Msg ID (col G)
            cutoff_msg_id = row.get("cutoff_msg_id")
            if cutoff_msg_id:
                try:
                    await _paced_send(lambda: context.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=int(cutoff_msg_id),
                        reply_markup=None,
                    ))
                except Exception as e:
                    print(f"[WELCOME TEA][WARN] Could not strip detail buttons for {user_id}: {e}")

            # Send cutoff DM with I'll Be There/Can't Make It buttons
            try:
                msg = await _paced_send(lambda: context.bot.send_message(
                    chat_id=user_id,
                    text=CUTOFF_MSG_TEXT,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_still_coming_keyboard(),
                ))
                writes.append((user_id, 8, msg.message_id))  # H = Final Cutoff Msg ID
                writes.append((user_id, 13, "SENT"))         # M = Cutoff Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Cutoff DM failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(pending), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)

    print(f"[WELCOME TEA] Cutoff broadcast complete. Sent: {sent}")


async def send_wtd_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled WTD reminder: confirmed users get dinner-included text; pending users get eat-beforehand text."""
    confirmed = get_welcome_tea_recipients({WELCOME_TEA_STATUS_ATTEND, WELCOME_TEA_STATUS_STILL_COMING})
    pending = get_welcome_tea_recipients({WELCOME_TEA_STATUS_NOT_CONFIRM})
    sent = 0

    targets = [(r, WTD_REMINDER_TEXT) for r in confirmed]
    targets += [(r, WTD_REMINDER_PENDING_TEXT) for r in pending]
    targets = [(r, t) for r, t in targets if r.get("wtd_reminder_sent") != "SENT"]

    writes = []
    ctx = get_wt_write_context()
    try:
        for i, (row, text) in enumerate(targets):
            user_id = row["user_id"]
            try:
                await _paced_send(lambda uid=user_id, t=text: context.bot.send_message(
                    chat_id=uid,
                    text=t,
                    parse_mode=ParseMode.HTML,
                ))
                writes.append((user_id, 14, "SENT"))  # N = WTD Reminder Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] WTD reminder failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(targets), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)
    print(f"[WELCOME TEA] WTD reminder complete. Sent: {sent}")


async def final_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled 7:30 pm: silently strip I'll Be There/Can't Make It buttons."""
    recipients = get_welcome_tea_recipients({
        WELCOME_TEA_STATUS_NOT_CONFIRM,
        WELCOME_TEA_STATUS_STILL_COMING,
    })
    stripped = 0
    for row in recipients:
        user_id = row["user_id"]
        final_cutoff_msg_id = row.get("final_cutoff_msg_id")
        if not final_cutoff_msg_id:
            continue
        try:
            await _paced_send(lambda: context.bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=int(final_cutoff_msg_id),
                reply_markup=None,
            ))
            stripped += 1
        except Exception as e:
            print(f"[WELCOME TEA][WARN] Could not strip final buttons for {user_id}: {e}")
    print(f"[WELCOME TEA] Final cleanup complete. Stripped: {stripped}")


async def send_post_welcome_tea_followup_job(context: ContextTypes.DEFAULT_TYPE):
    """DM the thank-you + main-group invite to everyone who checked in.

    Targets only members with Attendance = "1" (col F), i.e. those who ran /attd
    on event day — not merely those who RSVP'd. Sent per-person rather than as a
    group post so delivery can be tracked in the Followup Sent column (O), which
    also makes the job safely re-runnable.
    """
    checked_in = [r for r in get_welcome_tea_recipients()
                  if str(r.get("attendance", "")).strip() == "1"]
    pending = [r for r in checked_in if r.get("followup_sent") != "SENT"]
    text = _welcome_tea_followup_text()
    sent = 0
    writes = []
    ctx = get_wt_write_context()
    try:
        for i, row in enumerate(pending):
            user_id = row["user_id"]
            try:
                await _paced_send(lambda uid=user_id: context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ))
                writes.append((user_id, 15, "SENT"))  # O = Followup Sent
                sent += 1
            except Exception as e:
                print(f"[WELCOME TEA][WARN] Follow-up DM failed for {user_id}: {e}")
            _flush_writes(writes, ctx)
            await _pace_batch(i, len(pending), writes, ctx)
    finally:
        _flush_writes(writes, ctx, force=True)
    print(f"[WELCOME TEA] Follow-up complete. Checked in: {len(checked_in)}, sent: {sent}")


# ---------------------------------------------------------------------------
# Join-request / approval helpers
# ---------------------------------------------------------------------------

async def _approve_welcome_tea_join_request(bot, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    request = welcome_tea_pending_requests.get(user_id)
    try:
        if request:
            await request.approve()
        else:
            await bot.approve_chat_join_request(
                chat_id=_welcome_tea_chat_id(user_id, context), user_id=user_id
            )
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
            await bot.decline_chat_join_request(
                chat_id=_welcome_tea_chat_id(user_id, context), user_id=user_id
            )
        welcome_tea_pending_requests.pop(user_id, None)
        welcome_tea_join_chats.pop(user_id, None)
        return True
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Could not decline join request for {user_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Scheduler tick — polls GSheet config and fires due jobs once
# ---------------------------------------------------------------------------

async def welcome_tea_scheduler_tick(context: ContextTypes.DEFAULT_TYPE):
    """Poll sheet settings every 30 s and run each WT job exactly once when due."""
    settings = get_welcome_tea_settings()
    if not settings:
        return

    now = datetime.now(sg_tz)
    bot_data = context.application.bot_data
    jobs_sent = bot_data.setdefault("wt_jobs_sent", {})

    # Reset job tracking when event_date changes (new WT cycle)
    event_date = settings.get("event_date")
    last_event_date = bot_data.get("wt_last_event_date")
    if event_date and str(event_date) != str(last_event_date):
        jobs_sent.clear()
        bot_data["wt_last_event_date"] = str(event_date)
        print(f"[WELCOME TEA] New event date {event_date} detected — job tracking reset.")

    jobs = [
        ("details",       settings.get("details_send_at"),  send_welcome_tea_details_job),
        ("reminder",      settings.get("reminder_send_at"), send_welcome_tea_reminder_job),
        ("approval",      settings.get("approval_at"),      process_welcome_tea_join_requests_job),
        ("pre_cutoff",    settings.get("pre_cutoff_at"),    send_pre_cutoff_nudge_job),
        ("cutoff",        settings.get("cutoff_at"),        send_cutoff_job),
        ("wtd_reminder",  settings.get("wtd_reminder_at"),  send_wtd_reminder_job),
        ("followup",      settings.get("followup_send_at"), send_post_welcome_tea_followup_job),
        ("final_cleanup", settings.get("final_cleanup_at"), final_cleanup_job),
    ]

    for name, when, callback in jobs:
        if when is None:
            continue
        # Track by exact scheduled datetime so changing GSheet times reruns the job
        if jobs_sent.get(name) == str(when):
            continue
        if now >= when:
            print(f"[WELCOME TEA] Running '{name}' job scheduled at {when}")
            try:
                await callback(context)
                jobs_sent[name] = str(when)
            except Exception as e:
                print(f"[WELCOME TEA][ERROR] Job '{name}' failed: {e}")


def schedule_welcome_tea_jobs(application):
    """Register the 30-second WT automation heartbeat."""
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
