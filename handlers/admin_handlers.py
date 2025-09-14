from telegram import Update
from telegram.ext import ContextTypes
from utils.decorators import admin_only
from services.google_sheets import get_gspread_sheet, get_attendance_ws
from config import sg_tz
from datetime import datetime

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat = update.effective_chat
    thread_id = getattr(update.effective_message, "message_thread_id", "N/A")
    await update.message.reply_text("👋 Hi!")
    print(f"[DEBUG] Chat ID: {chat.id}, Thread ID: {thread_id}")

@admin_only
async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /threadid command"""
    msg = update.effective_message
    if msg.is_topic_message:
        await msg.reply_text(
            f"\U0001F9F5 This topic's thread ID is: {msg.message_thread_id}", 
            parse_mode="Markdown"
        )
    else:
        await msg.reply_text(
            "\u2757 This command must be used *inside a topic*.", 
            parse_mode="Markdown"
        )
    try:
        await update.message.delete()
    except Exception as e:
        print(f"[DEBUG] Failed to delete /threadid command: {e}")

@admin_only
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remind command"""
    from config import EXEMPTED_THREAD_IDS
    
    msg = update.effective_message
    chat_id = msg.chat_id
    thread_id = msg.message_thread_id

    print("[DEBUG] /remind triggered.")
    print(f"[DEBUG] Thread ID: {thread_id}")

    try:
        await msg.delete()
        print("[DEBUG] Deleted /remind command message.")
    except Exception as e:
        print(f"[WARNING] Failed to delete /remind command message: {e}")

    if not msg.is_topic_message:
        print("[DEBUG] Not a topic message. Ignoring.")
        return

    if thread_id in EXEMPTED_THREAD_IDS:
        print("[DEBUG] Thread is exempted. Skipping.")
        return

    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()

        for row in records:
            if str(row["THREAD ID"]) == str(thread_id):
                status = row.get("STATUS", "").strip().upper()
                if status != "ACCEPTED":
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Reminder can only be used *after confirmation*.",
                        parse_mode="Markdown",
                        message_thread_id=thread_id
                    )
                    print("[DEBUG] Status not ACCEPTED. Reminder skipped.")
                    return

                date_str = row.get("CONFIRMED DATE | TIME", "").strip()
                date_lines = "\n".join([f"• {d.strip()}" for d in date_str.splitlines() if d.strip()])
                template = (
                    f"📢 *Performance Reminder*\n\n"
                    f"📍 *Event*\n• {row['EVENT']}\n\n"
                    f"📅 *Date | Time*\n{date_lines}\n\n"
                    f"📌 *Location*\n• {row['LOCATION']}\n\n"
                    f"*📝 Final Preparation Notes*\n\n"
                    f"*👀 Glasses & Contact Lens*\n"
                    f"If you wear glasses, try your best to perform without them (e.g. wear contact lens). "
                    f"Default is *no glasses* on stage. Make sure you're comfortable before show day.\n\n"
                    f"*⬇️💪 Shave Your Armpits*\n"
                    f"We want the audience to focus on our performance, not our underarms 🪒😌 "
                    f"So please make sure to shave before the show!\n\n"
                    f"*🎽 Costume Tips*\n"
                    f"Our costumes are sleeveless and v-neck. Avoid wearing bright-colored bras "
                    f"(neon pink/yellow/rainbow 🌈). A black sports bra is best.\n\n"
                    f"*🦶 Barefoot Reminder*\n"
                    f"Everyone will be performing *barefoot*. Don't forget!\n\n"
                    f"*💇 Hair Tying*\n"
                    f"If you have long hair, please tie it up neatly. "
                    f"You can also ask someone to help if needed.\n\n"
                    f"*📺 Recap the Drum Score*\n"
                    f"Make sure to go through the performance videos again and recap the score "
                    f"before the show. Stay sharp!"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=template,
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )
                print("[DEBUG] Reminder message sent.")
                return

        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ This thread has not been registered.",
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        print("[DEBUG] Thread ID not found in GSheet.")

    except Exception as e:
        print(f"[ERROR] Failed to execute /remind: {e}")

async def send_reminder(bot, chat_id, thread_id):
    """Send training reminder"""
    from utils.helpers import get_next_tuesday
    
    try:
        ws = get_attendance_ws()
        from services.google_sheets import _find_or_create_header_rows
        _find_or_create_header_rows(ws)

        poll_row = ws.row_values(1)
        date_row = ws.row_values(2)

        max_len = max(len(poll_row), len(date_row))
        if len(poll_row) < max_len:
            poll_row += [""] * (max_len - len(poll_row))
        if len(date_row) < max_len:
            date_row += [""] * (max_len - len(date_row))

        last_col = None
        for idx in range(max_len, 0, -1):
            if idx == 1:
                if (poll_row[0] or "").strip().isdigit():
                    last_col = 1
                break
            if (poll_row[idx - 1] or "").strip():
                last_col = idx
                break

        if last_col and last_col > 1:
            date_label_short = (date_row[last_col - 1] or "").strip()
            pretty_date = date_label_short
            try:
                d, m = [int(x) for x in date_label_short.split("/")]
                today = datetime.now(sg_tz)
                year = today.year + (1 if today.month > m else 0)
                dt = datetime(year, m, d, tzinfo=sg_tz)
                pretty_date = dt.strftime("%d %b %Y")
            except Exception:
                pass
        else:
            pretty_date = get_next_tuesday().strftime("%d %b %Y")

        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"Reminder: There's training tomorrow {pretty_date}."
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")