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
    # Your existing parse_flexible_date logic here
    pass

def parse_and_format_dates(dates_str):
    # Your existing parse_and_format_dates logic here
    pass