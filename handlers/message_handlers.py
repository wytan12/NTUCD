from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from utils.constants import OTHERS_THREAD_IDS, initialized_topics

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming group messages seamlessly, keeping channels completely untouched by admin inputs."""
    msg = update.effective_message
    if not msg:
        return

    chat = update.effective_chat
    thread_id = msg.message_thread_id

    if msg.forum_topic_created:
        
        # If the Thread ID is matched in our unified approved set, allow it to pass immediately!
        if thread_id in initialized_topics:
            return

        # If it is an unrecognized thread manually created by a user, shut it down!
        try:
            await context.bot.delete_forum_topic(chat_id=chat.id, message_thread_id=thread_id)
            user = update.effective_user
            if user and not user.is_bot:
                try:
                    # 🎯 Updated Alert Text
                    await context.bot.send_message(
                        chat_id=user.id,
                        text="⚠️ **Manual Topic Creation Blocked**\nPlease use the 🎪 **New Topic** button inside the Command Centre (`/start`) to generate events.",
                        parse_mode="Markdown"
                    )
                except Exception: pass
        except Exception as e:
            print(f"[ERROR] Failed to execute manual topic deletion: {e}")