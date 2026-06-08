from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import asyncio

from utils.constants import MODIFY_VALUE
from config import SHEET_COLUMNS, CHAT_ID
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

    modify_options = ["EVENT TYPE", "EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION", "OTHER INFO", "REMUNATION", "STATUS"]

    emoji_map = {
        "EVENT TYPE": "🎭",
        "EVENT NAME": "📍",
        "REHEARSAL DATE | TIME": "🔁",
        "PERF DATE | TIME": "📅",
        "LOCATION": "📌",
        "OTHER INFO": "📝",
        "REMUNATION": "💰",
        "STATUS": "📊",
    }

    keyboard = []
    for opt in modify_options:
        alias = FIELD_ALIAS_MAP.get(opt, opt.replace(" ", "_").upper())
        emoji = emoji_map.get(opt, "")
        keyboard.append([InlineKeyboardButton(f"{emoji} {opt.title()}", callback_data=f"MODIFY|{alias}|{thread_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Topic Selection List", callback_data=f"MODIFY|BACK_TO_LIST|{thread_id}")])
    keyboard.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])

    current_event_name = row.get("EVENT NAME", "Unnamed Event")
    
    # Prepend success banners natively if looping back from a completed update
    prompt_text = ""
    if success_banner:
        prompt_text = f"{success_banner}\n\n"
    prompt_text += f"✏️ What would you like to update for *{current_event_name}* (ID: `{thread_id}`)? "

    query = update.callback_query
    active_dash_id = context.user_data.get("master_dash_id")

    # 🎯 FORCE IN-PLACE EDITS ALWAYS: Never send a new bubble if an active workspace anchor exists
    if query:
        try:
            await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return
        except Exception:
            pass

    if active_dash_id:
        try:
            await context.bot.edit_message_text(
                chat_id=target_chat.id,
                message_id=active_dash_id,
                text=prompt_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
        except Exception:
            pass

    msg = await context.bot.send_message(
        chat_id=target_chat.id,
        text=prompt_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
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

    current_event_name = context.user_data.get("modify_event_name", "Selected Event")
    field = ALIAS_TO_FIELD.get(field_alias, field_alias)

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

    context.user_data["modify_field"] = field

    if field == "STATUS":
        buttons = [
            [InlineKeyboardButton("✅ ACCEPTED", callback_data="modify_status_selected|ACCEPTED")],
            [InlineKeyboardButton("❌ REJECTED", callback_data="modify_status_selected|REJECTED")],
            [InlineKeyboardButton("⏳ PENDING (Reset)", callback_data="modify_status_selected|PENDING")],
            [InlineKeyboardButton("🔙 Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await query.edit_message_text(
            text=(
                f"📊 Please select the new *STATUS* for *{current_event_name}*:\n"
                f"Note: Rejections will update logs but will NOT delete or close the topic channel"
            ),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if field == "EVENT TYPE":
        buttons = [
            [InlineKeyboardButton("🎭 EXT (External)", callback_data="modify_type_selected|EXT")],
            [InlineKeyboardButton("🏠 INT (Internal)", callback_data="modify_type_selected|INT")],
            [InlineKeyboardButton("🔙 Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await query.edit_message_text(
            text=f"🎭 Please select the new *EVENT TYPE* for *{current_event_name}*:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # =========================================================================
    # 🎯 FIXED FOR FREE TEXT ENTRIES: Edit the bubble in-place! Never delete!
    # =========================================================================
    escape_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")]
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

    # Pull "Tap to Copy" metrics from local state cache
    records = context.user_data.get("cached_records", [])
    row_record = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
    if row_record:
        old_val = row_record.get(field, "")
        if old_val and old_val != "-":
            prompt_text += f"\n\n📋 *Previous Value (Tap box below to copy instantly):*\n```\n{old_val}```\n"

    # Edit the exact same bubble in-place to display text input instructions
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
    row_number = next((idx for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)

    if row_number is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Thread row not found.")
        return ConversationHandler.END

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
            [InlineKeyboardButton("🔙 Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")]
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

    try:
        col_index = SHEET_COLUMNS.index(field) + 1
        sheet.update_cell(row_number, col_index, normalized_value)
        invalidate_sheet_cache()
    except Exception as exc:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to update sheet: `{exc}`", parse_mode="Markdown")
        return ConversationHandler.END

    updated_row = sheet.row_values(row_number)
    while len(updated_row) < len(SHEET_COLUMNS):
        updated_row.append("")
    updated_record = dict(zip(SHEET_COLUMNS, updated_row))
    
    if context.user_data.get("cached_records"):
        idx_match = row_number - 2
        if idx_match < len(context.user_data["cached_records"]):
            context.user_data["cached_records"][idx_match] = updated_record

    is_public_broadcast_field = field not in ["REMUNATION", "STATUS"]
    if is_public_broadcast_field:
        banner_msg = f"✨ *Success: Modified {field} n published a fresh Summary Opportunity directly to the topic!*"
        group_chat_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
        try:
            await publish_performance_summary(
                bot=context.bot, chat_data_store=group_chat_data, sheet=sheet, chat_id=CHAT_ID, thread_id=thread_id,
                event_name=updated_record.get("EVENT NAME", ""), rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
                perf_date=updated_record.get("PERF DATE | TIME", ""), location=updated_record.get("LOCATION", ""),
                other_info=updated_record.get("OTHER INFO", ""),
            )
        except Exception: pass
    else:
        banner_msg = f"🟢 *Success: Updated internal field [{field}] inside the database registry!*"

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
    row_number = next((idx for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)

    if row_number is None:
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ Entry row not found.")
        return

    try:
        col_index = SHEET_COLUMNS.index("EVENT TYPE") + 1
        sheet.update_cell(row_number, col_index, selection)
        invalidate_sheet_cache()
    except Exception: 
        pass

    banner_msg = f"🟢 *Success: Updated internal field [EVENT TYPE] inside the database registry!*"

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
    row_info = next(((idx, r) for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)

    if row_info is None:
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ Entry not found in sheet.")
        return

    row_number, record = row_info
    db_value = "" if selection == "PENDING" else selection

    try:
        sheet.update_cell(row_number, SHEET_COLUMNS.index("STATUS") + 1, db_value)
        invalidate_sheet_cache()
    except Exception as exc:
        print(f"[ERROR] Failed to update status cell: {exc}")
        return

    banner_msg = f"🟢 *Success: Updated internal field [STATUS] inside the database registry!*"

    context.user_data.pop("modify_field", None)
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True, success_banner=banner_msg)

async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("This action is no longer available.", show_alert=True)