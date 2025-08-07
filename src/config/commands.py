from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChatAdministrators
from config.settings import CHAT_ID

class BotCommands:
    """Manages bot command registration and scopes"""
    
    # Define admin-only commands
    ADMIN_COMMANDS = [
        BotCommand("start", "Test the bot and grab chat ID"),
        BotCommand("threadid", "Get the thread ID of current topic"),
        BotCommand("poll", "Create attendance poll in Attendance Topic"),
        BotCommand("modify", "Modify performance summary details"),
        BotCommand("remind", "Send performance reminder with preparation notes"),
        BotCommand("confirmation", "Accept or reject performance details"),
    ]
    
    # Define public commands (available to all users)
    PUBLIC_COMMANDS = [
        BotCommand("verify", "Verify your matriculation number to join"),
        BotCommand("help", "Show available commands"),
    ]

async def set_admin_commands(application):
    """Register commands that only admins can see in their command menu"""
    await application.bot.set_my_commands(
        commands=BotCommands.ADMIN_COMMANDS,
        scope=BotCommandScopeChatAdministrators(chat_id=CHAT_ID)
    )
    print(f"[INFO] Registered {len(BotCommands.ADMIN_COMMANDS)} admin commands")

async def set_public_commands(application):
    """Set commands for regular users"""
    await application.bot.set_my_commands(
        commands=BotCommands.PUBLIC_COMMANDS,
        scope=BotCommandScopeDefault()
    )
    print(f"[INFO] Set {len(BotCommands.PUBLIC_COMMANDS)} public commands")

async def set_all_commands(application):
    """Set up all command scopes during bot startup - called from main.py"""
    await set_admin_commands(application)
    await set_public_commands(application)
    print("[INFO] All bot commands configured successfully")