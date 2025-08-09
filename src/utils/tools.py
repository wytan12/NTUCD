import re
from datetime import date, timedelta, datetime, time
import pytz

sg_tz = pytz.timezone("Asia/Singapore")


def get_next_tuesday(today=None):
    if today is None:
        today = date.today()
    days_ahead = (1 - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)

def get_next_monday_8pm(now=None):
    if now is None:
        now = datetime.now(sg_tz)
    days_until_monday = (7 - now.weekday()) % 7
    next_monday = now + timedelta(days=days_until_monday)
    naive_dt = datetime.combine(next_monday, time(22, 0))
    this_monday_8pm = sg_tz.localize(naive_dt)
    if now >= this_monday_8pm:
        return this_monday_8pm + timedelta(days=7)
    return this_monday_8pm

def parse_flexible_date(date_str: str) -> str:
    original = date_str.strip()
    lower = original.lower()

    # Normalize dot-separated times (e.g., 2.30 → 2:30)
    lower = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', lower)

    # Normalize 4-digit shorthand times (e.g., 1430 → 14:30)
    lower = re.sub(r'\b(\d{4})\b', lambda m: f"{m.group(1)[:2]}:{m.group(1)[2:]}", lower)

    # Normalize 3-4 digit shorthand times like 830am → 8:30 am
    lower = re.sub(r'\b(\d{1,2})(\d{2})(am|pm)\b', r'\1:\2 \3', lower)

    # Normalize AM/PM time (e.g., 2pm → 2:00 pm, 2:30pm → 2:30 pm)
    lower = re.sub(r'(\d{1,2}:\d{2})(am|pm)\b', r'\1 \2', lower)
    lower = re.sub(r'\b(\d{1,2})(am|pm)\b', r'\1:00 \2', lower)

    # Match formats like: 24jun25 2:30pm, 24june2025, 24jun 2pm, 24june26
    pattern = re.match(r"(\d{1,2})[\s-]?([a-zA-Z]{3,9})[\s-]?(\d{2,4})?(?:\s+(\d{1,2}:\d{2}(?:\s?(?:am|pm))?))?$", lower)
    if not pattern:
        raise ValueError(f"❌ Invalid date format: '{original}'\n👉 Use formats like '24jun25' or '24jun25 2:30pm'.")

    day, month, year, time_part = pattern.groups()

    # Default year to current year if not given
    if not year:
        year = str(datetime.now().year)
    elif len(year) == 2:
        year = "20" + year

    # Build datetime string
    date_time_str = f"{day} {month} {year}"
    if time_part:
        date_time_str += f" {time_part}"

        # Try valid time formats (first 24h, then 12h)
        for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M", "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p"):
            try:
                dt = datetime.strptime(date_time_str, fmt)
                time_str = dt.strftime("%I:%M%p").lstrip("0").lower()
                return f"{dt.strftime('%d %b %Y').upper()} | {time_str}"
            except ValueError:
                continue

        raise ValueError(f"❌ Time format is invalid: '{time_part}'\n👉 Use formats like 2pm, 2:30pm, 1430")

    # No time part → just parse date
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(f"{day} {month} {year}", fmt)
            return dt.strftime("%d %b %Y").upper()
        except ValueError:
            continue

    raise ValueError(f"❌ Date format is invalid: '{original}'\n👉 Use formats like '24jun25', not numeric months.")


def parse_and_format_dates(dates_str):
    parts = [p.strip() for p in dates_str.split(",")]
    results = []
    invalid_parts = []

    for part in parts:
        if not part:
            invalid_parts.append("(empty)")
            continue
        try:
            formatted = parse_flexible_date(part)
            results.append(formatted)
        except ValueError:
            invalid_parts.append(part)

    if invalid_parts:
        if len(parts) == 1:
            # 🟥 Single entry error
            raise ValueError(
                "❌ Invalid *date/time* format.\n"
                "👉 Use formats like:\n"
                "- `23aug25 8:30pm`\n"
                "- `23aug 1430`\n"
                "- `23aug25`\n"
                "- `23aug`\n"
                "\n⚠️ Make sure your input uses letters for month (e.g. `aug`, not `08`)."
            )
        else:
            # 🟥 Multiple entry error
            raise ValueError(
                "❌ One or more *date/time* entries are invalid.\n"
                "👉 Use correct comma `,` between entries and formats like:\n"
                "- `23aug 8pm, 24aug 9pm`\n"
                "- `23aug25 1430, 24aug25`\n"
                "\n⚠️ Use *letter months*, not numeric (e.g. `aug`, not `08`)."
            )

    return results

async def delete_topic_with_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int, delay_seconds: int = 5):
    await asyncio.sleep(delay_seconds)
    try:
        await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        print(f"[INFO] Topic {thread_id} deleted after delay.")
    except Exception as e:
        print(f"[ERROR] Failed to delete topic {thread_id}: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("[DEBUG] Cancel triggered")
    chat = update.effective_chat

    # Clean up bot prompt messages
    try:
        if "modify_prompt_msg_ids" in context.chat_data:
            for msg_id in context.chat_data["modify_prompt_msg_ids"]:
                await chat.delete_message(msg_id)
            context.chat_data.pop("modify_prompt_msg_ids")
    except Exception as e:
        print(f"[WARNING] Failed to delete prompt messages on cancel: {e}")

    # Try to delete the command message issued by user (e.g. /modify or text input)
    try:
        await update.message.delete()
    except:
        pass

    return ConversationHandler.END