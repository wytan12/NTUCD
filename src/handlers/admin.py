from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import admin_only
from services.sheets import sheets_service

class AdminHandlers:
    @staticmethod
    @admin_only
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        thread_id = getattr(update.effective_message, "message_thread_id", "N/A")
        await update.message.reply_text("👋 Hi!")
        print(f"[DEBUG] Chat ID: {chat.id}, Thread ID: {thread_id}")
    
    @staticmethod
    @admin_only
    async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.is_topic_message:
            await msg.reply_text(f"🧵 This topic's thread ID is: {msg.message_thread_id}")
        else:
            await msg.reply_text("⚠️ This command must be used *inside a topic*.", parse_mode="Markdown")
        
        try:
            await update.message.delete()
        except Exception as e:
            print(f"[DEBUG] Failed to delete /threadid command: {e}")
    
    # Add other admin handlers here...