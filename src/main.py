from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ChatJoinRequestHandler, ChatMemberHandler, PollAnswerHandler, filters
from telegram import Update

from config.settings import BOT_TOKEN
from config.commands import set_all_commands  # ✅ Import from commands.py
from handlers.admin import AdminHandlers
from handlers.performance import PerformanceHandlers
from handlers.membership import MembershipHandlers
from handlers.message import MessageHandlers
from handlers.polls import PollHandlers
from models.constants import ConversationStates

async def on_startup(application):
    """Called when bot starts up"""
    await set_all_commands(application)  # ✅ Use function from commands.py
    print("[INFO] Bot startup complete")

def create_conversation_handlers():
    # Performance conversation handler
    perf_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(PerformanceHandlers.topic_type_selection, pattern="^topic_type\\|")],
        states={
            ConversationStates.DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, PerformanceHandlers.parse_perf_input)],
        },
        fallbacks=[],
    )
    
    # Modify conversation handler
    modify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("modify", PerformanceHandlers.start_modify)],
        states={
            ConversationStates.MODIFY_FIELD: [CallbackQueryHandler(PerformanceHandlers.get_modify_field_callback, pattern="^MODIFY\\|")],
            ConversationStates.MODIFY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, PerformanceHandlers.apply_modify_value)],
        },
        fallbacks=[CommandHandler("cancel", PerformanceHandlers.cancel)],
        allow_reentry=True,
    )
    
    # Verification conversation handler
    verify_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("verify", MembershipHandlers.start_verification)],
        states={
            ConversationStates.ASK_MATRIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, MembershipHandlers.handle_matric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    
    return perf_conv_handler, modify_conv_handler, verify_conv_handler

def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()  # ✅ Uses on_startup
    
    # Create conversation handlers
    perf_conv_handler, modify_conv_handler, verify_conv_handler = create_conversation_handlers()
    
    # Add command handlers (these correspond to the commands defined in commands.py)
    app.add_handler(CommandHandler("start", AdminHandlers.start))
    app.add_handler(CommandHandler("threadid", AdminHandlers.thread_id_command))
    app.add_handler(CommandHandler("remind", AdminHandlers.remind_command))
    app.add_handler(CommandHandler("confirmation", PerformanceHandlers.confirmation))
    app.add_handler(CommandHandler("poll", PollHandlers.send_poll_handler))
    app.add_handler(CommandHandler("verify", MembershipHandlers.start_verification))  # ✅ Public command
    
    # Conversation handlers
    app.add_handler(perf_conv_handler)
    app.add_handler(modify_conv_handler)
    app.add_handler(verify_conv_handler)
    
    # Callback query handlers
    app.add_handler(CallbackQueryHandler(PerformanceHandlers.topic_type_selection, pattern="^topic_type\\|"))
    app.add_handler(CallbackQueryHandler(PerformanceHandlers.confirmation_callback, pattern="^CONFIRM\\|"))
    app.add_handler(CallbackQueryHandler(PerformanceHandlers.final_date_selection, pattern="^FINALDATE\\|"))
    app.add_handler(CallbackQueryHandler(PerformanceHandlers.handle_modify_date_selection, pattern='^modify_date_selected\\|'))
    app.add_handler(CallbackQueryHandler(PerformanceHandlers.handle_modify_status_selection, pattern="^modify_status_selected\\|"))
    
    # Member management
    app.add_handler(ChatMemberHandler(MembershipHandlers.handle_member_status, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, MembershipHandlers.handle_new_member))
    app.add_handler(ChatJoinRequestHandler(MembershipHandlers.join_request_handler))
    
    # Poll handlers
    app.add_handler(PollAnswerHandler(PollHandlers.handle_poll_answer))
    
    # Message filtering (should be last)
    app.add_handler(MessageHandler(filters.ALL, MessageHandlers.handle_message))
    
    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise