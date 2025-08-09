from telegram import Update
from telegram.ext import ContextTypes, InlineKeyboardMarkup, InlineKeyboardButton, MessageHandler, filters
from utils.decorators import is_admin
from services.sheets import get_gspread_sheet
from config.settings import EXEMPTED_THREAD_IDS, OTHERS_THREAD_IDS, TOPIC_VOTING_ID, TOPIC_MEDIA_IDS, TOPIC_BLOCKED_ID
from config.settings import initialized_topics

class MessageHandlers:
    @staticmethod
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        chat = update.effective_chat  # ✅ always handy!
        thread_id = msg.message_thread_id
        user_is_admin = await is_admin(update, context)

        print(f"[DEBUG] handle_message called by user {user.id} in thread {thread_id}")
        print(f"[DEBUG] user_is_admin: {user_is_admin}")

        if thread_id in EXEMPTED_THREAD_IDS or thread_id in OTHERS_THREAD_IDS:
            pass
        else:
            if msg.is_topic_message and thread_id not in initialized_topics:
                initialized_topics.add(thread_id)

                # testing start
                sheet = get_gspread_sheet()
                records = sheet.get_all_records()
                for idx, row in enumerate(records):
                    if str(row["THREAD ID"]) == str(thread_id):
                        return
                # testing end

                if user_is_admin:
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("PERF", callback_data=f"topic_type|PERF|{thread_id}")],
                        [InlineKeyboardButton("OTHERS", callback_data=f"topic_type|OTHERS|{thread_id}")]
                    ])
                    prompt = await chat.send_message(
                        "What's the topic for? PERF or OTHERS?",
                        reply_markup=keyboard,
                        message_thread_id=thread_id
                    )
                    context.chat_data[f"init_prompt_{thread_id}"] = prompt.message_id

                # ✅ SAFE DELETE
                if chat.type in ["group", "supergroup"]:
                    await msg.delete()
                else:
                    print(f"[DEBUG] Not a group — skip delete.")
                return

        # === THREAD MESSAGE RESTRICTIONS ===
        # restrict all actions except for admin users
        if not user_is_admin:

            # Restrict all command messages (e.g., /command)
            if msg.text and msg.text.startswith("/"):
                print(f"[DEBUG] User {user.id} sent command in thread {thread_id}. Deleting.")
                if chat.type in ["group", "supergroup"]:
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to delete command message: {e}")
                else:
                    print(f"[DEBUG] Not a group — skip delete.")

            # About NTUCD — block all messages
            if thread_id is None:
                print(f"[DEBUG] Message in ABOUT NTUCD. Deleting.")
                if chat.type in ["group", "supergroup"]:
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to delete message in ABOUT thread: {e}")
                else:
                    print(f"[DEBUG] Not a group — skip delete.")

            # Voting Topic — only allow replies to polls
            elif thread_id == TOPIC_VOTING_ID:
                if msg.poll or not (msg.reply_to_message and msg.reply_to_message.poll):
                    print(f"[DEBUG] Message not a poll or poll reply in VOTING. Deleting.")
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to delete message in VOTING: {e}")

            elif thread_id in TOPIC_MEDIA_IDS:
                is_photo = msg.photo is not None
                is_video = msg.video is not None
                is_valid_doc = (
                    msg.document is not None and
                    getattr(msg.document, "mime_type", "").startswith(("image/", "video/"))
                )

                is_gif = msg.animation is not None
                is_sticker = msg.sticker is not None
                is_voice = msg.voice is not None
                is_audio = msg.audio is not None
                is_text = msg.text is not None
                is_contact = msg.contact is not None
                is_location = msg.location is not None
                is_venue = msg.venue is not None
                is_poll = msg.poll is not None
                is_video_note = msg.video_note is not None
                
                # is_web_app = getattr(msg, "web_app_data", None) is not None
                # is_via_bot = msg.via_bot is not None

                if is_gif or is_sticker or is_voice or is_audio or is_contact or is_location or is_venue or is_poll or is_video_note:
                    print(f"[DEBUG] ❌ msg in MEDIA thread {thread_id}. Deleting.")
                    await msg.delete()
                # elif is_web_app or is_via_bot:
                #     print(f"[DEBUG] ❌ Web App or Telebubble message in MEDIA thread {thread_id}. Deleting.")
                #     await msg.delete()
                elif is_text:
                    print(f"[DEBUG] ❌ Text message in MEDIA thread {thread_id}. Deleting.")
                    await msg.delete()
                elif msg.document:
                    print(f"[DEBUG] ❌ Invalid document type: {msg.document.mime_type} in MEDIA thread {thread_id}. Deleting.")
                    await msg.delete()
                elif not (is_photo or is_video or is_valid_doc):
                    print(f"[DEBUG] ❌ Non-media message in MEDIA thread {thread_id}. Deleting.")
                    print(f"[INFO] Message type details: {msg}")
                    await msg.delete()
                else:
                    print(f"[DEBUG] Valid media message{msg} in MEDIA thread {thread_id}.")
                    print(f"[ALLOWED] ✅ Valid media message in MEDIA thread {thread_id}")

            # BLOCKED Topic — block all messages
            elif thread_id == TOPIC_BLOCKED_ID:
                print(f"[DEBUG] User {user.id} tried to send message in BLOCKED thread {thread_id}. Deleting.")
                print(f"[INFO] Deleted message type: {msg}")
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to delete in BLOCKED topic: {e}")

        # === Fallback log for all messages
        else:
            print(f"[INFO] Message from admin in thread {thread_id}: allowed.")
                
