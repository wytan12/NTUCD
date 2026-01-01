import re
from datetime import datetime

def parse_flexible_date(date_str: str) -> str:
    """Parse flexible date formats"""
    original = date_str.strip()
    lower = original.lower()

    # Normalize various time formats
    lower = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', lower)
    lower = re.sub(r'\b(\d{1,2})(\d{2})(am|pm)\b', r'\1:\2 \3', lower)
    lower = re.sub(r'(\d{1,2}:\d{2})(am|pm)\b', r'\1 \2', lower)
    lower = re.sub(r'\b(\d{1,2})(am|pm)\b', r'\1:00 \2', lower)

    pattern = re.match(
        r"(\d{1,2})[\s-]?([a-zA-Z]{3,9})[\s-]?(\d{2,4})?(?:\s+(\d{1,2}:\d{2}(?:\s?(?:am|pm))?|\d{3,4}(?:\s?(?:am|pm))?))?$",
        lower,
    )
    
    if not pattern:
        raise ValueError(f"❌ Invalid date format: '{original}'\n👉 Use formats like '24jun25' or '24jun25 2:30pm'.")

    day, month, year, time_part = pattern.groups()

    if not year:
        year = str(datetime.now().year)
    elif len(year) == 2:
        year = "20" + year

    date_time_str = f"{day} {month} {year}"
    if time_part:
        time_part = time_part.strip()
        suffix = ""
        if time_part.lower().endswith(("am", "pm")):
            suffix = time_part[-2:]
            time_part = time_part[:-2].strip()
        if ":" not in time_part and time_part.isdigit() and 3 <= len(time_part) <= 4:
            time_part = f"{time_part[:-2]}:{time_part[-2:]}"
        if suffix:
            time_part = f"{time_part} {suffix}"

        date_time_with_time = f"{date_time_str} {time_part}"

        for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M", "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p"):
            try:
                dt = datetime.strptime(date_time_with_time, fmt)
                time_str = dt.strftime("%I:%M%p").lstrip("0").lower()
                return f"{dt.strftime('%d %b %Y').upper()} | {time_str}"
            except ValueError:
                continue

        raise ValueError(f"❌ Time format is invalid: '{time_part}'\n👉 Use formats like 2pm, 2:30pm, 1430")

    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(f"{day} {month} {year}", fmt)
            return dt.strftime("%d %b %Y").upper()
        except ValueError:
            continue

    raise ValueError(f"❌ Date format is invalid: '{original}'\n👉 Use formats like '24jun25', not numeric months.")

def parse_and_format_dates(dates_str):
    """Parse and format multiple dates"""
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
            raise ValueError(
                "❌ One or more *date/time* entries are invalid.\n"
                "👉 Use correct comma `,` between entries and formats like:\n"
                "- `23aug 8pm, 24aug 9pm`\n"
                "- `23aug25 1430, 24aug25`\n"
                "\n⚠️ Use *letter months*, not numeric (e.g. `aug`, not `08`)."
            )

    return results