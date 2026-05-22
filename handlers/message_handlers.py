from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from utils.decorators import is_admin
from config import TOPIC_VOTING_ID, TOPIC_MEDIA_IDS, TOPIC_BLOCKED_ID, EXEMPTED_THREAD_IDS
from utils.constants import  OTHERS_THREAD_IDS, initialized_topics
from services.google_sheets import get_gspread_sheet

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user
    thread_id = msg.message_thread_id
    user_is_admin = await is_admin(update, context)

    # --- 1. PROHIBIT MANUAL TOPIC CREATION ---
    # Triggered when an admin manually creates a topic in the group instead of using DM
    if msg.forum_topic_created:
        # If the thread ID is already in our 'safe' sets, DO NOT DELETE
        if thread_id in OTHERS_THREAD_IDS or thread_id in initialized_topics:
            return

        try:
            await context.bot.delete_forum_topic(chat_id=chat.id, message_thread_id=thread_id)
            
            # Only send DM if the creator is a real person and NOT the bot itself
            if user and not user.is_bot:
                try:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text="⚠️ **Manual Topic Creation Blocked**\nPlease use `/new` in our private chat."
                    )
                except Exception as dm_err:
                    print(f"[WARN] Could not DM user {user.id}: {dm_err}")
            return
        except Exception as e:
            print(f"[ERROR] Failed to delete manual topic: {e}")
            return

    # --- 2. ADMIN WHITELIST ---
    if user_is_admin:
        return  # Admins bypass all chat restrictions

    # --- 3. SPECIFIC THREAD RESTRICTIONS (For Non-Admins) ---
    
    # A. Restricted Commands
    if msg.text and msg.text.startswith("/"):
        if chat.type in ["group", "supergroup"]:
            try: await msg.delete()
            except: pass
        return

    # B. General/About Topic (thread_id is None)
    if thread_id is None:
        if chat.type in ["group", "supergroup"]:
            try: await msg.delete()
            except: pass
        return

    # C. Voting Topic - Only allow replies to polls
    elif thread_id == TOPIC_VOTING_ID:
        if not (msg.poll or (msg.reply_to_message and msg.reply_to_message.poll)):
            try: await msg.delete()
            except: pass
        return

    # D. Media Topics - ALLOW TEXT & MEDIA, BLOCK NOISE
    elif thread_id in TOPIC_MEDIA_IDS:
        # Define specific "Noise" objects to block
        is_noise = (
            msg.sticker or 
            msg.animation or # GIFs
            msg.voice or 
            msg.audio or 
            msg.video_note or 
            msg.contact or 
            msg.location or 
            msg.venue or
            msg.poll is not None  # Explicitly catch Polls here
        )
        
        if is_noise:
            print(f"[DEBUG] ❌ Blocked noise/poll in MEDIA thread {thread_id}. Deleting.")
            try:
                await msg.delete()
            except Exception as e:
                print(f"[ERROR] Failed to delete noise: {e}")
            return # Stop here so it doesn't hit the 'allow' return below

        # Validate Documents (Only allow image/video files)
        if msg.document:
            is_valid_doc = getattr(msg.document, "mime_type", "").startswith(("image/", "video/"))
            if not is_valid_doc:
                print(f"[DEBUG] ❌ Invalid document type in MEDIA thread {thread_id}. Deleting.")
                try: await msg.delete()
                except: pass
                return
        
        # Plain text, replies, photos, and videos are ALLOWED here.
        return

    # E. Blocked/Score Topic - No non-admin chat
    elif thread_id == TOPIC_BLOCKED_ID:
        try: await msg.delete()
        except: pass
        return

    # --- 4. ALLOW BASIC CHAT IN REGISTERED THREADS ---
    # Allows normal chat in Performance or Others topics created via the bot
    if thread_id in OTHERS_THREAD_IDS or thread_id in initialized_topics:
        return 

    # F. FALLBACK: Block non-admin messages in any unknown/unregistered thread
    if thread_id not in EXEMPTED_THREAD_IDS:
        try: await msg.delete()
        except: pass
