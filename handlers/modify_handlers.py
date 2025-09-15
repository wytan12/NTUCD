from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
import asyncio
from datetime import datetime

from utils.constants import MODIFY_FIELD, MODIFY_VALUE
from utils.decorators import is_admin
from config import SHEET_COLUMNS, EXEMPTED_THREAD_IDS
from services.google_sheets import get_gspread_sheet
from services.date_parser import parse_and_format_dates
from handlers.poll_handlers import send_interest_poll
from utils.helpers import delete_topic_with_delay

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
    
    if thread_id is None:
        return await msg.reply_text("⛔ This command must be used inside a topic thread.")

    context.user_data["modify_thread_id"] = thread_id
    
    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row = next((r for r in records if str(r["THREAD ID"]) == str(thread_id)), None)

    if not row:
        await msg.reply_text(
            "❌ This thread is not registered in the sheet.", 
            message_thread_id=thread_id
        )
        return

    status = row.get("STATUS", "").strip().upper()
    print(f"[DEBUG] Status for thread {thread_id}: {status}")

    if status == "REJECTED":
        await msg.reply_text(
            "❌ This performance is already REJECTED. You cannot modify it.", 
            message_thread_id=thread_id
        )
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
        emoji = emoji_map.get(opt, "")
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {opt.title()}", 
            callback_data=f"MODIFY|{opt}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="MODIFY|CANCEL")])

    prompt = await msg.chat.send_message(
        "✏️ What would you like to update?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_FIELD

async def get_modify_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection for modification"""
    query = update.callback_query
    await query.answer()

    _, field = query.data.split("|", 1)
    context.user_data["modify_field"] = field

    try:
        await context.bot.delete_message(
            chat_id=query.message.chat.id, 
            message_id=query.message.message_id
        )
    except:
        pass

    if field == "CANCEL":
        print("[DEBUG] User selected cancel button")
        return ConversationHandler.END
    
    context.user_data["modify_field"] = field
    
    if field == "CONFIRMED DATE | TIME":
        print("[DEBUG] User selected to modify CONFIRMED DATE | TIME")
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row["THREAD ID"]) == str(context.user_data["modify_thread_id"]):
                proposed = row.get("PROPOSED DATE | TIME", "").strip()
                proposed_dates = [d.strip() for d in proposed.splitlines() if d.strip()]
                if not proposed_dates:
                    await query.message.chat.send_message(
                        "⚠️ No proposed dates available to choose from.",
                        message_thread_id=context.user_data["modify_thread_id"]
                    )
                    return ConversationHandler.END

                context.user_data["proposed_dates"] = proposed_dates
                context.user_data["selected_date_indices"] = []
                thread_id = context.user_data["modify_thread_id"]

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

                await query.message.chat.send_message(
                    "📅 Please choose the *confirmed date*, then press ✅ Confirm Selection:",
                    reply_markup=markup,
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )

                return ConversationHandler.END
    
    elif field == "STATUS":
        print("[DEBUG] User selected to modify STATUS")
        thread_id = context.user_data["modify_thread_id"]
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
        await query.message.chat.send_message(
            "🚦 Please select the new *STATUS*: ",
            reply_markup=markup,
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        return ConversationHandler.END
    
    prompt = await query.message.chat.send_message(
        f"✅ Got it! What is the new value for *{field}*?",
        parse_mode="Markdown",
        message_thread_id=context.user_data["modify_thread_id"]
    )
    context.chat_data["modify_prompt_msg_ids"] = [prompt.message_id]
    return MODIFY_VALUE

async def apply_modify_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if value.lower() == "/cancel":
        try:
            await update.message.delete()
        except:
            pass
        return ConversationHandler.END

    field = context.user_data["modify_field"]
    thread_id = context.user_data["modify_thread_id"]
    print(f"[DEBUG] Applying new value: {value} to field: {field} for thread ID: {thread_id}")

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()

    # === Step 1: Find the row number ===
    row_number = None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2  # Header row + 1-based index
            break

    if row_number is None:
        print("[DEBUG] Thread ID not found in sheet")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ This thread is not registered in the sheet."
        )
        return ConversationHandler.END
    
    status = row.get("STATUS", "").strip().upper()
    allowed_fields = []

    if status == "":
        allowed_fields = ["EVENT", "PROPOSED DATE | TIME", "LOCATION", "PERFORMANCE INFO"]
    elif status == "ACCEPTED":
        allowed_fields = ["EVENT", "CONFIRMED DATE | TIME", "LOCATION", "PERFORMANCE INFO", "STATUS"]
    else:
        await update.effective_chat.send_message("❌ This performance is already REJECTED. You cannot modify it.", message_thread_id=thread_id)
        return ConversationHandler.END

    if field not in allowed_fields:
        await update.effective_chat.send_message(f"⛔ You can’t modify *{field}* in the current status (*{status}*).", parse_mode="Markdown", message_thread_id=thread_id)
        return ConversationHandler.END

    try:
        if field in ["PROPOSED DATE | TIME", "CONFIRMED DATE | TIME"]:
            try:
                formatted = parse_and_format_dates(value)
                value = ", ".join(formatted)
                print(f"[DEBUG] Reformatted date value: {value}")
            except ValueError as e:
                error_msg = await update.effective_chat.send_message(
                    str(e),
                    parse_mode="Markdown",
                    message_thread_id=context.user_data["modify_thread_id"]
                )
                context.chat_data["modify_error_msg_id"] = error_msg.message_id
                context.chat_data["invalid_input_msg_id"] = update.message.message_id
                
                return MODIFY_VALUE
            
        for key in ["modify_error_msg_id", "invalid_input_msg_id"]:
            msg_id = context.chat_data.pop(key, None)
            if msg_id:
                try:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except Exception as ex:
                    print(f"[WARNING] Failed to delete previous {key} message: {ex}")

        # === Step 3: Delete the new input message ===
        try:
            await update.message.delete()
        except:
            pass

        # === Step 4: Update the sheet ===
        col_index = SHEET_COLUMNS.index(field) + 1
        if field == "PROPOSED DATE | TIME":
            value = "\n".join(value.split(", "))
            print(f"[DEBUG] Rewritten date value with newline: {repr(value)}")

        range_name = f"{chr(64 + col_index)}{row_number}"  # e.g., C5
        sheet.update(range_name=range_name, values=[[value]])
        print(f"[DEBUG] Sheet updated at row {row_number}, column {col_index}")

        # === Step 5: Clean up prompt messages ===
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                try:
                    await asyncio.sleep(0.3)
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                except BadRequest as e:
                    print(f"[INFO] Prompt message {msg_id} already deleted or not found: {e}")
            context.chat_data.pop("modify_prompt_msg_ids")

        # === Step 6: Delete previous summary ===
        prev_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
        if prev_msg_id:
            await asyncio.sleep(0.3)
            try:
                await context.bot.unpin_chat_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
                print(f"[DEBUG] Deleted previous summary message: {prev_msg_id}")
            except Exception as e:
                print(f"[WARNING] Could not delete previous summary: {e}")

        # === Step 7: Prepare and send updated summary ===
        updated_row = sheet.row_values(row_number)
        while len(updated_row) < 5:
            updated_row.append("")
        status = updated_row[6] if len(updated_row) > 6 else ""
        summary_title = "Performance Summary" if status else "Performance Opportunity"

        date_lines = []
        for entry in updated_row[2].splitlines():
            entry = entry.strip()
            if not entry:
                continue
            try:
                dt = datetime.strptime(entry, "%d %b %Y %H%M")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                date_lines.append(formatted)
            except:
                date_lines.append(f"• {entry}")
        formatted_dates = "\n".join(date_lines)

        template = (
            f"📢 *{summary_title}*\n\n"
            f"📍 *Event*\n"
            f"• {updated_row[1]}\n\n"
            f"📅 *Date | Time*\n"
            f"{formatted_dates}\n\n"
            f"📌 *Location*\n"
            f"• {updated_row[3]}\n\n"
            f"📌 *Performance Information:*\n"
            f"{updated_row[4].strip()}"
        )

        msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
        print(f"[DEBUG] Pinned updated summary for thread {thread_id}")

        # === Step 8: Resend poll if date changed ===
        if field == "PROPOSED DATE | TIME":
            try:
                old_poll_msg_id = context.chat_data.get(f"interest_poll_msg_{thread_id}")
                if old_poll_msg_id:
                    try:
                        await asyncio.sleep(0.3)
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_poll_msg_id)
                        print(f"[DEBUG] Deleted previous interest poll: {old_poll_msg_id}")
                    except Exception as e:
                        print(f"[WARNING] Failed to delete old poll message {old_poll_msg_id}: {e}")

                poll_msg = await send_interest_poll(
                    bot=context.bot,
                    chat_id=update.effective_chat.id,
                    thread_id=thread_id,
                    sheet=sheet
                )

                if poll_msg:
                    context.chat_data[f"interest_poll_msg_{thread_id}"] = poll_msg["message_id"]
                    print(f"[DEBUG] Saved new poll message ID: {poll_msg['message_id']}")
            except Exception as e:
                print(f"[WARNING] Failed to send interest poll: {e}")

        return ConversationHandler.END

    except Exception as e:
        print(f"[ERROR] Failed to update sheet: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Failed to update: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    
async def handle_modify_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, selected = query.data.split("|")
    thread_id = context.user_data.get("modify_thread_id")
    proposed_dates = context.user_data.get("proposed_dates", [])

    selected_indices = set(context.user_data.get("selected_date_indices", []))

    if selected == "CONFIRM":
        if not selected_indices:
            await query.message.reply_text(
                "⚠️ Please select at least one date before confirming.",
                message_thread_id=thread_id
            )
            return

        # ✅ Always follow original order from GSheet, not click order
        final_dates = []
        for i, raw in enumerate(proposed_dates):
            if str(i) not in selected_indices:
                continue

            raw = raw.strip()
            parsed = False
            for fmt in ("%d %b %Y | %I:%M%p", "%d %b %Y %H%M", "%d %b %Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    formatted = f"{dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                    final_dates.append(formatted)
                    parsed = True
                    break
                except Exception as e:
                    continue
            if not parsed:
                print(f"[WARNING] Failed to parse date '{raw}', storing as-is")
                final_dates.append(raw.upper())

        final_value = "\n".join(final_dates)

        # === Write to GSheet ===
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        row = None
        row_index = None
        for idx, r in enumerate(records):
            if str(r["THREAD ID"]) == str(thread_id):
                row = r
                row_index = idx + 2
                sheet.update_cell(row_index, 6, final_value)  # CONFIRMED DATE | TIME
                break

        # === Delete previous summary
        prev_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
        if prev_msg_id:
            try:
                await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=prev_msg_id)
                await context.bot.delete_message(chat_id=query.message.chat.id, message_id=prev_msg_id)
            except Exception as e:
                print(f"[WARNING] Failed to delete previous summary: {e}")

        # === Print new summary
        formatted_lines = [f"• {d}" for d in final_dates]
        template = (
            f"📢 *Performance Summary*\n\n"
            f"📍 *Event*\n• {row['EVENT']}\n\n"
            f"📅 *Date | Time*\n" +
            "\n".join(formatted_lines) + "\n\n"
            f"📌 *Location*\n• {row['LOCATION']}\n\n"
            f"📌 *Performance Information:*\n{row['PERFORMANCE INFO'].strip()}"
        )
        msg = await query.message.chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=query.message.chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id

        # Cleanup
        context.user_data.pop("proposed_dates", None)
        context.user_data.pop("selected_date_indices", None)
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
        except:
            pass
        return

    # === Toggle selection
    if selected in selected_indices:
        selected_indices.remove(selected)
    else:
        selected_indices.add(selected)
    context.user_data["selected_date_indices"] = selected_indices

    # === Redraw button UI
    buttons = []
    for i, d in enumerate(proposed_dates):
        label = f"✅ {d}" if str(i) in selected_indices else d
        buttons.append([InlineKeyboardButton(label, callback_data=f"modify_date_selected|{i}")])
    buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="modify_date_selected|CONFIRM")])
    markup = InlineKeyboardMarkup(buttons)

    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        print(f"[WARNING] Could not update buttons: {e}")

async def handle_modify_status_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, selection = query.data.split("|")
    thread_id = context.user_data.get("modify_thread_id")

    if selection == "CANCEL":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except:
            pass
        return

    if selection == "REJECTED":
        print(f"[DEBUG] Admin rejected performance in thread {thread_id}")
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            if str(row["THREAD ID"]) == str(thread_id):
                row_index = idx + 2
                sheet.update_cell(row_index, 7, "REJECTED")  # STATUS column
                break

        # Print cancellation notice
        try:
            await query.message.chat.send_message(
                "🚫 This performance has been *cancelled*. The topic will be deleted shortly.",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
        except:
            pass

        # Delay then delete topic
        await delete_topic_with_delay(context, chat_id=query.message.chat.id, thread_id=thread_id)
        return