from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from utils.decorators import admin_only
from services.google_sheets import get_gspread_sheet, get_attendance_ws, get_cached_records, invalidate_sheet_cache
from config import sg_tz, CHAT_ID, ADMIN_DM_USER_IDS, SHEET_COLUMNS
from datetime import datetime, timedelta
from handlers.private_handlers import DASHBOARD_TEXT, _dashboard_keyboard
import re

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command inside Private DMs strictly (admins only)."""
    if update.effective_chat.type != "private":
        return # 🤐 TOTAL PASSIVITY: Leaves the message completely untouched inside group channels

    # 🛡️ Only verified dashboard admins get a response — stay silent otherwise.
    # Role-driven: MAIN + SECONDARY admin roles from MEMBER INFO (+ config fallback).
    from services.google_sheets import is_dashboard_admin
    current_uid = update.effective_user.id
    if not is_dashboard_admin(current_uid):
        print(f"[SECURITY] Unauthorized /start attempt blocked for user ID: {current_uid}")
        return

    # 🧼 Clear out any stale session context variables first
    saved_master_id = context.user_data.get("master_dash_id")
    context.user_data.clear()

    # 🛑 Neutralise the PREVIOUS panel so whatever sub-section it was showing can
    # no longer be operated. Everything runs as one in-place bubble, so the old
    # master id IS the sub-section the user was on — stripping its keyboard kills
    # it outright (no reliance on every handler's click-time guard).
    if saved_master_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=saved_master_id,
                text="🛑 *This dashboard has been closed.*\n\n"
                     "A fresh one is open below — please use that one.",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass  # old message gone / identical / too old — the click guards still cover it

    # 🚀 Send the master cockpit layout and save its ID safely
    msg = await update.message.reply_text(
        DASHBOARD_TEXT,
        reply_markup=_dashboard_keyboard(),
        parse_mode="Markdown",
    )
    # Lock this ID permanently; older bubbles will deactivate instantly
    context.user_data["master_dash_id"] = msg.message_id

@admin_only
async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🧠 COMPREHENSIVE DIRECTORY INDEXER: Maps out all available forum channels

    grouped beautifully into performance and non-performance layout segments.
    """
    if update.effective_chat.type != "private":
        return  # 🤐 Stay completely passive inside public group topic chats

    status_msg = await update.effective_message.reply_text("⏳ Compiling live workspace forum index...")
    
    try:
        # 1. Fetch Performance Records directly from cache ledger tracking
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        
        chart_lines = ["🧵 *NTU Festive Drums — Forum Directory Chart*\n"]
        chart_lines.append("• 0 | **General Topic (Main Channel)**\n")

        # 2. Compile Performance List segment
        chart_lines.append("🎭 *Performance Thread IDs*")
        if records:
            perf_count = 0
            for row in records:
                tid = row.get("THREAD ID")
                if str(tid).isdigit():
                    name = row.get("EVENT NAME", "Unnamed Event")
                    status = row.get("STATUS", "PENDING").strip() or "PENDING"
                    chart_lines.append(f"• {tid} | *{name}* ({status})")
                    perf_count += 1
            if perf_count == 0:
                chart_lines.append("  _(No performance topics listed)_")
        else:
            chart_lines.append("  _(No performance topics listed)_")

        chart_lines.append("") # Spacer line breakout block

        # 3. Compile Non-Performance List segment pulling name fields from OTHERS tab cache
        chart_lines.append("☕️ *Non-Performance Thread IDs*")
        try:
            cached_others = []
            # Read from the active global storage layout row structures matching your friend's cache
            from services.google_sheets import get_cached_values
            others_rows = get_cached_values(tab_name="OTHERS")
            
            if others_rows:
                for o_row in others_rows:
                    if o_row and str(o_row[0]).isdigit():
                        o_tid = int(o_row[0])
                        # Read index column 1 for the custom string title given to the sub-room
                        o_name = o_row[1].strip() if len(o_row) > 1 and o_row[1] else "Bonding/Misc Thread"
                        cached_others.append(f"• {o_tid} | *{o_name}*")
            
            if cached_others:
                chart_lines.extend(cached_others)
            else:
                chart_lines.append("  _(No non-performance topics listed)_")
                
        except Exception as cache_err:
            print(f"[WARN] Failed to read sub-tab parameters inside map: {cache_err}")
            chart_lines.append("  _(No non-performance topics listed)_")

        # Join text parameters together and print update
        final_text = "\n".join(chart_lines)
        await status_msg.edit_text(final_text, parse_mode="Markdown")

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to parse data directory channels: {str(e)}", parse_mode="Markdown")
        
def _extract_first_date_object(date_cell_text: str) -> datetime | None:
    """Helper parser to safely read the earliest date entry from multi-line text cells."""
    if not date_cell_text or not date_cell_text.strip():
        return None
    first_line = date_cell_text.strip().splitlines()[0].strip()
    date_only_match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3}\s+\d{4})', first_line)
    if not date_only_match:
        return None
    try:
        return datetime.strptime(date_only_match.group(1), "%d %b %Y")
    except Exception:
        return None

def _compile_checklist_notice_template(event_name: str, location: str, formatted_dates: str) -> str:
    """Standardized checklist layout template block for public announcements."""
    return (
        f"🎉✨ *SHOWTIME IS COMING!* ✨🎉\n"
        f"Only *7 days* to go — time for the final prep ritual! 🥁🔥\n\n"
        f"🎭 *Event*\n• {event_name}\n\n"
        f"📅 *Performance Date | Time*\n{formatted_dates}\n\n"
        f"📍 *Location*\n• {location}\n\n"
        f"\n"
        f"📝 *FINAL PREP CHECKLIST* 📝\n\n"
        f"*👀 Glasses & Contact Lens*\n"
        f"If you wear glasses, try your best to perform without them (e.g. wear contact lens). "
        f"Default is *no glasses* on stage — make sure you're comfortable before show day! 🤓➡️😎\n\n"
        f"*🪒 Shave Your Armpits*\n"
        f"We want the audience hypnotised by our performance, not our underarms 😌💨 "
        f"Please shave before the show!\n\n"
        f"*🎽 Costume Tips*\n"
        f"Our costumes are sleeveless and v-neck. Skip the bright-colored bras "
        f"(neon pink/yellow/rainbow 🌈🙅) — a black sports bra is the MVP. 🖤\n\n"
        f"*🦶 Barefoot Reminder*\n"
        f"Everyone performs *barefoot* — leave the socks backstage! 🧦❌\n\n"
        f"*💇 Hair Tying*\n"
        f"Long hair? Tie it up neat & tight — grab a buddy to help if needed! 🤝\n\n"
        f"*📺 Recap the Drum Score*\n"
        f"Rewatch the performance videos and drill that score one more time. Stay sharp! 🧠⚡\n\n"
        f"\n"
        f"💥 Let's make some NOISE — see you on stage! 🥁🔥"
    )

async def process_reminder_scan_cycle(bot, fallback_user_id: int = None) -> str:
    """Core logic scanner evaluating dates against a 7-day milestone window."""
    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
    except Exception as e:
        return f"❌ Failed to reach Google Sheets database: {e}"

    today_date = datetime.now(sg_tz).date()
    target_milestone = today_date + timedelta(days=7)
    
    counters = {"reminders": 0, "nudges": 0}
    
    for row in records:
        tid = row.get("THREAD ID")
        if not str(tid).isdigit():
            continue
            
        perf_cell = row.get("PERF DATE | TIME", "").strip()
        event_date_obj = _extract_first_date_object(perf_cell)
        
        # Check if the performance matches the 7-day milestone date window exactly
        if event_date_obj and event_date_obj.date() == target_milestone:
            status = row.get("STATUS", "").strip().upper()
            event_name = row.get("EVENT NAME", "Unnamed Event")
            location = row.get("LOCATION", "TBD")
            
            # Action A: Broadcast checklist notice if the performance is ACCEPTED
            if status == "ACCEPTED":
                date_lines = "\n".join([f"• {d.strip()}" for d in perf_cell.splitlines() if d.strip()])
                notice_text = _compile_checklist_notice_template(event_name, location, date_lines)
                
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=notice_text,
                        parse_mode="Markdown",
                        message_thread_id=int(tid)
                    )
                    counters["reminders"] += 1
                except Exception as err:
                    print(f"[ERROR] Failed to send automated public reminder to thread {tid}: {err}")
                    
            # Action B: Send private admin alerts if STATUS column is completely empty/blank
            elif status == "":
                nudge_text = (
                    f"⏰🚨 *Knock knock, boss!* 🚨⏰\n\n"
                    f"🎭 *{event_name}* (ID: `{tid}`) is exactly *7 days away*…\n"
                    f"but its status is still ⏳ *PENDING* in the sheet! 😱\n\n"
                    f"👉 Hop into the Command Centre → 🛠️ *Edit Performance* and flip it to "
                    f"✅ `ACCEPTED` or ❌ `REJECTED`, so I can fire the prep checklist on time! 🥁💨"
                )
                
                # 🧠 ROLE-DRIVEN ALERT TARGETS: only MAIN admins (chairperson /
                # vice chair / secretary / SDE) receive these nudges; secondary
                # admins (treasurer, logistics, business manager, PNP) do not.
                from services.google_sheets import get_alert_admin_ids
                admin_targets = set(get_alert_admin_ids())
                if fallback_user_id:
                    admin_targets.add(int(fallback_user_id))

                for target_chat_id in admin_targets:
                    try:
                        await bot.send_message(chat_id=target_chat_id, text=nudge_text, parse_mode="Markdown")
                    except Exception: pass
                        
                counters["nudges"] += 1

    return f"✅ Scan complete.\n• Public checklists sent: `{counters['reminders']}`\n• Admin escrow alert nudges triggered: `{counters['nudges']}`"

async def daily_reminder_cron_job(context: ContextTypes.DEFAULT_TYPE):
    """Automated daily callback runner wrapper hooked into the Application Job Queue scheduler loop."""
    print("[AUTOMATION] Executing daily morning 7-day reminder spreadsheet scan sequence...")
    log_summary = await process_reminder_scan_cycle(context.bot)
    print(f"[AUTOMATION] {log_summary}")

# 🧠 FIX: This helper function handles your manual execution without passing through the group decorator layer!
# async def execute_manual_test_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     status_msg = await update.message.reply_text("⏳ Processing live spreadsheet timeline analysis rules...")
#     try:
#         summary = await process_reminder_scan_cycle(context.bot, fallback_user_id=update.effective_user.id)
#         await status_msg.edit_text(summary)
#     except Exception as e:
#         await status_msg.edit_text(f"❌ Error encountered: `{str(e)}`", parse_mode="Markdown")

@admin_only
# async def manual_test_reminder_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Direct diagnostic command hook allowing admins to run validation tests manually via private dashboard."""
#     if update.effective_chat.type != "private":
#         return # 🤐 TOTAL PASSIVITY: Leaves the message completely untouched inside group channels

#     # 🛡️ Only verified dashboard admins may fire the reminder scan — stay silent otherwise.
#     from services.google_sheets import is_dashboard_admin
#     current_uid = update.effective_user.id
#     if not is_dashboard_admin(current_uid):
#         print(f"[SECURITY] Unauthorized /testremind attempt blocked for user ID: {current_uid}")
#         return

#     await execute_manual_test_scan(update, context)

# 🧠 NEW DM PORTAL GENERATOR: Prompts admin to select which event they want to trigger manual reminders for
async def initiate_remind_portal_via_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, success_banner: str = ""):
    """Prompts admin to select an event to trigger manual reminders cleanly inside a single persistent bubble window."""
    target_chat = update.effective_user
    query = update.callback_query
    active_dash_id = context.user_data.get("master_dash_id")
    
    prompt_text = ""
    if success_banner:
        prompt_text = f"{success_banner}\n\n"
    prompt_text += "🔔 *Manual Reminder Dispatch Center*\nSelect which performance topic thread you want to issue checklist reminders into:"

    try:
        from services.google_sheets import get_cached_records
        records = get_cached_records()
        if not records:
            text_empty = "📋 The performance sheet is currently empty."
            if query: await query.edit_message_text(text_empty)
            elif active_dash_id: await context.bot.edit_message_text(chat_id=target_chat.id, message_id=active_dash_id, text=text_empty)
            else: await context.bot.send_message(chat_id=target_chat.id, text=text_empty)
            return

        import re
        from datetime import datetime
        
        # 🎯 Sorting Logic Applied Here
        def get_sort_date(r):
            date_str = str(r.get("PERF DATE | TIME", "")).strip()
            if not date_str or date_str == "-": return datetime.min
            first_line = date_str.splitlines()[0].strip()
            match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
            if match:
                try: return datetime.strptime(match.group(1), "%d %b %Y")
                except ValueError: pass
            return datetime.min

        sorted_records = sorted(records, key=get_sort_date, reverse=True)
            
        buttons = []
        for row in sorted_records:
            tid = row.get("THREAD ID")
            if str(tid).isdigit():
                event_name = row.get("EVENT NAME", "Unnamed Event")
                buttons.append([InlineKeyboardButton(f"📢 Remind: {event_name} ({tid})", callback_data=f"MANUAL_REMIND_TID|{tid}")])
                
        buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
        markup = InlineKeyboardMarkup(buttons)
        
        # 🎯 FORCE IN-PLACE SINGLE BUBBLE ALWAYS
        if query:
            await query.edit_message_text(prompt_text, reply_markup=markup, parse_mode="Markdown")
            return

        if active_dash_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=target_chat.id, message_id=active_dash_id,
                    text=prompt_text, reply_markup=markup, parse_mode="Markdown"
                )
                return
            except Exception: pass

        msg = await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=markup, parse_mode="Markdown")
        context.user_data["master_dash_id"] = msg.message_id
        
    except Exception as e:
        err_msg = f"❌ Failed to build manual remind matrix: {e}"
        if query: await query.edit_message_text(err_msg)
        else: await context.bot.send_message(chat_id=target_chat.id, text=err_msg)

async def execute_manual_remind_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validates approval, blocks past/REJECTED topics, and routes inline escrow confirmation gates."""
    query = update.callback_query
    await query.answer() 
    
    try:
        callback_data = query.data 
        thread_id = int(callback_data.split("|")[1])
    except (IndexError, ValueError) as parse_err:
        await query.edit_message_text(f"❌ Error parsing callback metadata parameters: `{parse_err}`")
        return
    
    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        
        row_info = next(((idx, r) for idx, r in enumerate(records, start=2) if str(r.get("THREAD ID")) == str(thread_id)), None)
        if not row_info:
            await query.edit_message_text("❌ Error: Selected performance ID was not found inside the sheet records.")
            return
            
        row_number, row = row_info
        status = row.get("STATUS", "").strip().upper()
        event_name = row.get("EVENT NAME", "Unnamed Event")
        perf_cell = row.get("PERF DATE | TIME", "").strip()

        # GUARD 1: Block Expired Shows
        event_date_obj = _extract_first_date_object(perf_cell)
        today_date = datetime.now(sg_tz).date()
        if event_date_obj and event_date_obj.date() < today_date:
            banner = f"⏳ *Manual Remind Blocked: The performance '{event_name}' is already over! Cannot issue reminders into past dates.*"
            await initiate_remind_portal_via_dm(update, context, success_banner=banner)
            return

        # GUARD 2: Block REJECTED Bookings
        if status == "REJECTED":
            banner = (
                f"❌ *Manual Remind Blocked: The performance '{event_name}' is currently marked as REJECTED!*\n\n"
                f"👉 _Reminders are explicitly disabled. To proceed, please update the status to **ACCEPTED** first via the edit cockpit fields panel view._"
            )
            await initiate_remind_portal_via_dm(update, context, success_banner=banner)
            return

        # STEP 2 MID-FLOW INTERCEPTOR: If status is blank/PENDING, pause and offer inline approval
        if status != "ACCEPTED":
            context.user_data["remind_escrow_tid"] = thread_id
            context.user_data["remind_escrow_row"] = row_number
            
            escrow_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ YES, Approve & Broadcast", callback_data="REMIND_ESCROW_CONFIRM|YES")],
                [InlineKeyboardButton("❌ NO, Back to Topics", callback_data="REMIND_ESCROW_CONFIRM|NO")]
            ])
            
            await query.edit_message_text(
                text=(
                    f"⚠️ **STATUS NOTICE:** Cannot push reminders out to the public thread channel for *{event_name}* "
                    f"because its status is currently **{status or 'PENDING'}**.\n\n"
                    f"❓ Would you like to update its row status to **ACCEPTED** right now inside the database to authorize this public broadcast?"
                ),
                reply_markup=escrow_keyboard,
                parse_mode="Markdown"
            )
            return

        # Status is already ACCEPTED -> Run standard broadcast immediately
        date_lines = "\n".join([f"• {d.strip()}" for d in perf_cell.splitlines() if d.strip()])
        notice_text = _compile_checklist_notice_template(event_name, row.get("LOCATION", "TBD"), date_lines)
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=notice_text,
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        
        banner = f"✨ *Success! Manual checklist reminder layout has been successfully broadcasted and posted into topic thread {event_name} ({thread_id}).*"
        await initiate_remind_portal_via_dm(update, context, success_banner=banner)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Critical error during dispatch runtime loop: `{e}`", parse_mode="Markdown")

@admin_only
# async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Handle standard manual chat trigger command /remind securely."""
#     if update.effective_chat.type != "private":
#         return

#     from services.google_sheets import is_dashboard_admin
#     current_uid = update.effective_user.id
#     if not is_dashboard_admin(current_uid):
#         print(f"[SECURITY] Unauthorized command trigger blocked for user ID: {current_uid}")
#         return

#     # In-place tracking update configuration
#     context.user_data["master_dash_id"] = update.effective_message.message_id
#     await initiate_remind_portal_via_dm(update, context)

async def send_reminder(bot, chat_id, thread_id):
    """Send training reminder"""
    from utils.helpers import get_next_tuesday
    try:
        from config import ATT_FIRST_DATE_COL, ATT_POLL_ROW, ATT_DATE_ROW
        from services.google_sheets import get_attendance_ws as _get_ws, parse_sheet_date

        ws = _get_ws()
        poll_row = ws.row_values(ATT_POLL_ROW)
        date_row = ws.row_values(ATT_DATE_ROW)

        # Find the right-most date column that has a poll id (the latest poll sent).
        last_col = None
        for col in range(max(len(poll_row), len(date_row)), ATT_FIRST_DATE_COL - 1, -1):
            if col - 1 < len(poll_row) and (poll_row[col - 1] or "").strip():
                last_col = col
                break

        pretty_date = get_next_tuesday().strftime("%d %b %Y")
        if last_col and last_col - 1 < len(date_row):
            date_label = (date_row[last_col - 1] or "").strip()
            parsed = parse_sheet_date(date_label)
            pretty_date = parsed.strftime("%d %b %Y") if parsed else (date_label or pretty_date)

        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=(
                f"🥁 *DRUM ROLL, PLEASE…* 🥁\n\n"
                f"Training is *TOMORROW* — {pretty_date}! 🔥\n"
                f"💧 Bring water · 💪 bring energy · 🎶 bring the vibes\n\n"
                f"See you there, drummers! 👋😄"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")

async def handle_remind_escrow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes fast-track status override confirmations directly inside the single-bubble matrix."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("|")
    selection = parts[1]
    
    target_tid = context.user_data.pop("remind_escrow_tid", None)
    row_number = context.user_data.pop("remind_escrow_row", None)
    
    if not target_tid or not row_number:
        await query.edit_message_text("⚠️ Interrupted tracking context. Please re-open the Reminder panel.")
        return

    if selection == "NO":
        await initiate_remind_portal_via_dm(update, context)
        return

    sheet = get_gspread_sheet()
    status_col = SHEET_COLUMNS.index("STATUS") + 1
    sheet.update_cell(row_number, status_col, "ACCEPTED")
    invalidate_sheet_cache()
    
    updated_row = sheet.row_values(row_number)
    while len(updated_row) < len(SHEET_COLUMNS):
        updated_row.append("")
    row_data = dict(zip(SHEET_COLUMNS, updated_row))
    
    event_name = row_data.get("EVENT NAME", "Unnamed Event")
    perf_cell = row_data.get("PERF DATE | TIME", "").strip()
    date_lines = "\n".join([f"• {d.strip()}" for d in perf_cell.splitlines() if d.strip()])
    notice_text = _compile_checklist_notice_template(event_name, row_data.get("LOCATION", "TBD"), date_lines)
    
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=notice_text,
        parse_mode="Markdown",
        message_thread_id=target_tid
    )
    
    banner_alert = (
        f"🟢 *Success: Updated internal field [STATUS] inside the database registry to ACCEPTED!*\n"
        f"✨ *Success: Manual checklist reminder layout has been successfully broadcasted and posted into topic thread {event_name} ({target_tid})!*"
    )
    await initiate_remind_portal_via_dm(update, context, success_banner=banner_alert)

async def initiate_announce_portal_via_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, success_banner: str = ""):
    """Renders all available forum threads for announcement target selection inside a single bubble workspace window."""
    target_chat = update.effective_user
    query = update.callback_query
    active_dash_id = context.user_data.get("master_dash_id")

    prompt_text = ""
    if success_banner:
        prompt_text = f"{success_banner}\n\n"
    prompt_text += "📢 *Broadcast Announcement Center*\nSelect which active topic thread channel you want to broadcast an official announcement into:"

    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        buttons = []
        
        # Inject main landing channel configuration option safely
        buttons.append([InlineKeyboardButton("💬 Post to: General Chat (Main Channel)", callback_data="ANNOUNCE_TARGET|0")])
        
        if records:
            import re
            from datetime import datetime
            
            # 🎯 Sorting Logic Applied Here
            def get_sort_date(r):
                date_str = str(r.get("PERF DATE | TIME", "")).strip()
                if not date_str or date_str == "-": return datetime.min
                first_line = date_str.splitlines()[0].strip()
                match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
                if match:
                    try: return datetime.strptime(match.group(1), "%d %b %Y")
                    except ValueError: pass
                return datetime.min

            sorted_records = sorted(records, key=get_sort_date, reverse=True)

            for row in sorted_records:
                tid = row.get("THREAD ID")
                if str(tid).isdigit():
                    event_name = row.get("EVENT NAME", "Unnamed Event")
                    buttons.append([InlineKeyboardButton(f"📣 Post to: {event_name} ({tid})", callback_data=f"ANNOUNCE_TARGET|{tid}")])
                    
        try:
            from services.google_sheets import get_cached_values
            others_rows = get_cached_values(tab_name="OTHERS")
            if others_rows:
                for o_row in others_rows:
                    if o_row and str(o_row[0]).isdigit():
                        o_tid = int(o_row[0])
                        o_name = o_row[1].strip() if len(o_row) > 1 and o_row[1] else "Bonding/Misc Thread"
                        buttons.append([InlineKeyboardButton(f"☕ Post to: {o_name} ({o_tid})", callback_data=f"ANNOUNCE_TARGET|{o_tid}")])
        except Exception as err:
            print(f"[WARN] Failed to read OTHERS tab inside announcement matrix list template view parameters: {err}")

        buttons.append([InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")])
        markup = InlineKeyboardMarkup(buttons)

        if query:
            await query.edit_message_text(prompt_text, reply_markup=markup, parse_mode="Markdown")
            return

        if active_dash_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=target_chat.id, message_id=active_dash_id,
                    text=prompt_text, reply_markup=markup, parse_mode="Markdown"
                )
                return
            except Exception: pass

        msg = await context.bot.send_message(chat_id=target_chat.id, text=prompt_text, reply_markup=markup, parse_mode="Markdown")
        context.user_data["master_dash_id"] = msg.message_id

    except Exception as e:
        err_msg = f"❌ Failed to build announcement portal chart matrix: {e}"
        if query: await query.edit_message_text(err_msg)
        else: await context.bot.send_message(chat_id=target_chat.id, text=err_msg)

async def execute_manual_announcement_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercepts target clicks, checks validation gates, and puts dashboard bubble into input prompt text mode."""
    query = update.callback_query
    await query.answer()

    try:
        callback_data = query.data  
        target_tid = int(callback_data.split("|")[1])
    except (IndexError, ValueError) as parse_err:
        await query.edit_message_text(f"❌ Error parsing callback metadata parameters: `{parse_err}`")
        return

    # Skip validation layer security checks cleanly if targeted to General Chat room directly
    if target_tid != 0:
        try:
            sheet = get_gspread_sheet()
            records = sheet.get_all_records()
            perf_row = next((r for r in records if str(r.get("THREAD ID")) == str(target_tid)), None)
            
            if perf_row:
                status = perf_row.get("STATUS", "").strip().upper()
                event_name = perf_row.get("EVENT NAME", "Unnamed Event")
                perf_cell = perf_row.get("PERF DATE | TIME", "").strip()

                event_date_obj = _extract_first_date_object(perf_cell)
                today_date = datetime.now(sg_tz).date()
                if event_date_obj and event_date_obj.date() < today_date:
                    banner = f"⏳ *Broadcast Blocked: The performance '{event_name}' is already over! Cannot post text into expired topic timelines.*"
                    await initiate_announce_portal_via_dm(update, context, success_banner=banner)
                    return

                if status == "REJECTED":
                    banner = f"❌ *Broadcast Blocked: '{event_name}' is currently REJECTED! Official announcements are locked for cancelled bookings.*"
                    await initiate_announce_portal_via_dm(update, context, success_banner=banner)
                    return
        except Exception as err:
            print(f"[WARN] Failed validation scan inside announcement intercept: {err}")

    context.user_data["announcement_target_tid"] = target_tid
    context.user_data["modify_field"] = "ANNOUNCEMENT_TEXT_CAPTURE" 

    display_target_title = "General Chat (Main Channel)" if target_tid == 0 else f"Topic Thread ID: `{target_tid}`"
    prompt_text = (
        f"📢 *Broadcast New Announcement*\n"
        f"Target Destination: *{display_target_title}*\n\n"
        f"👉 _Type your announcement message content down below and press send:_ "
    )
    
    escape_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Topics List", callback_data="DASH_VIEW|LAUNCH_ANNOUNCE")],
        [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")]
    ])
    
    await query.edit_message_text(prompt_text, reply_markup=escape_keyboard, parse_mode="Markdown")

async def apply_announcement_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes announcement text, automatically shifts thread IDs, and triggers clean dashboard portal refreshes."""
    raw_text = update.message.text.strip()
    
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception: pass
        
    target_tid = context.user_data.pop("announcement_target_tid", None)
    active_dash_id = context.user_data.get("master_dash_id")
    context.user_data.pop("modify_field", None) 
    
    if target_tid is None or not active_dash_id:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Interrupted tracking context session parameters. Restart using cockpit panel view.")
        return

    # 🎯 FIX: IF TARGET IS 0, OMIT THE THREAD ID SO IT DROPS CLEANLY INTO GENERAL CHAT ROOM CHANNELS!
    actual_thread_id = None if target_tid == 0 else int(target_tid)

    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            message_thread_id=actual_thread_id,
            text=f"{raw_text}",
            parse_mode="Markdown"
        )
        dest_label = "General Chat" if target_tid == 0 else f"thread ID `{target_tid}`"
        banner_msg = f"✨ *Success: Announcement broadcast was successfully deployed and published directly into {dest_label}!*"
    except Exception as exc:
        banner_msg = f"❌ *Failed to broadcast announcement to topic room thread:* `{exc}`"

    await initiate_announce_portal_via_dm(update, context, success_banner=banner_msg)