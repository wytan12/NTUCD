from __future__ import annotations

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS, CHAT_ID
from handlers.conversation_handlers import publish_performance_summary
from services.date_parser import parse_and_format_dates
from services.google_sheets import get_gspread_sheet


def _parse_command_payload(text: str) -> tuple[str, str | None, str]:
    """Extract the command keyword, thread id token, and remaining payload."""
    stripped = text.lstrip()
    if not stripped:
        return "", None, ""

    first_space = stripped.find(" ")
    if first_space == -1:
        return stripped.lower().lstrip("/"), None, ""

    command = stripped[:first_space].lower().lstrip("/")
    remainder = stripped[first_space + 1 :].lstrip()
    if not remainder:
        return command, None, ""

    idx = 0
    while idx < len(remainder) and remainder[idx].isdigit():
        idx += 1

    thread_token = remainder[:idx]
    payload = remainder[idx:].lstrip("\n\r ")
    return command, thread_token or None, payload


async def handle_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow approved users to control the bot via private messages."""

    message = update.effective_message
    if not message or not message.text:
        return

    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_DM_USER_IDS:
        await message.reply_text(
            "⚠️ No DM admins configured. Please update `ADMIN_DM_USER_IDS` in config.py.",
            parse_mode="Markdown",
        )
        return

    if user_id not in ADMIN_DM_USER_IDS:
        await message.reply_text("⛔ You are not authorized to use DM commands.")
        return

    command, thread_token, payload = _parse_command_payload(message.text)
    if command == "help":
        await message.reply_text(
            "Available commands:\n"
            "• `announce <thread_id> <message>` — send announcement to a topic.\n"
            "• `perf <thread_id> <Event // Date // Location // Info>` — create a performance entry.",
            parse_mode="Markdown",
        )
        return

    if command not in {"announce", "perf", "performance"}:
        await message.reply_text("❓ Unknown command. Send `help` for usage.")
        return

    if not thread_token:
        await message.reply_text("⚠️ Please provide a thread ID after the command.")
        return

    if not thread_token.isdigit():
        await message.reply_text("⚠️ Thread ID must be numeric.")
        return

    thread_id = int(thread_token)
    if thread_id <= 0:
        await message.reply_text("⚠️ Thread ID must be positive.")
        return

    if command == "announce":
        if not payload:
            await message.reply_text("⚠️ Please provide an announcement message.")
            return
        try:
            sent = await context.bot.send_message(
                chat_id=CHAT_ID,
                text=payload,
                message_thread_id=thread_id,
            )
        except BadRequest as exc:
            await message.reply_text(f"❌ Failed to send announcement: {exc.message}")
            return
        except Exception as exc:  # pragma: no cover - network
            await message.reply_text(f"❌ Failed to send announcement: {exc}")
            return

        await message.reply_text(
            f"✅ Announcement sent to thread `{thread_id}` (message {sent.message_id}).",
            parse_mode="Markdown",
        )
        return

    # Performance creation flow
    content = payload
    if not content:
        await message.reply_text(
            "⚠️ Please provide performance details using `Event // Date // Location // Info`.",
            parse_mode="Markdown",
        )
        return

    parts = [part.strip() for part in content.split("//")]
    if len(parts) < 3 or any(not part for part in parts[:3]):
        await message.reply_text(
            "❌ Invalid format. Use `Event // Date // Location // Info (optional)`.",
            parse_mode="Markdown",
        )
        return

    event, raw_date, location = parts[0], parts[1], parts[2]
    info = parts[3] if len(parts) >= 4 else ""

    try:
        formatted_dates = parse_and_format_dates(raw_date)
        date_text = "\n".join(formatted_dates)
    except ValueError as exc:
        await message.reply_text(str(exc), parse_mode="Markdown")
        return

    sheet = get_gspread_sheet()
    try:
        sheet.append_row([thread_id, event, date_text, location, info, "", ""])
    except Exception as exc:  # pragma: no cover - network
        await message.reply_text(f"❌ Failed to save to sheet: {exc}")
        return

    group_chat_data_cache = context.application.bot_data.setdefault("group_chat_data", {})
    group_chat_data = group_chat_data_cache.setdefault(CHAT_ID, {})
    try:
        await publish_performance_summary(
            bot=context.bot,
            chat_data_store=group_chat_data,
            sheet=sheet,
            chat_id=CHAT_ID,
            thread_id=thread_id,
            event=event,
            date_text=date_text,
            location=location,
            info=info,
        )
    except Exception as exc:  # pragma: no cover - network
        await message.reply_text(f"❌ Failed to publish summary: {exc}")
        return

    await message.reply_text(
        f"✅ Performance entry created for thread `{thread_id}`.",
        parse_mode="Markdown",
    )