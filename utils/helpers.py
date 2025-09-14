from datetime import date, timedelta, datetime, time
from config import sg_tz
import asyncio

def get_next_tuesday(today=None):
    """Get the next Tuesday date"""
    if today is None:
        today = date.today()
    days_ahead = (1 - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)

def get_next_monday_8pm(now=None):
    """Get the next Monday at 8pm"""
    if now is None:
        now = datetime.now(sg_tz)
    days_until_monday = (7 - now.weekday()) % 7
    next_monday = now + timedelta(days=days_until_monday)
    naive_dt = datetime.combine(next_monday, time(22, 0))
    this_monday_8pm = sg_tz.localize(naive_dt)
    if now >= this_monday_8pm:
        return this_monday_8pm + timedelta(days=7)
    return this_monday_8pm

async def delete_topic_with_delay(context, chat_id: int, thread_id: int, delay_seconds: int = 5):
    """Delete a forum topic after a delay"""
    await asyncio.sleep(delay_seconds)
    try:
        await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        print(f"[INFO] Topic {thread_id} deleted after delay.")
    except Exception as e:
        print(f"[ERROR] Failed to delete topic {thread_id}: {e}")

def _date_label_from_display(date_str: str) -> str:
    """Convert date display format to short format"""
    date_str = (date_str or "").strip()
    if "/" in date_str and date_str.count("/") == 1 and all(p.isdigit() for p in date_str.split("/")):
        return date_str

    for fmt in ("%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            d, m = dt.day, dt.month
            return f"{d}/{m}"
        except Exception:
            continue
    return date_str