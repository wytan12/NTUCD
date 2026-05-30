from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from utils.decorators import admin_only
from services.google_sheets import get_gspread_sheet, get_attendance_ws
from config import sg_tz, CHAT_ID, ADMIN_DM_USER_IDS
from datetime import datetime, timedelta
import re

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command inside Private DMs strictly."""
    if update.effective_chat.type != "private":
        return # 🤐 TOTAL PASSIVITY: Leaves the message completely untouched inside group channels
    await update.message.reply_text("👋 Hi!")

@admin_only
async def thread_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /threadid command safely."""
    if update.effective_chat.type != "private":
        return # 🤐 TOTAL PASSIVITY: No action and no deletion if typed inside a group topic thread channel

    # Only verified administrators can use it inside private DMs
    is_approved_admin = False
    current_uid = update.effective_user.id
    if isinstance(ADMIN_DM_USER_IDS, dict):
        if current_uid in ADMIN_DM_USER_IDS or str(current_uid) in ADMIN_DM_USER_IDS: is_approved_admin = True
    elif isinstance(ADMIN_DM_USER_IDS, (list, set)) and current_uid in ADMIN_DM_USER_IDS:
        is_approved_admin = True
        
    if is_approved_admin:
        await update.effective_message.reply_text("❗ Use `threadid` or `threads` inside our private administrative dashboard to print data directory grids.")

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
        f"📢 *Performance Reminder*\n\n"
        f"📍 *Event*\n• {event_name}\n\n"
        f"📅 *Performance Date | Time*\n{formatted_dates}\n\n"
        f"📌 *Location*\n• {location}\n\n"
        f"*📝 Final Preparation Notes*\n\n"
        f"*👀 Glasses & Contact Lens*\n"
        f"If you wear glasses, try your best to perform without them (e.g. wear contact lens). "
        f"Default is *no glasses* on stage. Make sure you're comfortable before show day.\n\n"
        f"*⬇️💪 Shave Your Armpits*\n"
        f"We want the audience to focus on our performance, not our underarms 🪒😌 \n"
        f"So please make sure to shave before the show!\n\n"
        f"*🎽 Costume Tips*\n"
        f"Our costumes are sleeveless and v-neck. Avoid wearing bright-colored bras "
        f"(neon pink/yellow/rainbow 🌈). A black sports bra is best.\n\n"
        f"*🦶 Barefoot Reminder*\n"
        f"Everyone will be performing *barefoot*. Don't forget!\n\n"
        f"*💇 Hair Tying*\n"
        f"If you have long hair, please tie it up neatly. "
        f"You can also ask someone to help if needed.\n\n"
        f"*📺 Recap the Drum Score*\n"
        f"Make sure to go through the performance videos again and recap the score "
        f"before the show. Stay sharp!"
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
                    f"⚠️ *Admin Escrow Alert Nudge* ⚠️\n\n"
                    f"The performance *{event_name}* (Thread ID: `{tid}`) is exactly *7 days away*, "
                    f"but its approval status is still completely **PENDING/BLANK** inside your sheet logs!\n\n"
                    f"👉 Please use `modify` in your DMs or open your desktop browser to update its status to "
                    f"`ACCEPTED` or `REJECTED` so the bot can handle reminders and rosters correctly."
                )
                
                # 🧠 CRASH-PROOF ID RESOLVER: Extract numerical IDs safely from config keys OR values
                admin_targets = set()
                if fallback_user_id:
                    admin_targets.add(int(fallback_user_id))
                    
                if isinstance(ADMIN_DM_USER_IDS, dict):
                    for k, v in ADMIN_DM_USER_IDS.items():
                        if str(k).isdigit(): admin_targets.add(int(k))
                        if str(v).isdigit(): admin_targets.add(int(v))
                elif isinstance(ADMIN_DM_USER_IDS, (list, set)):
                    for item in ADMIN_DM_USER_IDS:
                        if str(item).isdigit(): admin_targets.add(int(item))

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
async def execute_manual_test_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ Processing live spreadsheet timeline analysis rules...")
    try:
        summary = await process_reminder_scan_cycle(context.bot, fallback_user_id=update.effective_user.id)
        await status_msg.edit_text(summary)
    except Exception as e:
        await status_msg.edit_text(f"❌ Error encountered: `{str(e)}`", parse_mode="Markdown")

@admin_only
async def manual_test_reminder_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct diagnostic command hook allowing admins to run validation tests manually via private dashboard."""
    if update.effective_chat.type != "private":
        return # 🤐 TOTAL PASSIVITY: Leaves the message completely untouched inside group channels
    await execute_manual_test_scan(update, context)

# 🧠 NEW DM PORTAL GENERATOR: Prompts admin to select which event they want to trigger manual reminders for
async def initiate_remind_portal_via_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompts admin to select which event they want to trigger manual reminders for."""
    target_chat = update.effective_user
    status_loading = await context.bot.send_message(chat_id=target_chat.id, text="⏳ Fetching performance log records...")
    
    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        if not records:
            await status_loading.edit_text("📋 The performance sheet is currently empty.")
            return
            
        buttons = []
        for row in records:
            tid = row.get("THREAD ID")
            if str(tid).isdigit():
                event_name = row.get("EVENT NAME", "Unnamed Event")
                buttons.append([InlineKeyboardButton(f"📢 Remind: {event_name} ({tid})", callback_data=f"MANUAL_REMIND_TID|{tid}")])
                
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_NEW_PERF")])
        await status_loading.delete()
        
        await context.bot.send_message(
            chat_id=target_chat.id,
            text="🔔 *Manual Reminder Dispatch Center*\nSelect which performance topic thread you want to issue checklist reminders into:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_loading.edit_text(f"❌ Failed to build manual remind matrix: {e}")

async def execute_manual_remind_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validates approval before triggering checklist broadcast public postings."""
    query = update.callback_query
    await query.answer() # Acknowledge the click instantly so the button stops spinning
    await query.edit_message_text("⏳ Verifying sheet entry approval parameters...")
    
    # 🧠 EXTRACT THREAD ID: Parse the thread ID string directly from the callback payload
    try:
        callback_data = query.data # e.g. "MANUAL_REMIND_TID|12345"
        thread_id = int(callback_data.split("|")[1])
    except (IndexError, ValueError) as parse_err:
        await query.edit_message_text(f"❌ Error parsing callback metadata parameters: `{parse_err}`")
        return
    
    try:
        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        row = next((r for r in records if str(r.get("THREAD ID")) == str(thread_id)), None)
        
        if not row:
            await query.edit_message_text("❌ Error: Selected performance ID was not found inside the sheet records.")
            return
            
        status = row.get("STATUS", "").strip().upper()
        event_name = row.get("EVENT NAME", "Unnamed Event")
        
        if status != "ACCEPTED":
            display_status = f"`{status}`" if status else "*BLANK / PENDING*"
            await query.edit_message_text(
                f"⚠️ *Manual Remind Blocked* ⚠️\n\n"
                f"Cannot push reminders out to the public thread channel for *{event_name}* because its sheet status is currently {display_status}.\n\n"
                f"👉 Please use `modify` inside your dashboard to approve it first!",
                parse_mode="Markdown"
            )
            return

        perf_cell = row.get("PERF DATE | TIME", "").strip()
        date_lines = "\n".join([f"• {d.strip()}" for d in perf_cell.splitlines() if d.strip()])
        notice_text = _compile_checklist_notice_template(event_name, row.get("LOCATION", "TBD"), date_lines)
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=notice_text,
            parse_mode="Markdown",
            message_thread_id=thread_id
        )
        
        await query.edit_message_text(f"✅ *Success!* Manual checklist reminder layout has been successfully broadcasted and posted into topic thread *{event_name}* (`{thread_id}`).", parse_mode="Markdown")
        
    except Exception as e:
        await query.edit_message_text(f"❌ Critical error during dispatch runtime loop: `{e}`", parse_mode="Markdown")

@admin_only
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle standard manual chat trigger command /remind securely."""
    # 🤐 RULE 1: If typed inside group topics, remain 100% passive and leave text untouched
    if update.effective_chat.type != "private":
        return

    # 🛡️ RULE 2: STRICT PRIVATE GATEWAY - Only listed admins can execute it inside DMs
    is_approved_admin = False
    current_uid = update.effective_user.id
    
    if isinstance(ADMIN_DM_USER_IDS, dict):
        if current_uid in ADMIN_DM_USER_IDS or str(current_uid) in ADMIN_DM_USER_IDS: 
            is_approved_admin = True
    elif isinstance(ADMIN_DM_USER_IDS, (list, set)) and current_uid in ADMIN_DM_USER_IDS:
        is_approved_admin = True

    # If unverified stranger or non-admin attempts execution, drop thread silently
    if not is_approved_admin:
        print(f"[SECURITY] Unauthorized command trigger blocked for user ID: {current_uid}")
        return

    # 🚀 Valid Admin verified: Fire up the interactive selection buttons panel layout
    from handlers.admin_handlers import initiate_remind_portal_via_dm
    await initiate_remind_portal_via_dm(update, context)

async def send_reminder(bot, chat_id, thread_id):
    """Send training reminder"""
    from utils.helpers import get_next_tuesday
    try:
        ws = get_attendance_ws()
        from services.google_sheets import _find_or_create_header_rows
        _find_or_create_header_rows(ws)

        poll_row = ws.row_values(1)
        date_row = ws.row_values(2)

        max_len = max(len(poll_row), len(date_row))
        if len(poll_row) < max_len: poll_row += [""] * (max_len - len(poll_row))
        if len(date_row) < max_len: date_row += [""] * (max_len - len(date_row))

        last_col = None
        for idx in range(max_len, 0, -1):
            if idx == 1:
                if (poll_row[0] or "").strip().isdigit(): last_col = 1
                break
            if (poll_row[idx - 1] or "").strip():
                last_col = idx
                break

        if last_col and last_col > 1:
            date_label_short = (date_row[last_col - 1] or "").strip()
            pretty_date = date_label_short
            try:
                d, m = [int(x) for x in date_label_short.split("/")]
                today = datetime.now(sg_tz)
                year = today.year + (1 if today.month > m else 0)
                dt = datetime(year, m, d, tzinfo=sg_tz)
                pretty_date = dt.strftime("%d %b %Y")
            except Exception: pass
        else:
            pretty_date = get_next_tuesday().strftime("%d %b %Y")

        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"Reminder: There's training tomorrow {pretty_date}."
        )
    except Exception as e:
        print(f"[ERROR] Failed to send reminder: {e}")