from telegram import Update
from telegram.ext import ContextTypes
from services.google_sheets import (
    get_gspread_sheet,
    copy_user_to_timeline,
    user_already_in_timeline,
    mark_user_left_in_sheet
)
from config import WELCOME_TEA_SHEET, WELCOME_TEA_TAB


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for NEW_CHAT_MEMBERS status updates.

    Inserts a minimal row into the PERFORMER Info sheet if the user is not
    already present.  Acts as a safety net alongside handle_member_status.
    """
    for member in update.message.new_chat_members:
        user_id = member.id
        name = member.first_name or member.last_name

        print(f"[INFO] ✅ New member joined: {name} ({user_id})")
        print(f"[DEBUG] Telegram user object: is_bot={member.is_bot}, full_name={member.full_name}")

        if user_already_in_timeline(user_id):
            print(f"[INFO] User ID {user_id} already exists in timeline — skipping fallback insert.")
        else:
            # Fallback row → use name for both name & nickname
            fallback_row = {
                "Your Full Name (according to matric card)": name,
                "What name or nickname do you prefer to be called? ": name
            }
            copy_user_to_timeline(fallback_row, user_id)

        # await update.effective_chat.send_message(
        #     f"👋 Welcome, {name}!"
        # )
        
async def handle_member_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track member join and leave events via ChatMember updates.

    On first join: adds the member to PERFORMER Info if not already present.
    On leave/kick: stamps the member's row with status "Left" and the timestamp.
    """
    status_change = update.chat_member
    old_status = status_change.old_chat_member.status
    new_status = status_change.new_chat_member.status
    user = status_change.new_chat_member.user
    
    print(f"[DEBUG] Status change for {user.full_name} ({user.id}): {old_status} ➝ {new_status}")

    # ✅ Detect first join
    if old_status == "left" and new_status == "member":
        print(f"[INFO] 🎉 User {user.full_name} ({user.id}) has joined the group for the first time.")
        
        if not user_already_in_timeline(user.id):
            fallback_row = {
                "Your Full Name (according to matric card)": user.full_name,
                "What name or nickname do you prefer to be called? ": user.full_name
            }
            copy_user_to_timeline(fallback_row, user.id)
        else:
            print(f"[INFO] User {user.id} already exists in sheet — skip adding.")
    
    # ✅ Detect leave (either voluntarily or kicked)
    elif old_status in ("member", "administrator") and new_status in ("left", "kicked"):
        print(f"[INFO] 🚪 User {user.full_name} ({user.id}) has left the group.")
        success = mark_user_left_in_sheet(user.id)
        print(f"[INFO] Marked as 'Left' in sheet: {success}")
 