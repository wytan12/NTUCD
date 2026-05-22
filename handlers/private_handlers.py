from __future__ import annotations

from telegram import Update
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS, CHAT_ID
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary
from services.date_parser import parse_and_format_dates
from services.google_sheets import get_gspread_sheet 
from services.google_sheets import append_standard_topic_to_sheet
from utils.constants import WAITING_TOPIC_TITLE, WAITING_PERF_DETAILS, TOPIC_RULES_CACHE

ALL_PERMISSIONS = [
    "TEXT", "MEDIA", "MEDIA_DOC", "GEN_DOC", 
    "POLLS", "POLL_REPLY", "STICKERS", "VOICE", 
    "VIDEO_NOTE", "CONTACT", "LOC_VEN"
]

def _parse_command_payload(text: str) -> tuple[str, str | None, str]:
    """Extract the command keyword, thread id token, and remaining payload."""
    stripped = text.lstrip()
    if not stripped:
        return "", None, ""

    first_space = stripped.find(" ")
    if first_space == -1:
        return stripped.lower().lstrip("/"), None, ""

    command = stripped[:first_space].lower().lstrip("/")
    remainder = stripped[first_space + 1 :].lstrip()
    if not remainder:
        return command, None, ""

    idx = 0
    while idx < len(remainder) and remainder[idx].isdigit():
        idx += 1

    thread_token = remainder[:idx]
    payload = remainder[idx:].lstrip("\n\r ")
    return command, thread_token or None, payload

async def handle_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text or update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if user_id not in ADMIN_DM_USER_IDS:
        return

    text = message.text.strip()
    state = context.user_data.get("dm_state")

    # --- 1. NEW TOPIC FLOW ---
    if text.lower() == "/new":
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
            [InlineKeyboardButton("🛠 STANDARD (Necessary)", callback_data="TYPE_SELECTED|STANDARD")]        
        ]
        await message.reply_text("What kind of topic are you creating?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- 2. CONVERSATION STATES ---
    if state == WAITING_TOPIC_TITLE:
        context.user_data["temp_title"] = text
        topic_type = context.user_data.get("temp_type")

        if topic_type == "STANDARD":
            # Jump straight to Rule Selection
            context.user_data["dm_state"] = None
            context.user_data["temp_rules"] = set() # Initialize empty rules
            # We call the checklist function which we will define below
            await handle_standard_setup(update, context, step="CHOOSE_RULES")
            return
        
        # If it was OTHERS, we can finish now
        if context.user_data.get("temp_type") == "OTHERS":
            # We add a confirmation step with a CANCEL button
            keyboard = [
                [InlineKeyboardButton("✅ CREATE OTHERS TOPIC", callback_data="CONFIRM_NEW_OTHERS")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")]
            ]
            await message.reply_text(
                f"Confirm creating **OTHERS** topic: `{text}`?\n(This will NOT be added to the performance sheet.)", 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            context.user_data["dm_state"] = None # Stop waiting for text input
        else:
            # If it's PERF, continue to details
            context.user_data["dm_state"] = WAITING_PERF_DETAILS
            await message.reply_text("📝 **Step 2/2: Details**\nEnter: `Event // Date // Location // Info` (optional)")
        return

    if state == WAITING_PERF_DETAILS:
        parts = [p.strip() for p in text.split("//")]
        if len(parts) < 3:
            await message.reply_text("❌ Format wrong. Need at least `Event // Date // Location`.")
            return
        
        context.user_data["temp_parts"] = parts
        context.user_data["dm_state"] = None 
        
        title = context.user_data["temp_title"]
        event, raw_date, location = parts[0], parts[1], parts[2]
        info = parts[3] if len(parts) >= 4 else ""
        summary = build_performance_summary(event, raw_date, location, info)
        
        preview = f"🆕 **Confirm New Performance?**\nTitle: `{title}`\n\n{summary}"
        keyboard = [
            [InlineKeyboardButton("✅ CREATE & PUBLISH", callback_data="CONFIRM_NEW_PERF")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")]
        ]
        await message.reply_text(preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # Handle ongoing modification input
    if context.user_data.get("modify_field") and not text.startswith("/"):
        from handlers.modify_handlers import apply_modify_value
        await apply_modify_value(update, context)
        return

    # --- 2. COMMAND PARSING ---
    command, thread_token, payload = _parse_command_payload(text)

    if command == "new":
        context.user_data["dm_state"] = WAITING_TOPIC_TITLE
        await message.reply_text("🥁 **Step 1/2: Topic Title**\nWhat's the name of the new Forum Topic?")
        return

    if command == "list":
        try:
            sheet = get_gspread_sheet()
            records = sheet.get_all_records()
            
            if not records:
                await message.reply_text("📋 The performance sheet is currently empty.")
                return

            lines = ["📋 **Performance Overview**\n"]
            for row in records:
                tid = row.get("THREAD ID")
                # Only list rows that have a valid numeric Thread ID
                if str(tid).isdigit():
                    event = row.get("EVENT", "Unnamed Event")
                    status = row.get("STATUS", "").strip().upper() or "PENDING"
                    
                    # Determine status emoji
                    status_emoji = "⏳" if status == "PENDING" else "✅" if status == "ACCEPTED" else "❌"
                    
                    lines.append(f"{status_emoji} `{tid}` | **{event}**")
            
            await message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await message.reply_text(f"❌ Failed to fetch list: {e}")
        return

    if command == "announce":
        if not thread_token or not payload:
            await message.reply_text("Usage: `announce <thread_id> <message>`")
            return
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text=payload, message_thread_id=int(thread_token))
            await message.reply_text(f"✅ Sent to thread `{thread_token}`")
        except Exception as e:
            await message.reply_text(f"❌ Failed: {e}")
        return

    if command == "modify":
        from handlers.modify_handlers import initiate_modify_via_dm
        if not thread_token:
            await message.reply_text("Please provide a thread ID. Example: `modify 123` (Use `list` to see IDs)")
        else:
            await initiate_modify_via_dm(update, context, int(thread_token), True)
        return

    if command == "info" and thread_token:
        sheet = get_gspread_sheet()
        row = next((r for r in sheet.get_all_records() if str(r.get("THREAD ID")) == thread_token), None)
        if row:
            dt = row.get("CONFIRMED DATE | TIME") or row.get("PROPOSED DATE | TIME") or ""
            await message.reply_text(build_performance_summary(row['EVENT'], dt, row['LOCATION'], row['PERFORMANCE INFO']), parse_mode="Markdown")
        return

    if command == "help" or text == "/start":
        await message.reply_text(
            "🚀 **Admin DM Dashboard**\n\n"
            "• `/new` — Create new topic + sheet entry\n"
            "• `list` — See all thread IDs and events\n"
            "• `modify <id>` — Edit a performance\n"
            "• `announce <id> <msg>` — Message to group topic\n"
            "• `info <id>` — Preview summary in DM",
            parse_mode="Markdown"
        )

async def handle_standard_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, step="START"):
    query = update.callback_query
    user_data = context.user_data
    
    if step == "CHOOSE_RULES":
        selected = user_data.get("temp_rules", set())
        keyboard = []
        # Build checklist 2-by-2
        for i in range(0, len(ALL_PERMISSIONS), 2):
            row = []
            for tag in ALL_PERMISSIONS[i:i+2]:
                label = f"✅ {tag}" if tag in selected else tag
                row.append(InlineKeyboardButton(label, callback_data=f"TOGGLE_RULE|{tag}"))
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("🚀 CONFIRM & CREATE", callback_data="CONFIRM_STANDARD")])
        keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")])

        text = f"🛠 **Rule Setup for: {user_data.get('temp_title')}**\nSelect what non-admins CAN do:"
        
        # If this came from a text message, use reply_text, otherwise edit_message
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# --- CALLBACK HANDLER FOR THE CONFIRM BUTTON ---
async def handle_confirm_new_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    
    if data.startswith("TOGGLE_RULE|"):
        tag = data.split("|")[1]
        if "temp_rules" not in context.user_data:
            context.user_data["temp_rules"] = set()
        rules = context.user_data["temp_rules"]
        if tag in rules: rules.remove(tag)
        else: rules.add(tag)
        await handle_standard_setup(update, context, step="CHOOSE_RULES")
        return
    
    if data == "CONFIRM_STANDARD":
        title = context.user_data.get("temp_title")
        rules = context.user_data.get("temp_rules", set())
        rules_str = ",".join(sorted(list(rules))) if rules else ""
        
        await query.edit_message_text(f"⏳ Creating STANDARD topic: `{title}`...")
        try:
            # 1. Create Topic
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            tid = topic.message_thread_id
            
            # 2. Add to RAM Cache immediately
            from utils.constants import TOPIC_RULES_CACHE
            TOPIC_RULES_CACHE[tid] = [r.upper() for r in list(rules)]
            
            # 3. Log to Sheet
            append_standard_topic_to_sheet(tid, title, rules_str)
            
            await query.message.reply_text(f"✅ Created `{title}` (ID: `{tid}`)\nRules: `{rules_str or 'READ_ONLY'}`")
            context.user_data.clear()
        except Exception as e:
            await query.message.reply_text(f"❌ Failed: {e}")
        return
    
    # --- A. HANDLE TYPE SELECTION ---
    if data.startswith("TYPE_SELECTED|"):
        selected_type = data.split("|")[1]
        context.user_data["temp_type"] = selected_type
        context.user_data["dm_state"] = WAITING_TOPIC_TITLE
        
        icon = "🎭" if selected_type == "PERF" else "☕"
        await query.edit_message_text(
            f"Selected Type: **{selected_type}** {icon}\n\n"
            "**Step 1/2:** Please enter the **Topic Title** for the group forum:"
        )
        return

    # --- B. HANDLE OTHERS CREATION ---
    if data == "CONFIRM_NEW_OTHERS":
        title = context.user_data.get("temp_title")
        await query.edit_message_text(f"⏳ Creating OTHERS topic: `{title}`...")
        try:
            # 1. Create the topic
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            tid = topic.message_thread_id
            
            # 2. IMMEDIATELY add to memory sets before anything else
            from utils.constants import OTHERS_THREAD_IDS, initialized_topics
            OTHERS_THREAD_IDS.add(tid)
            initialized_topics.add(tid)

            # 3. Log to Sheet
            try:
                from services.google_sheets import append_to_others_list
                append_to_others_list(tid)
            except Exception as e:
                print(f"[ERROR] Sheet log failed: {e}")

            await query.message.reply_text(f"✅ Successfully created OTHERS Topic `{tid}`.")
            context.user_data.clear()
        except Exception as e:
            # This is likely where the 'Forbidden' error was caught
            print(f"[ERROR] Topic creation block failed: {e}")
            await query.message.reply_text("❌ Failed to create topic. Check if I am Admin in the group.")

    # --- C. HANDLE PERFORMANCE (PERF) CREATION ---
    if data == "CONFIRM_NEW_PERF":
        title = context.user_data.get("temp_title")
        parts = context.user_data.get("temp_parts")
        
        if not title or not parts:
            await query.edit_message_text("❌ Data expired. Please use /new again.")
            return

        await query.edit_message_text("⏳ Processing: Creating Topic & Logging...")

        try:
            # 1. Create Forum Topic
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            thread_id = topic.message_thread_id

            # --- NEW CRITICAL STEP: Add to memory IMMEDIATELY ---
            # This prevents message_handlers.py from deleting the topic
            from utils.constants import initialized_topics
            initialized_topics.add(thread_id)

            # 2. Extract details
            event, raw_date, location = parts[0], parts[1], parts[2]
            info = parts[3] if len(parts) >= 4 else ""
            formatted_dates = parse_and_format_dates(raw_date)
            date_text = "\n".join(formatted_dates)

            # 3. Write to Google Sheet
            sheet = get_gspread_sheet()
            sheet.append_row([thread_id, event, date_text, location, info, "", ""])

            # 4. Post Summary to the new topic
            group_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
            await publish_performance_summary(
                bot=context.bot,
                chat_data_store=group_data,
                sheet=sheet,
                chat_id=CHAT_ID,
                thread_id=thread_id,
                event=event,
                date_text=date_text,
                location=location,
                info=info,
            )

            await query.message.reply_text(f"✅ **Success!** Topic `{thread_id}` is ready.")
            context.user_data.clear()
            
        except Exception as e:
            print(f"[ERROR] Perf creation failed: {e}")
            await query.message.reply_text(f"❌ Error: {e}")
        return

    # --- D. HANDLE CANCEL ---
    if data == "CANCEL_NEW_PERF":
        context.user_data.clear()
        await query.edit_message_text("❌ Action cancelled.")
        return