"""Admin DM flow for marking attendance.

`attd` opens a top-level menu with two categories:
  • 📋 Regular Training — the ATTENDANCE tab, columns keyed by poll id (a date
    picker, then a member toggle; supports the ✏️ Modify a Date sub-flow).
  • 🎭 Performance — the PERF TABULATION tab, columns keyed by the performance
    topic's THREAD ID (an event picker sourced from the PERF tab, then a member
    toggle). No dates, no polls, no modify-date here.

Both categories share the member-toggle UI and CONFIRM batch-write; only the
target worksheet differs (`commit_attendance_column` vs `commit_perf_column`).
The active category is tracked in `user_data["attd_mode"]` ("REG" / "PERF").
"""
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS, TOPIC_VOTING_ID, CHAT_ID, sg_tz
from utils.constants import active_polls, yes_voters
from services.google_sheets import (
    get_training_date_columns, get_attendees_for_date, commit_attendance_column,
    parse_sheet_date, replace_training_date_column,
    get_perf_event_list, get_perf_event_column, get_perf_attendees, commit_perf_column,
)
from services.date_parser import parse_date_line

HOME_TEXT = "✅ *Take Attendance*\nChoose a category:"


def _render_attendance_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Build the toggle keyboard from cached marks (2 attendees per row)."""
    marks = context.user_data.get("attd_marks", {})
    names = context.user_data.get("attd_names", {})
    order = context.user_data.get("attd_order", [])

    keyboard, buf = [], []
    for row in order:
        label = ("✅ " if marks.get(row) else "⬜ ") + names.get(row, str(row))
        buf.append(InlineKeyboardButton(label, callback_data=f"ATTD_TOGGLE|{row}"))
        if len(buf) == 2:
            keyboard.append(buf)
            buf = []
    if buf:
        keyboard.append(buf)

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ATTD_BACK")])
    keyboard.append([InlineKeyboardButton("💾 CONFIRM & SAVE", callback_data="ATTD_CONFIRM")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _build_home_keyboard() -> InlineKeyboardMarkup:
    """Top-level attendance menu: Regular Training vs Performance."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Regular Training", callback_data="ATTD_MODE|REG")],
        [InlineKeyboardButton("🎭 Performance", callback_data="ATTD_MODE|PERF")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")],
    ])


def _build_date_list_keyboard(dates) -> InlineKeyboardMarkup:
    """Build the training-date picker keyboard (the first layer of the flow).

    Latest date first; each button shows the present count in brackets.
    """
    keyboard = [[InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_DATE|{col}")]
                for col, label, present in dates]
    keyboard.append([InlineKeyboardButton("✏️ Modify a Date", callback_data="ATTD_MODMENU")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ATTD_HOME")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _build_perf_event_list_keyboard(events) -> InlineKeyboardMarkup:
    """Performance-event picker — one button per PERF-tab event, latest first,
    each showing the present count in brackets."""
    keyboard = [[InlineKeyboardButton(f"{name} ({present})", callback_data=f"ATTD_PEVT|{tid}")]
                for tid, name, present in events]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ATTD_HOME")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _build_moddate_list_keyboard(dates) -> InlineKeyboardMarkup:
    """Build the date picker for the 'modify a date' sub-flow (latest date first)."""
    keyboard = [[InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_MODDATE|{col}")]
                for col, label, present in dates]
    keyboard.append([InlineKeyboardButton("🔙 Back to Dates", callback_data="ATTD_BACK")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _future_dates(dates):
    """Keep only polled dates that are today or later.

    Used by the Modify-a-Date sub-flow: a past training's date can't be changed,
    so it's hidden from the modify picker. (The mark-attendance picker still
    shows past dates — you mark attendance after the training happened.)
    Unparseable dates are kept so they stay editable.
    """
    today = datetime.now(sg_tz).date()
    out = []
    for item in dates:
        d = parse_sheet_date(item[1])
        if d is None or d >= today:
            out.append(item)
    return out


def _load_dates(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    """Return the training-date columns, caching them in user_data.

    The date list is read from the sheet once per session; the back button (and
    the date-label lookup) reuse the cached copy instead of re-reading Sheets.
    """
    if not force:
        cached = context.user_data.get("attd_dates")
        if cached is not None:
            return cached
    dates = get_training_date_columns()
    context.user_data["attd_dates"] = dates
    return dates


async def _show_reg_dates(query, context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    """Edit the message to show the Regular-Training date picker."""
    context.user_data["attd_mode"] = "REG"
    dates = _load_dates(context, force=force)
    if not dates:
        await query.edit_message_text(
            "📅 No polled training dates yet. A date appears here once it has a poll id "
            "(auto-polled at D-4, or via Modify a Date)."
        )
        return
    await query.edit_message_text(
        "📋 *Mark Attendance [REG]*\nSelect a training date:",
        reply_markup=_build_date_list_keyboard(dates),
        parse_mode="Markdown",
    )


async def _show_perf_events(query, context: ContextTypes.DEFAULT_TYPE):
    """Edit the message to show the Performance-event picker."""
    context.user_data["attd_mode"] = "PERF"
    events = get_perf_event_list()
    context.user_data["attd_pevents"] = events
    if not events:
        await query.edit_message_text(
            "🎭 No performances found in the PERF tab yet. Create one with `/new` first."
        )
        return
    await query.edit_message_text(
        "🎭 *Mark Attendance [PERF]*\nSelect a performance event:",
        reply_markup=_build_perf_event_list_keyboard(events),
        parse_mode="Markdown",
    )


async def start_attendance_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point (DM `attd`): show the top-level category menu (REG / PERF)."""
    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return
    _clear(context)
    _clear_moddate(context)
    context.user_data.pop("attd_dates", None)
    context.user_data.pop("attd_pevents", None)
    await update.effective_message.reply_text(
        HOME_TEXT, reply_markup=_build_home_keyboard(), parse_mode="Markdown"
    )


async def attendance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all ATTD_* callbacks for the modify-attendance flow."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return

    data = query.data

    # --- Top-level category menu ---
    if data == "ATTD_MODE|REG":
        _clear(context)
        try:
            await _show_reg_dates(query, context, force=True)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read Attendance sheet: {e}")
        return

    if data == "ATTD_MODE|PERF":
        _clear(context)
        try:
            await _show_perf_events(query, context)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read PERF TABULATION: {e}")
        return

    if data == "ATTD_HOME":
        _clear(context)
        _clear_moddate(context)
        await query.edit_message_text(
            HOME_TEXT, reply_markup=_build_home_keyboard(), parse_mode="Markdown"
        )
        return

    # --- Performance event chosen: find/create its column, render toggles ---
    if data.startswith("ATTD_PEVT|"):
        tid = int(data.split("|")[1])
        events = context.user_data.get("attd_pevents", [])
        name = next((n for t, n, _p in events if t == tid), str(tid))
        try:
            col = get_perf_event_column(tid, create=True, event_name=name)
            attendees = get_perf_attendees(col)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to load attendees: {e}")
            return

        if not attendees:
            await query.edit_message_text(
                "⚠️ No members listed in the PERF TABULATION tab.\n"
                "Pre-fill the member roster in column A first."
            )
            return

        context.user_data["attd_mode"] = "PERF"
        context.user_data["attd_col"] = col
        context.user_data["attd_date_label"] = name
        context.user_data["attd_order"] = [r for r, _n, _t, _m in attendees]
        context.user_data["attd_names"] = {r: n for r, n, _t, _m in attendees}
        context.user_data["attd_marks"] = {r: m for r, _n, _t, m in attendees}

        await query.edit_message_text(
            f"🎭 *{name}* — tap to toggle, then CONFIRM.\n"
            "✅ = present · ⬜ = absent (sorted by attendance).",
            reply_markup=_render_attendance_keyboard(context),
            parse_mode="Markdown",
        )
        return

    # --- Date chosen: load attendees and render toggles ---
    if data.startswith("ATTD_DATE|"):
        col = int(data.split("|")[1])
        try:
            attendees = get_attendees_for_date(col)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to load attendees: {e}")
            return

        if not attendees:
            await query.edit_message_text(
                "⚠️ No members found in the Attendance sheet yet.\n"
                "Members are added automatically the first time they vote Yes."
            )
            return

        label = next((d for c, d, _p in _load_dates(context) if c == col), str(col))
        context.user_data["attd_mode"] = "REG"
        context.user_data["attd_col"] = col
        context.user_data["attd_date_label"] = label
        context.user_data["attd_order"] = [r for r, _n, _t, _m in attendees]
        context.user_data["attd_names"] = {r: n for r, n, _t, _m in attendees}
        context.user_data["attd_marks"] = {r: m for r, _n, _t, m in attendees}

        await query.edit_message_text(
            f"📅 *{label}* — tap to toggle, then CONFIRM.\n"
            "✅ = present · ⬜ = absent (sorted by attendance).",
            reply_markup=_render_attendance_keyboard(context),
            parse_mode="Markdown",
        )
        return

    # --- Open the "modify a date" sub-menu (pick which date to change) ---
    if data == "ATTD_MODMENU":
        _clear(context)
        _clear_moddate(context)
        try:
            dates = _future_dates(_load_dates(context))  # past trainings can't be re-dated
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read Attendance sheet: {e}")
            return
        if not dates:
            await query.edit_message_text(
                "📅 No upcoming training dates to modify (all polled dates are in the past)."
            )
            return
        await query.edit_message_text(
            "✏️ *Modify a Training Date*\nSelect the date to change (upcoming only):",
            reply_markup=_build_moddate_list_keyboard(dates),
            parse_mode="Markdown",
        )
        return

    # --- A date was chosen to modify: prompt for the new date ---
    if data.startswith("ATTD_MODDATE|"):
        col = int(data.split("|")[1])
        label = next((d for c, d, _p in _load_dates(context) if c == col), str(col))
        context.user_data["attd_moddate_col"] = col
        context.user_data["attd_moddate_label"] = label
        context.user_data.pop("attd_moddate_new", None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="ATTD_MODMENU")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_MODDATE_CANCEL")],
        ])
        await query.edit_message_text(
            f"✏️ Modifying *{label}*.\n\n"
            "Type the *new training date* in standard format — e.g. `12 June`, "
            "`12 Jun`, or `12 June 2026` (not `12/6`).\n\n"
            "⚠️ This column's existing ticks will be cleared. A poll is posted now "
            "if the new date is within 4 days — otherwise it's auto-posted on schedule.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    # --- Confirm the date change: replace the column; post a poll now only if
    #     the new date is 4 days away or sooner (same rule as auto_poll_check) ---
    if data == "ATTD_MODDATE_CONFIRM":
        col = context.user_data.get("attd_moddate_col")
        new_str = context.user_data.get("attd_moddate_new")
        old_label = context.user_data.get("attd_moddate_label", "")
        if not col or not new_str:
            await query.edit_message_text("❌ Session expired. Run `attd` again.")
            _clear_moddate(context)
            return

        # Authoritative past-date guard at the write point: never modify a
        # training to a date before today, no matter how new_str was set (typo,
        # or a once-valid date left pending until it became past).
        d = parse_sheet_date(new_str)
        today = datetime.now(sg_tz).date()
        if not d or d < today:
            await query.edit_message_text(
                f"❌ `{new_str}` is in the past — can't modify a training to a past date.\n"
                "Run `attd` → Modify a Date again with today or a future date.",
                parse_mode="Markdown",
            )
            _clear_moddate(context)
            return

        # Send the poll now when the new date is 4 days away or sooner (down to
        # today) — same window as auto_poll_check. A further-out date leaves the
        # poll id blank so the daily auto_poll_check posts it when it's due.
        send_now = (0 <= (d - today).days <= 4)

        await query.edit_message_text(f"⏳ Updating training date to {new_str}...")
        poll_id = None
        try:
            if send_now:
                msg = await context.bot.send_poll(
                    chat_id=CHAT_ID,
                    question=f"Are you joining the training on {new_str}?",
                    options=["Yes", "No"],
                    is_anonymous=False,
                    message_thread_id=TOPIC_VOTING_ID,
                )
                active_polls[msg.poll.id] = "training"
                yes_voters.clear()
                poll_id = msg.poll.id
            # poll_id=None clears row 1, so auto_poll_check will pick it up later.
            replace_training_date_column(col, new_str, poll_id)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to modify the date: {e}")
            _clear_moddate(context)
            return

        # Drop the cached date list so the renamed date shows up next time.
        context.user_data.pop("attd_dates", None)
        if send_now:
            tail = "A fresh poll has been posted to the Voting topic (it's within 4 days)."
        else:
            tail = "A poll will be auto-posted when the date is 4 days away."
        await query.edit_message_text(
            f"✅ Date changed: *{old_label}* → *{new_str}*.\n"
            f"The column was reset. {tail}",
            parse_mode="Markdown",
        )
        _clear_moddate(context)
        return

    if data == "ATTD_MODDATE_CANCEL":
        _clear_moddate(context)
        await query.edit_message_text("❌ Date modification cancelled.")
        return

    # --- Back to the list for the current mode (REG dates / PERF events) ---
    if data == "ATTD_BACK":
        mode = context.user_data.get("attd_mode", "REG")
        _clear(context)
        try:
            if mode == "PERF":
                await _show_perf_events(query, context)
            else:
                await _show_reg_dates(query, context)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read sheet: {e}")
        return

    # --- Toggle one member ---
    if data.startswith("ATTD_TOGGLE|"):
        row = int(data.split("|")[1])
        marks = context.user_data.get("attd_marks")
        if not marks or row not in marks:
            await query.answer("Session expired — run `attd` again.", show_alert=True)
            return
        marks[row] = not marks[row]
        await query.edit_message_reply_markup(reply_markup=_render_attendance_keyboard(context))
        return

    # --- Confirm: batch-write the column (to the right tab for the mode) ---
    if data == "ATTD_CONFIRM":
        col = context.user_data.get("attd_col")
        marks = context.user_data.get("attd_marks")
        label = context.user_data.get("attd_date_label", "")
        mode = context.user_data.get("attd_mode", "REG")
        if not col or marks is None:
            await query.edit_message_text("❌ Session expired. Run `attd` again.")
            return

        await query.edit_message_text(f"⏳ Saving attendance for {label}...")
        try:
            if mode == "PERF":
                commit_perf_column(col, marks)
            else:
                commit_attendance_column(col, marks)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to save: {e}")
            _clear(context)
            return

        present = sum(1 for v in marks.values() if v)
        await query.edit_message_text(
            f"✅ Attendance saved for *{label}*.\n{present} present / {len(marks)} members.",
            parse_mode="Markdown",
        )
        _clear(context)
        return

    # --- Cancel ---
    if data == "ATTD_CANCEL":
        _clear(context)
        await query.edit_message_text("❌ Attendance editing cancelled.")
        return


async def handle_moddate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Capture the typed new date during the 'modify a date' sub-flow.

    Returns True if the message was consumed (i.e. a date modification is in
    progress), so the DM dispatcher can stop processing it as anything else.
    """
    if not context.user_data.get("attd_moddate_col"):
        return False
    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return False

    text = (update.effective_message.text or "").strip()
    try:
        # Enforce the standardised date format via the shared date parser:
        # accepts "12 June", "12 Jun", "12 June 2026" — rejects slash formats like
        # "12/6". Output is the canonical "DD Mon YYYY" label; drop any trailing
        # time component so the cell stays a pure date (parse_sheet_date reads it).
        new_str = parse_date_line(text).split("  ")[0].strip()
    except ValueError:
        await update.effective_message.reply_text(
            "⚠️ Please enter the date in a standard format like `12 June`, "
            "`12 Jun`, or `12 June 2026` — *not* `12/6`.",
            parse_mode="Markdown",
        )
        return True

    # Check immediately, the moment the date is entered.
    import re
    from datetime import date as _date
    d = parse_sheet_date(new_str)
    today = datetime.now(sg_tz).date()

    # If the user typed no year (no digit after the month), judge the day/month in
    # the CURRENT year — so a date that has already passed (e.g. "2 June" when it's
    # 5 June) is flagged as past right away instead of the parser silently rolling
    # it forward to next year. (Type the year explicitly to schedule across years.)
    year_typed = bool(re.search(r"[A-Za-z]\s*\d", text))
    if d and not year_typed:
        try:
            d = _date(today.year, d.month, d.day)
            new_str = d.strftime("%d %b %Y")
        except ValueError:
            pass

    if not d or d < today:
        await update.effective_message.reply_text(
            f"⚠️ `{new_str}` is in the past. Enter a date that's today or later.",
            parse_mode="Markdown",
        )
        return True
    context.user_data["attd_moddate_new"] = new_str
    old_label = context.user_data.get("attd_moddate_label", "")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ CONFIRM", callback_data="ATTD_MODDATE_CONFIRM")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_MODDATE_CANCEL")],
    ])
    await update.effective_message.reply_text(
        f"Change *{old_label}* → *{new_str}*?\n\n"
        "This clears the column's ticks. A poll is posted now if the new date "
        "is within 4 days; otherwise it's auto-posted when due.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return True


def _clear(context: ContextTypes.DEFAULT_TYPE):
    for key in ("attd_col", "attd_date_label", "attd_order", "attd_names", "attd_marks"):
        context.user_data.pop(key, None)


def _clear_moddate(context: ContextTypes.DEFAULT_TYPE):
    for key in ("attd_moddate_col", "attd_moddate_label", "attd_moddate_new"):
        context.user_data.pop(key, None)
