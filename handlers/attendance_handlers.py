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

from config import TOPIC_VOTING_ID, CHAT_ID, sg_tz
from utils.constants import active_polls, yes_voters
from utils.ui import paginate, pagination_row
from services.google_sheets import (
    get_training_date_columns, get_attendees_for_date, commit_attendance_column,
    parse_sheet_date, replace_training_date_column,
    get_perf_event_list, get_perf_event_column, get_perf_attendees, commit_perf_column,
    get_training_poll_ref, is_dashboard_admin,
)
from services.date_parser import parse_date_line
from services.alerts import who

HOME_TEXT = "✅ *Take Attendance*\nChoose a category:"
PAGE_SIZE = 20


def _attd_group(name: str) -> str:
    """"(SU3) Wei Yin" → "SU"; "(JEG) X" → "JEG"; no-prefix name → ""."""
    if not name.startswith("("):
        return ""
    end = name.find(")")
    if end < 0:
        return ""
    return name[1:end].rstrip("0123456789")


def _render_attendance_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    marks = context.user_data.get("attd_marks", {})
    names = context.user_data.get("attd_names", {})
    order = context.user_data.get("attd_order", [])
    page = context.user_data.get("attd_page", 0)
    total = len(order)
    page_order, page, total_pages = paginate(order, page, PAGE_SIZE)

    keyboard, buf = [], []
    prev_group = None
    for row in page_order:
        name = names.get(row, str(row))
        group = _attd_group(name)
        if prev_group is not None and group != prev_group:
            if buf:
                keyboard.append(buf)
                buf = []
            keyboard.append([InlineKeyboardButton(f"─── {group or '—'} ───", callback_data="ATTD_NOOP")])
        prev_group = group
        label = ("✅ " if marks.get(row) else "⬜ ") + name
        buf.append(InlineKeyboardButton(label, callback_data=f"ATTD_TOGGLE|{row}"))
        if len(buf) == 2:
            keyboard.append(buf)
            buf = []
    if buf:
        keyboard.append(buf)

    present = sum(1 for v in marks.values() if v)
    keyboard.append([InlineKeyboardButton(f"✅ Present: {present} / {total}", callback_data="ATTD_NOOP")])

    nav = pagination_row(page, total_pages, lambda p: f"ATTD_PAGE|{p}", "ATTD_NOOP")
    if nav:
        keyboard.append(nav)

    mode = context.user_data.get("attd_mode", "REG")
    if mode == "REG":
        back_label = "🔙 Back to Training Dates"
        back_data = "ATTD_BACK_REG"
    else:
        back_label = "🔙 Back to Performance Topics"
        back_data = "ATTD_BACK_PERF"

    keyboard.append([InlineKeyboardButton("💾 Confirm & Save Record", callback_data="ATTD_CONFIRM")])
    keyboard.append([InlineKeyboardButton(back_label, callback_data=back_data)])
    return InlineKeyboardMarkup(keyboard)

# 1. Main Home Menu: Keep the Exit button (this is the top level)
def _build_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏋️ For Regular Training", callback_data="ATTD_CAT|REGULAR")],
        [InlineKeyboardButton("🎭 For Performance", callback_data="ATTD_CAT|PERF")],
        [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")] 
    ])

# 2. Date/Event Selection Lists: Remove Exit, keep Back to Category
def _build_date_list_keyboard(dates) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_DATE|{col}")]
        for col, label, present in dates
    ]
    keyboard.append([InlineKeyboardButton("✏️ Modify a Date", callback_data="ATTD_MODMENU")])
    # THIS DATA MUST MATCH THE HANDLER ABOVE
    keyboard.append([InlineKeyboardButton("🔙 Back to Category Menu", callback_data="ATTD_HOME")])
    return InlineKeyboardMarkup(keyboard)

def _build_perf_event_list_keyboard(events) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(f"{name} ({present})", callback_data=f"ATTD_PEVT|{tid}")]
        for tid, name, present in events
    ]
    # Back from the event LIST returns to the category menu (the toggle screen's
    # own back button uses ATTD_BACK_PERF to return here instead).
    keyboard.append([InlineKeyboardButton("🔙 Back to Category", callback_data="ATTD_HOME")])
    return InlineKeyboardMarkup(keyboard)

# 3. Modify Date Sub-Menu: Keep Back to Training Dates, remove Exit
def _build_moddate_list_keyboard(dates) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(f"{label} ({present})", callback_data=f"ATTD_MODDATE|{col}")]
        for col, label, present in dates
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back to Training Dates", callback_data="ATTD_BACK")])
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


async def _remove_old_training_poll(bot, col: int) -> str:
    """Delete or close the old training poll for a date column when possible."""
    old_poll_id, old_message_id = get_training_poll_ref(col)
    if old_poll_id:
        active_polls.pop(old_poll_id, None)
    if not old_poll_id:
        return ""
    if not old_message_id:
        return "Old poll was cleared from the sheet, but it was created before message tracking so I could not remove it from the topic."

    try:
        await bot.delete_message(chat_id=CHAT_ID, message_id=old_message_id)
        return "Old poll was removed from the Voting topic."
    except Exception as delete_err:
        try:
            await bot.stop_poll(chat_id=CHAT_ID, message_id=old_message_id)
            return "Old poll could not be deleted, so it was closed instead."
        except Exception as stop_err:
            print(f"[ATTD][WARN] Could not remove old poll {old_poll_id}/{old_message_id}: delete={delete_err}; stop={stop_err}")
            return "Old poll was cleared from the sheet, but Telegram did not let me delete or close the topic message."


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

async def _update_attendance_bubble(query, text, keyboard, context):
    """Render into the SINGLE live attendance bubble — never a new message.

    A callback always fires from the currently-visible bubble, so we edit
    `query.message` in place and keep `attd_bubble_id` in sync with it. A
    "message is not modified" no-op is swallowed (not turned into a fresh
    message), so every attendance screen — including the Performance back
    buttons — stays inside the one dashboard bubble.
    """
    context.user_data["attd_bubble_id"] = query.message.message_id
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        if "not modified" in str(e).lower():
            return  # content unchanged — nothing to do, do NOT spawn a new bubble
        # Last resort: refresh just the buttons in place (still the same message).
        print(f"[DEBUG] bubble edit failed: {e}")
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e2:
            print(f"[DEBUG] markup refresh failed: {e2}")

async def _show_reg_dates(query, context, force=False, success_banner: str = ""):
    context.user_data["attd_mode"] = "REG"
    dates = _load_dates(context, force=force)
    banner = f"{success_banner}\n\n" if success_banner else ""

    if not dates:
        await _update_attendance_bubble(
            query,
            banner + "📅 *No polled training dates yet.*",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Category", callback_data="ATTD_HOME")]]),
            context
        )
        return

    await _update_attendance_bubble(
        query,
        banner + "📋 *Mark Attendance [REG]*\nSelect a training date:",
        _build_date_list_keyboard(dates),
        context
    )

async def _show_perf_events(query, context: ContextTypes.DEFAULT_TYPE, success_banner: str = ""):
    """Edit the message to show the Performance-event picker with optional success banner."""
    context.user_data["attd_mode"] = "PERF"
    events = get_perf_event_list()
    context.user_data["attd_pevents"] = events
    
    # 🎯 Build the text, prepending the banner if it exists
    text = f"{success_banner}\n\n" if success_banner else ""
    text += "🎭 *Mark Attendance [PERF]*\nSelect a performance event:"

    if not events:
        error_text = "🎭 No performances found in the PERF tab yet. Create one with `/new` first."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Category", callback_data="ATTD_HOME")],
            [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")],
        ])
        await _update_attendance_bubble(query, error_text, keyboard, context)
        return
        
    await _update_attendance_bubble(
        query, 
        text, 
        _build_perf_event_list_keyboard(events), 
        context
    )


async def start_attendance_modify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_dashboard_admin(update.effective_user.id):
        return
    old_master = context.user_data.get("master_dash_id")
    _clear(context)
    _clear_moddate(context)

    # Close the previous panel so its sub-section can't be operated anymore.
    if old_master:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=old_master,
                text="🛑 *This panel has been closed.*\n\nA fresh one is open below.",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass

    # Save the bubble ID immediately
    sent = await update.effective_message.reply_text(
        "📋 Attendance Cockpit\nSelect a category:",
        reply_markup=_build_home_keyboard(),
        parse_mode="Markdown"
    )
    context.user_data["attd_bubble_id"] = sent.message_id
    # This new bubble becomes THE live panel — any older dashboard/attendance
    # bubble is now superseded and will be rejected by the expiry guard below.
    context.user_data["master_dash_id"] = sent.message_id


async def attendance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # 🎯 Data must be defined here
    data = query.data

    await query.answer()

    # 🔒 Only the LATEST dashboard / attendance bubble is live. After a fresh
    # /start (or /attd) `master_dash_id` points at the newest panel, so a click on
    # any older, superseded bubble is rejected here instead of operating a stale
    # session. (Matches handle_dashboard_navigation's guard for DASH_VIEW.)
    active = context.user_data.get("master_dash_id")
    if active and query.message and query.message.message_id != active:
        try:
            await query.message.edit_text(
                "🛑 *This panel has expired.*\n\n"
                "You opened a newer dashboard further down the chat — "
                "please use the latest one (run /start if unsure).",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass
        return
    if not active and query.message:
        context.user_data["master_dash_id"] = query.message.message_id

    # Ensure attd_bubble_id is set (adopt this live bubble for in-place edits)
    if not context.user_data.get("attd_bubble_id"):
        context.user_data["attd_bubble_id"] = query.message.message_id

    # --- 1. BACK-BUTTON HANDLERS ---
    if data == "ATTD_BACK_REG":
        _clear(context)
        _clear_moddate(context)  # leaving the modify-date flow: drop any pending date state
        context.user_data["attd_mode"] = "REG"
        await _show_reg_dates(query, context, force=True)
        return

    if data == "ATTD_BACK_PERF":
        context.user_data["attd_mode"] = "PERF"
        _clear(context)
        context.user_data["attd_mode"] = "PERF"
        await _show_perf_events(query, context)
        return

    # --- 2. ENTRY HANDLERS ---
    if data == "ATTD_CAT|REGULAR":
        _clear(context)
        context.user_data["attd_mode"] = "REG"
        await _show_reg_dates(query, context, force=True)
        return
    
    if data == "ATTD_CAT|PERF":
        _clear(context)
        context.user_data["attd_mode"] = "PERF"
        await _show_perf_events(query, context)
        return

    # --- 3. OTHER HANDLERS (The rest of your logic) ---
    if data == "ATTD_HOME":
        _clear(context)
        _clear_moddate(context)
        await _update_attendance_bubble(query, HOME_TEXT, _build_home_keyboard(), context)
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
            print(f"[ERROR] attendance: failed to load perf attendees - {who(update)}: {e}")
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
        context.user_data["attd_page"] = 0

        await query.edit_message_text(
            f"🎭 *{name}* — tap to toggle, then CONFIRM.\n\n"
            "✅ = present | ⬜ = absent",
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
            print(f"[ERROR] attendance: failed to load attendees for date - {who(update)}: {e}")
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
        context.user_data["attd_page"] = 0

        await query.edit_message_text(
            f"📅 *{label}* — tap to toggle, then CONFIRM.\n\n"
            "✅ = present | ⬜ = absent",
            reply_markup=_render_attendance_keyboard(context),
            parse_mode="Markdown",
        )
        return

    # --- Open the "modify a date" sub-menu (pick which date to change) ---
    if data == "ATTD_MODMENU":
        _clear(context)
        _clear_moddate(context)
        
        try:
            dates = _future_dates(_load_dates(context))
        except Exception as e:
            print(f"[ERROR] attendance: failed to read Attendance sheet - {who(update)}: {e}")
            # 🎯 THIS IS THE FIX: Use _update_attendance_bubble instead of reply_text
            await _update_attendance_bubble(
                query, 
                f"❌ Failed to read Attendance sheet: {e}", 
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ATTD_HOME")]]), 
                context
            )
            return
        
        if not dates:
            # Show it as a banner on the REG date list rather than a dead-end bubble.
            await _show_reg_dates(
                query, context,
                success_banner="⚠️ *No upcoming training dates to modify.*",
            )
            return

        await _update_attendance_bubble(
            query,
            "✏️ *Modify a Training Date*\nSelect the date to change:",
            _build_moddate_list_keyboard(dates),
            context
        )
        return

    # --- A date was chosen to modify: prompt for the new date ---
    if data.startswith("ATTD_MODDATE|"):
        col = int(data.split("|")[1])
        label = next((d for c, d, _p in _load_dates(context) if c == col), str(col))
        context.user_data["attd_moddate_col"] = col
        context.user_data["attd_moddate_label"] = label
        context.user_data.pop("attd_moddate_new", None)
        # Show the current date as a tap-to-copy code span, standardised so what
        # the admin copies is already a valid value to tweak and re-send.
        d_cur = parse_sheet_date(label)
        copy_str = d_cur.strftime("%d %b %Y") if d_cur else label
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="ATTD_MODMENU")],
            [InlineKeyboardButton("🔙 Back to Attendance [REG]", callback_data="ATTD_BACK_REG")],
        ])
        await query.edit_message_text(
            f"✏️ Modifying *{label}*.\n\n"
            f"📋 *Current date (tap to copy, then edit): *\n```\n{copy_str}```\n"
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
            _clear_moddate(context)
            await _show_reg_dates(
                query, context, force=True,
                success_banner=f"⚠️ *{new_str} is in the past — pick today or a future date.*",
            )
            return

        # Send the poll now when the new date is 4 days away or sooner (down to
        # today) — same window as auto_poll_check. A further-out date leaves the
        # poll id blank so the daily auto_poll_check posts it when it's due.
        send_now = (0 <= (d - today).days <= 4)

        await query.edit_message_text(f"⏳ Updating training date to {new_str}...")
        poll_id = None
        poll_message_id = None
        old_poll_note = ""
        try:
            old_poll_note = await _remove_old_training_poll(context.bot, col)
            if send_now:
                msg = await context.bot.send_poll(
                    chat_id=CHAT_ID,
                    question=f"🥁 Training on {new_str}, you in? 🔥",
                    options=["✅ Count me in!", "❌ Can't make it"],  # option 0 must stay = "yes"
                    is_anonymous=False,
                    message_thread_id=TOPIC_VOTING_ID,
                )
                active_polls[msg.poll.id] = "training"
                yes_voters.clear()
                poll_id = msg.poll.id
                poll_message_id = msg.message_id
            # poll_id=None clears row 1, so auto_poll_check will pick it up later.
            replace_training_date_column(col, new_str, poll_id, poll_message_id)
        except Exception as e:
            print(f"[ERROR] attendance: failed to modify training date - {who(update)}: {e}")
            await query.edit_message_text(f"❌ Failed to modify the date: {e}")
            _clear_moddate(context)
            return

        # Drop the cached date list so the renamed date shows up next time.
        context.user_data.pop("attd_dates", None)
        if send_now:
            tail = "A fresh poll has been posted to the Voting topic (it's within 4 days)."
        else:
            tail = "A poll will be auto-posted when the date is 4 days away."
        if old_poll_note:
            tail = f"{old_poll_note} {tail}"
        _clear_moddate(context)
        banner = f"🟢 *Date changed: {old_label} → {new_str}. {tail}*"
        await _show_reg_dates(query, context, force=True, success_banner=banner)
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
            print(f"[ERROR] attendance: failed to read sheet - {who(update)}: {e}")
            await query.edit_message_text(f"❌ Failed to read sheet: {e}")
        return

    # --- Page indicator tap (no-op) ---
    if data == "ATTD_NOOP":
        return

    # --- Page navigation ---
    if data.startswith("ATTD_PAGE|"):
        token = data.split("|")[1]
        page = context.user_data.get("attd_page", 0)
        order = context.user_data.get("attd_order", [])
        # Absolute page numbers now; "prev"/"next" still accepted so buttons on
        # an older message keep working after a restart.
        if token.isdigit():
            page = int(token)
        elif token == "next":
            page += 1
        else:
            page -= 1
        _chunk, page, _pages = paginate(order, page, PAGE_SIZE)
        context.user_data["attd_page"] = page
        await query.edit_message_reply_markup(reply_markup=_render_attendance_keyboard(context))
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
            print(f"[ERROR] attendance: failed to save attendance column - {who(update)}: {e}")
            await query.edit_message_text(f"❌ Failed to save: {e}")
            _clear(context)
            return

        # 🟢 Success banner
        banner = f"🟢 *Success: Attendance roster log record for {label} has been successfully compiled and saved to Google Sheets!*"
        
        # 🎯 FIX: Determine mode and re-trigger the appropriate list view instead of the Cockpit
        _clear(context)
        
        if mode == "PERF":
            # Pass the banner into the performance event list view
            await _show_perf_events(query, context, success_banner=banner)
        else:
            # Pass the banner into the regular training dates list view
            await _show_reg_dates(query, context, force=True, success_banner=banner)
        return

    # --- Cancel ---
    if data == "ATTD_CANCEL":
        _clear(context)
        from handlers.private_handlers import DASHBOARD_TEXT, _dashboard_keyboard
        await _update_attendance_bubble(query, DASHBOARD_TEXT, _dashboard_keyboard(), context)
        return


async def handle_moddate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("attd_moddate_col"): return False
    
    msg_id = context.user_data.get("attd_bubble_id")
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    
    # Delete the user's typed message to keep the chat clean
    await update.effective_message.delete()

    try:
        new_str = parse_date_line(text).split("  ")[0].strip()
    except ValueError:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="⚠️ Invalid format. Use `12 June` or `12 June 2026`.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ATTD_HOME")]])
        )
        return True

    import re
    from datetime import date as _date
    d = parse_sheet_date(new_str)
    today = datetime.now(sg_tz).date()

    year_typed = bool(re.search(r"[A-Za-z]\s*\d", text))
    if d and not year_typed:
        try:
            d = _date(today.year, d.month, d.day)
            new_str = d.strftime("%d %b %Y")
        except ValueError: pass

    if not d or d < today:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=f"⚠️ `{new_str}` is in the past. Enter a date that's today or later.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Category", callback_data="ATTD_HOME")]])
        )
        return True

    # SUCCESS: Update the SAME bubble
    context.user_data["attd_moddate_new"] = new_str
    old_label = context.user_data.get("attd_moddate_label", "")
    
    await context.bot.edit_message_text(
        chat_id=chat_id, message_id=msg_id,
        text=f"Change *{old_label}* → *{new_str}*?\n\nThis clears the column's ticks.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Modification", callback_data="ATTD_MODDATE_CONFIRM")],
            [InlineKeyboardButton("🔙 Back to Category", callback_data="ATTD_HOME")]
        ])
    )
    return True


def _clear(context: ContextTypes.DEFAULT_TYPE):
    for key in ("attd_col", "attd_date_label", "attd_order", "attd_names", "attd_marks", "attd_page"):
        context.user_data.pop(key, None)


def _clear_moddate(context: ContextTypes.DEFAULT_TYPE):
    for key in ("attd_moddate_col", "attd_moddate_label", "attd_moddate_new"):
        context.user_data.pop(key, None)
