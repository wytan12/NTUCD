from telegram import Update
from telegram.ext import ContextTypes
from services.google_sheets import update_member_join_in_info, update_member_leave_in_info
from config import WELCOME_TEA_GROUP_CHAT_ID


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for NEW_CHAT_MEMBERS status updates.

    Stamps the member's MEMBER INFO row (Status=Join, Join Date) if the
    chat_member event was missed. Acts as a safety net alongside
    handle_member_status (idempotent — both write the same values).
    """
    if update.effective_chat.id == WELCOME_TEA_GROUP_CHAT_ID:
        print("[INFO] Skipping MEMBER INFO join tracking for the Welcome Tea group.")
        return

    for member in update.message.new_chat_members:
        if member.is_bot:
            print(f"[INFO] Skipping MEMBER INFO join stamp for bot {member.full_name} ({member.id}).")
            continue

        user_id = member.id
        name = member.first_name or member.last_name

        print(f"[INFO] ✅ New member joined: {name} ({user_id})")
        print(f"[DEBUG] Telegram user object: is_bot={member.is_bot}, full_name={member.full_name}")

        # Safety-net stamp into MEMBER INFO (idempotent — matched by Tele ID,
        # so it just refreshes the same row if handle_member_status already ran).
        try:
            update_member_join_in_info(user_id, name or "")
        except Exception as e:
            print(f"[MEMBER INFO][ERROR] fallback join stamp failed: {e}")

        # await update.effective_chat.send_message(
        #     f"👋 Welcome, {name}!"
        # )
        
async def handle_member_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track member join and leave events via ChatMember updates.

    On join/rejoin: stamps the member's MEMBER INFO row (Status=Join, Join
    Date=today, Leave Date cleared). On leave/kick: Leave Date=today,
    Status=Left. Rows are matched by Tele ID.
    """
    status_change = update.chat_member
    if status_change.chat.id == WELCOME_TEA_GROUP_CHAT_ID:
        print("[INFO] Skipping MEMBER INFO status tracking for the Welcome Tea group.")
        return

    old_status = status_change.old_chat_member.status
    new_status = status_change.new_chat_member.status
    user = status_change.new_chat_member.user

    if user.is_bot:
        print(f"[INFO] Skipping MEMBER INFO status tracking for bot {user.full_name} ({user.id}).")
        return
    
    print(f"[DEBUG] Status change for {user.full_name} ({user.id}): {old_status} ➝ {new_status}")

    # ✅ Detect join (first time OR rejoin)
    if old_status in ("left", "kicked") and new_status == "member":
        print(f"[INFO] 🎉 User {user.full_name} ({user.id}) has joined the group.")

        # MEMBER INFO AY26/27 (config MEMBER_INFO_TAB) is the single tracker:
        # stamp Join Date, clear any old Leave Date, set Status = Join — a
        # rejoin reuses the member's row, a new member gets a minimal row.
        try:
            update_member_join_in_info(user.id, user.full_name)
        except Exception as e:
            print(f"[MEMBER INFO][ERROR] join stamp failed: {e}")

    # ✅ Detect leave (either voluntarily or kicked)
    elif old_status in ("member", "administrator") and new_status in ("left", "kicked"):
        print(f"[INFO] 🚪 User {user.full_name} ({user.id}) has left the group.")
        try:
            update_member_leave_in_info(user.id)
        except Exception as e:
            print(f"[MEMBER INFO][ERROR] leave stamp failed: {e}")
