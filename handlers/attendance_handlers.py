"""Admin DM flow for manually marking regular-training attendance.

Flow:
  1. `attd` (DM)            → list all defined training dates as buttons
  2. ATTD_DATE|<col>        → load attendees (sorted by TOTAL desc), pre-tick those
                              already marked "1", show toggle buttons
  3. ATTD_TOGGLE|<row>      → flip a member's tick (cached in user_data, no write)
  4. ATTD_CONFIRM           → batch-write the whole date column + refresh TOTALs
  5. ATTD_CANCEL            → discard cached toggles

Only the attendance column for the chosen date is written, all at once, on
confirm — so there is no per-tap back-and-forth with Google Sheets.
"""
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS, TOPIC_VOTING_ID, CHAT_ID, sg_tz
from utils.constants import active_polls, yes_voters
from services.google_sheets import (
    get_training_date_columns, get_attendees_for_date, commit_attendance_column,
    parse_sheet_date, replace_training_date_column,
)


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

    keyboard.append([InlineKeyboardButton("🔙 Back to Dates", callback_data="ATTD_BACK")])
    keyboard.append([InlineKeyboardButton("💾 CONFIRM & SAVE", callback_data="ATTD_CONFIRM")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _build_date_list_keyboard(dates) -> InlineKeyboardMarkup:
    """Build the training-date picker keyboard (the first layer of the flow).

    Latest date first; each button shows the present count in brackets.
    """
    keyboard = [[InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_DATE|{col}")]
                for col, label, present in dates]
    keyboard.append([InlineKeyboardButton("✏️ Modify a Date", callback_data="ATTD_MODMENU")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


def _build_moddate_list_keyboard(dates) -> InlineKeyboardMarkup:
    """Build the date picker for the 'modify a date' sub-flow (latest date first)."""
    keyboard = [[InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_MODDATE|{col}")]
                for col, label, present in dates]
    keyboard.append([InlineKeyboardButton("🔙 Back to Dates", callback_data="ATTD_BACK")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


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


async def start_attendance_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point (DM `attd`): show the list of training dates to choose from."""
    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return
    try:
        # Fresh read on entry so a newly-added training date shows up.
        dates = _load_dates(context, force=True)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Failed to read Attendance sheet: {e}")
        return

    if not dates:
        await update.effective_message.reply_text(
            "📅 No training dates defined yet. Add them to row 2 of the Attendance tab first."
        )
        return

    await update.effective_message.reply_text(
        "📋 *Mark Attendance [REG]*\nSelect a training date:",
        reply_markup=_build_date_list_keyboard(dates),
        parse_mode="Markdown",
    )


async def attendance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all ATTD_* callbacks for the modify-attendance flow."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return

    data = query.data

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
        try:
            dates = _load_dates(context)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read Attendance sheet: {e}")
            return
        if not dates:
            await query.edit_message_text(
                "📅 No training dates defined yet. Add them to row 2 of the Attendance tab first."
            )
            return
        await query.edit_message_text(
            "✏️ *Modify a Training Date*\nSelect the date to change:",
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
        await query.edit_message_text(
            f"✏️ Modifying *{label}*.\n\n"
            "Type the *new training date* (e.g. `12/8`, `12 Aug`, `12 Aug 2026`).\n\n"
            "⚠️ This column's existing ticks will be cleared. A poll is posted only "
            "when the new date is 2 days away — otherwise it's auto-posted on schedule.",
            parse_mode="Markdown",
        )
        return

    # --- Confirm the date change: replace the column; post a poll only if the
    #     new date is exactly 2 days away (same rule as auto_poll_check) ---
    if data == "ATTD_MODDATE_CONFIRM":
        col = context.user_data.get("attd_moddate_col")
        new_str = context.user_data.get("attd_moddate_new")
        old_label = context.user_data.get("attd_moddate_label", "")
        if not col or not new_str:
            await query.edit_message_text("❌ Session expired. Run `attd` again.")
            _clear_moddate(context)
            return

        # Only auto-send the poll when the new date is exactly 2 days out — for
        # any other date the daily auto_poll_check posts it when it's due.
        d = parse_sheet_date(new_str)
        today = datetime.now(sg_tz).date()
        send_now = bool(d) and (d - today == timedelta(days=2))

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
            tail = "A fresh poll has been posted to the Voting topic (it's 2 days away)."
        else:
            tail = "A poll will be auto-posted when the date is 2 days away."
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

    # --- Back to the date picker (first layer) ---
    if data == "ATTD_BACK":
        _clear(context)
        try:
            # Reuse the cached date list — going back never re-reads Sheets.
            dates = _load_dates(context)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to read Attendance sheet: {e}")
            return
        if not dates:
            await query.edit_message_text(
                "📅 No training dates defined yet. Add them to row 2 of the Attendance tab first."
            )
            return
        await query.edit_message_text(
            "📋 *Mark Attendance [REG]*\nSelect a training date:",
            reply_markup=_build_date_list_keyboard(dates),
            parse_mode="Markdown",
        )
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

    # --- Confirm: batch-write the column ---
    if data == "ATTD_CONFIRM":
        col = context.user_data.get("attd_col")
        marks = context.user_data.get("attd_marks")
        label = context.user_data.get("attd_date_label", "")
        if not col or marks is None:
            await query.edit_message_text("❌ Session expired. Run `attd` again.")
            return

        await query.edit_message_text(f"⏳ Saving attendance for {label}...")
        try:
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
    d = parse_sheet_date(text)
    if not d:
        await update.effective_message.reply_text(
            "⚠️ Couldn't read that date. Try a format like `12/8`, `12 Aug`, "
            "or `12 Aug 2026`.",
            parse_mode="Markdown",
        )
        return True

    new_str = f"{d.day}/{d.month}/{d.year}"
    context.user_data["attd_moddate_new"] = new_str
    old_label = context.user_data.get("attd_moddate_label", "")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ CONFIRM", callback_data="ATTD_MODDATE_CONFIRM")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_MODDATE_CANCEL")],
    ])
    await update.effective_message.reply_text(
        f"Change *{old_label}* → *{new_str}*?\n\n"
        "This clears the column's ticks. A poll is posted now only if the new date "
        "is 2 days away; otherwise it's auto-posted when due.",
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
