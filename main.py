from telegram import Update, Bot
from telegram.ext import ( 
    ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler,
    ConversationHandler, PollAnswerHandler, ChatMemberHandler, ChatJoinRequestHandler
)
from telegram.constants import ParseMode
import os
import json

from NTUCDConfig import (BOT_TOKEN, GOOGLE_CREDENTIALS_JSON, get_gspread_sheet, OTHERS_THREAD_IDS)
from command_handlers import (start_modify, )

# === Setup Bot ===
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
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))

    
    print("Bot is running...")
    
    try:
        others_sheet = get_gspread_sheet("OTHERS List")
        rows = others_sheet.col_values(1)
        OTHERS_THREAD_IDS.update({int(r.strip()) for r in rows if r.strip().isdigit()})
        print(f"[INFO] Loaded {len(OTHERS_THREAD_IDS)} OTHERS thread IDs.")
    except Exception as e:
        print(f"[ERROR] Failed to load OTHERS List: {e}")
    app.run_polling(allowed_updates=["chat_member"])

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] Bot failed to start: {e}")
        raise