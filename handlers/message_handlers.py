from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from services.google_sheets import is_main_admin
from utils.constants import initialized_topics

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

        user = update.effective_user
        if user and not user.is_bot and is_main_admin(user.id):
            initialized_topics.add(thread_id)
            print(f"[SYSTEM] Manual forum topic created by MAIN admin {user.id}. Permitted.")
            return

        if False and user and not user.is_bot:
            from services.google_sheets import get_cached_records
            
            try:
                # Fetch records from the RAM cache (Instant, no delay)
                records = get_cached_records(SHEET_NAME, MEMBER_INFO_TAB)
                user_role_lower = ""
                
                for row in records:
                    tele_id = str(row.get("Tele ID", "")).strip()
                    if tele_id == str(user.id):
                        # Safely handle both "Role" and "Role/Position" column headers
                        role_raw = row.get("Role/Position") or row.get("Role") or ""
                        user_role_lower = str(role_raw).strip().lower()
                        break

                # 🌟 MAIN ADMIN EXEMPTION CHECK (Substring Match)
                if user_role_lower and any(keyword in user_role_lower for keyword in MAIN_ADMIN_ROLE_KEYWORDS):
                    print(f"[SYSTEM] Core infrastructure topic manually created by {user_role_lower} (ID: {user.id}). Permitted.")
                    return
            except Exception as e:
                print(f"[SECURITY] Failed to verify topic creator role: {e}")

        # If it is an unrecognized thread manually created by a user, shut it down!
        try:
            await context.bot.delete_forum_topic(chat_id=chat.id, message_thread_id=thread_id)
            if user and not user.is_bot:
                try:
                    # 🎯 Updated Alert Text
                    await context.bot.send_message(
                        chat_id=user.id,
                        text="⚠️ *Manual Topic Creation Blocked*\nPlease use the 🎪 *New Topic* button inside the Command Centre (`/start`) to generate events.",
                        parse_mode="Markdown"
                    )
                except Exception: pass
        except Exception as e:
            print(f"[ERROR] Failed to execute manual topic deletion: {e}")
