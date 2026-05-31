from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    PollAnswerHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler
)
import datetime
import asyncio
from handlers.admin_handlers import start, thread_id_command, remind_command, daily_reminder_cron_job, manual_test_reminder_trigger, execute_manual_remind_dispatch
from handlers.message_handlers import handle_message
from handlers.private_handlers import handle_private_command
from handlers.poll_handlers import handle_poll_answer, auto_poll_check
from handlers.attendance_handlers import attendance_callback
from handlers.verification_handlers import start_verification, handle_matric, join_request_handler
from handlers.modify_handlers import start_modify, get_modify_field_callback
from handlers.modify_handlers import handle_modify_status_selection, handle_modify_date_selection
from handlers.conversation_handlers import confirmation, confirmation_callback, final_date_selection, topic_type_selection, parse_perf_input, cancel
from handlers.member_handlers import handle_member_status, handle_new_member
from handlers.private_handlers import handle_confirm_new_perf, handle_dashboard_refresh
from services.google_sheets import get_gspread_sheet
from handlers.private_handlers import handle_list_modify_callback
from handlers.modify_handlers import handle_modify_type_selection
from config import BOT_TOKEN, SHEET_NAME, CHAT_ID, sg_tz, ADMIN_DM_USER_IDS
from datetime import time as dt_time
from utils.constants import (
    initialized_topics,
    DATE,
    ASK_MATRIC, 
    OTHERS_THREAD_IDS
)

admin_commands = [
    BotCommand("start", "🚀 Admin DM Dashboard"),
    BotCommand("new", "📝 Create new topic + sheet entry"),
    BotCommand("list", "📋 See all thread IDs and events"),
    BotCommand("modify", "⚙️ Edit a performance details"),
    BotCommand("attd", "✅ Mark regular-training attendance"),
    BotCommand("announce", "📢 Broadcast a multi-line format message to any topic"),
    BotCommand("remind", "🔔 Trigger manual checklist reminder announcement"),
    BotCommand("threadid", "🧵 Print the entire group topic directory chart"),
    BotCommand("info", "Preview summary card details in DM")
]

async def register_private_admin_menus(application):
    """🧠 CRASH-PROOF DISPATCHER: Non-blocking async background worker that pushes menu suggestions

    strictly to valid numerical IDs without causing network timeout script lockups.
    """
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
    print(f"[AUTOMATION] Background automated checker established for daily execution at: {target_time}")

def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|")],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_perf_input)],
        },
        fallbacks=[],
    )
    
    verify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", start_verification)],
        states={
            ASK_MATRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_matric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # Core engine endpoint configurations
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("threadid", thread_id_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("testremind", manual_test_reminder_trigger))
    app.add_handler(CommandHandler("modify", start_modify))
    app.add_handler(CommandHandler("confirmation", confirmation))  
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(confirmation_callback, pattern="^CONFIRM\\|"))
    app.add_handler(CallbackQueryHandler(final_date_selection, pattern="^FINALDATE\\|"))
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
    app.add_handler(CallbackQueryHandler(handle_confirm_new_perf, pattern="^(CONFIRM_NEW_PERF|CONFIRM_NEW_OTHERS|CANCEL_NEW_PERF|ANNOUNCE_TARGET\\||ANNOUNCE_BACK_MAPPED|TYPE_SELECTED\\||TOGGLE_RULE\\||CONFIRM_STANDARD|PERF_EVENT_TYPE\\||SKIP_PERF_FIELD\\||PERF_RESET\\|)"))
    app.add_handler(CallbackQueryHandler(execute_manual_remind_dispatch, pattern="^MANUAL_REMIND_TID\\|"))
    app.add_handler(CallbackQueryHandler(handle_dashboard_refresh, pattern="^DASH_REFRESH$"))

    print("Bot is running...")
    
    try:
        print("Loading OTHERS List from Google Sheets...")
        others_sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="OTHERS")
        print("successfully got OTHERS List sheet")
        rows = others_sheet.col_values(1)
        print(f"[DEBUG] OTHERS List rows: {rows}")
        OTHERS_THREAD_IDS.update({int(r.strip()) for r in rows if r.strip().isdigit()})
        print(f"[INFO] Loaded {len(OTHERS_THREAD_IDS)} OTHERS thread IDs.")
    except Exception as e:
        print(f"[ERROR] Failed to load OTHERS List: {e}")
    
    # --- 3. Schedule daily auto-poll check (20:00 SGT) ---
    try:
        if app.job_queue:
            app.job_queue.run_daily(auto_poll_check, time=dt_time(20, 0, tzinfo=sg_tz))
            print("[INFO] Auto-poll daily check scheduled for 20:00 SGT.")
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