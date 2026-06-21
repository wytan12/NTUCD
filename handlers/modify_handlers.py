from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import asyncio

from utils.constants import MODIFY_VALUE
from config import SHEET_COLUMNS, CHAT_ID, SHEET_TAB_NAME
from services.google_sheets import get_gspread_sheet, get_cached_records, invalidate_sheet_cache
from services.date_parser import parse_and_format_dates
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary

# Maps sheet column names to short callback-safe aliases
FIELD_ALIAS_MAP = {
    "EVENT TYPE": "EVENT_TYPE",
    "EVENT NAME": "EVENT_NAME",
    "REHEARSAL DATE | TIME": "REHEARSAL_DATE",
    "PERF DATE | TIME": "PERF_DATE",
    "LOCATION": "LOCATION",
    "OTHER INFO": "OTHER_INFO",
    "REMUNATION": "REMUNERATION",
    "STATUS": "STATUS",
}

ALIAS_TO_FIELD = {alias: field for field, alias in FIELD_ALIAS_MAP.items()}
DATE_FIELDS = {"REHEARSAL DATE | TIME", "PERF DATE | TIME"}

def _pending_key(thread_id: int) -> str:
    return str(thread_id)

def _get_pending_edits(context: ContextTypes.DEFAULT_TYPE, thread_id: int) -> dict:
    return context.user_data.setdefault("modify_pending_edits", {}).get(_pending_key(thread_id), {})

def _set_pending_edit(context: ContextTypes.DEFAULT_TYPE, thread_id: int, field: str, value: str, original_value: str) -> bool:
    pending_by_thread = context.user_data.setdefault("modify_pending_edits", {})
    thread_edits = pending_by_thread.setdefault(_pending_key(thread_id), {})
    if str(value or "") == str(original_value or ""):
        thread_edits.pop(field, None)
    else:
        thread_edits[field] = value
    if not thread_edits:
        pending_by_thread.pop(_pending_key(thread_id), None)
        return False
    return True

def _clear_pending_edits(context: ContextTypes.DEFAULT_TYPE, thread_id: int) -> None:
    pending_by_thread = context.user_data.get("modify_pending_edits", {})
    pending_by_thread.pop(_pending_key(thread_id), None)

def _with_pending_edits(row: dict, context: ContextTypes.DEFAULT_TYPE, thread_id: int) -> dict:
    merged = dict(row)
    merged.update(_get_pending_edits(context, thread_id))
    return merged

async def start_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return ConversationHandler.END

    from handlers.private_handlers import handle_private_command
    await handle_private_command(update, context)
    return ConversationHandler.END

async def initiate_modify_via_dm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int,
    initiated_via_dm: bool,
    success_banner: str = ""
) -> None:
    """Send or edit the field-selection keyboard cleanly in a single persistent message bubble."""
    records = get_cached_records()
    context.user_data["cached_records"] = records
    row = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    target_chat = update.effective_user

    if not row:
        await target_chat.send_message("❌ This thread is not registered in the sheet.")
        return
        
    # Merge existing database row with any unsaved edits currently in memory
    pending_edits = _get_pending_edits(context, thread_id)
    row = _with_pending_edits(row, context, thread_id)

    # Build the Public Summary Preview
    public_preview = (
        f"👁️ ***Public Summary Preview:***\n"
        f"🎭 **Event:** {row.get('EVENT NAME', 'TBD')}\n"
        f"⏳ **Rehearsal:**\n{row.get('REHEARSAL DATE | TIME', 'TBD')}\n"
        f"📅 **Perf Date:**\n{row.get('PERF DATE | TIME', 'TBD')}\n"
        f"📍 **Location:** {row.get('LOCATION', 'TBD')}\n"
        f"ℹ️ **Other Info:** {row.get('OTHER INFO', 'TBD')}\n"
    )

    # Build the Internal Registry Preview
    internal_preview = (
        f"🔒 ***Internal Registry (Will not be posted):***\n"
        f"🏷️ **Event Type:** {row.get('EVENT TYPE', 'EXT')}\n"
        f"💰 **Remuneration:** {row.get('REMUNATION', 'TBD')}\n"
        f"🚥 **Status:** {row.get('STATUS', 'PENDING')}\n"
    )

    banner_text = f"{success_banner}\n\n" if success_banner else ""
    if pending_edits:
        banner_text += "⚠️ *Unsaved edits are shown below. Tap Save & Push Updates to go live.*\n\n"

    prompt_text = (
        f"{banner_text}"
        f"🛠️ ***Editing Performance: {row.get('EVENT NAME', 'Unnamed')} (ID: `{thread_id}`)***\n\n"
        f"{public_preview}\n"
        f"{internal_preview}\n"
        f"_Select a field below to modify. Changes won't go live until you save._"
    )

    # 8-Button Grid + Save + Back to N-1
    keyboard = [
        [
            InlineKeyboardButton("Event Type", callback_data=f"MODIFY|EVENT_TYPE|{thread_id}"),
            InlineKeyboardButton("Event Name", callback_data=f"MODIFY|EVENT_NAME|{thread_id}")      
        ],
        [
            InlineKeyboardButton("Rehearsal", callback_data=f"MODIFY|REHEARSAL_DATE|{thread_id}"),
            InlineKeyboardButton("Perf Date", callback_data=f"MODIFY|PERF_DATE|{thread_id}")
        ],
        [
            InlineKeyboardButton("Location", callback_data=f"MODIFY|LOCATION|{thread_id}"),
            InlineKeyboardButton("Other Info", callback_data=f"MODIFY|OTHER_INFO|{thread_id}")
        ],
        [
            InlineKeyboardButton("Remuneration", callback_data=f"MODIFY|REMUNERATION|{thread_id}"),
            InlineKeyboardButton("Status", callback_data=f"MODIFY|STATUS|{thread_id}")
        ],
        [InlineKeyboardButton("💾 Save & Push Updates", callback_data=f"MODIFY|PUBLISH_SUMMARY|{thread_id}")],
        [InlineKeyboardButton("🔙 Back to Topic Selection", callback_data=f"MODIFY|BACK_TO_LIST|{thread_id}")]
    ]

    markup = InlineKeyboardMarkup(keyboard)

    query = update.callback_query
    active_dash_id = context.user_data.get("master_dash_id")

    if query:
        try:
            await query.edit_message_text(prompt_text, reply_markup=markup, parse_mode="Markdown")
            return
        except Exception: pass

    if active_dash_id:
        try:
            await context.bot.edit_message_text(
                chat_id=target_chat.id, message_id=active_dash_id,
                text=prompt_text, reply_markup=markup, parse_mode="Markdown"
            )
            return
        except Exception: pass

    msg = await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=markup, parse_mode="Markdown")
    context.user_data["master_dash_id"] = msg.message_id
    context.user_data["modify_thread_id"] = thread_id

async def get_modify_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, field_alias, thread_token = parts
    thread_id = int(thread_token)
    context.user_data["modify_thread_id"] = thread_id

    field = ALIAS_TO_FIELD.get(field_alias, field_alias)
    records = context.user_data.get("cached_records") or get_cached_records()
    row_record = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    preview_row = _with_pending_edits(row_record, context, thread_id) if row_record else {}
    current_event_name = preview_row.get("EVENT NAME") or context.user_data.get("modify_event_name", "Selected Event")

    if field == "BACK_TO_LIST":
        context.user_data.pop("modify_field", None)
        from handlers.private_handlers import render_modify_list
        await render_modify_list(update, context, incoming_query=query)
        return ConversationHandler.END

    if field == "CANCEL" or field_alias == "DASH_VIEW" or thread_token == "HOME":
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        context.user_data.pop("modify_event_name", None)
        
        from handlers.private_handlers import DASHBOARD_TEXT, _dashboard_keyboard
        await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if field == "BACK_TO_MENU":
        context.user_data.pop("modify_field", None)
        await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)
        return ConversationHandler.END

    if field == "PUBLISH_SUMMARY":
        pending_edits = _get_pending_edits(context, thread_id)
        if not pending_edits:
            await initiate_modify_via_dm(
                update=update, context=context, thread_id=thread_id, initiated_via_dm=True,
                success_banner="ℹ️ *No edits to publish yet. Choose a section to edit first.*",
            )
            return ConversationHandler.END

        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        row_info = next(((idx, r) for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)
        if not row_info:
            await query.edit_message_text("❌ Entry not found in sheet. Please go back and select the topic again.")
            return ConversationHandler.END
        row_number, row = row_info
        updated_row = dict(row)
        updated_row.update(pending_edits)

        try:
            for edit_field, edit_value in pending_edits.items():
                col_index = SHEET_COLUMNS.index(edit_field) + 1
                sheet.update_cell(row_number, col_index, edit_value)
            invalidate_sheet_cache(tab_name=SHEET_TAB_NAME)
        except Exception as exc:
            await initiate_modify_via_dm(
                update=update, context=context, thread_id=thread_id, initiated_via_dm=True,
                success_banner=f"❌ *Failed to update Google Sheets:* `{exc}`",
            )
            return ConversationHandler.END

        rename_warning = ""
        if "EVENT NAME" in pending_edits:
            new_topic_name = f"PERF - {updated_row.get('EVENT NAME', '')}".strip()
            try:
                await context.bot.edit_forum_topic(chat_id=CHAT_ID, message_thread_id=thread_id, name=new_topic_name)
            except Exception as exc:
                rename_warning = f"\n\n⚠️ *Sheet and summary updated, but topic rename failed:* `{exc}`"

        old_status = str(row.get("STATUS", "")).strip().upper()
        new_status = str(updated_row.get("STATUS", "")).strip().upper()
        
        if new_status == "REJECTED" and old_status != "REJECTED":
            from handlers.admin_handlers import _extract_first_date_object
            from config import sg_tz
            from datetime import datetime
            
            perf_cell = str(updated_row.get("PERF DATE | TIME", "")).strip()
            event_date_obj = _extract_first_date_object(perf_cell)
            today_date = datetime.now(sg_tz).date()
            
            # Only send the message if the date is in the future (or today)
            if event_date_obj and event_date_obj.date() >= today_date:
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        message_thread_id=thread_id,
                        text="⚠️ **UPDATE:** Unfortunately, this performance has been cancelled. Thank you to everyone who showed interest!",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Failed to send cancellation notice: {e}")

        # 🧠 THE SMART LOGIC GATE & 🔔 NEW FEATURE 2: Bypass Summary for REJECTED
        public_fields = ["EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION", "OTHER INFO"]
        public_changed = any(f in pending_edits for f in public_fields)

        if public_changed:
            if new_status == "REJECTED":
                # Bypass publishing summary entirely if the event is rejected
                success_msg = f"✅ *Google Sheets updated! (Topic summary not renewed because performance is REJECTED).*{rename_warning}"
            else:
                group_chat_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
                try:
                    await publish_performance_summary(
                        bot=context.bot,
                        chat_data_store=group_chat_data,
                        sheet=sheet,
                        chat_id=CHAT_ID,
                        thread_id=thread_id,
                        event_name=updated_row.get("EVENT NAME", ""),
                        rehearsal_date=updated_row.get("REHEARSAL DATE | TIME", ""),
                        perf_date=updated_row.get("PERF DATE | TIME", ""),
                        location=updated_row.get("LOCATION", ""),
                        other_info=updated_row.get("OTHER INFO", ""),
                    )
                    success_msg = f"✅ *Published and pinned the updated summary in the topic!*{rename_warning}"
                except Exception as exc:
                    await initiate_modify_via_dm(
                        update=update,
                        context=context,
                        thread_id=thread_id,
                        initiated_via_dm=True,
                        success_banner=f"❌ *Failed to publish summary:* `{exc}`",
                    )
                    return ConversationHandler.END
        else:
            success_msg = f"✅ *Internal registry updated! (No public fields changed, so the Topic was not pinged).*{rename_warning}"
        context.user_data.pop("modify_field", None)
        _clear_pending_edits(context, thread_id)
        
        import re
        from datetime import datetime
        
        records = get_cached_records()
        context.user_data["cached_records"] = records
        
        def get_sort_date(r):
            date_str = str(r.get("PERF DATE | TIME", "")).strip()
            if not date_str or date_str == "-": return datetime.min
            first_line = date_str.splitlines()[0].strip()
            match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
            if match:
                try: return datetime.strptime(match.group(1), "%d %b %Y")
                except ValueError: pass
            return datetime.min

        sorted_records = sorted(records, key=get_sort_date, reverse=True)
        buttons = []
        for r in sorted_records:
            tid = r.get("THREAD ID")
            if str(tid).isdigit():
                event_name = r.get("EVENT NAME", "Unnamed Event")
                buttons.append([InlineKeyboardButton(f"⚙️ {event_name} ({tid})", callback_data=f"LIST_MODIFY|{tid}")])
                
        buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
        
        # Inject the success message above the list prompt!
        prompt_text = f"{success_msg}\n\n🛠️ ***Performance Modification Portal***\nWhich PERFORMANCE topic would you like to modify:"
        
        await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["modify_field"] = field

    if field == "STATUS":
        buttons = [
            [InlineKeyboardButton("✅ ACCEPTED", callback_data="modify_status_selected|ACCEPTED")],
            [InlineKeyboardButton("❌ REJECTED", callback_data="modify_status_selected|REJECTED")],
            [InlineKeyboardButton("⏳ PENDING (Reset)", callback_data="modify_status_selected|PENDING")],
            [InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await query.edit_message_text(
            text=(f"📊 Please select the new *STATUS* for *{current_event_name}*:\n"
                  f"Note: Rejections will update logs but will NOT delete or close the topic channel"),
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown",
        )
        return ConversationHandler.END

    if field == "EVENT TYPE":
        buttons = [
            [InlineKeyboardButton("🎭 EXT (External)", callback_data="modify_type_selected|EXT")],
            [InlineKeyboardButton("🏠 INT (Internal)", callback_data="modify_type_selected|INT")],
            [InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await query.edit_message_text(
            text=f"🎭 Please select the new *EVENT TYPE* for *{current_event_name}*:",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown",
        )
        return ConversationHandler.END

    # =========================================================================
    # FREE TEXT ENTRIES: Edit the bubble in-place!
    # =========================================================================
    escape_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")]
    ])

    if field == "REHEARSAL DATE | TIME":
        prompt_text = (
            "🔁 *Update Rehearsal Date & Time*\n\n"
            "*Examples:*\n"
            "Single date Single time: `31 aug 9pm`\n"
            "Single date Multiple times: `1 sep 7pm 2100`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug 9pm`\n"
            "`1 sep 7pm 2100`\n\n"
            
            "👉 _Type your new value below (or type `-` to clear it):_"
        )
    elif field == "PERF DATE | TIME":
        prompt_text = (
            "📅 *Update Performance Date & Time*\n\n"
            
            "*Examples:*\n"
            "Single date Single time: `31 aug 9pm`\n"
            "Single date Multiple times: `1 sep 7pm 2100`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug 9pm`\n"
            "`1 sep 7pm 2100`\n\n"
            
            "👉 _Type your new value below (or type `-` to clear it):_"
        )
    elif field == "REMUNATION":
        prompt_text = f"💰 Enter the *Remuneration* details for *{current_event_name}*:\n\n👉 _Type your new value below:_"
    else:
        prompt_text = f"✏️ Enter the new *{field}* for *{current_event_name}*:\n\n👉 _Type your new value below:_"

    records = context.user_data.get("cached_records", [])
    row_record = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    if row_record:
        row_record = _with_pending_edits(row_record, context, thread_id)
        old_val = row_record.get(field, "")
        if old_val and old_val != "-":
            prompt_text += f"\n\n📋 *Previous Value (Tap box below to copy instantly):*\n```\n{old_val}```\n"

    await query.edit_message_text(text=prompt_text, reply_markup=escape_keyboard, parse_mode="Markdown")
    return MODIFY_VALUE

async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply free-text updates and cleanly rewrite the exact same message bubble back to the matrix menu."""
    raw_value = update.message.text.strip()
    
    # Instantly wipe user's input text to keep DM timeline clean
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception:
        pass
    
    field = context.user_data.get("modify_field")
    thread_id = context.user_data.get("modify_thread_id")
    
    if raw_value.lower() in ["/cancel", "cancel", "exit"]:
        context.user_data.pop("modify_field", None)
        await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)
        return ConversationHandler.END

    if not field or thread_id is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Error tracking context. Use /modify.")
        return ConversationHandler.END

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_info = next(((idx, r) for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)

    if row_info is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Thread row not found.")
        return ConversationHandler.END
    _row_number, row_record = row_info

    normalized_value = raw_value
    try:
        if field in DATE_FIELDS and raw_value.strip() != "-":
            formatted = parse_and_format_dates(raw_value)
            normalized_value = "\n".join(formatted)

        for key in ["modify_error_msg_id"]:
            msg_id = context.chat_data.pop(key, None)
            if msg_id:
                try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except Exception: pass
    except ValueError as exc:
        escape_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")]
        ])
        
        error_prompt = f"⚠️ **ERROR:** {str(exc)}\n\n👉 **Please re-enter matching guidelines:**"
        if raw_value and raw_value != "-":
            error_prompt += f"\n\n📋 *Previous Value (Tap box below to copy instantly):*\n```\n{raw_value}```\n"
            
        active_dash_id = context.user_data.get("master_dash_id")
        if active_dash_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id, message_id=active_dash_id, 
                    text=error_prompt, reply_markup=escape_keyboard, parse_mode="Markdown"
                )
                return MODIFY_VALUE
            except Exception: pass
            
        error_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=error_prompt, reply_markup=escape_keyboard, parse_mode="Markdown")
        context.chat_data["modify_error_msg_id"] = error_msg.message_id
        return MODIFY_VALUE

    has_pending = _set_pending_edit(context, thread_id, field, normalized_value, row_record.get(field, ""))

    if not has_pending:
        banner_msg = f"ℹ️ *No unsaved edits for [{field}]. The preview matches Google Sheets.*"
    elif field in ["REMUNATION", "STATUS", "EVENT TYPE"]:
        banner_msg = f"🟢 *Staged internal field [{field}]. Tap Confirm & Publish to update Google Sheets.*"
    else:
        banner_msg = f"🟢 *Staged [{field}]. Review the preview, then tap Confirm & Publish when ready.*"

    # Clear field sub-state context
    context.user_data.pop("modify_field", None)
    
    # 🎯 LOOPBACK IN-PLACE: Rewrite the exact same active message bubble back to the fields grid list!
    await initiate_modify_via_dm(update, context, thread_id=thread_id, initiated_via_dm=True, success_banner=banner_msg)
    return ConversationHandler.END

async def handle_modify_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard buttons for updating the EVENT TYPE field strictly (internal only)."""
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if len(data) != 2:
        return

    _, selection = data
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        context.user_data.pop("modify_field", None)
        await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)
        return

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_record = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)

    if row_record is None:
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ Entry row not found.")
        return

    has_pending = _set_pending_edit(context, thread_id, "EVENT TYPE", selection, row_record.get("EVENT TYPE", ""))
    if has_pending:
        banner_msg = "🟢 *Staged internal field [EVENT TYPE]. Tap Confirm & Publish to update Google Sheets.*"
    else:
        banner_msg = "ℹ️ *No unsaved edits for [EVENT TYPE]. The preview matches Google Sheets.*"

    context.user_data.pop("modify_field", None)
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True, success_banner=banner_msg)

async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if len(data) != 2:
        return

    _, selection = data
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        context.user_data.pop("modify_field", None)
        await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)
        return

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    record = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)

    if record is None:
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ Entry not found in sheet.")
        return

    db_value = "" if selection == "PENDING" else selection
    has_pending = _set_pending_edit(context, thread_id, "STATUS", db_value, record.get("STATUS", ""))
    if has_pending:
        banner_msg = "🟢 *Staged internal field [STATUS]. Tap Confirm & Publish to update Google Sheets.*"
    else:
        banner_msg = "ℹ️ *No unsaved edits for [STATUS]. The preview matches Google Sheets.*"

    context.user_data.pop("modify_field", None)
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True, success_banner=banner_msg)

async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("This action is no longer available.", show_alert=True)
