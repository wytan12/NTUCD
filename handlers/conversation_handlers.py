from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from datetime import datetime
import asyncio
from telegram.error import BadRequest

from utils.constants import (
    pending_questions, initialized_topics, OTHERS_THREAD_IDS,
    DATE, EVENT, LOCATION
)
from config import SHEET_COLUMNS, CHAT_ID
from services.google_sheets import get_gspread_sheet, append_to_others_list, invalidate_sheet_cache
from services.date_parser import parse_and_format_dates
from utils.decorators import is_admin


async def topic_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic-type selection (PERF / DATE_ONLY / OTHERS) from inline buttons.

    Deletes the prompt message that triggered the callback, stores thread_id
    and topic_type in user_data, then either transitions to DATE conversation
    state (PERF/DATE_ONLY) or appends to the OTHERS list and ends.
    """
    query = update.callback_query
    await query.answer()

    _, selection, thread_id = query.data.split("|")
    thread_id = int(thread_id)

    prompt_store = context.application.bot_data.setdefault("topic_prompts", {})
    prompt_entry = prompt_store.pop((update.effective_user.id, thread_id), None)
    if prompt_entry:
        target_chat_id, prompt_message_id = prompt_entry
        try:
            await context.bot.delete_message(
                chat_id=target_chat_id,
                message_id=prompt_message_id,
            )
        except Exception as e:
            print(f"[DELETE ERROR] {e}")

    context.user_data["thread_id"] = thread_id
    context.user_data["topic_type"] = selection

    send_kwargs = {
        "chat_id": query.message.chat.id,
        "parse_mode": "Markdown",
    }
    if getattr(query.message, "is_topic_message", False) and query.message.chat.type in {"group", "supergroup"}:
        send_kwargs["message_thread_id"] = thread_id

    if selection == "PERF":
        prompt = await context.bot.send_message(
            text=(
                "\U0001F4DD Please enter details for thread ``{thread_id}``:\n"
                "*Event Name // Rehearsal Date // Perf Date // Location // Other Info (optional)*"
            ).format(thread_id=thread_id),
            **send_kwargs,
        )
        pending_questions["perf_input"] = prompt.message_id
        return DATE

    elif selection == "OTHERS":
        append_to_others_list(thread_id)
        
        # 🛠️ DYNAMIC RAM SYNCHRONIZATION: Insert this exact line right here!
        initialized_topics.add(thread_id)

    await query.message.edit_text(f"Topic marked as {selection}. No further action.")
    return ConversationHandler.END

async def parse_perf_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse the free-text performance details and write a new row to the sheet.

    Accepts input in the format:
      Event Name // Rehearsal Date // Perf Date // Location // Other Info (optional)
    Use '-' for Rehearsal Date if there is no rehearsal.
    On success publishes the pinned performance summary and interest poll to
    the topic thread.
    """
    if update.effective_chat.type in {"group", "supergroup"}:
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

    raw_text = update.message.text.strip()
    parts = [p.strip() for p in raw_text.split("//")]
    print(f"[DEBUG] Parsed parts: {len(parts)} - {parts}")

    thread_id = context.user_data.get("thread_id")

    if len(parts) < 4 or any(not p for p in parts[:4]):
        error_msg = await update.effective_chat.send_message(
            "❌ Invalid input format. Use:\n"
            "*Event Name // Rehearsal Date // Perf Date // Location // Other Info (optional)*\n\n"
            "Example:\n"
            "`NTU Welcome Tea // 22aug25 6pm // 23aug25 8pm // NYA // Formal wear required`\n"
            "Use `-` for Rehearsal Date if there is no rehearsal.",
            parse_mode="Markdown",
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

    event_name, rehearsal_raw, perf_raw, location = parts[0], parts[1], parts[2], parts[3]
    other_info = parts[4] if len(parts) >= 5 else ""

    # Validate and normalise date fields (skip rehearsal if it is a placeholder)
    rehearsal_date = rehearsal_raw
    perf_date = perf_raw
    try:
        if rehearsal_raw.strip() not in ("-", ""):
            rehearsal_date = "\n".join(parse_and_format_dates(rehearsal_raw))
        perf_date = "\n".join(parse_and_format_dates(perf_raw))
    except ValueError as e:
        error_msg = await update.effective_chat.send_message(str(e), parse_mode="Markdown")
        context.chat_data["last_error"] = error_msg.message_id
        context.chat_data["invalid_input"] = update.message.message_id
        return DATE

    try:
        sheet.append_row([thread_id, "", event_name, rehearsal_date, perf_date, location, other_info, "", ""])
        invalidate_sheet_cache()
        print("[DEBUG] Row appended successfully")
    except Exception as e:
        print(f"[ERROR] Failed to append row: {e}")

    group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
    group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})

    await publish_performance_summary(
        bot=context.bot,
        chat_data_store=group_chat_data,
        sheet=sheet,
        chat_id=CHAT_ID,
        thread_id=thread_id,
        event_name=event_name,
        rehearsal_date=rehearsal_date,
        perf_date=perf_date,
        location=location,
        other_info=other_info,
    )

    summary_preview = build_performance_summary(
        event_name=event_name,
        rehearsal_date=rehearsal_date,
        perf_date=perf_date,
        location=location,
        other_info=other_info,
    )
    await update.effective_chat.send_message(
        f"✅ Performance entry created for thread `{thread_id}`.\n\n{summary_preview}",
        parse_mode="Markdown",
    )

    return ConversationHandler.END

async def final_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stub — date selection is no longer part of the confirmation flow.

    Kept registered so stale FINALDATE callbacks don't raise unhandled errors.
    """
    query = update.callback_query
    await query.answer("This action is no longer available.", show_alert=True)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the active conversation, clean up pending prompt messages,

    and return safely to the primary Cockpit window.
    """
    chat = update.effective_chat
    query = update.callback_query

    # --- 1. Keep your original message history cleanup logic completely intact ---
    try:
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                await chat.delete_message(msg_id)
            context.chat_data.pop("modify_prompt_msg_ids")
    except Exception as e:
        print(f"[WARNING] Failed to delete prompt messages on cancel: {e}")

    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    # --- 2. ADD: Re-render the dashboard home layout instead of leaving it dead ---
    try:
        from handlers.private_handlers import DASHBOARD_TEXT, _dashboard_keyboard
        
        if query:
            # If they clicked an "Exit to Cockpit" button, edit the message in place
            await query.answer()
            await query.edit_message_text(DASHBOARD_TEXT, reply_markup=_dashboard_keyboard(), parse_mode="Markdown")
        else:
            # If they typed a cancellation command, update the current active dashboard bubble
            active_dash_id = context.user_data.get("master_dash_id")
            if active_dash_id:
                await context.bot.edit_message_text(
                    chat_id=chat.id,
                    message_id=active_dash_id,
                    text=DASHBOARD_TEXT,
                    reply_markup=_dashboard_keyboard(),
                    parse_mode="Markdown"
                )
    except Exception as e:
        print(f"[WARNING] Failed to restore dashboard home view on cancel: {e}")

    return ConversationHandler.END


def build_performance_summary(
    event_name: str,
    rehearsal_date: str,
    perf_date: str,
    location: str,
    other_info: str,
) -> str:
    """Return the formatted pinned performance summary message.

    Rehearsal date is omitted from display when it is '-' or blank.
    """
    lines = ["📢 *Performance Opportunity*\n"]

    lines.append(f"📍 *Event Name*: {event_name.strip()}\n")

    rehearsal = rehearsal_date.strip() if rehearsal_date else ""
    if rehearsal and rehearsal != "-":
        r_bullets = "\n".join(f"• {l.strip()}" for l in rehearsal.splitlines() if l.strip())
        lines.append(f"🔁 *Rehearsal Date | Time*\n{r_bullets}\n")

    perf = perf_date.strip() if perf_date else "TBD"
    if perf == "TBD":
        lines.append("📅 *Performance Date | Time*\n• TBD\n")
    else:
        p_bullets = "\n".join(f"• {l.strip()}" for l in perf.splitlines() if l.strip())
        lines.append(f"📅 *Performance Date | Time*\n{p_bullets}\n")

    lines.append(f"📌 *Location*\n• {location.strip()}\n")

    info = other_info.strip() if other_info else ""
    lines.append(f"📝 *Other Info*\n{info if info else '-'}")

    return "\n".join(lines)

async def publish_performance_summary(
    bot,
    chat_data_store: dict,
    sheet,
    chat_id: int,
    thread_id: int,
    event_name: str,
    rehearsal_date: str,
    perf_date: str,
    location: str,
    other_info: str,
):
    """Post or refresh the pinned summary using persistent Google Sheet tracking."""
    from config import SHEET_COLUMNS
    
    # 1. Look up the row matching this thread ID
    records = sheet.get_all_records()
    row_number = None
    old_msg_id = None
    
    for idx, row in enumerate(records, start=2):
        if str(row.get("THREAD ID")) == str(thread_id):
            row_number = idx
            old_msg_id = row.get("SUMMARY MSG ID")
            break

    # 2. Hard Target: If an old summary ID exists in the sheet, destroy it immediately
    if old_msg_id and str(old_msg_id).isdigit():
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=int(old_msg_id))
        except BadRequest:
            pass
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old_msg_id))
            print(f"[INFO] Cleaned up old summary message ID: {old_msg_id} from thread {thread_id}")
        except BadRequest:
            pass

    # 3. Create and send the fresh summary text banner
    template = build_performance_summary(
        event_name=event_name,
        rehearsal_date=rehearsal_date,
        perf_date=perf_date,
        location=location,
        other_info=other_info,
    )

    new_message = await bot.send_message(
        chat_id=chat_id,
        text=template,
        parse_mode="Markdown",
        message_thread_id=thread_id,
    )

    # 4. Pin the new summary message
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=new_message.message_id,
            disable_notification=True,
        )
    except BadRequest as exc:
        print(f"[WARNING] Failed to pin new summary: {exc}")

    # 5. Save the new message ID directly to the Google Sheet for future lookups
    if row_number:
        try:
            msg_col_idx = SHEET_COLUMNS.index("SUMMARY MSG ID") + 1
            sheet.update_cell(row_number, msg_col_idx, str(new_message.message_id))
            print(f"[GSHEET] Saved new summary message ID {new_message.message_id} to row {row_number}")
        except Exception as e:
            print(f"[ERROR] Failed to save message ID to Google Sheets: {e}")

    return new_message, None
