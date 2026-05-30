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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_DM_USER_IDS
from services.google_sheets import (
    get_training_date_columns, get_attendees_for_date, commit_attendance_column,
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

    keyboard.append([InlineKeyboardButton("💾 CONFIRM & SAVE", callback_data="ATTD_CONFIRM")])
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    return InlineKeyboardMarkup(keyboard)


async def start_attendance_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point (DM `attd`): show the list of training dates to choose from."""
    if update.effective_user.id not in ADMIN_DM_USER_IDS:
        return
    try:
        dates = get_training_date_columns()
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Failed to read Attendance sheet: {e}")
        return

    if not dates:
        await update.effective_message.reply_text(
            "📅 No training dates defined yet. Add them to row 2 of the Attendance tab first."
        )
        return

    keyboard = [[InlineKeyboardButton(label, callback_data=f"ATTD_DATE|{col}")]
                for col, label in dates]
    keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data="ATTD_CANCEL")])
    await update.effective_message.reply_text(
        "📋 *Mark Attendance [REG]*\nSelect a training date:",
        reply_markup=InlineKeyboardMarkup(keyboard),
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

        label = next((d for c, d in get_training_date_columns() if c == col), str(col))
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


def _clear(context: ContextTypes.DEFAULT_TYPE):
    for key in ("attd_col", "attd_date_label", "attd_order", "attd_names", "attd_marks"):
        context.user_data.pop(key, None)
