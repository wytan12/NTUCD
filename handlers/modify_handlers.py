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

FIELD_ALIAS_MAP = {
    "EVENT": "EVENT",
    "PROPOSED DATE | TIME": "PROPOSED_DATE",
    "LOCATION": "LOCATION",
    "PERFORMANCE INFO": "PERFORMANCE_INFO",
    "CONFIRMED DATE | TIME": "CONFIRMED_DATE",
    "STATUS": "STATUS",
}

ALIAS_TO_FIELD = {alias: field for field, alias in FIELD_ALIAS_MAP.items()}

async def start_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start modification process"""
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
    """Kick off the modify flow via DM, sharing logic with /modify command."""

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

    if status == "":
        date_field = "PROPOSED DATE | TIME"
        modify_options = ["EVENT", date_field, "LOCATION", "PERFORMANCE INFO"]
    else:
        date_field = "CONFIRMED DATE | TIME"
        modify_options = ["EVENT", date_field, "LOCATION", "PERFORMANCE INFO", "STATUS"]

    emoji_map = {
        "EVENT": "📍",
        "CONFIRMED DATE | TIME": "📅",
        "PROPOSED DATE | TIME": "📅",
        "LOCATION": "📌",
        "PERFORMANCE INFO": "📝",
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
    """Handle field selection for modification"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, field, thread_token = parts
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

    field = ALIAS_TO_FIELD.get(field, field)

    if field == "CANCEL":
        print("[DEBUG] User selected cancel button")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="❌ Modification cancelled."
        )
        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)
        return ConversationHandler.END

    context.user_data["modify_field"] = field

    if field == "CONFIRMED DATE | TIME":
        print("[DEBUG] User selected to modify CONFIRMED DATE | TIME")
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row["THREAD ID"]) == str(thread_id):
                proposed = row.get("PROPOSED DATE | TIME", "").strip()
                proposed_dates = [d.strip() for d in proposed.splitlines() if d.strip()]
                if not proposed_dates:
                    await context.bot.send_message(
                        chat_id=query.from_user.id,
                        text="⚠️ No proposed dates available to choose from.",
                    )
                    return ConversationHandler.END

                context.user_data["proposed_dates"] = proposed_dates
                context.user_data["selected_date_indices"] = []

                buttons = []
                for i, d in enumerate(proposed_dates):
                    label = d
                    buttons.append([
                        InlineKeyboardButton(label, callback_data=f"modify_date_selected|{i}")
                    ])

                buttons.append([
                    InlineKeyboardButton(
                        "✅ Confirm Selection", 
                        callback_data="modify_date_selected|CONFIRM"
                    )
                ])

                markup = InlineKeyboardMarkup(buttons)

                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text="📅 Please choose the *confirmed date*, then press ✅ Confirm Selection:",
                    reply_markup=markup,
                    parse_mode="Markdown",
                )

                return ConversationHandler.END
    
    elif field == "STATUS":
        print("[DEBUG] User selected to modify STATUS")
        buttons = [
            [InlineKeyboardButton(
                "❌ Reject Performance", 
                callback_data="modify_status_selected|REJECTED"
            )],
            [InlineKeyboardButton(
                "↩️ Cancel", 
                callback_data="modify_status_selected|CANCEL"
            )]
        ]
        markup = InlineKeyboardMarkup(buttons)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="🚦 Please select the new *STATUS*: ",
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    
    prompt = await context.bot.send_message(
        chat_id=query.from_user.id,
        text=f"✅ Got it! What is the new value for *{field}*?",
        parse_mode="Markdown",
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE

async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        print("[DEBUG] Thread ID not found in sheet")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This thread is not registered in the sheet."
        )
        return ConversationHandler.END

    status = record.get("STATUS", "").strip().upper()
    if status not in {"", "ACCEPTED"}:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This performance is already REJECTED. You cannot modify it."
        )
        return ConversationHandler.END

    allowed_fields: list[str]
    if status == "":
        allowed_fields = ["EVENT", "PROPOSED DATE | TIME", "LOCATION", "PERFORMANCE INFO"]
    else:
        allowed_fields = ["EVENT", "CONFIRMED DATE | TIME", "LOCATION", "PERFORMANCE INFO", "STATUS"]

    if field not in allowed_fields:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⛔ You can’t modify *{field}* while the status is *{status or 'PENDING'}*.",
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
        if field in {"PROPOSED DATE | TIME", "CONFIRMED DATE | TIME"}:
            formatted_dates = parse_and_format_dates(raw_value)
            normalized_value = "\n".join(formatted_dates)
            print(f"[DEBUG] Normalized date value: {normalized_value}")

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

    # Clean up DM prompt messages to keep the conversation tidy
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

    date_text = updated_record.get("CONFIRMED DATE | TIME") or updated_record.get("PROPOSED DATE | TIME") or ""
    summary_preview = build_performance_summary(
        event=updated_record.get("EVENT", ""),
        date_text=date_text,
        location=updated_record.get("LOCATION", ""),
        info=updated_record.get("PERFORMANCE INFO", ""),
    )

    group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
    group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})

    await publish_performance_summary(
        bot=context.bot,
        chat_data_store=group_chat_data,
        sheet=sheet,
        chat_id=CHAT_ID,
        thread_id=thread_id,
        event=updated_record.get("EVENT", ""),
        date_text=date_text,
        location=updated_record.get("LOCATION", ""),
        info=updated_record.get("PERFORMANCE INFO", ""),
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ Updated *{field}* for thread `{thread_id}`.\n\n"
            f"{summary_preview}"
        ),
        parse_mode="Markdown",
    )

    context.user_data.pop("modify_field", None)
    context.user_data.pop("modify_thread_id", None)

    return ConversationHandler.END

async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("|")
    if len(data) != 2:
        await query.answer()
        return

    _, selected = data
    thread_id = context.user_data.get("modify_thread_id")
    proposed_dates = context.user_data.get("proposed_dates", [])
    selected_indices = set(context.user_data.get("selected_date_indices", []))

    if selected == "CONFIRM":
        if not selected_indices:
            await query.answer("Select at least one date before confirming.", show_alert=True)
            return

        await query.answer()

        ordered_indices = sorted(selected_indices, key=lambda x: int(x))
        final_dates = [proposed_dates[int(idx)] for idx in ordered_indices]
        final_value = "\n".join(final_dates)

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
                chat_id=query.from_user.id,
                text="❌ Unable to locate this performance entry in the sheet."
            )
            return

        try:
            sheet.update_cell(row_number, SHEET_COLUMNS.index("CONFIRMED DATE | TIME") + 1, final_value)
        except Exception as exc:
            print(f"[ERROR] Failed to update confirmed date: {exc}")
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=f"❌ Failed to save confirmed dates: `{exc}`",
                parse_mode="Markdown",
            )
            return

        updated_row = sheet.row_values(row_number)
        while len(updated_row) < len(SHEET_COLUMNS):
            updated_row.append("")
        updated_record = dict(zip(SHEET_COLUMNS, updated_row))

        group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
        group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})

        await publish_performance_summary(
            bot=context.bot,
            chat_data_store=group_chat_data,
            sheet=sheet,
            chat_id=CHAT_ID,
            thread_id=thread_id,
            event=updated_record.get("EVENT", ""),
            date_text=final_value,
            location=updated_record.get("LOCATION", ""),
            info=updated_record.get("PERFORMANCE INFO", ""),
        )

        summary_preview = build_performance_summary(
            event=updated_record.get("EVENT", ""),
            date_text=final_value,
            location=updated_record.get("LOCATION", ""),
            info=updated_record.get("PERFORMANCE INFO", ""),
        )

        context.user_data.pop("proposed_dates", None)
        context.user_data.pop("selected_date_indices", None)

        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
        except Exception as exc:
            print(f"[WARNING] Failed to delete date selection keyboard: {exc}")

        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                "✅ Confirmed date/time updated successfully.\n\n"
                f"{summary_preview}"
            ),
            parse_mode="Markdown",
        )

        context.user_data.pop("modify_field", None)
        context.user_data.pop("modify_thread_id", None)

        return

    await query.answer()

    if selected in selected_indices:
        selected_indices.remove(selected)
    else:
        selected_indices.add(selected)
    context.user_data["selected_date_indices"] = selected_indices

    buttons = []
    for i, date_display in enumerate(proposed_dates):
        is_selected = str(i) in selected_indices
        label = f"✅ {date_display}" if is_selected else date_display
        buttons.append([InlineKeyboardButton(label, callback_data=f"modify_date_selected|{i}")])
    buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="modify_date_selected|CONFIRM")])

    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as exc:
        print(f"[WARNING] Could not update date selection buttons: {exc}")

async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

