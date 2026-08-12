import asyncio
from html import escape

from telegram import Update
from telegram.constants import ChatAction, ParseMode
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

# One /verification costs 5 Google Sheets reads (Form Responses lookup + MEMBER
# INFO sync). Google allows 60 reads/min, so the ceiling is ~12 verifications a
# minute. After the Follow-up job posts the main-group invite link, ~100 members
# can click it at once — well past that. Since PTB runs updates one at a time
# (max_concurrent_updates=1), pausing here throttles the whole queue and keeps
# the sustained rate under quota.
VERIFICATION_THROTTLE_SECONDS = 2


def _contact_admin_id() -> int:
    """Return the role-driven contact admin, with config as fallback."""
    from services.google_sheets import get_join_contact_admin_id
    return get_join_contact_admin_id() or JOIN_CONTACT_ADMIN_ID


async def _send_main_group_verification_prompt(join_request, context):
    """Keep a main-group request pending and ask the requester to verify."""
    user = join_request.from_user
    settings = get_welcome_tea_settings()
    signup_form_link = settings.get("signup_form_link", "")
    pending_users[user.id] = join_request

    try:
        await context.bot.send_message(
            chat_id=getattr(join_request, "user_chat_id", None) or user.id,
            text=(
                "🎉 <b>Welcome to the NTUFD family!</b>\n\n"
                "Thank you for requesting to join our main group. We are so excited to have you on board! 🥁✨\n\n"
                f"If you haven't filled out our <a href='{signup_form_link}'>Welcome Tea Registration Form</a> yet, please take a quick moment to do so first.\n\n"
                "To complete your entry, simply type /verification in this chat! I will ask for your matriculation number to quickly verify your registration, and then you'll be let right in! ✅"
            ),
            parse_mode=ParseMode.HTML, # 👈 Added so the link and bolding works!
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

    main_group_link = settings.get("main_group_welcome_tea_invite_link", "")
    welcome_tea_link = settings.get("welcome_tea_join_request_link", "")

    if main_group_link and link_url == main_group_link:
        await _send_main_group_verification_prompt(request, context)
        return

    is_welcome_tea_request = (
        request_chat_id == WELCOME_TEA_GROUP_CHAT_ID
        or (welcome_tea_link and link_url == welcome_tea_link)
        #or WELCOME_TEA_LINK_KEYWORD in link_name.lower()
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
            "Oops! 🙈 It looks like we don't have a pending main-group join request from you right now.\n\n"
            "Please make sure to click the main-group invite link and request to join first, then come back here and type /verification again! ✨",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "Awesome! Let's get you verified. 🎉\n\n"
        "Please enter your NTU matriculation number (for example: <code>U2512345F</code>).",
        parse_mode=ParseMode.HTML
    )
    # ASK_MATRIC is the "state" telling the bot to wait for the user's next message!
    return ASK_MATRIC


async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync the latest registration data, then approve the pending request."""
    user_id = update.effective_user.id
    matric = update.effective_message.text.strip()
    join_request = pending_users.get(user_id)

    if join_request is None:
        await update.effective_message.reply_text(
            "Oops! 🙈 It looks like we don't have a pending main-group join request from you.\n\n"
            "Please request to join the group first, and then type /verification again so we can let you in! ✨",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    # Show "typing…" so the throttle pause below doesn't read as the bot being
    # broken — otherwise people re-send their matric and make the burst worse.
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass
    await asyncio.sleep(VERIFICATION_THROTTLE_SECONDS)

    try:
        synced, result = sync_welcome_tea_member(matric, user_id)
    except Exception as e:
        # Google quota exhausted, or the sheet is unreachable. Without this the
        # exception escapes the handler and the member gets NO reply at all.
        print(f"[VERIFY][ERROR] Sheet lookup failed for {user_id}: {e}")
        await update.effective_message.reply_text(
            "Our system is a little busy right now. 😅\n\n"
            "Please wait a minute and type /verification again — your registration is safe, "
            "we just need a moment to catch up!",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if result == "matric_not_found":
        signup_form_link = get_welcome_tea_settings()["signup_form_link"]
        await update.effective_message.reply_text(
            "Hmm, we couldn't find that matriculation number in our records. 🤔\n\n"
            "If you haven't filled out our registration form yet, please do so here:\n"
            f"👉 <a href='{escape(signup_form_link, quote=True)}'>Welcome Tea Registration Form</a> 📝\n\n"
            "Once submitted, just type /verification again to retry!\n\n"
            "<i>(If you've already filled it out, please double-check your matriculation number for any typos. Still stuck? Just drop a message to our Chairpersons @ma_ning or @jurikawazu for help! ❤️)</i>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    if not synced:
        print(f"[VERIFY][ERROR] MEMBER INFO sync failed for {user_id}: {result}")
        await update.effective_message.reply_text(
            "Good news: We found your registration! 🎉\n"
            "Bad news: Our database is taking a little nap right now and couldn't sync your profile. 💤\n\n"
            "Please contact our friendly admins so we can help you out, and try /verification again a bit later!",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    try:
        await join_request.approve()
    except Exception as e:
        print(f"[VERIFY][ERROR] Join approval failed for {user_id}: {e}")
        await update.effective_message.reply_text(
            "Your details are perfectly verified! ✅\n\n"
            "However, Telegram had a little hiccup and couldn't approve your join request right now. 🫠\n"
            "Please try submitting the main-group join request one more time!",
            parse_mode=ParseMode.HTML
        )
        pending_users.pop(user_id, None)
        return ConversationHandler.END

    # Success!
    pending_users.pop(user_id, None)
    await update.effective_message.reply_text(
        "Woohoo! 🎉 Your matriculation number is fully verified and your join request has been approved.\n\n"
        "<b>Officially welcome to the NTUFD family! We are absolutely thrilled to have you here!</b> 🥁🔥",
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END
