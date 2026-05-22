from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import asyncio

from utils.constants import MODIFY_VALUE
from utils.decorators import is_admin
from config import SHEET_COLUMNS, EXEMPTED_THREAD_IDS, CHAT_ID
from services.google_sheets import get_gspread_sheet
from services.date_parser import parse_and_format_dates
from handlers.conversation_handlers import build_performance_summary, publish_performance_summary
from utils.helpers import delete_topic_with_delay

# Maps sheet column names to short callback-safe aliases
FIELD_ALIAS_MAP = {
    "EVENT TYPE": "EVENT_TYPE",
    "EVENT NAME": "EVENT_NAME",
    "REHEARSAL DATE | TIME": "REHEARSAL_DATE",
    "PERF DATE | TIME": "PERF_DATE",
    "LOCATION": "LOCATION",
    "OTHER INFO": "OTHER_INFO",
    "STATUS": "STATUS",
}

ALIAS_TO_FIELD = {alias: field for field, alias in FIELD_ALIAS_MAP.items()}

# Fields where text input is validated as a date string
DATE_FIELDS = {"REHEARSAL DATE | TIME", "PERF DATE | TIME"}


async def start_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /modify command in a group topic thread.

    Deletes the command message, checks admin rights and exemption list,
    then delegates to initiate_modify_via_dm which sends the field picker
    to the admin's DM.
    """
    msg = update.effective_message

    try:
        await update.message.delete()
    except Exception as e:
        print(f"[DEBUG] Failed to delete /modify command: {e}")

    if not msg.is_topic_message:
        return

    thread_id = msg.message_thread_id

    if thread_id in EXEMPTED_THREAD_IDS or not await is_admin(update, context):
        return

    print(f"[DEBUG] /modify triggered by user {update.effective_user.id} in chat {msg.chat_id}, thread {thread_id}")
    await initiate_modify_via_dm(update=update, context=context, thread_id=thread_id, initiated_via_dm=False)
    return ConversationHandler.END


async def initiate_modify_via_dm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int,
    initiated_via_dm: bool,
) -> None:
    """Send the field-selection keyboard to the admin (via DM or current chat).

    initiated_via_dm=True keeps the conversation in the DM chat;
    False redirects replies to the user's DM even when triggered from the group.
    """
    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)

    target_chat = update.effective_chat
    if not initiated_via_dm:
        target_chat = update.effective_user

    if not row:
        await target_chat.send_message("❌ This thread is not registered in the sheet.")
        return

    status = row.get("STATUS", "").strip().upper()
    if status == "REJECTED":
        await target_chat.send_message("❌ This performance is already REJECTED. You cannot modify it.")
        return

    modify_options = ["EVENT TYPE", "EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION", "OTHER INFO"]
    if status != "":
        modify_options.append("STATUS")

    emoji_map = {
        "EVENT TYPE": "🎭",
        "EVENT NAME": "📍",
        "REHEARSAL DATE | TIME": "🔁",
        "PERF DATE | TIME": "📅",
        "LOCATION": "📌",
        "OTHER INFO": "📝",
        "STATUS": "📊",
    }

    keyboard = []
    for opt in modify_options:
        alias = FIELD_ALIAS_MAP.get(opt, opt.replace(" ", "_").upper())
        emoji = emoji_map.get(opt, "")
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {opt.title()}",
            callback_data=f"MODIFY|{alias}|{thread_id}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"MODIFY|CANCEL|{thread_id}")])

    prompt = await target_chat.send_message(
        "✏️ What would you like to update?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    prompt_store = context.application.bot_data.setdefault("modify_prompts", {})
    prompt_store[(target_chat.id, thread_id)] = prompt.message_id

    context.user_data["modify_thread_id"] = thread_id
    context.user_data["modify_initiated_dm"] = initiated_via_dm


async def get_modify_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection from the modify keyboard.

    STATUS field shows a button picker; all other fields (including date
    fields) prompt for free-text input validated in apply_modify_value.
    """
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, field_alias, thread_token = parts
    thread_id = int(thread_token)
    context.user_data["modify_thread_id"] = thread_id

    try:
        await context.bot.delete_message(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
        )
    except Exception:
        pass

    prompt_store = context.application.bot_data.get("modify_prompts", {})
    prompt_store.pop((query.message.chat.id, thread_id), None)

    field = ALIAS_TO_FIELD.get(field_alias, field_alias)

    if field == "CANCEL":
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="❌ Modification cancelled."
        )
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        return ConversationHandler.END

    context.user_data["modify_field"] = field

    if field == "STATUS":
        buttons = [
            [InlineKeyboardButton("❌ Reject Performance", callback_data="modify_status_selected|REJECTED")],
            [InlineKeyboardButton("↩️ Cancel", callback_data="modify_status_selected|CANCEL")],
        ]
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🚦 Please select the new *STATUS*:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    hint = ""
    if field in DATE_FIELDS:
        hint = "\n_Format: `23aug25 8pm` or `23aug25` — use `-` to clear_"

    prompt = await context.bot.send_message(
        chat_id=query.from_user.id,
        text=f"✏️ Enter the new value for *{field}*:{hint}",
        parse_mode="Markdown",
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE


async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply a free-text value to the field selected in get_modify_field_callback.

    Date fields (REHEARSAL DATE | TIME, PERF DATE | TIME) are validated and
    normalised through parse_and_format_dates before writing.  A '-' input
    for a date field is stored as-is (clears the date).  After a successful
    write the pinned performance summary in the group is refreshed.
    """
    raw_value = update.message.text.strip()
    if raw_value.lower() == "/cancel":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Modification cancelled."
        )
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        return ConversationHandler.END

    field = context.user_data.get("modify_field")
    thread_id = context.user_data.get("modify_thread_id")
    if not field or thread_id is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Unable to determine which field to update. Please start over with /modify."
        )
        return ConversationHandler.END

    print(f"[DEBUG] Applying new value: {raw_value} to field: {field} for thread ID: {thread_id}")

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()

    row_number = None
    record = None
    for idx, row in enumerate(records, start=2):
        if str(row.get("THREAD ID")) == str(thread_id):
            row_number = idx
            record = row
            break

    if row_number is None or record is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This thread is not registered in the sheet."
        )
        return ConversationHandler.END

    status = record.get("STATUS", "").strip().upper()
    if status == "REJECTED":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This performance is already REJECTED. You cannot modify it."
        )
        return ConversationHandler.END

    allowed_fields = ["EVENT TYPE", "EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION", "OTHER INFO"]
    if status != "":
        allowed_fields.append("STATUS")

    if field not in allowed_fields:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⛔ You can't modify *{field}* at this stage.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if field == "STATUS":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Please use the status buttons instead of typing the value."
        )
        return ConversationHandler.END

    normalized_value = raw_value

    try:
        if field in DATE_FIELDS and raw_value.strip() != "-":
            formatted = parse_and_format_dates(raw_value)
            normalized_value = "\n".join(formatted)
            print(f"[DEBUG] Normalised date value: {normalized_value}")

        for key in ["modify_error_msg_id", "invalid_input_msg_id"]:
            msg_id = context.chat_data.pop(key, None)
            if msg_id:
                try:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except Exception as ex:
                    print(f"[WARNING] Failed to delete previous {key} message: {ex}")

    except ValueError as exc:
        error_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=str(exc),
            parse_mode="Markdown",
        )
        context.chat_data["modify_error_msg_id"] = error_msg.message_id
        context.chat_data["invalid_input_msg_id"] = update.message.message_id
        return MODIFY_VALUE

    col_index = SHEET_COLUMNS.index(field) + 1

    try:
        sheet.update_cell(row_number, col_index, normalized_value)
        print(f"[DEBUG] Sheet updated at row {row_number}, column {col_index}")
    except Exception as exc:
        print(f"[ERROR] Failed to update sheet: {exc}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Failed to update: `{exc}`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Clean up DM prompt messages
    prompt_ids = context.chat_data.pop("modify_prompt_msg_ids", [])
    for msg_id in prompt_ids:
        try:
            await asyncio.sleep(0.2)
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except BadRequest as exc:
            print(f"[INFO] Prompt message {msg_id} already handled: {exc}")
        except Exception as exc:
            print(f"[WARNING] Failed to delete prompt message {msg_id}: {exc}")

    try:
        await update.message.delete()
    except Exception:
        pass

    updated_row = sheet.row_values(row_number)
    while len(updated_row) < len(SHEET_COLUMNS):
        updated_row.append("")
    updated_record = dict(zip(SHEET_COLUMNS, updated_row))

    summary_preview = build_performance_summary(
        event_name=updated_record.get("EVENT NAME", ""),
        rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
        perf_date=updated_record.get("PERF DATE | TIME", ""),
        location=updated_record.get("LOCATION", ""),
        other_info=updated_record.get("OTHER INFO", ""),
    )

    group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
    group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})

    try:
        await publish_performance_summary(
            bot=context.bot,
            chat_data_store=group_chat_data,
            sheet=sheet,
            chat_id=CHAT_ID,
            thread_id=thread_id,
            event_name=updated_record.get("EVENT NAME", ""),
            rehearsal_date=updated_record.get("REHEARSAL DATE | TIME", ""),
            perf_date=updated_record.get("PERF DATE | TIME", ""),
            location=updated_record.get("LOCATION", ""),
            other_info=updated_record.get("OTHER INFO", ""),
        )
    except BadRequest as exc:
        print(f"[WARNING] Could not refresh pinned summary for thread {thread_id}: {exc}")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Updated *{field}* for thread `{thread_id}`.\n\n{summary_preview}",
        parse_mode="Markdown",
    )

    context.user_data.pop("modify_field", None)
    context.user_data.pop("modify_thread_id", None)

    return ConversationHandler.END


async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stub — the date picker is no longer used in the modify flow.

    Kept registered so stale modify_date_selected callbacks don't raise errors.
    """
    query = update.callback_query
    await query.answer("This action is no longer available.", show_alert=True)


async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle STATUS change buttons — currently only supports REJECTED.

    On REJECTED: updates the sheet, unpins and deletes the summary, notifies
    the group, then schedules topic deletion via delete_topic_with_delay.
    """
    query = update.callback_query
    data = query.data.split("|")
    if len(data) != 2:
        await query.answer()
        return

    _, selection = data
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        await query.answer("Status update cancelled.", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        return

    if selection != "REJECTED":
        await query.answer()
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="⚠️ Unsupported status option."
        )
        return

    await query.answer()

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_number = None
    for idx, row in enumerate(records, start=2):
        if str(row.get("THREAD ID")) == str(thread_id):
            row_number = idx
            break

    if row_number is None:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="❌ Unable to locate this performance entry in the sheet."
        )
        return

    try:
        sheet.update_cell(row_number, SHEET_COLUMNS.index("STATUS") + 1, "REJECTED")
    except Exception as exc:
        print(f"[ERROR] Failed to update status: {exc}")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"❌ Failed to update status: `{exc}`",
            parse_mode="Markdown",
        )
        return

    group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
    group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})
    summary_key = f"summary_msg_{thread_id}"
    poll_key = f"interest_poll_msg_{thread_id}"

    summary_msg_id = group_chat_data.pop(summary_key, None)
    if summary_msg_id:
        try:
            await context.bot.unpin_chat_message(chat_id=CHAT_ID, message_id=summary_msg_id)
        except Exception as exc:
            print(f"[WARNING] Failed to unpin summary during rejection: {exc}")
        try:
            await context.bot.delete_message(chat_id=CHAT_ID, message_id=summary_msg_id)
        except Exception as exc:
            print(f"[WARNING] Failed to delete summary during rejection: {exc}")

    poll_msg_id = group_chat_data.pop(poll_key, None)
    if poll_msg_id:
        try:
            await context.bot.delete_message(chat_id=CHAT_ID, message_id=poll_msg_id)
        except Exception as exc:
            print(f"[WARNING] Failed to delete poll during rejection: {exc}")

    try:
        await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="🚫 Performance marked as *REJECTED*. The topic will be deleted shortly.",
        parse_mode="Markdown",
    )

    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="🚫 This performance has been *cancelled*. The topic will be deleted shortly.",
            parse_mode="Markdown",
            message_thread_id=thread_id,
        )
    except Exception as exc:
        print(f"[WARNING] Failed to notify group about rejection: {exc}")

    await delete_topic_with_delay(context, chat_id=CHAT_ID, thread_id=thread_id)
    context.user_data.pop("modify_field", None)
    context.user_data.pop("modify_thread_id", None)
