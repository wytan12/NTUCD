from __future__ import annotations
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import ADMIN_DM_USER_IDS, CHAT_ID, SHEET_TAB_NAME
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary
from services.date_parser import parse_and_format_dates
from services.google_sheets import get_gspread_sheet, get_cached_records, get_cached_values, invalidate_sheet_cache, is_main_admin
from utils.constants import (
    WAITING_TOPIC_TITLE, PERF_EVENT_NAME, PERF_REHEARSAL, PERF_DATE, PERF_LOCATION, PERF_OTHER_INFO,
)

PERF_EVENT_TYPES = [
    ("🎭 EXT (External)", "EXT"),
    ("🏠 INT (Internal)", "INT"),
]

_DASHBOARD_CACHE_KEYS = (
    "cached_records",
    "attd_dates",
)

MAIN_ADMIN_TOPIC_ONLY_TEXT = (
    "🔒 *Main-admin action only.*\n\n"
    "Topic creation is limited to main admins. You can still use the other Cockpit tools."
)

DASHBOARD_TEXT = (
    "🥁 *NTUFD Command Centre* 🥁\n"
    "Welcome back, maestro! 🎶 The whole show runs from here.\n\n"
    "📂 *Workspace Guide:*\n"
    "🎭 *Events:* Create Topics | Modify Perf Details | Perf Ledger Preview\n"
    "✅ *Rosters:* Attendance | Topic Thread Index | Member Roaster\n"
    "📢 *Comms:* Broadcast Announcements | Auto/Manual Checklists.\n\n"
    "🤖 *Active Bot Background Autopilots:*\n"
    "• *Attendance:* Auto-sends regular training attendance poll 4 days ahead, plus a night-before reminder at 10 PM.\n"
    "• *Performance Pre-flight:* Triggers final checklists and admin nudges exactly 7 days before gig day.\n"
    "• *Recruitment Engine:* Manages the Welcome Tea RSVP timeline and auto-verifies new members.\n"
    "• *Database Sync:* Silently updates your Google Sheet the moment members join or leave the chat.\n"
    "• *Forum Security:* Instantly deletes unauthorized topics to keep your channels perfectly clean.\n\n"
    "👇 *Pick your move:*"
)

def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎪 New Topic", callback_data="DASH_VIEW|LAUNCH_NEW"),
            InlineKeyboardButton("🛠️ Edit Performance", callback_data="DASH_VIEW|LAUNCH_MODIFY")
        ],
        [
            InlineKeyboardButton("✅ Take Attendance", callback_data="DASH_VIEW|LAUNCH_ATTD"),
            InlineKeyboardButton("⏰ Reminders", callback_data="DASH_VIEW|LAUNCH_REMIND")
        ],
        [
            InlineKeyboardButton("📣 Broadcast", callback_data="DASH_VIEW|LAUNCH_ANNOUNCE"),
            InlineKeyboardButton("📊 Performance Ledger", callback_data="DASH_VIEW|LEDGER")
        ],
        [
            InlineKeyboardButton("🧵 Thread Index", callback_data="DASH_VIEW|THREADS"),
            InlineKeyboardButton("👥 Member Roster", callback_data="DASH_VIEW|MEMBERS")
        ],
        [
            InlineKeyboardButton("♻️ Refresh Data", callback_data="DASH_REFRESH")
        ]
    ])

def _dashboard_back_keyboard(back_callback: str = "DASH_VIEW|HOME") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data=back_callback)],
        [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")],
    ])

def _parse_command_payload(text: str) -> tuple[str, str | None, str]:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return "", None, ""
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

def _build_progress_tracker(ud: dict) -> str:
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
    buttons = []
    if field_to_clear:
        buttons.append([InlineKeyboardButton("🔙 Edit Previous Step", callback_data=f"PERF_RESET|{field_to_clear}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")])
    return InlineKeyboardMarkup(buttons)

async def _render_step(message, prompt: str, reply_markup: InlineKeyboardMarkup) -> None:
    from telegram.ext import CallbackContext
    context = CallbackContext.from_message(message) if hasattr(CallbackContext, "from_message") else None
    msg = await message.reply_text(prompt, reply_markup=reply_markup, parse_mode="Markdown")
    if context and hasattr(context, "user_data"):
        context.user_data["master_dash_id"] = msg.message_id

async def _show_perf_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = context.user_data
    ud["in_summary_mode"] = True

    event_type = ud.get("temp_event_type", "TBD")
    event_name = ud.get("temp_event_name", "")
    rehearsal = ud.get("temp_rehearsal", "-")
    perf_date = ud.get("temp_perf_date", "")
    location = ud.get("temp_location", "")
    other_info = ud.get("temp_other_info", "-")

    summary_card = build_performance_summary(
        event_name=event_name, rehearsal_date=rehearsal,
        perf_date=perf_date, location=location, other_info=other_info
    )
    
    prompt_text = (
        f"📋 *Performance Topic Entry Preview*\n"
        f"Plss verify all fields details before creating\n\n"
        f"{summary_card}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit Event Type", callback_data="PERF_RESET|temp_event_type"),
            InlineKeyboardButton("✏️ Edit Event Name", callback_data="PERF_RESET|temp_event_name")
        ],
        [
            InlineKeyboardButton("✏️ Edit Rehearsal Dates", callback_data="PERF_RESET|temp_rehearsal"),
            InlineKeyboardButton("✏️ Edit Performance Date", callback_data="PERF_RESET|temp_perf_date")
        ],
        [
            InlineKeyboardButton("✏️ Edit Location", callback_data="PERF_RESET|temp_location"),
            InlineKeyboardButton("✏️ Edit Other Info", callback_data="PERF_RESET|temp_other_info")
        ],
        [
            InlineKeyboardButton("✅ CONFIRM & PUBLISH FORUM TOPIC", callback_data="CONFIRM_NEW_PERF")
        ],
        [
            InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")
        ]
    ])
    
    query = update.callback_query
    active_dash_id = ud.get("master_dash_id")
    
    if query:
        await query.edit_message_text(prompt_text, reply_markup=keyboard, parse_mode="Markdown")
    elif active_dash_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=active_dash_id,
                text=prompt_text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception:
            msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=prompt_text, reply_markup=keyboard, parse_mode="Markdown")
            ud["master_dash_id"] = msg.message_id
    else:
        msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=prompt_text, reply_markup=keyboard, parse_mode="Markdown")
        ud["master_dash_id"] = msg.message_id

async def initiate_announce_portal_via_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, incoming_query=None) -> None:
    target_chat = update.effective_user
    status_loading = None
    if incoming_query is None:
        status_loading = await context.bot.send_message(chat_id=target_chat.id, text="⏳ Generating channel communication routing links...")

    records = get_cached_records()
    cached_others = []
    try:
        for o_row in get_cached_values(tab_name="OTHERS"):
            if o_row and str(o_row[0]).isdigit():
                cached_others.append((o_row[0], o_row[1] if len(o_row) > 1 and o_row[1] else "Others Thread"))
    except Exception: pass

    buttons = [[InlineKeyboardButton("💬 General Topic (Main)", callback_data="ANNOUNCE_TARGET|0|General Topic")]]
    for row in records:
        tid = row.get("THREAD ID")
        if str(tid).isdigit():
            event_name = row.get("EVENT NAME", "Unnamed Event")
            buttons.append([InlineKeyboardButton(f"🎭 {event_name} ({tid})", callback_data=f"ANNOUNCE_TARGET|{tid}|{event_name}")])
            
    for o_tid, o_name in cached_others:
        buttons.append([InlineKeyboardButton(f"☕ {o_name} ({o_tid})", callback_data=f"ANNOUNCE_TARGET|{o_tid}|{o_name}")])

    buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
    prompt_text = "📢 *ANNOUNCEMENT Portal*\nPlease select which TOPIC you want to announce into:"
    
    if incoming_query:
        await incoming_query.edit_message_text(text=prompt_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        if status_loading: await status_loading.delete()
        await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def render_modify_list(update: Update, context: ContextTypes.DEFAULT_TYPE, incoming_query=None) -> None:
    target_chat = update.effective_user
    try:
        status_loading = None
        if incoming_query is None:
            status_loading = await context.bot.send_message(chat_id=target_chat.id, text="⏳ Fetching active performance log...")
        
        records = get_cached_records()
        context.user_data["cached_records"] = records
        
        if not records:
            text = "📋 The performance sheet is empty."
            keyboard = _dashboard_back_keyboard()
            if incoming_query:
                await incoming_query.edit_message_text(text, reply_markup=keyboard)
            elif status_loading:
                await status_loading.edit_text(text, reply_markup=keyboard)
            return

        from datetime import datetime
        import re
        
        # Helper to parse the date for sorting
        def get_sort_date(row):
            date_str = str(row.get("PERF DATE | TIME", "")).strip()
            if not date_str or date_str == "-": 
                return datetime.min
            first_line = date_str.splitlines()[0].strip()
            match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
            if match:
                try:
                    return datetime.strptime(match.group(1), "%d %b %Y")
                except ValueError:
                    pass
            return datetime.min

        # Sort descending (Latest date at the top)[cite: 23]
        sorted_records = sorted(records, key=get_sort_date, reverse=True)

        buttons = []
        for row in sorted_records:
            tid = row.get("THREAD ID")
            if str(tid).isdigit():
                event_name = row.get("EVENT NAME", "Unnamed Event")
                buttons.append([InlineKeyboardButton(f"⚙️ {event_name} ({tid})", callback_data=f"LIST_MODIFY|{tid}")])
        
        buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
        prompt_text = "🛠️ *Performance Modification Portal*\nWhich PERFORMANCE topic would you like to modify:"
        markup = InlineKeyboardMarkup(buttons)
        
        if incoming_query:
            await incoming_query.edit_message_text(prompt_text, reply_markup=markup, parse_mode="Markdown")
        else:
            if status_loading: await status_loading.delete()
            await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        print(f"Modify List error: {e}")

async def handle_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text or update.effective_chat.type != "private":
        return

    # Role-driven gate: MAIN + SECONDARY admins (from MEMBER INFO Role/Position)
    # plus the config fallback ids. Non-admins get total silence — but a user
    # mid-/verify (pending join request) must NOT be swallowed here; that flow
    # is handled by the ConversationHandler before this dispatcher runs.
    from services.google_sheets import is_dashboard_admin
    current_uid = update.effective_user.id
    if not is_dashboard_admin(current_uid):
        return

    text = message.text.strip()
    
    # 🎯 STRICT WHITELIST: Only /start is allowed as a text command now.
    OFFICIAL_MENU_COMMANDS = {"/start"}

    if text.lower() in OFFICIAL_MENU_COMMANDS:
        if context.user_data.get("dm_state") is not None or context.user_data.get("waiting_announcement_text") is not None or context.user_data.get("modify_field") is not None:
            current_dash_id = context.user_data.get("master_dash_id")
            context.user_data.clear() 
            if current_dash_id:
                context.user_data["master_dash_id"] = current_dash_id
                try:
                    await context.bot.edit_message_text(chat_id=message.chat.id, message_id=current_dash_id, text=DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
                except Exception: pass
            await message.reply_text("🛑 *Wizard Cancelled*\nYour active configuration flow was terminated cleanly.", parse_mode="Markdown")
            return

    command, thread_token, payload = _parse_command_payload(text)

    if not text.startswith("/"):
        from handlers.attendance_handlers import handle_moddate_text
        if await handle_moddate_text(update, context): return

    if context.user_data.get("waiting_announcement_text") is not None:
        target_thread = context.user_data.pop("waiting_announcement_text")
        display_target_name = context.user_data.pop("waiting_announcement_name", "the group topic")
        try:
            thread_param = None if target_thread == 0 else target_thread
            await context.bot.send_message(chat_id=CHAT_ID, text=message.text_markdown, message_thread_id=thread_param, parse_mode="Markdown")
            await message.reply_text(f"✅ Announcement broadcasted successfully into *{display_target_name}*!", parse_mode="Markdown")
            current_dash_id = context.user_data.get("master_dash_id")
            if current_dash_id:
                await context.bot.edit_message_text(chat_id=message.chat.id, message_id=current_dash_id, text=DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        except Exception as e:
            await message.reply_text(f"❌ Failed to dispatch announcement: {e}")
        return

    # =========================================================================
    # WIZARD TEXT ENTRIES STATE SEQUENCES
    # =========================================================================
    state = context.user_data.get("dm_state")
    is_summary_mode = context.user_data.get("in_summary_mode", False)

    if state == PERF_EVENT_NAME:
        context.user_data["temp_event_name"] = text
        try: await message.delete()
        except Exception: pass
        
        if is_summary_mode:
            context.user_data["dm_state"] = None
            await _show_perf_summary(update, context)
            return
            
        context.user_data["dm_state"] = PERF_REHEARSAL
        tracker = _build_progress_tracker(context.user_data)
        prompt = (
            f"{tracker}📅 *Step 3/6: Rehearsal Date & Time*\n\n"
            f"*Examples:*\n"
            f"• Single date Single time: `31 aug, 9pm`\n"
            f"• Single date Multiple times: `1 sep, 7pm 2100`\n"
            f"• Multiple dates Single/Multiple times:\n"
            f"`31 aug, 9pm`\n"
            f"`1 sep, 7pm 2100`"
        )
        
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            await context.bot.edit_message_text(chat_id=message.chat.id, message_id=active_dash_id, text=prompt, reply_markup=_get_back_keyboard("temp_event_name"), parse_mode="Markdown")
            return
        await _render_step(message, prompt, _get_back_keyboard("temp_event_name"))
        return

    if state == PERF_REHEARSAL:
        try:
            rehearsal_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            try: await message.delete()
            except Exception: pass
            
            tracker = _build_progress_tracker(context.user_data)
            error_prompt = (
                f"⚠️ *ERROR:* {str(e)}\n\n"
                f"{tracker}"
                f"✏️ *Updating Rehearsal Date & Time*\n"
                f"👉 *Please re-enter matching the format guidelines above:*"
            )
            
            old_val = context.user_data.get("temp_rehearsal", "")
            if old_val and old_val != "-":
                error_prompt += f"\n\n📋 *Previous Value (Tap box below to copy instantly):*\n```\n{old_val}```\n"

            active_dash_id = context.user_data.get("master_dash_id")
            if active_dash_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=message.chat.id, message_id=active_dash_id, text=error_prompt, 
                        reply_markup=_get_back_keyboard("temp_event_name"), parse_mode="Markdown"
                    )
                except Exception: pass
            return

        context.user_data["temp_rehearsal"] = rehearsal_date
        try: await message.delete()
        except Exception: pass
        
        if is_summary_mode:
            context.user_data["dm_state"] = None
            await _show_perf_summary(update, context)
            return
            
        context.user_data["dm_state"] = PERF_DATE
        tracker = _build_progress_tracker(context.user_data)
        prompt = (
            f"{tracker}📅 *Step 4/6: Performance Date & Time*\n\n"
            f"*Examples:*\n"
            f"• Single date Single time: `31 aug, 9pm`\n"
            f"• Single date Multiple times: `1 sep, 7pm 2100`\n"
            f"• Multiple dates Single/Multiple times:\n"
            f"`31 aug, 9pm`\n"
            f"`1 sep, 7pm 2100`"
        )
        
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            await context.bot.edit_message_text(chat_id=message.chat.id, message_id=active_dash_id, text=prompt, reply_markup=_get_back_keyboard("temp_rehearsal"), parse_mode="Markdown")
            return
        await _render_step(message, prompt, _get_back_keyboard("temp_rehearsal"))
        return

    if state == PERF_DATE:
        try:
            perf_date = "\n".join(parse_and_format_dates(text))
        except ValueError as e:
            try: await message.delete()
            except Exception: pass
            
            tracker = _build_progress_tracker(context.user_data)
            error_prompt = (
                f"⚠️ *ERROR:* {str(e)}\n\n"
                f"{tracker}"
                f"✏️ *Updating Performance Date & Time*\n"
                f"👉 *Please re-enter matching the format guidelines above:*"
            )
            
            old_val = context.user_data.get("temp_perf_date", "")
            if old_val and old_val != "-":
                error_prompt += f"\n\n📋 *Previous Value (Tap box below to copy instantly):*\n```\n{old_val}```\n"

            active_dash_id = context.user_data.get("master_dash_id")
            if active_dash_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=message.chat.id, message_id=active_dash_id, text=error_prompt, 
                        reply_markup=_get_back_keyboard("temp_rehearsal"), parse_mode="Markdown"
                    )
                except Exception: pass
            return

        context.user_data["temp_perf_date"] = perf_date
        try: await message.delete()
        except Exception: pass
        
        if is_summary_mode:
            context.user_data["dm_state"] = None
            await _show_perf_summary(update, context)
            return
            
        context.user_data["dm_state"] = PERF_LOCATION
        tracker = _build_progress_tracker(context.user_data)
        prompt = f"{tracker}📌 *Step 5/6: Location*\nWhere is the performance taking place?"
        
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            await context.bot.edit_message_text(chat_id=message.chat.id, message_id=active_dash_id, text=prompt, reply_markup=_get_back_keyboard("temp_perf_date"), parse_mode="Markdown")
            return
        await _render_step(message, prompt, _get_back_keyboard("temp_perf_date"))
        return

    if state == PERF_LOCATION:
        context.user_data["temp_location"] = text
        try: await message.delete()
        except Exception: pass
        
        if is_summary_mode or "temp_other_info" in context.user_data:
            context.user_data["dm_state"] = None
            await _show_perf_summary(update, context)
            return
            
        context.user_data["dm_state"] = PERF_OTHER_INFO
        tracker = _build_progress_tracker(context.user_data)
        prompt = f"{tracker}📝 *Step 6/6: Other Info*\nAny additional info?"
        
        keyboard = [
            [InlineKeyboardButton("⏭ Skip (No Additional Info)", callback_data="SKIP_PERF_FIELD|other_info")],
            [InlineKeyboardButton("🔙 Back to Location Entry", callback_data="PERF_RESET|temp_location")],
            [InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")]
        ]
        
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            await context.bot.edit_message_text(chat_id=message.chat.id, message_id=active_dash_id, text=prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return
        await _render_step(message, prompt, InlineKeyboardMarkup(keyboard))
        return

    if state == PERF_OTHER_INFO:
        context.user_data["temp_other_info"] = text
        try: await message.delete()
        except Exception: pass
        context.user_data["dm_state"] = None
        await _show_perf_summary(update, context)
        return

    if state == WAITING_TOPIC_TITLE:
        context.user_data["temp_title"] = text
        if context.user_data.get("temp_type") == "OTHERS":
            keyboard = [
                [InlineKeyboardButton("✅ Create Others Topic", callback_data="CONFIRM_NEW_OTHERS")],
                [InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")]
            ]
            active_dash_id = context.user_data.get("master_dash_id")
            if active_dash_id:
                try: await message.delete()
                except Exception: pass
                await context.bot.edit_message_text(
                    chat_id=message.chat.id, message_id=active_dash_id,
                    text=f"❓ *Confirm creation request*\n\nWould you like to build the following *OTHERS* forum topic channel entry?\n• Title: `{text}`",
                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
                )
                context.user_data["dm_state"] = None
                return

    if context.user_data.get("modify_field") and not text.startswith("/"):
        current_field = context.user_data.get("modify_field")
        if current_field == "ANNOUNCEMENT_TEXT_CAPTURE":
            from handlers.admin_handlers import apply_announcement_broadcast
            await apply_announcement_broadcast(update, context)
            return
        from handlers.modify_handlers import apply_modify_value
        await apply_modify_value(update, context)
        return

    if command == "help" or text == "/start":
        try: await message.delete()
        except Exception: pass
        
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            try:
                await context.bot.edit_message_text(chat_id=message.chat.id, message_id=active_dash_id, text=DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
                return
            except Exception: pass
            
        msg = await message.reply_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        context.user_data["master_dash_id"] = msg.message_id
        return
    return

async def handle_dashboard_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    from services.google_sheets import is_dashboard_admin
    if not is_dashboard_admin(update.effective_user.id):
        await query.answer()
        return

    for key in _DASHBOARD_CACHE_KEYS: context.user_data.pop(key, None)
    invalidate_sheet_cache()
    await query.answer("✅ Cache cleared — lists will pull fresh.", show_alert=False)
    try:
        await query.edit_message_text(DASHBOARD_TEXT + "\n\n♻️ *Data refreshed just now.*", reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
    except Exception: pass

async def handle_confirm_new_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    active_dash_id = context.user_data.get("master_dash_id")
    if active_dash_id and query.message.message_id != active_dash_id:
        await query.message.edit_text("🛑 *This menu panel has expired!*\n\nPlease use the latest menu screen at the bottom of your screen!", reply_markup=None)
        return

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
        display_name = parts[2]
        context.user_data["waiting_announcement_text"] = target_thread
        context.user_data["waiting_announcement_name"] = display_name
        
        escape_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Announcement Topics List", callback_data="ANNOUNCE_BACK_MAPPED")],
            [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]
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

    if data.startswith(("TYPE_SELECTED|", "PERF_EVENT_TYPE|", "SKIP_PERF_FIELD|", "PERF_RESET|")) or data in {"CONFIRM_NEW_OTHERS", "CONFIRM_NEW_PERF"}:
        if not is_main_admin(update.effective_user.id):
            await query.answer("Topic creation is limited to main admins.", show_alert=True)
            await query.edit_message_text(
                MAIN_ADMIN_TOPIC_ONLY_TEXT,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]]),
                parse_mode="Markdown",
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
            "temp_event_type": (None, "🎭 *Event Type Selection*\nIs this External or Internal?"),
            "temp_event_name": (PERF_EVENT_NAME, "✏️ *Updating Event Name*\nWhat is the adjusted name of this performance?"),
            "temp_rehearsal": (PERF_REHEARSAL, "✏️ *Updating Rehearsal Date & Time*\nEnter scheduling details below:"),
            "temp_perf_date": (PERF_DATE, "✏️ *Updating Performance Date & Time*\nEnter scheduling details below:"),
            "temp_location": (PERF_LOCATION, "✏️ *Updating Location*\nEnter localized arena info below:"),
            "temp_other_info": (PERF_OTHER_INFO, "✏️ *Updating Other Info*\nEnter additional remarks below:")
        }
        if field_to_clear in state_map:
            target_state, text_prompt = state_map[field_to_clear]
            context.user_data["dm_state"] = target_state
            
            if field_to_clear == "temp_event_type":
                keyboard = [[InlineKeyboardButton(label, callback_data=f"PERF_EVENT_TYPE|{value}")] for label, value in PERF_EVENT_TYPES]
                keyboard.append([InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")])
                await query.edit_message_text(text_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                return
                
            tracker = _build_progress_tracker(context.user_data)
            back_maps = {PERF_EVENT_NAME: "temp_event_type", PERF_REHEARSAL: "temp_event_name", PERF_DATE: "temp_rehearsal", PERF_LOCATION: "temp_perf_date", PERF_OTHER_INFO: "temp_location"}
            
            prompt_header = f"{tracker}{text_prompt}"
            if context.user_data.get("in_summary_mode"):
                if target_state in (PERF_REHEARSAL, PERF_DATE):
                    prompt_header = (
                        f"{tracker}{text_prompt}\n\n"
                        f"*Examples:*\n"
                        f"• Single date Single time: `31 aug, 9pm`\n"
                        f"• Single date Multiple times: `1 sep, 7pm 9pm`\n\n"
                        f"👉 _Separate date/time with a comma (,)._"
                    )

            # 🎯 FIX FOR POINT 1: Added explicit "\n" right after the open backticks to resolve the markdown language parser glitch!
            old_value = context.user_data.get(field_to_clear, "")
            if old_value and old_value != "-":
                prompt_header = (
                    f"{prompt_header}\n\n"
                    f"📋 *Previous Value (Tap box below to copy instantly):*\n"
                    f"```\n"
                    f"{old_value}```\n"
                    f"👉 _Paste your copied text into the chat bar, edit it, and send!_"
                )
            
            await query.edit_message_text(prompt_header, reply_markup=_get_back_keyboard(back_maps.get(target_state)), parse_mode="Markdown")
        return

    if data.startswith("TYPE_SELECTED|"):
        selected_type = data.split("|")[1]
        context.user_data["temp_type"] = selected_type
        if selected_type == "PERF":
            keyboard = [[InlineKeyboardButton(label, callback_data=f"PERF_EVENT_TYPE|{value}")] for label, value in PERF_EVENT_TYPES]
            keyboard.append([InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")])
            await query.edit_message_text("🎭 *New PERF Topic*\n\n*Step 1/6: Event Type*\nIs this External or Internal?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return
            
        context.user_data["dm_state"] = WAITING_TOPIC_TITLE
        step_one_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Type Selection", callback_data="DASH_VIEW|LAUNCH_NEW")]])
        await query.edit_message_text(f"Selected Type: *{selected_type}* ☕\n\n*Step 1:* Please type and enter the *Topic Title* for the group forum below:", reply_markup=step_one_keyboard, parse_mode="Markdown")
        return

    if data.startswith("PERF_EVENT_TYPE|"):
        event_type = data.split("|")[1]
        context.user_data["temp_event_type"] = event_type
        if context.user_data.get("in_summary_mode"):
            await _show_perf_summary(update, context)
            return
            
        context.user_data["dm_state"] = PERF_EVENT_NAME
        tracker = _build_progress_tracker(context.user_data)
        await query.edit_message_text(f"{tracker}📍 *Step 2/6: Event Name*\nWhat is the name of this performance?", reply_markup=_get_back_keyboard("temp_event_type"), parse_mode="Markdown")
        return

    if data.startswith("SKIP_PERF_FIELD|"):
        field = data.split("|")[1]
        if field == "rehearsal":
            context.user_data["temp_rehearsal"] = "-"
            if context.user_data.get("in_summary_mode"):
                await _show_perf_summary(update, context)
                return
                
            context.user_data["dm_state"] = PERF_DATE
            tracker = _build_progress_tracker(context.user_data)
            prompt = f"{tracker}📅 *Step 4/6: Performance Date & Time*\n\nSeparate date/time with a *comma (,)*."
            await query.edit_message_text(text=prompt, reply_markup=_get_back_keyboard("temp_rehearsal"), parse_mode="Markdown")
        elif field == "other_info":
            context.user_data["temp_other_info"] = "-"
            context.user_data["dm_state"] = None
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
            
            await query.answer(f"🎉 Success! OTHERS Topic '{title}' established!", show_alert=True)
            current_dash_id = context.user_data.get("master_dash_id")
            context.user_data.clear()
            if current_dash_id: context.user_data["master_dash_id"] = current_dash_id
            await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
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
            await query.edit_message_text("❌ Data expired. Please restart.")
            return

        context.user_data["creation_completed"] = True
        await query.edit_message_text("⏳ Processing: Building forum channels and updating sheet logs...")
        
        try:
            topic = await context.bot.create_forum_topic(chat_id=CHAT_ID, name=title)
            thread_id = topic.message_thread_id
            from utils.constants import initialized_topics
            initialized_topics.add(thread_id)
        except Exception as e:
            await query.edit_message_text(f"❌ Forum setup failed: {e}")
            return

        try:
            sheet = get_gspread_sheet()
            sheet.append_row([thread_id, event_type, event_name, rehearsal_date, perf_date, location, other_info, "", "", ""], value_input_option="USER_ENTERED")
            invalidate_sheet_cache(tab_name=SHEET_TAB_NAME)
        except Exception as e:
            await query.edit_message_text(f"❌ Sheet Write failure: {e}")
            return

        try:
            group_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
            await publish_performance_summary(bot=context.bot, chat_data_store=group_data, sheet=sheet, chat_id=CHAT_ID, thread_id=thread_id, event_name=event_name, rehearsal_date=rehearsal_date, perf_date=perf_date, location=location, other_info=other_info)
        except Exception: pass

        await query.answer(f"🎉 Success! Forum Topic ID {thread_id} established and published!", show_alert=True)
        current_dash_id = context.user_data.get("master_dash_id")
        context.user_data.clear()
        if current_dash_id: context.user_data["master_dash_id"] = current_dash_id
        await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        return

    if data == "CANCEL_NEW_PERF" or data == "DASH_VIEW|HOME":
        current_dash_id = context.user_data.get("master_dash_id")
        context.user_data.clear()
        if current_dash_id: context.user_data["master_dash_id"] = current_dash_id
        await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        return

async def handle_list_modify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, thread_id_str = query.data.split("|")
    thread_id = int(thread_id_str)
    records = context.user_data.get("cached_records", [])
    if not records:
        from services.google_sheets import get_cached_records
        records = get_cached_records()
        context.user_data["cached_records"] = records
    row = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    if row: context.user_data["modify_event_name"] = row.get("EVENT NAME", "Unnamed Event")
    from handlers.modify_handlers import initiate_modify_via_dm
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)

async def handle_dashboard_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    _, target_view = data.split("|")

    active_dash_id = context.user_data.get("master_dash_id")
    if active_dash_id and query.message.message_id != active_dash_id:
        await query.message.edit_text(
            "🛑 *This menu panel has expired!*\n\n"
            "You opened a newer dashboard bubble further down in your chat history. "
            "Please use the latest menu screen at the bottom of your screen!",
            reply_markup=None
        )
        return

    if target_view == "HOME":
        await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        return

    elif target_view == "LEDGER":
        records = get_cached_records()
        if not records: 
            text = "📋 The performance sheet is currently empty."
        else:
            from datetime import datetime
            import re
            
            # Helper to parse the date for sorting
            def get_sort_date(row):
                date_str = str(row.get("PERF DATE | TIME", "")).strip()
                if not date_str or date_str == "-": 
                    return datetime.min
                # Grab the first line and extract the date part (e.g., "1 Sep 2026")
                first_line = date_str.splitlines()[0].strip()
                match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
                if match:
                    try:
                        return datetime.strptime(match.group(1), "%d %b %Y")
                    except ValueError:
                        pass
                return datetime.min

            # Sort records descending (Latest date first, TBD/Blank at the bottom)
            sorted_records = sorted(records, key=get_sort_date, reverse=True)

            lines = ["📊 *Performance Status Ledger*\nQuick overview of all registered bookings\n"]
            for row in sorted_records:
                event_name = row.get("EVENT NAME", "Unnamed Event")
                status = row.get("STATUS", "").strip().upper() or "PENDING"
                emoji = "⏳" if status == "PENDING" else "✅" if status == "ACCEPTED" else "❌"
                
                # Format the date display cleanly
                perf_cell = str(row.get("PERF DATE | TIME", "")).strip()
                if not perf_cell or perf_cell == "-":
                    date_display = "TBD"
                else:
                    date_lines = [d.strip() for d in perf_cell.splitlines() if d.strip()]
                    first_date = date_lines[0]
                    # If multiple dates exist, show the first one and indicate how many are hidden
                    if len(date_lines) > 1:
                        date_display = f"{first_date} (+{len(date_lines) - 1} more)"
                    else:
                        date_display = first_date

                lines.append(f"{emoji} *{event_name}* | {date_display}")
            text = "\n".join(lines)
            
        keyboard = (
            _dashboard_back_keyboard()
            if not records
            else InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]])
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif target_view == "THREADS":
        records = get_cached_records()
        lines = ["🧵 *Forum Thread Directory Chart*\nAn overview of individual topic Thread IDs\n"]
        lines.append("• `0` | 💬 *General/Chit Chat Channel*")
        lines.append("• `53` | 📅 *Attendance*")
        for row in records:
            tid = row.get("THREAD ID")
            if str(tid).isdigit(): lines.append(f"• `{tid}` | 🎭 *PERF:* *{row.get('EVENT NAME', 'Unnamed Event')}*")
        try:
            for o_row in get_cached_values(tab_name="OTHERS"):
                if o_row and str(o_row[0]).isdigit(): lines.append(f"• `{o_row[0]}` | ☕ *OTHERS:* *{o_row[1] if len(o_row) > 1 and o_row[1] else 'Unnamed'}*")
        except Exception: pass
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif target_view == "MEMBERS" or target_view.startswith("MEMBERS_"):
        from services.google_sheets import get_active_members
        try:
            members = get_active_members()  # [(sort_key, token, label, name), ...] sorted
        except Exception as e:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]])
            await query.edit_message_text(f"❌ Failed to read the member info sheet: `{e}`", reply_markup=keyboard, parse_mode="Markdown")
            return

        # --- Drill-down: one group's members (tap a button below) ---
        if target_view != "MEMBERS":
            token = target_view[len("MEMBERS_"):]
            group = [(lbl, nm) for _sk, tok, lbl, nm in members if tok == token]
            label = group[0][0] if group else "Group"
            names = [nm for _lbl, nm in group]
            lines = [f"👥 *Active Members — {label}*\n_{len(names)} members_\n"]
            lines += [f"• {n}" for n in names] or ["_(none)_"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Group Menu", callback_data="DASH_VIEW|MEMBERS")],
            ])
            await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")
            return

        # --- Group menu: groups in seniority order (Graduates → years → named → —) ---
        if not members:
            text = "👥 *Active Member Roster Listing*\n\n📭 No members marked *Active* in the MEMBER INFO sheet yet."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]])
        else:
            order, meta = [], {}
            for _sk, tok, lbl, _nm in members:  # already sorted
                if tok not in meta:
                    meta[tok] = {"label": lbl, "count": 0}
                    order.append(tok)
                meta[tok]["count"] += 1
            text = (
                "👥 *Active Member Roster Listing*\n"
                f"_{len(members)} active members — pick a group to view:_"
            )
            buttons = [
                [InlineKeyboardButton(
                    f"{meta[tok]['label']} ({meta[tok]['count']} members)",
                    callback_data=f"DASH_VIEW|MEMBERS_{tok}",
                )]
                for tok in order
            ]
            buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
            keyboard = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif target_view == "LAUNCH_NEW":
        if not is_main_admin(update.effective_user.id):
            await query.edit_message_text(
                MAIN_ADMIN_TOPIC_ONLY_TEXT,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]]),
                parse_mode="Markdown",
            )
            return

        current_dash_id = context.user_data.get("master_dash_id")
        context.user_data.clear()
        if current_dash_id: context.user_data["master_dash_id"] = current_dash_id
        
        keyboard = [
            [InlineKeyboardButton("🎭 PERFORMANCE", callback_data="TYPE_SELECTED|PERF")],
            [InlineKeyboardButton("☕ OTHERS (Bonding/Misc)", callback_data="TYPE_SELECTED|OTHERS")],
            [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]
        ]
        await query.edit_message_text("📝 *Topic Creation Wizard*\nWhat kind of topic are you creating inside the group forum?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif target_view == "LAUNCH_MODIFY":
        from handlers.private_handlers import render_modify_list
        await render_modify_list(update, context, incoming_query=query)
        return

    elif target_view == "LAUNCH_ATTD":
        from handlers.attendance_handlers import HOME_TEXT, _build_home_keyboard
        await query.edit_message_text(HOME_TEXT, reply_markup=_build_home_keyboard(), parse_mode="Markdown")
        return

    # 🎯 FIX: Point this to the new single-bubble portal inside admin_handlers
    elif target_view == "LAUNCH_ANNOUNCE":
        from handlers.admin_handlers import initiate_announce_portal_via_dm
        await initiate_announce_portal_via_dm(update, context)
        return

    # 🎯 FIX: Point this to the new single-bubble reminder portal inside admin_handlers
    elif target_view == "LAUNCH_REMIND":
        from handlers.admin_handlers import initiate_remind_portal_via_dm
        await initiate_remind_portal_via_dm(update, context)
        return
