from __future__ import annotations
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import ADMIN_DM_USER_IDS, CHAT_ID
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary
from services.date_parser import parse_and_format_dates
from services.google_sheets import get_gspread_sheet, get_cached_records, get_cached_values, invalidate_sheet_cache
from utils.constants import (
    WAITING_TOPIC_TITLE, PERF_EVENT_NAME, PERF_REHEARSAL, PERF_DATE, PERF_LOCATION, PERF_OTHER_INFO,
)

PERF_EVENT_TYPES = [
    ("🎭 EXT (External)", "EXT"),
    ("🏠 INT (Internal)", "INT"),
]

# Per-session snapshot keys mirrored in user_data. The shared module cache
# (services.google_sheets) auto-refreshes via a modifiedTime check, so these are
# only convenience copies; the dashboard "🔄 Refresh" also force-invalidates the
# shared cache so the next read re-downloads immediately.
_DASHBOARD_CACHE_KEYS = (
    "cached_records",
    "attd_dates",
)

DASHBOARD_TEXT = (
    "🚀 **Admin DM Dashboard**\n\n"
    "• `/new` — Create new topic + sheet entry\n"
    "• `/list` — See all thread IDs and events\n"
    "• `/modify` — Edit a performance details\n"
    "• `/attd` — Mark regular-training attendance\n"
    "• `/announce` — Broadcast a multi-line format message to any topic\n"
    "• `/remind` — Trigger manual checklist reminder announcement\n"
    "• `/threadid` — Print the entire group topic directory chart\n"
    "• `/info <id>` — Preview summary in DM\n\n"
    "_Lists are cached for instant back-navigation. Tap 🔄 Refresh after editing "
    "the sheet directly in your browser to force a fresh pull._"
)

def _dashboard_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for the dashboard — a single refresh control."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Data", callback_data="DASH_REFRESH")]
    ])

def _parse_command_payload(text: str) -> tuple[str, str | None, str]:
    """Extract the command keyword, thread id token, and remaining payload.

    A command is recognised **only** when the text begins with a leading slash
    (e.g. `/modify`, `/announce 5 hi`). Plain text with no slash returns an empty
    command, so it falls through to the multi-step flow state machine (event
    names, dates, announcement text, …) or is ignored — typing `modify` or
    `Modify start` will NOT trigger the command.
    """
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return "", None, ""
    # Drop the leading slash, then tokenise the rest.
    stripped = stripped[1:].lstrip()
    if not stripped:
        return "", None, ""
    first_space = stripped.find(" ")
    if first_space == -1:
        return stripped.lower(), None, ""
    command = stripped[:first_space].lower()
    remainder = stripped[first_space + 1:].lstrip()
    if not remainder:
        return command, None, ""
    idx = 0
    while idx < len(remainder) and remainder[idx].isdigit():
        idx += 1
    thread_token = remainder[:idx]
    payload = remainder[idx:].lstrip("\n\r ")
    return command, thread_token or None, payload

def _build_progress_tracker(ud: dict, current_step: int) -> str:
    """Compile a dynamic status card tracking what the admin has typed."""
    lines = ["📝 *Current Progress Tracker*"]
    if ud.get("temp_event_type"):
        lines.append(f"✅ *Event Type:* {ud['temp_event_type']}")
    if ud.get("temp_event_name"):
        lines.append(f"✅ *Event Name:* {ud['temp_event_name']}")
    rehearsal = ud.get("temp_rehearsal")
    if rehearsal:
        if rehearsal == "-":
            lines.append("✅ *Rehearsal Date:* None")
        else:
            r_formatted = "\n" + "\n".join([f"  • {line.strip()}" for line in rehearsal.splitlines() if line.strip()])
            lines.append(f"✅ *Rehearsal Date:* {r_formatted}")
    perf = ud.get("temp_perf_date")
    if perf:
        p_formatted = "\n" + "\n".join([f"  • {line.strip()}" for line in perf.splitlines() if line.strip()])
        lines.append(f"✅ *Perf Date:* {p_formatted}")
    if ud.get("temp_location"):
        lines.append(f"✅ *Location:* {ud['temp_location']}")
    if ud.get("temp_other_info"):
        lines.append(f"✅ *Other Info:* {ud['temp_other_info']}")
    lines.append("—" * 15)
    return "\n".join(lines) + "\n\n"

def _get_back_keyboard(field_to_clear: str | None) -> InlineKeyboardMarkup:
    """Generate layout rows allowing users to step backward configuration states securely."""
    buttons = []
    if field_to_clear:
        buttons.append([InlineKeyboardButton("🔙 Edit Previous Step", callback_data=f"PERF_RESET|{field_to_clear}")])
    buttons.append([InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")])
    return InlineKeyboardMarkup(buttons)

async def _render_step(message, text: str, reply_markup=None):
    """Utility wrapper to post updates cleanly, preserving conversation context flows."""
    return await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def _show_perf_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send final summary dashboard preview containing explicit field controls."""
    ud = context.user_data
    event_name = ud.get("temp_event_name", "")
    rehearsal = ud.get("temp_rehearsal", "-")
    perf_date = ud.get("temp_perf_date", "")
    location = ud.get("temp_location", "")
    other_info = ud.get("temp_other_info", "")

    topic_title = f"PERF - {event_name}"
    summary = build_performance_summary(event_name, rehearsal, perf_date, location, other_info)
    preview = f"🆕 *Confirm New Performance Topic?*\nTitle: `{topic_title}`\n\n{summary}"
    
    keyboard = [
        [InlineKeyboardButton("✅ Create & Publish", callback_data="CONFIRM_NEW_PERF")],
        [InlineKeyboardButton("✏️ Edit Event Type", callback_data="PERF_RESET|temp_event_type")],
        [InlineKeyboardButton("✏️ Edit Event Name", callback_data="PERF_RESET|temp_event_name")],
        [InlineKeyboardButton("✏️ Edit Rehearsal Dates", callback_data="PERF_RESET|temp_rehearsal")],
        [InlineKeyboardButton("✏️ Edit Perf Dates", callback_data="PERF_RESET|temp_perf_date")],
        [InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")],
    ]
    
    msg = await update.effective_message.reply_text(
        preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )
    context.user_data["final_preview_msg_id"] = msg.message_id

async def initiate_announce_portal_via_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, incoming_query=None) -> None:
    """Reusable interactive matrix builder for announcements using absolute local memory caching profiles."""
    target_chat = update.effective_user

    status_loading = None
    if incoming_query is None:
        status_loading = await context.bot.send_message(chat_id=target_chat.id, text="⏳ Generating channel communication routing links...")

    # 🧠 SMART CACHE: get_cached_records / get_cached_values do a tiny modifiedTime
    # check — they re-download only when the sheet actually changed.
    records = get_cached_records()
    cached_others = []
    try:
        for o_row in get_cached_values(tab_name="OTHERS"):
            if o_row and str(o_row[0]).isdigit():
                cached_others.append((o_row[0], o_row[1] if len(o_row) > 1 and o_row[1] else "Others Thread"))
    except Exception:
        cached_others = []

    buttons = [[InlineKeyboardButton("💬 General Topic (Main)", callback_data="ANNOUNCE_TARGET|0|General Topic")]]
    
    # Render main sheet items directly from local storage rows
    for row in records:
        tid = row.get("THREAD ID")
        if str(tid).isdigit():
            event_name = row.get("EVENT NAME", "Unnamed Event")
            buttons.append([InlineKeyboardButton(f"🎭 {event_name} ({tid})", callback_data=f"ANNOUNCE_TARGET|{tid}|{event_name}")])
            
    # Render OTHERS items directly from local storage rows
    for o_tid, o_name in cached_others:
        buttons.append([InlineKeyboardButton(f"☕ {o_name} ({o_tid})", callback_data=f"ANNOUNCE_TARGET|{o_tid}|{o_name}")])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")])
    
    prompt_text = (
        "📢 *ANNOUNCEMENT Portal*\n"
        "Please select which TOPIC you want to announce into:"
    )
    
    if incoming_query:
        # 🚀 LIGHTNING SPEED: Swapping menus instantly because ALL data paths are now 100% running off memory!
        await incoming_query.edit_message_text(text=prompt_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        if status_loading: await status_loading.delete()
        await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def render_modify_list(update: Update, context: ContextTypes.DEFAULT_TYPE, incoming_query=None) -> None:
    """Render the PERFORMANCE modification list (first layer of the /modify flow).

    Reused both by the `/modify` command and by the "🔙 Back to List" button on the
    field-selection menu. Data comes from ``get_cached_records()`` which does a
    tiny Drive modifiedTime check: it serves the cached rows instantly while the
    sheet is unchanged, and only re-downloads when the sheet was actually edited.
    """
    target_chat = update.effective_user
    try:
        status_loading = None
        if incoming_query is None:
            status_loading = await context.bot.send_message(chat_id=target_chat.id, text="⏳ Fetching active performance log...")
        records = get_cached_records()
        context.user_data["cached_records"] = records
        if not records:
            text = "📋 The performance sheet is empty."
            if incoming_query:
                await incoming_query.edit_message_text(text)
            elif status_loading:
                await status_loading.edit_text(text)
            else:
                await context.bot.send_message(chat_id=target_chat.id, text=text)
            return
        buttons = []
        for row in records:
            tid = row.get("THREAD ID")
            if str(tid).isdigit():
                event_name = row.get("EVENT NAME", "Unnamed Event")
                buttons.append([InlineKeyboardButton(f"⚙️ {event_name} ({tid})", callback_data=f"LIST_MODIFY|{tid}")])
        prompt_text = (
            "🛠 *PERFORMANCE Modification Portal*\n\n"
            "Which PERFORMANCE topic you would like to modify:"
        )
        markup = InlineKeyboardMarkup(buttons)
        if incoming_query:
            await incoming_query.edit_message_text(prompt_text, reply_markup=markup, parse_mode="Markdown")
        else:
            if status_loading:
                await status_loading.delete()
            await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        err = f"❌ Failed to load modification dashboard menu: {e}"
        if incoming_query:
            await incoming_query.edit_message_text(err)
        else:
            await context.bot.send_message(chat_id=target_chat.id, text=err)


async def handle_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main private DM state machine routing engine for administrative accounts."""
    message = update.effective_message
    if not message or not message.text or update.effective_chat.type != "private":
        return

    # Check if user ID belongs to the approved admin set values
    is_approved_admin = False
    current_uid = update.effective_user.id
    
    if isinstance(ADMIN_DM_USER_IDS, dict):
        for k, v in ADMIN_DM_USER_IDS.items():
            if str(k).isdigit() and int(k) == current_uid: is_approved_admin = True
            if str(v).isdigit() and int(v) == current_uid: is_approved_admin = True
    elif isinstance(ADMIN_DM_USER_IDS, (list, set)):
        if current_uid in ADMIN_DM_USER_IDS: is_approved_admin = True
        
    if not is_approved_admin:
        print(f"[SECURITY] Unauthorized access attempt blocked for user ID: {current_uid}")
        return

    text = message.text.strip()

    # 🚨 THE STRICT MENU-COMMAND WHITELIST: Define your actual clickable commands
    OFFICIAL_MENU_COMMANDS = {
        "/start", "/modify", "/list", "/attd", "/attendance", 
        "/announce", "/remind", "/testremind", "/threadid", "/cancel"
    }

    # Only cancel if the message is exactly one of your registered dashboard buttons
    if text.lower() in OFFICIAL_MENU_COMMANDS:
        if context.user_data.get("dm_state") is not None or context.user_data.get("waiting_announcement_text") is not None or context.user_data.get("modify_field") is not None:
            context.user_data.clear() # 🧼 Wipes the active session parameters cleanly
            await message.reply_text(
                "🛑 **Wizard Cancelled**\n"
                "Your active configuration flow was terminated because an official menu shortcut option was clicked.",
                parse_mode="Markdown"
            )
    command, thread_token, payload = _parse_command_payload(text)

    # 🧠 ATTENDANCE DATE-MODIFY TEXT CAPTURE: if an attd date change is awaiting a
    # typed new date, consume this (non-command) message as that date.
    if not text.startswith("/"):
        from handlers.attendance_handlers import handle_moddate_text
        if await handle_moddate_text(update, context):
            return

    # 🧠 EMERGENCY TESTREMIND ESCAPE LATCH
    if command == "testremind":
        context.user_data.pop("modify_field", None)
        from handlers.admin_handlers import execute_manual_test_scan
        await execute_manual_test_scan(update, context)
        return ConversationHandler.END

    # 🧠 NEW REMIND DM REDIRECT HOOK: If admin types 'remind' in private DM, show menu!
    if command == "remind":
        context.user_data.pop("modify_field", None)
        from handlers.admin_handlers import initiate_remind_portal_via_dm
        await initiate_remind_portal_via_dm(update, context)
        return ConversationHandler.END

    if context.user_data.get("waiting_announcement_text") is not None:
        target_thread = context.user_data.pop("waiting_announcement_text")
        display_target_name = context.user_data.pop("waiting_announcement_name", "the group topic")
        try:
            thread_param = None if target_thread == 0 else target_thread
            
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message.text_markdown,
                message_thread_id=thread_param,
                parse_mode="Markdown"
            )
            # 🎯 FIXED: Standardized text feedback to explicitly echo your selected Event Name string!
            await message.reply_text(f"✅ Announcement broadcasted successfully into *{display_target_name}*!", parse_mode="Markdown")
        except Exception as e:
            await message.reply_text(f"❌ Failed to dispatch announcement: {e}")
        return

    if text.lower() == "/new":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
        ]
        await message.reply_text("What kind of topic are you creating?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    state = context.user_data.get("dm_state")

    if state == PERF_EVENT_NAME:
        context.user_data["temp_event_name"] = text
        context.user_data["dm_state"] = PERF_REHEARSAL
        tracker = _build_progress_tracker(context.user_data, 3)
        prompt = (
            f"{tracker}📅 *Step 3/6: Rehearsal Date & Time*\n\n"
            
            "*Examples:*\n"
            "Single date Single time: `31 aug, 9pm`\n"
            "Single date Multiple times: `1 sep, 7pm 9pm`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug, 9pm`\n"
            "`1 sep, 7pm 9pm`\n\n"

            "Separate the date and time using a **comma (,)**."
        )
        keyboard = [
            [InlineKeyboardButton("⏭ Skip (No Rehearsal)", callback_data="SKIP_PERF_FIELD|rehearsal")],
            [InlineKeyboardButton("↩️ Edit Event Name", callback_data="PERF_RESET|temp_event_name")],
            [InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")]
        ]
        await _render_step(message, prompt, InlineKeyboardMarkup(keyboard))
        return

    if state == PERF_REHEARSAL:
        try: rehearsal_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            await message.reply_text(str(e), parse_mode="Markdown")
            return
        context.user_data["temp_rehearsal"] = rehearsal_date
        context.user_data["dm_state"] = PERF_DATE
        tracker = _build_progress_tracker(context.user_data, 4)
        prompt = (
            f"{tracker}📅 *Step 4/6: Performance Date & Time*\n\n"
            
            "*Examples:*\n"
            "Single date Single time: `31 aug, 9pm`\n"
            "Single date Multiple times: `1 sep, 7pm 9pm`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug, 9pm`\n"
            "`1 sep, 7pm 9pm`\n\n"

            "Separate the date and time using a **comma (,)**."
        )
        await _render_step(message, prompt, _get_back_keyboard("temp_rehearsal"))
        return

    if state == PERF_DATE:
        try: perf_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            await message.reply_text(str(e), parse_mode="Markdown")
            return
        context.user_data["temp_perf_date"] = perf_date
        context.user_data["dm_state"] = PERF_LOCATION
        tracker = _build_progress_tracker(context.user_data, 5)
        prompt = f"{tracker}📌 *Step 5/6: Location*\nWhere is the performance taking place?"
        await _render_step(message, prompt, _get_back_keyboard("temp_perf_date"))
        return

    if state == PERF_LOCATION:
        context.user_data["temp_location"] = text
        context.user_data["dm_state"] = PERF_OTHER_INFO
        tracker = _build_progress_tracker(context.user_data, 6)
        prompt = f"{tracker}📝 *Step 6/6: Other Info*\nAny additional info?"
        keyboard = [
            [InlineKeyboardButton("⏭ Skip (No Additional Info)", callback_data="SKIP_PERF_FIELD|other_info")],
            [InlineKeyboardButton("↩️ Edit Location", callback_data="PERF_RESET|temp_location")],
            [InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")]
        ]
        await _render_step(message, prompt, InlineKeyboardMarkup(keyboard))
        return

    if state == PERF_OTHER_INFO:
        context.user_data["temp_other_info"] = text
        context.user_data["dm_state"] = None
        await _show_perf_summary(update, context)
        return

    if state == WAITING_TOPIC_TITLE:
        context.user_data["temp_title"] = text
        if context.user_data.get("temp_type") == "OTHERS":
            keyboard = [
                [InlineKeyboardButton("✅ Create Others Topic", callback_data="CONFIRM_NEW_OTHERS")],
                [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")],
            ]
            await message.reply_text(f"Confirm creating **OTHERS** topic: `{text}`?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            context.user_data["dm_state"] = None
            return

    if context.user_data.get("modify_field") and not text.startswith("/"):
        from handlers.modify_handlers import apply_modify_value
        await apply_modify_value(update, context)
        return

    if command == "new":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
        ]
        await message.reply_text("What kind of topic are you creating?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if command == "list":
        status_loading = await message.reply_text("⏳ Fetching live performance list...")
        try:
            records = get_cached_records()
            if not records:
                await status_loading.edit_text("📋 The performance sheet is empty.")
                return
            lines = ["📋 **Performance Overview**\n"]
            for row in records:
                tid = row.get("THREAD ID")
                if str(tid).isdigit():
                    event_name = row.get("EVENT NAME", "Unnamed Event")
                    status = row.get("STATUS", "").strip().upper() or "PENDING"
                    emoji = "⏳" if status == "PENDING" else "✅" if status == "ACCEPTED" else "❌"
                    lines.append(f"{emoji} `{tid}` | **{event_name}**")
            await status_loading.delete()
            await message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await status_loading.edit_text(f"❌ Failed to fetch overview log: {e}")
        return

    if command == "modify":
        from handlers.modify_handlers import initiate_modify_via_dm
        if thread_token:
            await initiate_modify_via_dm(update, context, int(thread_token), True)
            return
        await render_modify_list(update, context)
        return

    if command == "announce":
        await initiate_announce_portal_via_dm(update, context)
        return

    if command == "info" and thread_token:
        status_loading = await message.reply_text(f"⏳ Downloading details card snapshot for ID {thread_token}...")
        row = next((r for r in get_cached_records() if str(r.get("THREAD ID")) == thread_token), None)
        await status_loading.delete()
        if row:
            await message.reply_text(
                build_performance_summary(
                    event_name=row.get("EVENT NAME", ""), rehearsal_date=row.get("REHEARSAL DATE | TIME", ""),
                    perf_date=row.get("PERF DATE | TIME", ""), location=row.get("LOCATION", ""), other_info=row.get("OTHER INFO", ""),
                ), parse_mode="Markdown"
            )
        else:
            await message.reply_text("❌ Performance ID not found inside Google Sheets records.")
        return

    if command in ["threadid", "threads", "thread"]:
        status_loading = await message.reply_text("⏳ Analyzing group channel layouts and compiling index...")
        try:
            records = get_cached_records()
            lines = ["🧵 *Active Workspace Thread Directory*\n"]
            lines.append("• `0` | 💬 **General/Main Landing Channel**")

            for row in records:
                tid = row.get("THREAD ID")
                if str(tid).isdigit():
                    lines.append(f"• `{tid}` | 🎭 *PERF:* **{row.get('EVENT NAME', 'Unnamed Event')}**")

            try:
                for o_row in get_cached_values(tab_name="OTHERS"):
                    if o_row and str(o_row[0]).isdigit():
                        lines.append(f"• `{o_row[0]}` | ☕ *OTHERS:* **{o_row[1] if len(o_row) > 1 and o_row[1] else 'Unnamed'}**")
            except Exception: pass
            
            await status_loading.delete()
            await message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await status_loading.edit_text(f"❌ Failed to read thread maps: {e}")
        return

    if command in ("attd", "attendance"):
        from handlers.attendance_handlers import start_attendance_modify
        await start_attendance_modify(update, context)
        return

    if command == "help" or text == "/start":
        await message.reply_text(
            DASHBOARD_TEXT,
            reply_markup=_dashboard_keyboard(),
            parse_mode="Markdown",
        )


async def handle_dashboard_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all cached list snapshots so the next list pulls fresh from Sheets.

    A single control on the DM dashboard instead of a refresh button on every
    individual list screen. After this, `/modify`, `/announce`, `/attd`, etc.
    re-read Google Sheets the next time they build a list.
    """
    query = update.callback_query

    # Authorize: only dashboard admins may refresh.
    uid = update.effective_user.id
    is_admin = False
    if isinstance(ADMIN_DM_USER_IDS, dict):
        is_admin = any(str(v).isdigit() and int(v) == uid for v in ADMIN_DM_USER_IDS.values()) \
            or any(str(k).isdigit() and int(k) == uid for k in ADMIN_DM_USER_IDS)
    elif isinstance(ADMIN_DM_USER_IDS, (list, set)):
        is_admin = uid in ADMIN_DM_USER_IDS
    if not is_admin:
        await query.answer()
        return

    for key in _DASHBOARD_CACHE_KEYS:
        context.user_data.pop(key, None)
    # Force the shared module cache to re-download on the next read.
    invalidate_sheet_cache()

    await query.answer("✅ Cache cleared — lists will pull fresh.", show_alert=False)
    try:
        await query.edit_message_text(
            DASHBOARD_TEXT + "\n\n♻️ *Data refreshed just now.*",
            reply_markup=_dashboard_keyboard(),
            parse_mode="Markdown",
        )
    except Exception:
        pass

async def handle_confirm_new_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback matrix router handling all administrative wizard modifications."""
    query = update.callback_query
    data = query.data

    # 🧠 INTERCEPT MANUAL REMINDER TRIGGER SELECTION CALLBACK BUTTONS
    if data.startswith("MANUAL_REMIND_TID|"):
        await query.answer()
        parts = data.split("|")
        target_tid = int(parts[1])
        from handlers.admin_handlers import execute_manual_remind_dispatch
        await execute_manual_remind_dispatch(update, context, target_tid)
        return

    if data.startswith("ANNOUNCE_TARGET|"):
        await query.answer()
        parts = data.split("|")
        target_thread = int(parts[1])
        display_name = parts[2] if len(parts) > 2 else f"Thread {target_thread}"
        
        context.user_data["waiting_announcement_text"] = target_thread
        context.user_data["waiting_announcement_name"] = display_name
        
        # ✅ FIXED TYPO: Changed InlineKeyboardMarkup to InlineKeyboardMarkup
        escape_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Topics Menu", callback_data="ANNOUNCE_BACK_MAPPED")],
            [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")]
        ])
        
        await query.edit_message_text(
            text=(
                f"✍️ *Target set to: {display_name}*\n\n"
                f"Please type or paste your announcement message directly below this text line.\n"
                f"You can use multiple paragraphs, line breaks, bold text, or emojis freely. "
                f"The bot will forward it exactly as you format it."
            ),
            reply_markup=escape_keyboard,
            parse_mode="Markdown"
        )
        return

    if data == "ANNOUNCE_BACK_MAPPED":
        await query.answer()
        context.user_data.pop("waiting_announcement_text", None)
        context.user_data.pop("waiting_announcement_name", None)
        await initiate_announce_portal_via_dm(update, context, incoming_query=query)
        return

    if context.user_data.get("creation_completed") and not data.startswith("CONFIRM_NEW_PERF"):
        await query.answer("This menu has expired.", show_alert=False)
        return

    await query.answer()

    if data.startswith("PERF_RESET|"):
        field_to_clear = data.split("|")[1]
        state_map = {
            "temp_event_type": (None, "🎭 *Resetting Step 1/6: Event Type*\nIs this External or Internal?"),
            "temp_event_name": (PERF_EVENT_NAME, "🎭 *Resetting Step 2/6: Event Name*\nWhat is the name of this performance?"),
            "temp_rehearsal": (PERF_REHEARSAL, "📅 *Resetting Step 1/6: Rehearsal Date & Time*\nEnter details:"),
            "temp_perf_date": (PERF_DATE, "📅 *Resetting Step 4/6: Performance Date & Time*\nEnter scheduling details below:"),
            "temp_location": (PERF_LOCATION, "📌 *Resetting Step 5/6: Location*\nEnter localized arena info below:")
        }
        if field_to_clear in state_map:
            target_state, text_prompt = state_map[field_to_clear]
            fields = ["temp_event_type", "temp_event_name", "temp_rehearsal", "temp_perf_date", "temp_location", "temp_other_info"]
            start_flushing = False
            for f in fields:
                if f == field_to_clear: start_flushing = True
                if start_flushing: context.user_data.pop(f, None)
            context.user_data["dm_state"] = target_state
            if field_to_clear == "temp_event_type":
                keyboard = [[InlineKeyboardButton(label, callback_data=f"PERF_EVENT_TYPE|{value}")] for label, value in PERF_EVENT_TYPES]
                keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")])
                await query.edit_message_text(text_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                return
            tracker = _build_progress_tracker(context.user_data, target_state)
            if target_state == PERF_EVENT_NAME:
                keyboard = [[InlineKeyboardButton("↩️ Edit Event Type", callback_data="PERF_RESET|temp_event_type")], [InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")]]
            else:
                back_targets = {PERF_REHEARSAL: "temp_event_name", PERF_DATE: "temp_rehearsal", PERF_LOCATION: "temp_perf_date"}
                keyboard = _get_back_keyboard(back_targets.get(target_state)).inline_keyboard
            await query.edit_message_text(f"{tracker}{text_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("TYPE_SELECTED|"):
        selected_type = data.split("|")[1]
        context.user_data["temp_type"] = selected_type
        if selected_type == "PERF":
            keyboard = [[InlineKeyboardButton(label, callback_data=f"PERF_EVENT_TYPE|{value}")] for label, value in PERF_EVENT_TYPES]
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")])
            await query.edit_message_text("🎭 *New PERF Topic*\n\n*Step 1/6: Event Type*\nIs this External or Internal?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return
        context.user_data["dm_state"] = WAITING_TOPIC_TITLE
        await query.edit_message_text(f"Selected Type: **{selected_type}** ☕\n\n**Step 1:** Please enter the **Topic Title** for the group forum:", parse_mode="Markdown")
        return

    if data.startswith("PERF_EVENT_TYPE|"):
        event_type = data.split("|")[1]
        context.user_data["temp_event_type"] = event_type
        context.user_data["dm_state"] = PERF_EVENT_NAME
        tracker = _build_progress_tracker(context.user_data, 2)
        keyboard = [[InlineKeyboardButton("↩️ Edit Event Type", callback_data="PERF_RESET|temp_event_type")], [InlineKeyboardButton("❌ Cancel Flow", callback_data="CANCEL_NEW_PERF")]]
        await query.edit_message_text(f"{tracker}📍 *Step 2/6: Event Name*\nWhat is the name of this performance?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("SKIP_PERF_FIELD|"):
        field = data.split("|")[1]
        if field == "rehearsal":
            context.user_data["temp_rehearsal"] = "-"
            context.user_data["dm_state"] = PERF_DATE
            tracker = _build_progress_tracker(context.user_data, 4)
            prompt = f"{tracker}📅 *Step 4/6: Performance Date & Time*\n\nSeparate the date and time using a **comma (,)**."
            await query.edit_message_text(text=prompt, reply_markup=_get_back_keyboard("temp_rehearsal"), parse_mode="Markdown")
        elif field == "other_info":
            context.user_data["temp_other_info"] = "-"
            context.user_data["dm_state"] = None
            await query.edit_message_text("⏳ Compiling Final Summary Dashboard Preview...")
            await _show_perf_summary(update, context)
        return

    if data == "CONFIRM_NEW_OTHERS":
        title = context.user_data.get("temp_title")
        await query.edit_message_text(f"⏳ Creating OTHERS topic: `{title}`...")
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            tid = topic.message_thread_id
            from utils.constants import OTHERS_THREAD_IDS, initialized_topics
            OTHERS_THREAD_IDS.add(tid)
            initialized_topics.add(tid)
            from services.google_sheets import append_to_others_list
            append_to_others_list(tid, title)
            await query.message.reply_text(f"✅ Successfully created OTHERS Topic `{tid}`.")
            context.user_data.clear()
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to create topic: {e}")
        return

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
            await query.edit_message_text("❌ Data expired. Please use `/new` to restart.")
            return

        context.user_data["creation_completed"] = True
        preview_msg_id = context.user_data.get("final_preview_msg_id")
        if preview_msg_id:
            try: await context.bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=preview_msg_id, reply_markup=None)
            except Exception: pass

        status_msg = await query.message.reply_text("⏳ Processing: Building forum channels and updating logs...")
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            thread_id = topic.message_thread_id
            from utils.constants import initialized_topics
            initialized_topics.add(thread_id)
        except Exception as e:
            await status_msg.edit_text(f"❌ Forum setup failed: {e}")
            return

        try:
            sheet = get_gspread_sheet()
            sheet.append_row([thread_id, event_type, event_name, rehearsal_date, perf_date, location, other_info, "", "", ""], value_input_option="USER_ENTERED")
            invalidate_sheet_cache()  # new row — re-pull on next /list or /modify
        except Exception as e:
            await status_msg.edit_text(f"❌ Topic created (`{thread_id}`), but logging encountered a write failure: {e}")
            return

        try:
            group_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
            await publish_performance_summary(bot=context.bot, chat_data_store=group_data, sheet=sheet, chat_id=CHAT_ID, thread_id=thread_id, event_name=event_name, rehearsal_date=rehearsal_date, perf_date=perf_date, location=location, other_info=other_info)
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Channel created successfully, but publishing summary logs failed: {e}")
            context.user_data.clear()
            return

        await status_msg.edit_text(f"✅ *Success!* Forum Topic `{thread_id}` (`{title}`) has been established and published.", parse_mode="Markdown")
        context.user_data.clear()
        return

    if data == "CANCEL_NEW_PERF":
        context.user_data.clear()
        await query.edit_message_text("❌ Action cancelled")
        return

async def handle_list_modify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle clicking a specific performance button directly from the list prompt."""
    query = update.callback_query
    await query.answer()
    _, thread_id_str = query.data.split("|")
    thread_id = int(thread_id_str)
    try: await query.message.delete()
    except Exception: pass
    
    records = context.user_data.get("cached_records", [])
    row = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    if row:
        context.user_data["modify_event_name"] = row.get("EVENT NAME", "Unnamed Event")
        
    from handlers.modify_handlers import initiate_modify_via_dm
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)