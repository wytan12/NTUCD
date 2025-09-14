from functools import wraps
from telegram import Update, ChatMember
from telegram.ext import ContextTypes

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is admin"""
    member = await context.bot.get_chat_member(
        update.effective_chat.id, 
        update.effective_user.id
    )
    return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]

def admin_only(func):
    """Decorator to restrict function to admins only"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
            except Exception as e:
                print(f"[DELETE ERROR] {e}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper