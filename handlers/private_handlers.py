from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS, CHAT_ID
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary
from services.date_parser import parse_and_format_dates
from services.google_sheets import get_gspread_sheet
from services.google_sheets import append_standard_topic_to_sheet
from utils.constants import (
    WAITING_TOPIC_TITLE, WAITING_PERF_DETAILS, WAITING_EVENT_TYPE, TOPIC_RULES_CACHE,
    PERF_EVENT_NAME, PERF_REHEARSAL, PERF_DATE, PERF_LOCATION, PERF_OTHER_INFO,
)

ALL_PERMISSIONS = [
    "TEXT", "MEDIA", "MEDIA_DOC", "GEN_DOC",
    "POLLS", "POLL_REPLY", "STICKERS", "VOICE",
    "VIDEO_NOTE", "CONTACT", "LOC_VEN"
]

# Only EXT and INT event types
PERF_EVENT_TYPES = [
    ("🎭 EXT (External)", "EXT"),
    ("🏠 INT (Internal)", "INT"),
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
    remainder = stripped[first_space + 1:].lstrip()
    if not remainder:
        return command, None, ""

    idx = 0
    while idx < len(remainder) and remainder[idx].isdigit():
        idx += 1

    thread_token = remainder[:idx]
    payload = remainder[idx:].lstrip("\n\r ")
    return command, thread_token or None, payload


async def _show_perf_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the PERF summary preview with CONFIRM and CANCEL buttons."""
    ud = context.user_data
    event_type = ud.get("temp_event_type", "")
    event_name = ud.get("temp_event_name", "")
    rehearsal = ud.get("temp_rehearsal", "-")
    perf_date = ud.get("temp_perf_date", "")
    location = ud.get("temp_location", "")
    other_info = ud.get("temp_other_info", "")

    topic_title = f"PERF - {event_name}"
    summary = build_performance_summary(event_name, rehearsal, perf_date, location, other_info)
    preview = f"🆕 *Confirm New Performance?*\nTitle: `{topic_title}`\n\n{summary}"
    keyboard = [
        [InlineKeyboardButton("✅ CREATE & PUBLISH", callback_data="CONFIRM_NEW_PERF")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")],
    ]
    await update.effective_message.reply_text(
        preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def handle_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main DM entry point for admin commands.

    Only responds to users in ADMIN_DM_USER_IDS.  Manages a lightweight
    state machine via context.user_data["dm_state"] to handle multi-step
    flows (/new topic creation, modify field entry).  Single-step commands
    (list, announce, info, help) are dispatched directly.
    """
    message = update.effective_message
    if not message or not message.text or update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if user_id not in ADMIN_DM_USER_IDS:
        return

    text = message.text.strip()
    state = context.user_data.get("dm_state")

    # --- /new shortcut ---
    if text.lower() == "/new":
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
            [InlineKeyboardButton("🛠 STANDARD (Necessary)", callback_data="TYPE_SELECTED|STANDARD")],
        ]
        await message.reply_text("What kind of topic are you creating?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- PERF step-by-step: event name ---
    if state == PERF_EVENT_NAME:
        context.user_data["temp_event_name"] = text
        context.user_data["dm_state"] = PERF_REHEARSAL
        keyboard = [[InlineKeyboardButton("⏭ Skip (No Rehearsal)", callback_data="SKIP_PERF_FIELD|rehearsal")]]
        await message.reply_text(
            "📅 *Step 3/6: Rehearsal Date & Time*\n\n"
            "One date per line. Multiple times on the same day go on the same line, space-separated.\n\n"
            "*Examples:*\n"
            "Single date + time: `28 aug 8pm`\n"
            "Multiple times same day: `28 aug 8am 1030pm`\n"
            "Multiple days (Shift+Enter for new line):\n"
            "`28 aug 8am 1030pm`\n"
            "`30 aug 1130pm`\n\n"
            "Or click Skip if there's no rehearsal.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # --- PERF step-by-step: rehearsal date ---
    if state == PERF_REHEARSAL:
        try:
            rehearsal_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            await message.reply_text(str(e), parse_mode="Markdown")
            return
        context.user_data["temp_rehearsal"] = rehearsal_date
        context.user_data["dm_state"] = PERF_DATE
        await message.reply_text(
            "📅 *Step 4/6: Performance Date & Time*\n\n"
            "One date per line. Multiple times on the same day go on the same line, space-separated.\n\n"
            "*Examples:*\n"
            "Single date + time: `31 aug 9pm`\n"
            "Multiple times same day: `31 aug 7pm 9pm`\n"
            "Multiple days (Shift+Enter for new line):\n"
            "`31 aug 7pm`\n"
            "`1 sep 9pm`",
            parse_mode="Markdown",
        )
        return

    # --- PERF step-by-step: performance date ---
    if state == PERF_DATE:
        try:
            perf_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            await message.reply_text(str(e), parse_mode="Markdown")
            return
        context.user_data["temp_perf_date"] = perf_date
        context.user_data["dm_state"] = PERF_LOCATION
        await message.reply_text(
            "📌 *Step 5/6: Location*\nWhere is the performance?",
            parse_mode="Markdown",
        )
        return

    # --- PERF step-by-step: location ---
    if state == PERF_LOCATION:
        context.user_data["temp_location"] = text
        context.user_data["dm_state"] = PERF_OTHER_INFO
        keyboard = [[InlineKeyboardButton("⏭ Skip (No Additional Info)", callback_data="SKIP_PERF_FIELD|other_info")]]
        await message.reply_text(
            "📝 *Step 6/6: Other Info*\n"
            "Any additional info (e.g. dress code, requirements)?\n\n"
            "Or click Skip if there's nothing to add.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # --- PERF step-by-step: other info ---
    if state == PERF_OTHER_INFO:
        context.user_data["temp_other_info"] = text
        context.user_data["dm_state"] = None
        await _show_perf_summary(update, context)
        return

    # --- Step 1: waiting for Forum Topic title (OTHERS / STANDARD only) ---
    if state == WAITING_TOPIC_TITLE:
        context.user_data["temp_title"] = text
        topic_type = context.user_data.get("temp_type")

        if topic_type == "STANDARD":
            context.user_data["dm_state"] = None
            context.user_data["temp_rules"] = set()
            await handle_standard_setup(update, context, step="CHOOSE_RULES")
            return

        if topic_type == "OTHERS":
            keyboard = [
                [InlineKeyboardButton("✅ CREATE OTHERS TOPIC", callback_data="CONFIRM_NEW_OTHERS")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")],
            ]
            await message.reply_text(
                f"Confirm creating **OTHERS** topic: `{text}`?\n(This will NOT be added to the performance sheet.)",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            context.user_data["dm_state"] = None
            return

    # --- Ongoing modify field input ---
    if context.user_data.get("modify_field") and not text.startswith("/"):
        from handlers.modify_handlers import apply_modify_value
        await apply_modify_value(update, context)
        return

    # --- Single-step DM commands ---
    command, thread_token, payload = _parse_command_payload(text)

    if command == "new":
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
            [InlineKeyboardButton("🛠 STANDARD (Necessary)", callback_data="TYPE_SELECTED|STANDARD")],
        ]
        await message.reply_text("What kind of topic are you creating?", reply_markup=InlineKeyboardMarkup(keyboard))
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
                if str(tid).isdigit():
                    event_name = row.get("EVENT NAME", "Unnamed Event")
                    status = row.get("STATUS", "").strip().upper() or "PENDING"
                    emoji = "⏳" if status == "PENDING" else "✅" if status == "ACCEPTED" else "❌"
                    lines.append(f"{emoji} `{tid}` | **{event_name}**")
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
            await message.reply_text(
                build_performance_summary(
                    event_name=row.get("EVENT NAME", ""),
                    rehearsal_date=row.get("REHEARSAL DATE | TIME", ""),
                    perf_date=row.get("PERF DATE | TIME", ""),
                    location=row.get("LOCATION", ""),
                    other_info=row.get("OTHER INFO", ""),
                ),
                parse_mode="Markdown"
            )
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
    """Render the permission-rule checklist for creating a STANDARD topic.

    Builds an inline keyboard from ALL_PERMISSIONS with toggle checkmarks
    driven by context.user_data["temp_rules"].  Called both from text input
    (first entry) and from TOGGLE_RULE callbacks (re-renders on each toggle).
    """
    query = update.callback_query
    user_data = context.user_data

    if step == "CHOOSE_RULES":
        selected = user_data.get("temp_rules", set())
        keyboard = []
        for i in range(0, len(ALL_PERMISSIONS), 2):
            row = []
            for tag in ALL_PERMISSIONS[i:i + 2]:
                label = f"✅ {tag}" if tag in selected else tag
                row.append(InlineKeyboardButton(label, callback_data=f"TOGGLE_RULE|{tag}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("🚀 CONFIRM & CREATE", callback_data="CONFIRM_STANDARD")])
        keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")])

        text = f"🛠 **Rule Setup for: {user_data.get('temp_title')}**\nSelect what non-admins CAN do:"

        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_confirm_new_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatch all callback actions from the /new topic creation flow.

    Handles: TYPE_SELECTED (set type; PERF goes straight to event-type buttons,
    OTHERS/STANDARD go to title step), PERF_EVENT_TYPE (store event type, advance
    to event-name step), SKIP_PERF_FIELD (skip rehearsal or other-info step),
    TOGGLE_RULE (permission checklist toggle), CONFIRM_STANDARD (create STANDARD
    topic), CONFIRM_NEW_OTHERS (create OTHERS topic), CONFIRM_NEW_PERF (create PERF
    topic + post summary + interest poll), CANCEL_NEW_PERF (abort).
    """
    query = update.callback_query
    data = query.data
    await query.answer()

    # --- Topic type selection (PERF / OTHERS / STANDARD) ---
    if data.startswith("TYPE_SELECTED|"):
        selected_type = data.split("|")[1]
        context.user_data["temp_type"] = selected_type

        if selected_type == "PERF":
            # Skip the title step — title is auto-generated as "PERF - {event_name}"
            keyboard = [[InlineKeyboardButton(label, callback_data=f"PERF_EVENT_TYPE|{value}")]
                        for label, value in PERF_EVENT_TYPES]
            keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")])
            await query.edit_message_text(
                "🎭 *New PERF Topic*\n\n*Step 1/6: Event Type*\nIs this External or Internal?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        context.user_data["dm_state"] = WAITING_TOPIC_TITLE
        icon = "☕" if selected_type == "OTHERS" else "🛠"
        await query.edit_message_text(
            f"Selected Type: **{selected_type}** {icon}\n\n"
            "**Step 1:** Please enter the **Topic Title** for the group forum:",
            parse_mode="Markdown",
        )
        return

    # --- Event type selected for PERF (EXT / INT) ---
    if data.startswith("PERF_EVENT_TYPE|"):
        event_type = data.split("|")[1]
        context.user_data["temp_event_type"] = event_type
        context.user_data["dm_state"] = PERF_EVENT_NAME
        await query.edit_message_text(
            f"✅ Event type: *{event_type}*\n\n"
            "*Step 2/6: Event Name*\nWhat is the name of this performance?",
            parse_mode="Markdown",
        )
        return

    # --- Skip optional PERF fields ---
    if data.startswith("SKIP_PERF_FIELD|"):
        field = data.split("|")[1]

        if field == "rehearsal":
            context.user_data["temp_rehearsal"] = "-"
            context.user_data["dm_state"] = PERF_DATE
            await query.edit_message_text(
                "📅 *Step 4/6: Performance Date & Time*\n\n"
                "One date per line. Multiple times on the same day go on the same line, space-separated.\n\n"
                "*Examples:*\n"
                "Single date + time: `31 aug 9pm`\n"
                "Multiple times same day: `31 aug 7pm 9pm`\n"
                "Multiple days (Shift+Enter for new line):\n"
                "`31 aug 7pm`\n"
                "`1 sep 9pm`",
                parse_mode="Markdown",
            )

        elif field == "other_info":
            context.user_data["temp_other_info"] = ""
            context.user_data["dm_state"] = None
            ud = context.user_data
            summary = build_performance_summary(
                event_name=ud.get("temp_event_name", ""),
                rehearsal_date=ud.get("temp_rehearsal", "-"),
                perf_date=ud.get("temp_perf_date", ""),
                location=ud.get("temp_location", ""),
                other_info="",
            )
            topic_title = f"PERF - {ud.get('temp_event_name', '')}"
            preview = f"🆕 *Confirm New Performance?*\nTitle: `{topic_title}`\n\n{summary}"
            keyboard = [
                [InlineKeyboardButton("✅ CREATE & PUBLISH", callback_data="CONFIRM_NEW_PERF")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="CANCEL_NEW_PERF")],
            ]
            await query.edit_message_text(preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # --- Permission rule toggle for STANDARD ---
    if data.startswith("TOGGLE_RULE|"):
        tag = data.split("|")[1]
        if "temp_rules" not in context.user_data:
            context.user_data["temp_rules"] = set()
        rules = context.user_data["temp_rules"]
        if tag in rules:
            rules.remove(tag)
        else:
            rules.add(tag)
        await handle_standard_setup(update, context, step="CHOOSE_RULES")
        return

    if data == "CONFIRM_STANDARD":
        title = context.user_data.get("temp_title")
        rules = context.user_data.get("temp_rules", set())
        rules_str = ",".join(sorted(list(rules))) if rules else ""

        await query.edit_message_text(f"⏳ Creating STANDARD topic: `{title}`...")
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            tid = topic.message_thread_id
            TOPIC_RULES_CACHE[tid] = [r.upper() for r in list(rules)]
            append_standard_topic_to_sheet(tid, title, rules_str)
            await query.message.reply_text(f"✅ Created `{title}` (ID: `{tid}`)\nRules: `{rules_str or 'READ_ONLY'}`")
            context.user_data.clear()
        except Exception as e:
            await query.message.reply_text(f"❌ Failed: {e}")
        return

    # --- Create OTHERS topic ---
    if data == "CONFIRM_NEW_OTHERS":
        title = context.user_data.get("temp_title")
        await query.edit_message_text(f"⏳ Creating OTHERS topic: `{title}`...")
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            tid = topic.message_thread_id

            from utils.constants import OTHERS_THREAD_IDS, initialized_topics
            OTHERS_THREAD_IDS.add(tid)
            initialized_topics.add(tid)

            try:
                from services.google_sheets import append_to_others_list
                append_to_others_list(tid, title)
            except Exception as e:
                print(f"[ERROR] Sheet log failed: {e}")

            await query.message.reply_text(f"✅ Successfully created OTHERS Topic `{tid}`.")
            context.user_data.clear()
        except Exception as e:
            print(f"[ERROR] Topic creation block failed: {e}")
            await query.message.reply_text("❌ Failed to create topic. Check if I am Admin in the group.")
        return

    # --- Create PERF topic ---
    if data == "CONFIRM_NEW_PERF":
        ud = context.user_data
        event_type = ud.get("temp_event_type", "")
        event_name = ud.get("temp_event_name", "")
        rehearsal_date = ud.get("temp_rehearsal", "-")
        perf_date = ud.get("temp_perf_date", "")
        location = ud.get("temp_location", "")
        other_info = ud.get("temp_other_info", "")
        title = f"PERF - {event_name}"

        if not event_name or not perf_date or not location:
            await query.edit_message_text("❌ Data expired. Please use /new again.")
            return

        await query.edit_message_text("⏳ Processing: Creating Topic & Logging...")

        # Step 1: create forum topic
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            thread_id = topic.message_thread_id
            from utils.constants import initialized_topics
            initialized_topics.add(thread_id)
            print(f"[DEBUG] Forum topic created: {thread_id}")
        except Exception as e:
            print(f"[ERROR] Topic creation failed: {e}")
            await query.message.reply_text(f"❌ Failed to create forum topic: {e}")
            return

        # Step 2: write to Google Sheet
        try:
            sheet = get_gspread_sheet()
            sheet.append_row(
                [thread_id, event_type, event_name, rehearsal_date, perf_date, location, other_info, "", ""],
                value_input_option="USER_ENTERED",
            )
            print(f"[DEBUG] Sheet row appended for thread {thread_id}")
        except Exception as e:
            error_detail = e.response.text if hasattr(e, "response") else str(e)
            print(f"[ERROR] Sheet write failed: {error_detail}")
            await query.message.reply_text(f"❌ Topic created (ID: `{thread_id}`) but sheet write failed:\n`{error_detail}`", parse_mode="Markdown")
            return

        # Step 3: post pinned summary + interest poll
        try:
            group_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
            await publish_performance_summary(
                bot=context.bot,
                chat_data_store=group_data,
                sheet=sheet,
                chat_id=CHAT_ID,
                thread_id=thread_id,
                event_name=event_name,
                rehearsal_date=rehearsal_date,
                perf_date=perf_date,
                location=location,
                other_info=other_info,
            )
        except Exception as e:
            print(f"[ERROR] publish_performance_summary failed: {e}")
            await query.message.reply_text(f"⚠️ Topic + sheet done, but summary/poll failed: {e}")
            context.user_data.clear()
            return

        await query.message.reply_text(
            f"✅ *Success!* Topic `{thread_id}` (`{title}`) is ready.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return

    # --- Cancel ---
    if data == "CANCEL_NEW_PERF":
        context.user_data.clear()
        await query.edit_message_text("❌ Action cancelled.")
        return
