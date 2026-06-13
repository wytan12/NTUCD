from telegram import Update
from telegram.ext import ContextTypes
from services.google_sheets import append_welcome_tea_id

WELCOME_TEA_MESSAGE = (
    "🎉 **Thank you for scanning the Welcome Tea QR code!**\n\n"
    "We've successfully recorded your information for the Welcome Tea event.\n\n"
    "We will disseminate more information nearer to the Welcome Tea event.\n\n"
    "_If you have any questions, feel free to reach out to the chairpersons!_"
)

async def handle_welcome_tea_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle QR code scan for Welcome Tea registration.
    
    Triggered when user clicks a link like: https://t.me/bot_username?start=welcome_tea
    This captures their Telegram ID and username, sends a thank you message,
    and stores the data in Google Sheets.
    """
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    
    # Store the Telegram ID in Google Sheets
    success = append_welcome_tea_id(user_id, username)
    
    # Send thank you message
    if success:
        await update.message.reply_text(WELCOME_TEA_MESSAGE, parse_mode="Markdown")
        print(f"[INFO] Welcome Tea registration successful for user {user_id} (@{username})")
    else:
        error_msg = (
            "❌ We encountered an issue saving your information. "
            "Please try again or contact an admin."
        )
        await update.message.reply_text(error_msg)
        print(f"[ERROR] Failed to register Welcome Tea user {user_id}")
