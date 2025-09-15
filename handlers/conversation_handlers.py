from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from datetime import datetime
import asyncio
from telegram.error import BadRequest

from utils.constants import (
    pending_questions, initialized_topics, OTHERS_THREAD_IDS,
    DATE, EVENT, LOCATION, MODIFY_FIELD, MODIFY_VALUE
)
from config import SHEET_COLUMNS, EXEMPTED_THREAD_IDS
from services.google_sheets import get_gspread_sheet, append_to_others_list
from services.date_parser import parse_and_format_dates
from handlers.poll_handlers import send_interest_poll
from utils.decorators import is_admin
from utils.helpers import delete_topic_with_delay

async def topic_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PERF/OTHERS selection"""
    query = update.callback_query
    await query.answer()

    _, selection, thread_id = query.data.split("|")
    thread_id = int(thread_id)

    prompt_id = context.chat_data.pop(f"init_prompt_{thread_id}", None)
    if prompt_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat.id, 
                message_id=prompt_id
            )
        except Exception as e:
            print(f"[DELETE ERROR] {e}")

    context.user_data["thread_id"] = thread_id
    context.user_data["topic_type"] = selection

    if selection == "PERF":
        prompt = await query.message.chat.send_message(
            "\U0001F4DD Please enter: *Event // Date // Location // Info (If any)*",
            parse_mode="Markdown", 
            message_thread_id=thread_id
        )
        pending_questions["perf_input"] = prompt.message_id
        return DATE

    elif selection == "DATE_ONLY":
        prompt = await query.message.chat.send_message(
            "\U0001F4C5 Please enter the *Performance Date* (e.g. 12 MAR 2025):",
            parse_mode="Markdown", 
            message_thread_id=thread_id
        )
        pending_questions["date"] = prompt.message_id
        return DATE
    
    elif selection == "OTHERS":
        append_to_others_list(thread_id)
    
    await query.message.edit_text(f"Topic marked as {selection}. No further action.")
    return ConversationHandler.END

async def parse_perf_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse performance input and save to sheet"""
    await update.message.delete()
    sheet = get_gspread_sheet()

    try:
        if "perf_input" in pending_questions:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=pending_questions.pop("perf_input")
            )
    except Exception as e:
        print(f"[WARNING] Failed to delete perf input prompt: {e}")
    
    try:
        if "last_error" in context.chat_data:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.chat_data.pop("last_error")
            )
    except Exception as e:
        print(f"[WARNING] Failed to delete previous error message: {e}")

    # Case 1: Retrying Date Only
    if "perf_temp" in context.user_data:
        raw_date = update.message.text.strip()
        try:
            formatted_dates = parse_and_format_dates(raw_date)
            date = "\n".join(formatted_dates)
        except ValueError as e:
            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id, 
                            message_id=msg_id
                        )
                    except Exception as ex:
                        print(f"[WARNING] Failed to delete previous {key} message: {ex}")

            error_msg = await update.effective_chat.send_message(
                str(e),
                parse_mode="Markdown",
                message_thread_id=context.user_data["perf_temp"]["thread_id"]
            )
            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
            return DATE

        temp = context.user_data.pop("perf_temp")
        event = temp["event"]
        location = temp["location"]
        info = temp["info"]
        thread_id = temp["thread_id"]

        all_rows = sheet.get_all_values()
        for idx, row in enumerate(all_rows, start=1):
            if row and str(row[0]) == str(thread_id):
                sheet.update(
                    values=[[event, date, location, info, "", ""]], 
                    range_name=f"B{idx}:G{idx}"
                )
                break

    # Case 2: Full Input
    else:
        raw_text = update.message.text.strip()
        parts = [p.strip() for p in raw_text.split("//")]
        print(f"[DEBUG] Parsed parts: {len(parts)} - {parts}")

        thread_id = context.user_data.get("thread_id")

        if len(parts) < 3 or any(not p for p in parts[:3]):
            error_msg = await update.effective_chat.send_message(
                "❌ Invalid input format. Use:\n*Event // Date // Location // Info (optional)*\n\n"
                "Example:\n`NTU Welcome Tea // 23 JUN 2025 8:00pm // NYA // Formal wear required`",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )

            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id, 
                            message_id=msg_id
                        )
                    except Exception as e:
                        print(f"[WARNING] Failed to delete previous {key} message: {e}")

            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
            return DATE

        event, date, location = parts[0], parts[1], parts[2]
        info = parts[3] if len(parts) >= 4 else ""
        thread_id = context.user_data.get("thread_id")

        try:
            raw_date = date
            formatted_dates = parse_and_format_dates(raw_date)
            date = "\n".join(formatted_dates)
        except ValueError as e:
            date = raw_date
            context.user_data["perf_temp"] = {
                "event": event,
                "location": location,
                "info": info,
                "thread_id": thread_id
            }
            try:
                sheet.append_row([thread_id, event, date, location, info, "", ""])
                print("[DEBUG] Row appended with invalid date")
            except Exception as e2:
                print(f"[ERROR] Failed to append row with invalid date: {e}")
 
            for key in ["last_error", "invalid_input"]:
                msg_id = context.chat_data.pop(key, None)
                if msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id, 
                            message_id=msg_id
                        )
                    except Exception as ex:
                        print(f"[WARNING] Failed to delete previous {key} message: {ex}")

            error_msg = await update.effective_chat.send_message(
                str(e),
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
            context.chat_data["last_error"] = error_msg.message_id
            context.chat_data["invalid_input"] = update.message.message_id
            return DATE

        try:
            sheet.append_row([thread_id, event, date, location, info, "", ""])
            print("[DEBUG] Row appended successfully")
        except Exception as e:
            print(f"[ERROR] Failed to append row: {e}")

    # Format and send summary
    date_lines = []
    for entry in date.splitlines(): 
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
        f"\U0001F4E2 *Performance Opportunity*\n\n"
        f"\U0001F4CD *Event*\n"
        f"• {event}\n\n"
        f"\U0001F4C5 *Date | Time*\n"
        f"{formatted_dates}\n\n"
        f"\U0001F4CC *Location*\n"
        f"• {location}\n\n"
        f"*Performance Information:*\n"
        f"{info.strip()}"
    )
    msg = await update.effective_chat.send_message(
        template, 
        parse_mode="Markdown", 
        message_thread_id=thread_id
    )
    await context.bot.pin_chat_message(
        chat_id=update.effective_chat.id, 
        message_id=msg.message_id, 
        disable_notification=True
    )
    context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
    
    poll = await send_interest_poll(
        context.bot, 
        update.effective_chat.id, 
        thread_id, 
        sheet
    )
    context.chat_data[f"interest_poll_msg_{thread_id}"] = poll["message_id"] if poll else None
    print(f"[DEBUG] Saved new poll message ID: {poll['message_id'] if poll else 'None'}")

    return ConversationHandler.END

async def confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /confirmation command"""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg.is_topic_message:
        return await msg.reply_text("⛔ This command must be used inside a topic thread.")

    thread_id = msg.message_thread_id
    if not await is_admin(update, context):
        return await msg.reply_text("⛔ Only admins can use this command.")

    try:
        await msg.delete()
    except:
        pass

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_number, row_data = None, None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2
            row_data = row
            break

    if not row_data:
        return await msg.reply_text("❌ This thread is not registered.")

    if row_data["STATUS"]:
        return await msg.reply_text(
            f"❌ This performance is already marked as `{row_data['STATUS']}`.",
            parse_mode="Markdown"
        )

    keyboard = [
        [InlineKeyboardButton("✅ ACCEPT", callback_data=f"CONFIRM|{thread_id}|ACCEPT")],
        [InlineKeyboardButton("❌ REJECT", callback_data=f"CONFIRM|{thread_id}|REJECT")],
        [InlineKeyboardButton("🚫 CANCEL", callback_data=f"CONFIRM|{thread_id}|CANCEL")]
    ]
    prompt = await chat.send_message(
        "🎯 Is this performance *ACCEPTED* or *REJECTED*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id
    )
    context.chat_data[f"confirm_prompt_{thread_id}"] = prompt.message_id

async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation button callbacks"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) != 3:
        return
    _, thread_id, action = parts
    thread_id = int(thread_id)

    msg_id = context.chat_data.pop(f"confirm_prompt_{thread_id}", None)
    if msg_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat.id, 
                message_id=msg_id
            )
        except:
            pass

    sheet = get_gspread_sheet()
    records = sheet.get_all_records()
    row_number, row_data = None, None
    for idx, row in enumerate(records):
        if str(row["THREAD ID"]) == str(thread_id):
            row_number = idx + 2
            row_data = row
            break
    if not row_data:
        return

    if action == "CANCEL":
        return

    if action == "REJECT":
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        for idx, row in enumerate(records):
            if str(row["THREAD ID"]) == str(thread_id):
                row_index = idx + 2
                sheet.update_cell(row_index, 7, "REJECTED")
                break

        await query.message.chat.send_message(
            "❌ Performance rejected. This topic will now be closed.", 
            message_thread_id=thread_id
        )
        await delete_topic_with_delay(
            context, 
            chat_id=query.message.chat.id, 
            thread_id=thread_id
        )
    
    if action == "ACCEPT":
        all_dates = row_data["PROPOSED DATE | TIME"].splitlines()
        context.chat_data[f"final_row_number_{thread_id}"] = row_number
        context.chat_data[f"final_all_dates_{thread_id}"] = all_dates
        context.chat_data[f"selected_dates_{thread_id}"] = []

        buttons = [[InlineKeyboardButton(
            text=d, 
            callback_data=f"FINALDATE|{thread_id}|{i}"
        )] for i, d in enumerate(all_dates)]
        buttons.append([InlineKeyboardButton(
            "✅ Confirm Selection", 
            callback_data=f"FINALDATE|{thread_id}|CONFIRM"
        )])
        prompt = await query.message.chat.send_message(
            "🗓️ Select final date(s) to confirm:",
            reply_markup=InlineKeyboardMarkup(buttons),
            message_thread_id=thread_id
        )
        context.chat_data[f"final_prompt_{thread_id}"] = prompt.message_id

async def final_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle final date selection for accepted performances"""
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    if len(data) != 3:
        return

    _, thread_id, selection = data
    thread_id = int(thread_id)

    row_number = context.chat_data.get(f"final_row_number_{thread_id}")
    all_dates = context.chat_data.get(f"final_all_dates_{thread_id}", [])
    selected = context.chat_data.setdefault(f"selected_dates_{thread_id}", [])

    if selection == "CONFIRM":
        if not selected:
            return await query.message.reply_text(
                "⚠️ Please select at least one date before confirming."
            )

        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id, 
                message_id=query.message.message_id
            )
        except Exception as e:
            print(f"[WARNING] Failed to delete final date selection message: {e}")

        try:
            msg_id = context.chat_data.pop("confirm_prompt_msg_id", None)
            if msg_id:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id, 
                    message_id=msg_id
                )
        except Exception as e:
            print(f"[WARNING] Failed to delete ACCEPT/REJECT message: {e}")

        value = "\n".join([all_dates[i] for i in sorted(map(int, selected))])
        sheet = get_gspread_sheet()
        sheet.update_cell(row_number, 6, value)
        sheet.update_cell(row_number, 7, "ACCEPTED")

        updated_row = sheet.row_values(row_number)
        while len(updated_row) < 7:
            updated_row += [""] * (7 - len(updated_row))

        event, proposed, location, info, confirmed, status = updated_row[1:7]

        date_lines = []
        for line in confirmed.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                dt = datetime.strptime(line, "%d %b %Y | %I:%M%p")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
            except:
                formatted = f"• {line}"
            date_lines.append(formatted)

        template = (
            f"📢 *Performance Summary*\n\n"
            f"📍 *Event*\n"
            f"• {event}\n\n"
            f"📅 *Confirmed Date | Time*\n"
            f"{chr(10).join(date_lines)}\n\n"
            f"📌 *Location*\n"
            f"• {location}\n\n"
            f"📝 *Performance Information:*\n"
            f"{info.strip()}"
        )

        try:
            old_summary_id = context.chat_data.pop(f"summary_msg_{thread_id}", None)
            if old_summary_id:
                await context.bot.unpin_chat_message(
                    chat_id=query.message.chat.id, 
                    message_id=old_summary_id
                )
        except Exception as e:
            print(f"[WARNING] Failed to unpin previous summary: {e}")

        msg = await query.message.chat.send_message(
            template, 
            parse_mode="Markdown", 
            message_thread_id=thread_id
        )
        await context.bot.pin_chat_message(
            chat_id=query.message.chat.id, 
            message_id=msg.message_id, 
            disable_notification=True
        )
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
        return

    # Handle toggling checkboxes
    if selection not in selected:
        selected.append(selection)
    else:
        selected.remove(selection)

    new_buttons = []
    for i, d in enumerate(all_dates):
        is_selected = str(i) in selected
        label = f"✅ {d}" if is_selected else d
        new_buttons.append([InlineKeyboardButton(
            text=label, 
            callback_data=f"FINALDATE|{thread_id}|{i}"
        )])
    new_buttons.append([InlineKeyboardButton(
        "✅ Confirm Selection", 
        callback_data=f"FINALDATE|{thread_id}|CONFIRM"
    )])

    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation handler"""
    print("[DEBUG] Cancel triggered")
    chat = update.effective_chat

    try:
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                await chat.delete_message(msg_id)
            context.chat_data.pop("modify_prompt_msg_ids")
    except Exception as e:
        print(f"[WARNING] Failed to delete prompt messages on cancel: {e}")

    try:
        await update.message.delete()
    except:
        pass

    return ConversationHandler.END