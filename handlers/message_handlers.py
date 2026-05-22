from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import is_admin
from utils.constants import OTHERS_THREAD_IDS, initialized_topics


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming group messages.

    Blocks topics created manually outside the bot flow (admins must use /new
    via DM).  All other messages from admins and members are allowed through
    without restriction.
    """
    msg = update.effective_message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user
    thread_id = msg.message_thread_id

    # Block topics created manually — only topics created via the bot's /new
    # flow should exist, so any manually-created topic is deleted and the
    # creator is informed via DM.
    if msg.forum_topic_created:
        if thread_id in OTHERS_THREAD_IDS or thread_id in initialized_topics:
            return

        try:
            await context.bot.delete_forum_topic(chat_id=chat.id, message_thread_id=thread_id)

            if user and not user.is_bot:
                try:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text="⚠️ **Manual Topic Creation Blocked**\nPlease use `/new` in our private chat."
                    )
                except Exception as dm_err:
                    print(f"[WARN] Could not DM user {user.id}: {dm_err}")
        except Exception as e:
            print(f"[ERROR] Failed to delete manual topic: {e}")
