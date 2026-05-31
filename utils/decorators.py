from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config import ADMIN_DM_USER_IDS

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the calling user is an administrator (or creator) of the
    current chat. Used to gate group commands like /poll.
    """
    try:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return False
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        print(f"[ERROR] is_admin check failed: {e}")
        return False


async def check_is_authenticated_admin(user_id: int) -> bool:
    """Helper to verify if a user ID exists within the admin configuration list/dict."""
    if isinstance(ADMIN_DM_USER_IDS, dict):
        if user_id in ADMIN_DM_USER_IDS or str(user_id) in ADMIN_DM_USER_IDS:
            return True
        # Check values in case IDs were mapped as dictionary values
        for k, v in ADMIN_DM_USER_IDS.items():
            if str(k).isdigit() and int(k) == user_id: return True
            if str(v).isdigit() and int(v) == user_id: return True
    elif isinstance(ADMIN_DM_USER_IDS, (list, set)):
        if user_id in ADMIN_DM_USER_IDS or str(user_id) in ADMIN_DM_USER_IDS:
            return True
    return False

def admin_only(func):
    """Decorator to restrict administrative execution to verified admin DMs only.

    Enforces 100% total passivity and silence for unauthorized access.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # 🤐 RULE 1: If typed inside group topics, stay 100% passive (no response, NO deletions)
        if update.effective_chat.type != "private":
            return

        # 🛡️ RULE 2: If inside private DM, strictly check admin credentials
        user_id = update.effective_user.id
        if not await check_is_authenticated_admin(user_id):
            print(f"[SECURITY] Passive block: Unauthorized private DM attempt by user ID {user_id}")
            return # Absolute dead silence

        return await func(update, context, *args, **kwargs)
    return wrapper