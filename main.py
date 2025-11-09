from telegram import BotCommand, BotCommandScopeChatAdministrators, BotCommandScopeDefault, Update
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
from handlers.admin_handlers import start, thread_id_command, remind_command
from handlers.message_handlers import handle_message
from handlers.private_handlers import handle_private_command
from handlers.poll_handlers import send_poll_handler, handle_poll_answer
from handlers.verification_handlers import start_verification, handle_matric, join_request_handler
from handlers.modify_handlers import start_modify, get_modify_field_callback, apply_modify_value
from handlers.modify_handlers import handle_modify_status_selection, handle_modify_date_selection
from handlers.conversation_handlers import confirmation, confirmation_callback, final_date_selection, topic_type_selection, parse_perf_input, cancel
from handlers.member_handlers import handle_member_status, handle_new_member
from services.google_sheets import get_gspread_sheet
from config import BOT_TOKEN, SHEET_NAME, CHAT_ID
from utils.constants import (
    initialized_topics,
    DATE,
    MODIFY_FIELD,
    MODIFY_VALUE,
    ASK_MATRIC, 
    OTHERS_THREAD_IDS
)

async def debug_id(update, context):
    await update.message.reply_text(str(update.effective_chat.id))
    print("CHAT ID:", update.effective_chat.id)

admin_commands = [
    BotCommand("start", "Just to test the bot and grab chat ID"),
    BotCommand("threadid", "To obtain the thread ID of the current topic"),
    BotCommand("poll", "Create attendance poll in Attendance Topic"),
    BotCommand("modify", "Modify performance summary details"),
    BotCommand("remind", "Remind about performance"),
    BotCommand("confirmation", "Confirm performance details"),
]

async def set_admin_commands(application):
    await application.bot.set_my_commands(
        commands=admin_commands,
        scope=BotCommandScopeChatAdministrators(chat_id=CHAT_ID)  # Your group ID
    )

async def clear_global_commands(application):
    await application.bot.set_my_commands([], scope=BotCommandScopeDefault())

async def on_startup(application):
    await set_admin_commands(application)
    await clear_global_commands(application)

def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|")],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_perf_input)],
        },
        fallbacks=[],  # 🔧 This is required
    )
    
    # Add the new ConversationHandler
    modify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("modify", start_modify)],
        states={
            MODIFY_FIELD: [CallbackQueryHandler(get_modify_field_callback, pattern="^MODIFY\\|")],
            MODIFY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_modify_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    # New verify handler
    verify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", start_verification)],
        states={
            ASK_MATRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_matric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug_id", debug_id))
    app.add_handler(CommandHandler("threadid", thread_id_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("confirmation", confirmation))  
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(topic_type_selection, pattern="^topic_type\\|"))
    app.add_handler(CallbackQueryHandler(confirmation_callback, pattern="^CONFIRM\\|"))
    app.add_handler(CallbackQueryHandler(final_date_selection, pattern="^FINALDATE\\|"))
    app.add_handler(CallbackQueryHandler(handle_modify_status_selection, pattern="^modify_status_selected\\|"))
    app.add_handler(modify_conv_handler)
    app.add_handler(CallbackQueryHandler(handle_modify_date_selection, pattern='^modify_date_selected\\|'))
    app.add_handler(CommandHandler("poll", send_poll_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(verify_conv_handler)
    app.add_handler(ChatMemberHandler(handle_member_status, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_command))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_message))

    
    print("Bot is running...")
    
    try:
        print("Loading OTHERS List from Google Sheets...")
        others_sheet = get_gspread_sheet(sheet_name="NTUCD AY25/26 Timeline (Tele Debug)", tab_name="OTHERS List")
        print("successfully got OTHERS List sheet")
        rows = others_sheet.col_values(1)
        print(f"[DEBUG] OTHERS List rows: {rows}")
        OTHERS_THREAD_IDS.update({int(r.strip()) for r in rows if r.strip().isdigit()})
        print(f"[INFO] Loaded {len(OTHERS_THREAD_IDS)} OTHERS thread IDs.")
    except Exception as e:
        print(f"[ERROR] Failed to load OTHERS List: {e}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise



