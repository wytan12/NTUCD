import json
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ChatMember

from config.settings import GOOGLE_CREDENTIALS_JSON
from models.constants import ConversationStates
from utils.decorators import admin_only
from utils.google_sheets import get_gspread_sheet, get_gspread_sheet_welcome_tea

# Global state for pending users
pending_users = {}

class MembershipHandlers:
    
    @staticmethod
    async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle join requests - send form and instructions to user"""
        user = update.chat_join_request.from_user
        FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header"

        print(f"Join request received from {user.first_name}")

        try:
            # Send welcome message with form
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"Hi {user.first_name}, thanks for your request to join NTU Chinese Drums Welcome Tea Session!\n\n"
                    f"Please fill up the registration form:\n{FORM_LINK}\n\n"
                    "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
                )
            )
            
            # Send event details
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "NTU Chinese Drums' Welcome Tea Session\n\n"
                    "Date: 19th August 2025 (Tuesday)\n"
                    "Time: 1830-2130 (GMT+8)\n"
                    "Venue: Nanyang House Foyer (https://goo.gl/maps/7yqc3EfYNE92)\n"
                    "Dress Code: Comfortable & Casual (we generally go barefoot for our practices / performances so preferably wear slippers, but covered shoes are fine too!)\n\n"
                    "Dinner is provided and time is allocated to eat it so no need to takeaway any food, you can come here straight away!\n\n"
                    "For more information, check out NTU Chinese Drums Instagram: \n"
                    "https://www.instagram.com/ntu_chinesedrums?igsh=ZnR2dWswcnpxbmM5"
                )
            )
            
            # Send location guide
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "Written guide to Nanyang House: \n\n"
                    "Closest bus stops to Nanyang House are Hall 2 (red) and Hall 6 (blue)!\n\n"
                    "Take to either bus stop and follow the map below to reach the crossroads; you should see a road + sheltered walkway that slowly inclines upwards.\n\n"
                    "When you walk up the sheltered walkway, you will find a LONG ASS stair on the right. Go UP the stairs until you reach the top! (cArdio 😮‍💨)\n\n"
                    "You should see a concrete/stoneish path; walk forward until you see the clear glass doors on the right, then go through the door (DON'T GO UP ANY STAIRS) and out the opposite door where you'll see a zebra crossing thing leading to a little opening into a building; just head straight ahead and we are there!!\n\n"
                    "Video guide from Hall 2 bus stop to Nanyang House if you are taking red bus: \n"
                    "https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link \n\n"
                    "Video guide from Hall 6 bus stop to Nanyang House if you are taking blue bus: \n"
                    "https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link \n\n"
                    "PDF Guide to Nanyang House: \n"
                    "https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link"
                )
            )
        except Exception as e:
            print("Could not message user:", e)

        # Store their pending join request
        pending_users[user.id] = update.chat_join_request

    @staticmethod
    async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the verification process for users"""
        user_id = update.message.from_user.id

        if user_id not in pending_users:
            await update.message.reply_text(
                "❌ You don't have a pending join request or it has already been approved. Please request to join the group first\n\n"
                "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
            )
            return ConversationHandler.END

        await update.message.reply_text("Please enter your NTU matriculation number (case sensitive) to verify. e.g U2512345F")
        return ConversationStates.ASK_MATRIC

    @staticmethod
    async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle matriculation number verification"""
        user_id = update.message.from_user.id
        matric = update.message.text.strip()

        if user_id not in pending_users:
            await update.message.reply_text(
                "❌ You don't have a pending join request or it has already been approved. Please request to join the group first.\n\n"
                "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
            )
            return ConversationHandler.END

        join_request = pending_users[user_id]

        if MembershipHandlers._matric_valid(matric):
            await join_request.approve()
            MembershipHandlers._update_user_id_in_sheet(matric, user_id)
            await update.message.reply_text(
                "✅ Matric number and attendance verified. You have been approved. Welcome!"
            )
            pending_users.pop(user_id, None)
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ We couldn't verify your matric number or attendance. Please check your entry and try again.\n\n"
                "🔁 Please enter your NTU matriculation number (case sensitive) again e.g U2512345F:\n\n"
                "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
            )
            return ConversationStates.ASK_MATRIC

    @staticmethod
    async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle manually added new members"""
        for member in update.message.new_chat_members:
            user_id = member.id
            name = member.first_name or member.last_name

            print(f"[INFO] ✅ New member joined: {name} ({user_id})")
            print(f"[DEBUG] Telegram user object: is_bot={member.is_bot}, full_name={member.full_name}")

            if MembershipHandlers._user_already_in_timeline(user_id):
                print(f"[INFO] User ID {user_id} already exists in timeline — skipping fallback insert.")
            else:
                # Fallback row → use name for both name & nickname
                fallback_row = {
                    "Your Full Name (according to matric card)": name,
                    "What name or nickname do you prefer to be called? ": name
                }
                MembershipHandlers._copy_user_to_timeline(fallback_row, user_id)

    @staticmethod
    async def handle_member_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle member status changes (join/leave)"""
        status_change = update.chat_member
        old_status = status_change.old_chat_member.status
        new_status = status_change.new_chat_member.status
        user = status_change.new_chat_member.user
        
        print(f"[DEBUG] Status change for {user.full_name} ({user.id}): {old_status} ➝ {new_status}")

        # Detect first join
        if old_status == "left" and new_status == "member":
            print(f"[INFO] 🎉 User {user.full_name} ({user.id}) has joined the group for the first time.")
            
            if not MembershipHandlers._user_already_in_timeline(user.id):
                fallback_row = {
                    "Your Full Name (according to matric card)": user.full_name,
                    "What name or nickname do you prefer to be called? ": user.full_name
                }
                MembershipHandlers._copy_user_to_timeline(fallback_row, user.id)
            else:
                print(f"[INFO] User {user.id} already exists in sheet — skip adding.")
        
        # Detect leave (either voluntarily or kicked)
        elif old_status in ("member", "administrator") and new_status in ("left", "kicked"):
            print(f"[INFO] 🚪 User {user.full_name} ({user.id}) has left the group.")
            success = MembershipHandlers._mark_user_left_in_sheet(user.id)
            print(f"[INFO] Marked as 'Left' in sheet: {success}")

    # Private helper methods
    @staticmethod
    def _matric_valid(matric_number: str) -> bool:
        """Check if matriculation number is valid and user attended"""
        sheet = get_gspread_sheet_welcome_tea()
        rows = sheet.get_all_records()

        for row in rows:
            matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
            attendance = str(row.get("Attendance", "")).strip()

            if matric_in_row == matric_number.strip().upper():
                return attendance == "1"

        return False

    @staticmethod
    def _update_user_id_in_sheet(matric_number: str, telegram_user_id: int):
        """Update user ID in the welcome tea sheet"""
        sheet = get_gspread_sheet_welcome_tea()
        rows = sheet.get_all_records()

        for idx, row in enumerate(rows, start=2):
            matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
            if matric_in_row == matric_number.strip().upper():
                # Find User ID column
                user_id_col = None
                header = sheet.row_values(1)
                for i, col_name in enumerate(header, start=1):
                    if col_name.strip().lower() == "user id":
                        user_id_col = i
                        break

                if user_id_col:
                    sheet.update_cell(idx, user_id_col, str(telegram_user_id))
                    print(f"[INFO] User ID {telegram_user_id} saved for {matric_number} in row {idx}.")
                    MembershipHandlers._copy_user_to_timeline(row, telegram_user_id)
                else:
                    print("[ERROR] 'User ID' column not found in sheet.")
                return

        print("[WARN] Matric number not found when trying to update User ID.")

    @staticmethod
    def _copy_user_to_timeline(welcome_row: dict, telegram_user_id: int):
        """Copy user info to timeline sheet"""
        others_sheet = get_gspread_sheet("PERFORMER Info")
        name = welcome_row.get("Your Full Name (according to matric card)", "").strip()
        nickname = welcome_row.get("What name or nickname do you prefer to be called? ", "").strip()

        new_row = [name, nickname, str(telegram_user_id), "Join", ""]
        others_sheet.append_row(new_row, value_input_option="USER_ENTERED")
        print(f"[INFO] Copied to PERFORMER Info List: {new_row}")

    @staticmethod
    def _user_already_in_timeline(user_id: int) -> bool:
        """Check if user already exists in timeline"""
        others_sheet = get_gspread_sheet("PERFORMER Info")
        all_rows = others_sheet.get_all_records()
        for row in all_rows:
            if str(row.get("User ID", "")).strip() == str(user_id):
                return True
        return False

    @staticmethod
    def _mark_user_left_in_sheet(user_id: int) -> bool:
        """Mark user as left in the sheet"""
        sheet = get_gspread_sheet("PERFORMER Info")
        records = sheet.get_all_records()
        header = sheet.row_values(1)

        # Get column indexes
        user_id_col = header.index("User ID") + 1
        status_col = header.index("Status") + 1 if "Status" in header else None
        leave_date_col = header.index("Leave Date") + 1 if "Leave Date" in header else None

        if not (user_id_col and status_col and leave_date_col):
            print("[ERROR] Missing required columns: 'User ID', 'Status', or 'Leave Date'")
            return False

        for idx, row in enumerate(records, start=2):
            if str(row.get("User ID", "")).strip() == str(user_id):
                sheet.update_cell(idx, status_col, "Left")
                leave_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.update_cell(idx, leave_date_col, leave_time)
                return True

        print(f"[WARN] User ID {user_id} not found in sheet.")
        return False