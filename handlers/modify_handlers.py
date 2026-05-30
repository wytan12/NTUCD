from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import asyncio

from utils.constants import MODIFY_VALUE
from utils.decorators import is_admin
from config import SHEET_COLUMNS, CHAT_ID
from services.google_sheets import get_gspread_sheet
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
    """LOCK DOWN: Block public group execution completely to keep channels clean."""
    msg = update.effective_message
    try:
        await update.message.delete()
    except Exception:
        pass

    if msg.is_topic_message or update.effective_chat.type in {"group", "supergroup"}:
        return ConversationHandler.END

    if not await is_admin(update, context):
        return ConversationHandler.END

    from handlers.private_handlers import handle_private_command
    await handle_private_command(update, context)
    return ConversationHandler.END

async def initiate_modify_via_dm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int,
    initiated_via_dm: bool,
) -> None:
    """Send the field-selection keyboard drawing details straight from local cache entries."""
    records = context.user_data.get("cached_records", [])
    
    if not records:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
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
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"MODIFY|CANCEL|{thread_id}")])

    current_event_name = row.get("EVENT NAME", "Unnamed Event")
    
    # 🧠 FIX: Capture the return object into 'prompt' to avoid Pylance unbound trace crashes
    prompt = await target_chat.send_message(
        f"✏️ What would you like to update for *{current_event_name}* (ID: `{thread_id}`)?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    
    prompt_store = context.application.bot_data.setdefault("modify_prompts", {})
    prompt_store[(target_chat.id, thread_id)] = prompt.message_id

    context.user_data["modify_thread_id"] = thread_id
    context.user_data["modify_initiated_dm"] = True

async def get_modify_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection from the modify keyboard panel with instant query resolving."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, field_alias, thread_token = parts
    thread_id = int(thread_token)
    context.user_data["modify_thread_id"] = thread_id

    current_event_name = context.user_data.get("modify_event_name", "Selected Event")

    try:
        await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
    except Exception:
        pass

    field = ALIAS_TO_FIELD.get(field_alias, field_alias)

    if field == "CANCEL":
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ Modification cancelled.")
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        context.user_data.pop("modify_event_name", None)
        return ConversationHandler.END

    # 🧠 BACK ROUTER LATCH: Intercepts if the user hits the sub-prompt "Back" button
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
            # Uses routing shortcut directly to jump backward easily
            [InlineKeyboardButton("↩️ Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await context.bot.send_message(
            chat_id=query.from_user.id,
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
            [InlineKeyboardButton("↩️ Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        ]
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"🎭 Please select the new *EVENT TYPE* for *{current_event_name}*:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # 🛠️ UPGRADED UI KEYBOARD: Added dual back and completely cancel button navigation layout lines
    escape_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
        [InlineKeyboardButton("❌ Cancel Completely", callback_data="MODIFY|CANCEL|0")]
    ])

    if field == "REHEARSAL DATE | TIME":
        prompt_text = (
            "🔁 *Update Rehearsal Date & Time*\n\n"
            
            "*Examples:*\n"
            "Single date Single time: `31 aug, 9pm`\n"
            "Single date Multiple times: `1 sep, 7pm 9pm`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug, 9pm`\n"
            "`1 sep, 7pm 9pm`\n"
            "Separate the date and time using a **comma (,)**.\n\n"
            "👉 _Type your new value below (or type `-` to clear it):_"
        )
    elif field == "PERF DATE | TIME":
        prompt_text = (
            "📅 *Update Performance Date & Time*\n\n"
            
            "*Examples:*\n"
            "Single date Single time: `31 aug, 9pm`\n"
            "Single date Multiple times: `1 sep, 7pm 9pm`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug, 9pm`\n"
            "`1 sep, 7pm 9pm`\n"
            "Separate the date and time using a **comma (,)**.\n\n"
            "👉 _Type your new value below (or type `-` to clear it):_"
        )
    elif field == "REMUNATION":
        prompt_text = f"💰 Enter the *Remuneration* details for *{current_event_name}*"
    else:
        prompt_text = f"✏️ Enter the new *{field}* for *{current_event_name}*"

    prompt = await context.bot.send_message(
        chat_id=query.from_user.id, 
        text=prompt_text, 
        reply_markup=escape_keyboard, 
        parse_mode="Markdown"
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE

async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply a free-text value to the selected row cell field without DM message deletion."""
    raw_value = update.message.text.strip()
    
    if raw_value.lower() in ["/cancel", "cancel", "exit"]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Modification cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    field = context.user_data.get("modify_field")
    thread_id = context.user_data.get("modify_thread_id")
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

        for key in ["modify_error_msg_id", "invalid_input_msg_id"]:
            msg_id = context.chat_data.pop(key, None)
            if msg_id:
                try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except Exception: pass
    except ValueError as exc:
        escape_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Back to Fields Menu", callback_data=f"MODIFY|BACK_TO_MENU|{thread_id}")],
            [InlineKeyboardButton("❌ Cancel Completely", callback_data="MODIFY|CANCEL|0")]
        ])
        error_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=str(exc), 
            reply_markup=escape_keyboard, 
            parse_mode="Markdown"
        )
        context.chat_data["modify_error_msg_id"] = error_msg.message_id
        context.chat_data["invalid_input_msg_id"] = update.message.message_id
        return MODIFY_VALUE

    try:
        col_index = SHEET_COLUMNS.index(field) + 1
        sheet.update_cell(row_number, col_index, normalized_value)
    except Exception as exc:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Failed to update sheet: `{exc}`", parse_mode="Markdown")
        return ConversationHandler.END

    prompt_ids = context.chat_data.pop("modify_prompt_msg_ids", [])
    for msg_id in prompt_ids:
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except Exception: pass

    # Update cache row context after a successful write
    updated_row = sheet.row_values(row_number)
    while len(updated_row) < len(SHEET_COLUMNS):
        updated_row.append("")
    updated_record = dict(zip(SHEET_COLUMNS, updated_row))
    
    # Refresh cache records to maintain integrity across backwards traversal jumps
    if context.user_data.get("cached_records"):
        idx_match = row_number - 2
        if idx_match < len(context.user_data["cached_records"]):
            context.user_data["cached_records"][idx_match] = updated_record

    if field not in ["REMUNATION", "STATUS"]:
        group_chat_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
        try:
            await publish_performance_summary(
                bot=context.bot, chat_data_store=group_chat_data, sheet=sheet, chat_id=CHAT_ID, thread_id=thread_id,
                event_name=updated_record.get("EVENT NAME", ""), rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
                perf_date=updated_record.get("PERF DATE | TIME", ""), location=updated_record.get("LOCATION", ""),
                other_info=updated_record.get("OTHER INFO", ""),
            )
        except Exception: pass

    current_name = updated_record.get("EVENT NAME", "Unnamed Event")
    if field in ["REMUNATION", "STATUS"]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Updated *{field}* for *{current_name}* to: `{normalized_value}`",
            parse_mode="Markdown",
        )
    else:
        summary_preview = build_performance_summary(
            event_name=updated_record.get("EVENT NAME", ""), rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
            perf_date=updated_record.get("PERF DATE | TIME", ""), location=updated_record.get("LOCATION", ""),
            other_info=updated_record.get("OTHER INFO", ""),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Updated *{field}* for *{current_name}*.\n\n{summary_preview}",
            parse_mode="Markdown",
        )

    # 🧠 APP BEHAVIOR UPDATE: Instead of ending the loop conversation after one update, 
    # route them straight back to the field options selection panel dynamically for a seamless multi-edit flow!
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)
    return ConversationHandler.END

async def handle_modify_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard buttons for updating the EVENT TYPE field strictly."""
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if len(data) != 2:
        return

    _, selection = data
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        # Handle backward step explicitly
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
        await query.message.delete()
    except Exception: pass

    updated_row = sheet.row_values(row_number)
    while len(updated_row) < len(SHEET_COLUMNS):
        updated_row.append("")
    updated_record = dict(zip(SHEET_COLUMNS, updated_row))

    group_chat_data = context.application.bot_data.setdefault("group_chat_data", {}).setdefault(CHAT_ID, {})
    try:
        await publish_performance_summary(
            bot=context.bot, chat_data_store=group_chat_data, sheet=sheet, chat_id=CHAT_ID, thread_id=thread_id,
            event_name=updated_record.get("EVENT NAME", ""), rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
            perf_date=updated_record.get("PERF DATE | TIME", ""), location=updated_record.get("LOCATION", ""),
            other_info=updated_record.get("OTHER INFO", ""),
        )
    except Exception: pass

    await context.bot.send_message(
        chat_id=query.from_user.id, text=f"✅ Updated *EVENT TYPE* to `{selection}` for thread `{thread_id}`.", parse_mode="Markdown"
    )
    
    # Loop back menu sequence
    context.user_data.pop("modify_field", None)
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)

async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle STATUS change buttons safely — strictly 1-to-1 DM confirmation without group topic spam."""
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
        await query.message.delete()
    except Exception as exc:
        print(f"[ERROR] Failed to update status cell: {exc}")
        return

    current_name = record.get("EVENT NAME", "Unnamed Event")
    await context.bot.send_message(
        chat_id=query.from_user.id, 
        text=f"📊 Status for *{current_name}* has been safely set to `{selection}`.", 
        parse_mode="Markdown"
    )
    
    # Loop back menu sequence
    context.user_data.pop("modify_field", None)
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=True)

async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stub — kept registered to prevent callback routing crashes."""
    await update.callback_query.answer("This action is no longer available.", show_alert=True)