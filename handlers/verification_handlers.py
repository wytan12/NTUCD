from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from config import (
    CHAT_ID,
    JOIN_CONTACT_ADMIN_ID,
    WELCOME_TEA_GROUP_CHAT_ID,
    WELCOME_TEA_LINK_KEYWORD,
)
from handlers.welcome_tea_handlers import handle_welcome_tea_join_request
from services.google_sheets import get_welcome_tea_settings, sync_welcome_tea_member
from utils.constants import ASK_MATRIC, pending_users


def _contact_admin_id() -> int:
    """Return the role-driven contact admin, with config as fallback."""
    from services.google_sheets import get_join_contact_admin_id
    return get_join_contact_admin_id() or JOIN_CONTACT_ADMIN_ID


async def _send_main_group_verification_prompt(join_request, context):
    """Keep a main-group request pending and ask the requester to verify."""
    user = join_request.from_user
    settings = get_welcome_tea_settings()
    pending_users[user.id] = join_request
    try:
        await context.bot.send_message(
            chat_id=getattr(join_request, "user_chat_id", None) or user.id,
            text=(
                "Thank you for requesting to join NTUFD!\n\n"
                "If you have not filled in the Welcome Tea Registration Form, please submit it first:\n"
                f"{settings['signup_form_link']}\n\n"
                "Please type /verification here as a message to the bot. The bot will then prompt you to enter your matriculation "
                "number to verify your Welcome Tea registration."
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[VERIFY][WARN] Could not send verification DM to {user.id}: {e}")


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route Welcome Tea, post-event, returning-member, and unknown requests."""
    from services.google_sheets import (
        get_alert_admin_ids,
        is_member_in_ay2526,
        update_member_join_in_info,
    )

    request = update.chat_join_request
    user = request.from_user
    link = request.invite_link
    link_name = (link.name or "") if link else ""
    link_url = (link.invite_link or "") if link else ""
    request_chat_id = request.chat.id
    settings = get_welcome_tea_settings()
    print(
        f"Join request received from {user.first_name} ({user.id}) "
        f"in chat {request_chat_id} via link '{link_name}'"
    )

    main_group_link = settings["main_group_welcome_tea_invite_link"]
    welcome_tea_link = settings["welcome_tea_join_request_link"]

    if main_group_link and link_url == main_group_link:
        await _send_main_group_verification_prompt(request, context)
        return

    is_welcome_tea_request = (
        request_chat_id == WELCOME_TEA_GROUP_CHAT_ID
        or (welcome_tea_link and link_url == welcome_tea_link)
        or WELCOME_TEA_LINK_KEYWORD in link_name.lower()
    )
    if is_welcome_tea_request:
        await handle_welcome_tea_join_request(request, context)
        return

    if is_member_in_ay2526(user.id):
        try:
            await context.bot.send_message(
                chat_id=getattr(request, "user_chat_id", None) or user.id,
                text=f"Welcome back, {user.first_name}! Your join request has been approved.",
            )
        except Exception as e:
            print(f"[JOIN][WARN] Welcome DM failed for {user.id}: {e}")
        try:
            await request.approve()
            update_member_join_in_info(user.id, user.full_name)
            print(f"[JOIN] Auto-approved returning member {user.full_name} ({user.id}).")
        except Exception as e:
            print(f"[JOIN][ERROR] Returning-member auto-approve failed for {user.id}: {e}")
        return

    # Any remaining request targeting the main NTUFD group must complete the
    # Welcome Tea registration verification flow. This also covers renamed or
    # regenerated admin-approval invite links whose URL no longer matches config.
    if request_chat_id == CHAT_ID:
        await _send_main_group_verification_prompt(request, context)
        return

    pending_users[user.id] = request
    try:
        await context.bot.send_message(
            chat_id=getattr(request, "user_chat_id", None) or user.id,
            text=(
                "Your join request is pending. Please contact "
                f"<a href='tg://user?id={_contact_admin_id()}'>our admin</a>."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"[JOIN][WARN] Could not message unknown requester {user.id}: {e}")

    alert_text = (
        "<b>Unknown main-group join request</b>\n\n"
        f"User: <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
        f"Tele ID: <code>{user.id}</code>\n\n"
        "Not found in MEMBER INFO AY25/26. Review the request in Telegram."
    )
    for admin_id in (get_alert_admin_ids() or {_contact_admin_id()}):
        try:
            await context.bot.send_message(chat_id=admin_id, text=alert_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"[JOIN][WARN] Could not alert admin {admin_id}: {e}")


async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask a pending main-group requester for their matriculation number."""
    if update.effective_chat.type != "private":
        return ConversationHandler.END

    user_id = update.effective_user.id
    if user_id not in pending_users:
        await update.effective_message.reply_text(
            "I do not see a pending main-group join request for you. "
            "Please request to join using the Welcome Tea main-group link first."
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "Please enter your NTU matriculation number, for example U2512345F."
    )
    return ASK_MATRIC


async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync the latest registration data, then approve the pending request."""
    user_id = update.effective_user.id
    matric = update.effective_message.text.strip()
    join_request = pending_users.get(user_id)

    if join_request is None:
        await update.effective_message.reply_text(
            "No pending main-group join request was found. "
            "Please request to join, then send /verification again."
        )
        return ConversationHandler.END

    synced, result = sync_welcome_tea_member(matric, user_id)
    if result == "matric_not_found":
        signup_form_link = get_welcome_tea_settings()["signup_form_link"]
        await update.effective_message.reply_text(
            "We could not find that matriculation number in the Welcome Tea registration form.\n\n"
            "Please fill in the form first if you have not done so:\n"
            f"<a href='{escape(signup_form_link, quote=True)}'>Welcome Tea Registration form</a>\n\n"
            "After submitting it, enter /verification again to retry.\n\n"
            "If you have already filled in the form, please check that you entered the correct matriculation number, or contact NTUFD chairperson @ma_ning (Ma Ning) or vice-chairperson @jurikawazu (Juri) on Telegram for assistance.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    if not synced:
        print(f"[VERIFY][ERROR] MEMBER INFO sync failed for {user_id}: {result}")
        await update.effective_message.reply_text(
            "We found your registration, but could not update the member database. "
            "Please contact an admin and try /verification again later."
        )
        return ConversationHandler.END

    try:
        await join_request.approve()
    except Exception as e:
        print(f"[VERIFY][ERROR] Join approval failed for {user_id}: {e}")
        await update.effective_message.reply_text(
            "Your details were verified, but Telegram could not approve the join request. "
            "Please submit the main-group join request again."
        )
        pending_users.pop(user_id, None)
        return ConversationHandler.END

    pending_users.pop(user_id, None)
    await update.effective_message.reply_text(
        "Your matriculation number has been verified and your join request was approved. "
        "Welcome to NTUFD!"
    )
    return ConversationHandler.END
