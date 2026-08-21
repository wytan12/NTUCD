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
from services.google_sheets import (
    get_welcome_tea_settings,
    sync_welcome_tea_member,
    sync_welcome_tea_members,
    lookup_welcome_tea_registration,
    WT_LOOKUP_FOUND,
    WT_LOOKUP_UNAVAILABLE,
)
from utils.constants import ASK_MATRIC, pending_users

# Matric lookup is cache-first: a matric already in the cached Form Responses costs
# zero API calls. Only a cache MISS goes back to Google.
# MEMBER INFO sync is handled by drain_member_writes_job running in the background.
# The throttle here is UX-only: shows typing while the bot works, prevents spam.
VERIFICATION_THROTTLE_SECONDS = 2

# Google returns a transient 503 every so often (roughly 1-2% of calls). It is a
# "come back in a moment" error that Google's own docs expect clients to retry, so
# one retry turns most of them into a success the member never notices. The sleep is
# awaited in this async handler rather than inside the sync sheets layer, so it never
# blocks the event loop.
VERIFICATION_RETRY_DELAY_SECONDS = 2

# Failures that retrying can never fix, so the queued write is dropped instead of
# looping forever. Everything NOT listed here (a 503, a read/write failure) is
# treated as transient and retried on the next tick — getting this list wrong in
# the "too broad" direction silently throws away a verified member's form data.
MEMBER_WRITE_DROP_CODES = (
    "matric_not_found",
    "header_mismatch",
    "no_empty_member_row",
    "member_info_missing_required_columns",
)


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
    # Persist the user ID so verification can still proceed after a bot restart.
    # The ChatJoinRequest object itself can't be pickled, but the ID lets us
    # fall back to bot.approve_chat_join_request() if the object is gone.
    context.application.bot_data.setdefault("pending_verification_ids", set()).add(user.id)

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
    """Verify matric against cached Form Responses, approve immediately, queue MEMBER INFO write."""
    user_id = update.effective_user.id
    matric = update.effective_message.text.strip()
    join_request = pending_users.get(user_id)
    pending_ids = context.application.bot_data.get("pending_verification_ids", set())

    if join_request is None and user_id not in pending_ids:
        await update.effective_message.reply_text(
            "Oops! 🙈 It looks like we don't have a pending main-group join request from you.\n\n"
            "Please request to join the group first, and then type /verification again so we can let you in! ✨",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass
    await asyncio.sleep(VERIFICATION_THROTTLE_SECONDS)

    # Cache-first lookup. A matric already in the cache costs no API call at all;
    # a miss re-reads the sheet once so a just-submitted form is picked up.
    registration, status = lookup_welcome_tea_registration(matric)

    # Google was unreachable (typically a transient 503) — retry once before giving up.
    if status == WT_LOOKUP_UNAVAILABLE:
        await asyncio.sleep(VERIFICATION_RETRY_DELAY_SECONDS)
        registration, status = lookup_welcome_tea_registration(matric)

    if status == WT_LOOKUP_UNAVAILABLE:
        # NOT "matric not found" — we never managed to check. Saying the wrong thing
        # here would send a legitimately registered member off to re-fill the form.
        # Stay in ASK_MATRIC so they can simply send the number again.
        print(f"[VERIFY][WARN] Lookup unavailable for {user_id} ({matric}) — asked them to retry.")
        await update.effective_message.reply_text(
            "😵‍💫 <b>Oops, our system is a little busy right now!</b>\n\n"
            "Please <b>send your matriculation number again in about a minute</b> and we'll get you right in! ✨",
            parse_mode=ParseMode.HTML,
        )
        return ASK_MATRIC

    if status != WT_LOOKUP_FOUND:
        signup_form_link = get_welcome_tea_settings().get("signup_form_link", "")
        # We just re-read the sheet, so this answer is fresh — no point telling them
        # to wait for our system to catch up. Either there's a typo, or no form yet.
        await update.effective_message.reply_text(
            "Hmm, we couldn't find that matriculation number in our records. 🤔\n\n"
            "If you haven't filled out our registration form yet, please do so here:\n"
            f"👉 <a href='{escape(signup_form_link, quote=True)}'>Welcome Tea Registration Form</a> 📝\n"
            "Once you've submitted it, just send your matriculation number here again. ✅\n\n"
            "<i>(Still stuck? Just drop a message to our Chairpersons @ma_ning or @jurikawazu for help! ❤️)</i>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return ASK_MATRIC

    # Matric found — approve into main group immediately (zero Sheets API).
    # If the bot restarted and the join_request object is gone, fall back to the API call.
    try:
        if join_request is not None:
            await join_request.approve()
        else:
            await context.bot.approve_chat_join_request(chat_id=CHAT_ID, user_id=user_id)
    except Exception as e:
        print(f"[VERIFY][ERROR] Join approval failed for {user_id}: {e}")
        await update.effective_message.reply_text(
            "Your details are perfectly verified! ✅\n\n"
            "However, Telegram had a little hiccup and couldn't approve your join request right now. 🫠\n"
            "Please try submitting the main-group join request one more time!",
            parse_mode=ParseMode.HTML
        )
        pending_users.pop(user_id, None)
        pending_ids.discard(user_id)
        return ConversationHandler.END

    pending_users.pop(user_id, None)
    pending_ids.discard(user_id)

    # Tell the member FIRST — they are already approved and in the group, so the
    # MEMBER INFO write below never keeps them waiting.
    await update.effective_message.reply_text(
        "Woohoo! 🎉 Your matriculation number is fully verified and your join request has been approved.\n\n"
        "<b>Officially welcome to the NTUFD family! We are absolutely thrilled to have you here!</b> 🥁🔥",
        parse_mode=ParseMode.HTML
    )

    # Write to MEMBER INFO right now, and only fall back to the queue if that fails.
    # Doing it inline means the common case is confirmed immediately instead of
    # sitting in an in-memory queue that a dyno restart would silently discard.
    #
    # If a queue already exists we are mid-burst: join it rather than writing
    # inline, so the drain job can batch everyone into a single write and keep
    # the ordering. gspread is synchronous, so the call goes to a worker thread
    # and never blocks the event loop.
    queue = context.application.bot_data.setdefault("pending_member_writes", {})
    written_now = False
    if not queue:
        try:
            written_now, info = await asyncio.to_thread(sync_welcome_tea_member, matric, user_id)
            if not written_now:
                print(f"[VERIFY] Inline write deferred for {user_id} ({matric}): {info}")
        except Exception as e:
            print(f"[VERIFY] Inline write errored for {user_id} ({matric}): {e}")
    if not written_now:
        queue[user_id] = matric

    return ConversationHandler.END


async def drain_member_writes_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: write one queued verification to MEMBER INFO per tick.

    Runs every 7 seconds. Processes one user at a time to stay well under the
    Google Sheets API quota (~2 API calls per user × ~8 users/min = 16 calls/min).
    Retries automatically on transient failures; drops entries on permanent ones.
    """
    queue = context.bot_data.get("pending_member_writes", {})
    if not queue:
        return

    # Take the whole queue in one go. Batching costs ~3 API calls no matter how
    # many members are waiting (vs ~4 EACH), and it removes the head-of-line
    # blocking the old one-per-tick loop had: a single stuck entry used to starve
    # everybody queued behind it forever.
    batch = list(queue.items())          # [(tele_id, matric), ...]
    try:
        results = await asyncio.to_thread(
            sync_welcome_tea_members, [(matric, tele_id) for tele_id, matric in batch]
        )
    except Exception as e:
        print(f"[DRAIN] Batch of {len(batch)} errored: {e} — will retry next tick")
        return

    synced = dropped = retrying = 0
    for tele_id, matric in batch:
        ok, info = results.get(tele_id, (False, "unknown_error"))
        if ok:
            queue.pop(tele_id, None)
            synced += 1
        elif info in MEMBER_WRITE_DROP_CODES:
            # Permanent failure — retrying can never fix it, so stop burning quota.
            queue.pop(tele_id, None)
            dropped += 1
            print(f"[DRAIN][WARN] Dropped {tele_id} ({matric}): {info} — manual check needed")
        else:
            retrying += 1
            print(f"[DRAIN] Will retry {tele_id} ({matric}): {info}")

    if synced or dropped:
        print(f"[DRAIN] Batch of {len(batch)}: synced={synced} dropped={dropped} retrying={retrying}")
