import os
from telegram import Update
from telegram.constants import ParseMode # 🧠 FIX: Explicit import added to prevent silent compiler lockups
from telegram.ext import ContextTypes, ConversationHandler
from services.google_sheets import (
    matric_valid, 
    update_user_id_in_sheet
)   
from utils.constants import ASK_MATRIC, pending_users
from config import (
    JOIN_CONTACT_ADMIN_ID,
    WELCOME_TEA_GROUP_CHAT_ID,
    WELCOME_TEA_JOIN_REQUEST_LINK,
    WELCOME_TEA_LINK_KEYWORD,
)
from handlers.welcome_tea_handlers import handle_welcome_tea_join_request


def _contact_admin_id() -> int:
    """Role-driven contact admin (chairperson → secretary → sde), with the
    config JOIN_CONTACT_ADMIN_ID as last resort."""
    from services.google_sheets import get_join_contact_admin_id
    return get_join_contact_admin_id() or JOIN_CONTACT_ADMIN_ID


async def _send_welcome_tea_flow(user, context):
    """Welcome Tea recruitment flow: DM the event details + registration form
    and leave the request pending until the user passes /verify (matric check)."""
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
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"🌸 To make our Welcome Tea run smoothly, we’d love it if you could fill in the sign-up form below 📝💛\n"
                f"<a href='https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header'>Registration Form</a>\n"
                f"(you can ignore this if you’ve already done so ✔️)\n\n"
                f"✅ After attending, send /verify here with your matric number to get approved.\n\n"
                f"💬 If you have any queries, feel free to contact "
                f"<a href='tg://user?id={_contact_admin_id()}'>our admin</a>.\n\n"
                f"👋 See you there! 🎉"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print("Could not message user:", e)


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route group join requests to one of two onboarding flows, by invite link:

    • Invite link named with "tea" (WELCOME_TEA_LINK_KEYWORD) → **Welcome Tea
      recruitment flow**: DM the event details + form; request stays pending
      until the user passes /verify (matric check).
    • Any other link (the unique returning-member link) → **Tele-ID gate**
      against MEMBER INFO AY25/26: found → auto-approve (+ MEMBER INFO AY26/27
      stamped); not found → waiting room + contact-admin DM + admin alert.
    """
    from services.google_sheets import is_member_in_ay2526

    user = update.chat_join_request.from_user
    link = update.chat_join_request.invite_link
    link_name = (link.name or "") if link else ""
    link_url = (link.invite_link or "") if link else ""
    request_chat_id = update.chat_join_request.chat.id
    print(f"Join request received from {user.first_name} ({user.id}) via link '{link_name}'")

    # --- Flow 1: Welcome Tea recruitment link ---
    is_welcome_tea_request = (
        request_chat_id == WELCOME_TEA_GROUP_CHAT_ID
        or link_url == WELCOME_TEA_JOIN_REQUEST_LINK
        or WELCOME_TEA_LINK_KEYWORD in link_name.lower()
    )
    if is_welcome_tea_request:
        await handle_welcome_tea_join_request(update.chat_join_request, context)
        return

    # --- Flow 2: returning-member link (Tele-ID gate) ---

    if is_member_in_ay2526(user.id):
        # DM BEFORE approving: the join-request window (which lets the bot
        # message a user who never started it) closes the moment the request
        # is processed — so a welcome sent after approve() could never arrive.
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"🎉 *Welcome back, {user.first_name}!* 🎉\n\n"
                    f"✅ You're recognised from last year's roster — straight through the door, "
                    f"no queue for you! 😎\n\n"
                    f"🥁 The drums missed you. See you at training! 🔥"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"[JOIN][WARN] Welcome DM failed for {user.id}: {e}")
        try:
            await update.chat_join_request.approve()
            print(f"[JOIN] Auto-approved {user.full_name} ({user.id}) — found in MEMBER INFO AY25/26.")
        except Exception as e:
            print(f"[JOIN][ERROR] Auto-approve failed for {user.id}: {e}")
            return
        # Stamp MEMBER INFO AY26/27 right away (Status=Join, Join Date=today,
        # Leave Date cleared). The subsequent chat_member join event re-stamps
        # the same values, so this is a safe belt-and-braces double-write.
        try:
            from services.google_sheets import update_member_join_in_info
            update_member_join_in_info(user.id, user.full_name)
        except Exception as e:
            print(f"[JOIN][WARN] Join stamp failed for {user.id}: {e}")
        return

    # Unknown Tele ID → keep the request pending (Telegram's "waiting room")
    # and point them to the admin. NOTE: a join request opens a ~5-minute
    # window in which the bot may DM the requester even if they never started
    # it — we send immediately, so this reaches them.
    pending_users[user.id] = update.chat_join_request
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"Hey {user.first_name}! 🥁👋\n\n"
                "🎫 You're in the <b>waiting room</b> — your join request is pending! ⏳\n\n"
                f"👉 Give <a href='tg://user?id={_contact_admin_id()}'>our admin</a> a shout "
                "and we'll wave you through. 😄✨"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print("Could not message user:", e)

    # Proactively alert the MAIN admins (chairperson / vice chair / secretary /
    # SDE) that someone is in the waiting room, so approval doesn't depend on
    # the user reaching out first. Secondary admins are not alerted.
    from services.google_sheets import get_alert_admin_ids
    handle = f"@{user.username}" if user.username else "no username"
    alert_text = (
        "🔔 <b>Ding dong! Someone's at the door</b> 🚪👀\n\n"
        f"👤 <a href='tg://user?id={user.id}'>{user.full_name}</a> ({handle})\n"
        f"🆔 Tele ID: <code>{user.id}</code>\n\n"
        "🔍 Not found in MEMBER INFO AY25/26 — they're chilling in the waiting "
        "room. ⏳\n👉 Approve or decline from the group's <b>Join Requests</b> panel."
    )
    for admin_id in (get_alert_admin_ids() or {_contact_admin_id()}):
        try:
            await context.bot.send_message(chat_id=admin_id, text=alert_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"[JOIN][WARN] Could not alert admin {admin_id}: {e}")

async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the /verify conversation — only works in DM.

    Rejects users who have no pending join request to prevent misuse.
    Transitions to ASK_MATRIC state on success.
    """
    user_id = update.message.from_user.id

    if user_id not in pending_users:
        await update.message.reply_text(
            "🤔 Hmm, I don't see a pending join request for you — either it's already "
            "approved 🎉 or you haven't requested to join yet.\n\n"
            "👉 Tap the group invite link first, then come back here!\n\n"
            f"💬 Stuck? <a href='tg://user?id={_contact_admin_id()}'>Our admin</a> is happy to help! 😄",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🪪 Almost there! Please enter your NTU matriculation number "
        "(case sensitive) to verify — e.g. U2512345F 👇"
    )
    return ASK_MATRIC

async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate the submitted matriculation number against the Welcome Tea sheet.

    Approves the join request and records the Telegram user ID in the sheet on
    success.  Stays in ASK_MATRIC state so the user can retry on failure.
    """
    user_id = update.message.from_user.id
    matric = update.message.text.strip()

    if user_id not in pending_users:
        await update.message.reply_text(
            "🤔 No pending join request found for you — maybe it's already approved 🎉 "
            "or you haven't tapped the invite link yet.\n\n"
            f"💬 Need a hand? <a href='tg://user?id={_contact_admin_id()}'>Our admin</a> has your back! 😄",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    join_request = pending_users[user_id]

    if matric_valid(matric):
        await join_request.approve()
        update_user_id_in_sheet(matric, user_id)
        await update.message.reply_text(
            "🎉🥳 *YOU'RE IN!* 🥳🎉\n\n"
            "✅ Matric & attendance verified — welcome to the NTUCD family! 🥁❤️\n"
            "Get ready to make some noise! 🔥",
            parse_mode="Markdown",
        )
        pending_users.pop(user_id, None)
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            "😅 Oops! I couldn't match that matric number / attendance record.\n\n"
            "🔁 Double-check and try again (case sensitive!) — e.g. U2512345F 👇\n\n"
            f"💬 Still stuck? <a href='tg://user?id={_contact_admin_id()}'>Our admin</a> can sort it out! 🙌",
            parse_mode=ParseMode.HTML,
        )
        # Stay in ASK_MATRIC state
        return ASK_MATRIC

