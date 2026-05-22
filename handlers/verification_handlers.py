import os
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from services.google_sheets import (
    matric_valid, 
    update_user_id_in_sheet
)   
from utils.constants import ASK_MATRIC, pending_users

async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send Welcome Tea details to every new join-request applicant via DM.

    Stores the raw ChatJoinRequest in pending_users so start_verification can
    approve it once the user passes the matric check.
    """
    user = update.chat_join_request.from_user
    #FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header"

    print(f"Join request received from {user.first_name}")

    # Always send the form
    # First message
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"Hi {user.first_name}, Welcome!!🥁\n\n"
                "<b>NTU Chinese Drums' Welcome Tea Session</b>\n"
                "<b>Date:</b> 19th August 2025 (Tuesday)\n"
                "<b>Time:</b> 1830 – 2130 (GMT+8)\n"
                "<b>Venue:</b> <a href='https://goo.gl/maps/7yqc3EfYNE92'>Nanyang House Foyer</a>\n"
                "<b>Dress Code:</b> Comfortable & Casual (we generally go barefoot for our practices/performances, "
                "so preferably wear slippers – but covered shoes are fine too!)\n\n"
                "🍱 <b>Dinner is provided</b> and time is allocated to eat, so no need to dabao. You can come straight from class!\n\n"
                "<b>🗺 Video Guide to Nanyang House</b>\n"
                "▶️ <b>Red Bus (Hall 2) Video Guide:</b>\n"
                "<a href='https://drive.google.com/file/d/1PWvvD4kmmnYbFOL0AzE25NEiQ2983ZJl/view?usp=drive_link'>Watch here</a>\n"
                "▶️ <b>Blue Bus (Hall 6) Video Guide:</b>\n"
                "<a href='https://drive.google.com/file/d/1ZFmAQHcFQL6VpNzO87UG6KB0u0FuOAlh/view?usp=drive_link'>Watch here</a>\n"
                "📄 <b>PDF Guide to Nanyang House:</b>\n"
                "<a href='https://drive.google.com/file/d/1pO1GoNn4MReqFXqBUowyZPL7EJqKpmHb/view?usp=drive_link'>View PDF</a>\n\n"
            ),
            parse_mode=ParseMode.HTML
        )  

        # Send registration form link
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"🌸 To make our Welcome Tea run smoothly, we’d love it if you could fill in the sign-up form below 📝💛\n"
                f"<a href='https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header'>Registration Form</a>\n"
                f"(you can ignore this if you’ve already done so ✔️)\n\n"
                f"💬 If you have any queries, feel free to contact:\n"
                f"👉 Chairperson Brandon: @Brandonkjj\n"
                f"👉 Vice-chairperson Pip Hui: @pip_1218\n\n"
                f"👋 See you next Tuesday! 🎉"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print("Could not message user:", e)

    # ✅ Store their pending join request
    pending_users[user.id] = update.chat_join_request

async def start_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the /verify conversation — only works in DM.

    Rejects users who have no pending join request to prevent misuse.
    Transitions to ASK_MATRIC state on success.
    """
    user_id = update.message.from_user.id

    if user_id not in pending_users:
        await update.message.reply_text(
            "❌ You don't have a pending join request or it has already been approved. Please request to join the group first\n\n"
            "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
        )
        return ConversationHandler.END

    await update.message.reply_text("Please enter your NTU matriculation number (case sensitive) to verify. e.g U2512345F")
    return ASK_MATRIC

async def handle_matric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate the submitted matriculation number against the Welcome Tea sheet.

    Approves the join request and records the Telegram user ID in the sheet on
    success.  Stays in ASK_MATRIC state so the user can retry on failure.
    """
    user_id = update.message.from_user.id
    matric = update.message.text.strip()

    if user_id not in pending_users:
        await update.message.reply_text(
            "❌ You don't have a pending join request or it has already been approved. Please request to join the group first.\n\n"
            "Any issues feel free to contact chairperson Brandon @Brandonkjj or vice-chairperson Pip Hui @pip_1218 on telegram!"
        )
        return ConversationHandler.END

    join_request = pending_users[user_id]

    if matric_valid(matric):
        await join_request.approve()
        update_user_id_in_sheet(matric, user_id)
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
        # Stay in ASK_MATRIC state
        return ASK_MATRIC

