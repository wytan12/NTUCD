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
    """True if the user may use the admin DM dashboard.

    Role-driven: MAIN (chairperson / vice chair / secretary / SDE) and
    SECONDARY (treasurer / logistics / business manager / PNP) admins from the
    MEMBER INFO tab, plus the config ADMIN_DM_USER_IDS fallback ids.
    """
    from services.google_sheets import is_dashboard_admin
    # Commands (/start, /threadid) are MAJOR entry points — verify against the
    # live sheet so a just-granted role works immediately (throttled internally).
    return is_dashboard_admin(user_id, force=True)

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