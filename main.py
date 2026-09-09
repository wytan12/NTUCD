from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, Update
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    PollAnswerHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    PicklePersistence,
)
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
import datetime
import asyncio
import traceback
from handlers.admin_handlers import handle_remind_escrow_callback, start, thread_id_command, daily_reminder_cron_job, execute_manual_remind_dispatch, execute_manual_announcement_dispatch
from handlers.message_handlers import handle_message
from handlers.private_handlers import handle_private_command
from handlers.poll_handlers import handle_poll_answer, auto_poll_check
from handlers.attendance_handlers import attendance_callback
from handlers.verification_handlers import start_verification, handle_matric, join_request_handler, drain_member_writes_job
from handlers.modify_handlers import start_modify, get_modify_field_callback
from handlers.modify_handlers import handle_modify_status_selection, handle_modify_date_selection
from handlers.member_handlers import handle_member_status, handle_new_member
from handlers.private_handlers import handle_confirm_new_perf, handle_dashboard_refresh, handle_dashboard_navigation
from handlers.logistics_handlers import logistics_callback
from handlers.welcome_tea_handlers import (
    handle_welcome_tea_confirmation,
    handle_welcome_tea_still_coming,
    handle_welcome_tea_cant_make_it,
    handle_dietary_reply,
    handle_attd_checkin,
    schedule_welcome_tea_jobs,
)
from services.google_sheets import get_gspread_sheet
from handlers.private_handlers import handle_list_modify_callback
from handlers.modify_handlers import handle_modify_type_selection
from config import BOT_TOKEN, SHEET_NAME, CHAT_ID, sg_tz, ADMIN_DM_USER_IDS
from services.alerts import alert, install_alerts, who
from datetime import time as dt_time
from utils.constants import (
    initialized_topics,
    ASK_MATRIC,
    OTHERS_THREAD_IDS
)

admin_commands = [
    BotCommand("start", "🚀 Admin DM Dashboard"),
    # 🦅 Unified dashboard: every other action lives inside the /start Cockpit, so
    # the Telegram command menu only advertises /start. (Re-enable any line below
    # to surface that command in the menu again.)
    # BotCommand("new", "📝 Create new topic + sheet entry"),
    # BotCommand("list", "📋 See all thread IDs and events"),
    # BotCommand("modify", "⚙️ Edit a performance details"),
    # BotCommand("attd", "✅ Mark regular-training attendance"),
    # BotCommand("announce", "📢 Broadcast a multi-line format message to any topic"),
    # BotCommand("remind", "🔔 Trigger manual checklist reminder announcement"),
    # BotCommand("threadid", "🧵 Print the entire group topic directory chart"),
    # BotCommand("info", "Preview summary card details in DM")
]

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch every exception that escapes a handler.

    Without this, PTB logs 'No error handlers are registered' and dumps a raw
    traceback, and — worse — the user who triggered it gets NOTHING back. That is
    how a transient Google 503 silently stranded a member mid-verification on
    19-20 Aug 2026: the handler died at the failing line and the approve/reply
    steps after it never ran.

    Two classes of error are treated differently:
      • transient network noise -> one log line (PTB retries these itself)
      • anything else           -> full traceback + a reply to the user
    """
    err = context.error

    # Telegram's polling loop drops connections routinely (httpx.ReadError surfaces
    # as a plain NetworkError) and users who never opened a DM raise Forbidden.
    # Both are expected and self-healing, so log one line instead of 40.
    # NOTE: BadRequest subclasses NetworkError in PTB but usually means a REAL bug
    # (bad Markdown, stale message id), so it is deliberately excluded here.
    is_transient = isinstance(err, (TimedOut, Forbidden)) or (
        isinstance(err, NetworkError) and not isinstance(err, BadRequest)
    )
    if is_transient:
        print(f"[ERROR][transient] {type(err).__name__}: {err}")
        return

    summary = f"[ERROR] Unhandled exception while processing an update: {type(err).__name__}: {err}"
    print(summary)
    traceback.print_exception(type(err), err, err.__traceback__)

    # The tee already alerted on the printed line above, but without the who and
    # the what. Re-raising it here with the update context is what turns
    # "something broke" into "I can go reproduce it". The throttle's signature
    # dedupe collapses the two into one DM.
    frames = None
    if err.__traceback__:
        frames = "".join(traceback.format_tb(err.__traceback__)[-3:]).strip()
    bits = [f"👤 {who(update)}"]
    if isinstance(update, Update) and update.callback_query and update.callback_query.data:
        bits.append(f"🧩 {update.callback_query.data}")
    alert(summary, level="ERROR", context=" · ".join(bits), frames=frames)

    # Tell the user something went wrong — but ONLY in a private chat. The bot is
    # deliberately silent inside group topics, and an error must not break that.
    if not isinstance(update, Update):
        return
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return

    notice = (
        "😵‍💫 <b>Oops, something went wrong on our side.</b>\n\n"
        "Please try that again in a moment. If it keeps happening, drop a message "
        "to our Chairpersons @ma_ning or @jurikawazu and we'll sort it out! ❤️"
    )
    try:
        if update.callback_query:
            # May fail if the query was already answered — harmless, caught below.
            await update.callback_query.answer("Something went wrong. Please try again.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(notice, parse_mode="HTML")
    except Exception as notify_err:
        print(f"[ERROR] Could not notify user about the failure: {notify_err}")


async def global_button_security_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercepts EVERY button click globally before it reaches any handler."""
    if update.callback_query:
        query = update.callback_query
        
        # 🟢 VIP PASS: Let all Welcome Tea RSVP buttons bypass the admin security check
        if query.data in [
            "WELCOME_TEA_CONFIRM", "WELCOME_TEA_REJECT",
            "WELCOME_TEA_STILL_COMING", "WELCOME_TEA_CANT_MAKE_IT",
        ]:
            return  # Let it pass cleanly to the normal handlers!

        # 🟢 VIP PASS: /costume is member-facing, so its buttons must survive this
        # gate. The handler itself re-checks that every row belongs to the caller.
        if (query.data or "").startswith("MYCOS|"):
            return
            
        user_id = query.from_user.id
        from services.google_sheets import is_dashboard_admin

        # 🔴 SECURITY CHECK: Verify if they are a Dashboard Admin.
        # MAJOR clicks (Cockpit navigation / Refresh) re-verify the role against
        # the LIVE sheet, so a role granted/removed in the web UI applies on that
        # very click. In-task actions (attendance toggles, wizard steps, field
        # edits) use the cached role so an admin mid-task is never slowed down.
        data = query.data or ""
        force_fresh = data.startswith("DASH_VIEW|") or data == "DASH_REFRESH"
        if not is_dashboard_admin(user_id, force=force_fresh):
            await query.answer()
            try:
                await query.edit_message_text(
                    text="⛔ *Access Denied*\nYour admin rights have been revoked. This dashboard session has expired.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"[SECURITY] Failed to edit expired dashboard: {e}")
            
            # 3. Instantly kill the request so no other file processes it
            raise ApplicationHandlerStop

async def register_private_admin_menus(application):
    """🧠 CRASH-PROOF DISPATCHER: Non-blocking async background worker that pushes menu suggestions

    strictly to valid numerical IDs without causing network timeout script lockups.
    """
    # Bind the /start menu for BOTH admin tiers (main + secondary roles from
    # MEMBER INFO) plus the config fallback ids.
    try:
        from services.google_sheets import get_dashboard_admin_ids
        admin_targets = set(get_dashboard_admin_ids())
    except Exception as e:
        print(f"[WARN] Role lookup failed during menu bind, using config ids: {e}")
        admin_targets = set()
        if isinstance(ADMIN_DM_USER_IDS, dict):
            for k, v in ADMIN_DM_USER_IDS.items():
                if str(k).isdigit(): admin_targets.add(int(k))
                if str(v).isdigit(): admin_targets.add(int(v))
        elif isinstance(ADMIN_DM_USER_IDS, (list, set)):
            for item in ADMIN_DM_USER_IDS:
                if str(item).isdigit(): admin_targets.add(int(item))

    for uid in admin_targets:
        try:
            # Short sleep to comply with Telegram rate limits safely
            await asyncio.sleep(0.1)
            await application.bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=uid)
            )
        except Exception as e:
            print(f"[WARN] Skipping menu bind for chat entry {uid}: {e}")

async def clear_all_public_menus(application):
    """🧠 TOTAL HIDDEN SCOPE: Wipes out global/default suggestion layouts completely."""
    try:
        await application.bot.set_my_commands(commands=[], scope=BotCommandScopeDefault())
    except Exception as e:
        print(f"[WARN] Default command clear skipped: {e}")

async def on_startup(application):
    # Run UI cleanup commands
    await clear_all_public_menus(application)
    
    # 🧠 NON-BLOCKING FORK: Spawns the admin profile builder as a detached task 
    # to guarantee the main bot engine can boot up instantly without timing out!
    asyncio.create_task(register_private_admin_menus(application))
    
    # Background automation clock setup (09:00 AM SGT)
    target_time = datetime.time(hour=9, minute=0, second=0, tzinfo=sg_tz)
    application.job_queue.run_daily(daily_reminder_cron_job, time=target_time)
    # application.job_queue.run_once(daily_reminder_cron_job, when=10)  
    print(f"[AUTOMATION] Background automated checker established for daily execution at: {target_time}")
    # Welcome Tea automation is run manually — the 30s sheet-polling tick is
    # disabled. /attd and /verification read settings directly and are unaffected.
    # schedule_welcome_tea_jobs(application)
    application.job_queue.run_repeating(drain_member_writes_job, interval=7, first=10)

async def start_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route /start to the admin dashboard."""
    await start(update, context)

def main():
    # Route [ERROR]/[WARN] log lines to the maintainer's DM. No-ops without
    # ALERT_CHAT_ID, so local runs are unaffected. See services/alerts.py.
    install_alerts()
    print("Bot starting...")
    persistence = PicklePersistence(filepath="bot_data.pkl")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(on_startup)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(3)
        .build()
    )
    
    verify_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("verify", start_verification),
            CommandHandler("verification", start_verification),
        ],
        states={
            ASK_MATRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_matric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # /attd: must run before handle_private_command's admin gate (group -3)
    app.add_handler(CommandHandler("attd", handle_attd_checkin), group=-3)

    # Member-facing costume screen: /costume lets a member record passing their
    # costume to someone else. Returns stay with logistics.
    from handlers.costume_member import handle_my_costume, member_callback
    app.add_handler(CommandHandler("costume", handle_my_costume), group=-3)
    app.add_handler(CallbackQueryHandler(member_callback, pattern="^MYCOS\|"))

    # Dietary reply intercept: runs before the admin gate (group -2)
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_dietary_reply),
        group=-2,
    )

    # Catches anything that escapes a handler, so a failure is never silent.
    app.add_error_handler(global_error_handler)

    # Core engine endpoint configurations
    app.add_handler(TypeHandler(Update, global_button_security_check), group=-1)
    app.add_handler(CommandHandler("start", start_command_router))
    app.add_handler(CommandHandler("threadid", thread_id_command))
    #app.add_handler(CommandHandler("remind", remind_command))
    #app.add_handler(CommandHandler("testremind", manual_test_reminder_trigger))
    #app.add_handler(CommandHandler("modify", start_modify))
    app.add_handler(CallbackQueryHandler(get_modify_field_callback, pattern="^MODIFY\\|"))
    app.add_handler(CallbackQueryHandler(handle_modify_status_selection, pattern="^modify_status_selected\\|"))
    app.add_handler(CallbackQueryHandler(handle_modify_date_selection, pattern='^modify_date_selected\\|'))
    app.add_handler(CallbackQueryHandler(handle_list_modify_callback, pattern="^LIST_MODIFY\\|"))
    app.add_handler(CallbackQueryHandler(handle_modify_type_selection, pattern="^modify_type_selected\\|"))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(CallbackQueryHandler(attendance_callback, pattern="^ATTD_"))
    app.add_handler(verify_conv_handler)
    app.add_handler(ChatMemberHandler(handle_member_status, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_command))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_message))
    app.add_handler(CallbackQueryHandler(handle_confirm_new_perf, pattern="^(CONFIRM_NEW_PERF|CONFIRM_NEW_OTHERS|CANCEL_NEW_PERF|ANNOUNCE_BACK_MAPPED|TYPE_SELECTED\\||TOGGLE_RULE\\||CONFIRM_STANDARD|PERF_EVENT_TYPE\\||SKIP_PERF_FIELD\\||PERF_RESET\\|)"))
    app.add_handler(CallbackQueryHandler(execute_manual_remind_dispatch, pattern="^MANUAL_REMIND_TID\\|"))
    app.add_handler(CallbackQueryHandler(handle_dashboard_refresh, pattern="^DASH_REFRESH$"))
    app.add_handler(CallbackQueryHandler(handle_dashboard_navigation, pattern="^DASH_VIEW\\|"))
    app.add_handler(CallbackQueryHandler(logistics_callback, pattern="^LOGI\\|"))
    app.add_handler(CallbackQueryHandler(handle_remind_escrow_callback, pattern=r"^REMIND_ESCROW_CONFIRM\|"))
    app.add_handler(CallbackQueryHandler(execute_manual_announcement_dispatch, pattern=r"^ANNOUNCE_TARGET\|"))
    app.add_handler(CallbackQueryHandler(handle_welcome_tea_confirmation, pattern=r"^WELCOME_TEA_(CONFIRM|REJECT)$"))
    app.add_handler(CallbackQueryHandler(handle_welcome_tea_still_coming, pattern=r"^WELCOME_TEA_STILL_COMING$"))
    app.add_handler(CallbackQueryHandler(handle_welcome_tea_cant_make_it, pattern=r"^WELCOME_TEA_CANT_MAKE_IT$"))
    
    print("Bot is running...")
    
    # 🛠️ UNIFIED REBOOT CACHE LOADER: Synchronizes all valid temporary threads on boot
    try:
        print("📥 Synchronizing active forum directories...")
        from utils.constants import initialized_topics
        
        # Pull and cache miscellaneous event thread IDs from OTHERS sheet
        others_sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="OTHERS")
        others_ids = others_sheet.col_values(1)
        for tid in others_ids:
            if tid.strip().isdigit():
                initialized_topics.add(int(tid.strip()))
                
        # Pull and cache performance event thread IDs from PERF sheet
        perf_sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="PERF")
        perf_records = perf_sheet.get_all_records()
        for row in perf_records:
            p_tid = row.get("THREAD ID")
            if str(p_tid).isdigit():
                initialized_topics.add(int(p_tid))
                
        print(f"✅ Framework Online! Cached {len(initialized_topics)} verified event threads.")
    except Exception as e:
        print(f"❌ Critical error sync failed during framework boot: {e}")
    
    # --- 3. Schedule daily auto-poll check (09:00 SGT) ---
    try:
        if app.job_queue:
            app.job_queue.run_daily(auto_poll_check, time=dt_time(9, 0, tzinfo=sg_tz))
            # # TEST: run 20s after start, then every 2 min
            # app.job_queue.run_repeating(auto_poll_check, interval=120, first=5)
            print("[INFO] Auto-poll daily check scheduled for 09:00 SGT.")
        else:
            print("[WARN] JobQueue unavailable — auto-poll not scheduled. "
                  "Install python-telegram-bot[job-queue].")
    except Exception as e:
        print(f"[ERROR] Failed to schedule auto-poll: {e}")

    # --- 4. Start the Bot ---
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise
