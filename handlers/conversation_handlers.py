from telegram.error import BadRequest


def build_performance_summary(
    event_name: str,
    rehearsal_date: str,
    perf_date: str,
    location: str,
    other_info: str,
) -> str:
    """Return the formatted pinned performance summary message.

    Rehearsal date is omitted from display when it is '-' or blank.
    Performance date shows 'TBD' when it is 'TBD' or blank (skipped in wizard).
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

    # 2. If an old summary message exists in the sheet, delete it first
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

    # 3. Post the fresh summary
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

    # 5. Save the new message ID to the sheet for future refresh lookups
    if row_number:
        try:
            msg_col_idx = SHEET_COLUMNS.index("SUMMARY MSG ID") + 1
            sheet.update_cell(row_number, msg_col_idx, str(new_message.message_id))
            print(f"[GSHEET] Saved new summary message ID {new_message.message_id} to row {row_number}")
        except Exception as e:
            print(f"[ERROR] Failed to save message ID to Google Sheets: {e}")

    return new_message, None
