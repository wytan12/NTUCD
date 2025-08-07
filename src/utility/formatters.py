from datetime import datetime
from typing import List, Dict, Any

class MessageFormatter:
    """Handles all message formatting and templates"""
    
    # Message templates
    PERFORMANCE_OPPORTUNITY_TEMPLATE = (
        "📢 *Performance Opportunity*\n\n"
        "📍 *Event*\n• {event}\n\n"
        "📅 *Date | Time*\n{formatted_dates}\n\n"
        "📌 *Location*\n• {location}\n\n"
        "📝 *Performance Information:*\n{info}"
    )
    
    PERFORMANCE_SUMMARY_TEMPLATE = (
        "📢 *Performance Summary*\n\n"
        "📍 *Event*\n• {event}\n\n"
        "📅 *Confirmed Date | Time*\n{formatted_dates}\n\n"
        "📌 *Location*\n• {location}\n\n"
        "📝 *Performance Information:*\n{info}"
    )
    
    REMINDER_TEMPLATE = (
        "📢 *Performance Reminder*\n\n"
        "📍 *Event*\n• {event}\n\n"
        "📅 *Date | Time*\n{date_lines}\n\n"
        "📌 *Location*\n• {location}\n\n"
        "📝 *Final Preparation Notes*\n\n"
        "👀 *Glasses & Contact Lens*\n"
        "If you wear glasses, try your best to perform without them (e.g. wear contact lens). "
        "Default is *no glasses* on stage. Make sure you're comfortable before show day.\n\n"
        "⬇️💪 *Shave Your Armpits*\n"
        "We want the audience to focus on our performance, not our underarms 🪒😌 "
        "So please make sure to shave before the show!\n\n"
        "🎽 *Costume Tips*\n"
        "Our costumes are sleeveless and v-neck. Avoid wearing bright-colored bras "
        "(neon pink/yellow/rainbow 🌈). A black sports bra is best.\n\n"
        "🦶 *Barefoot Reminder*\n"
        "Everyone will be performing *barefoot*. Don't forget!\n\n"
        "💇 *Hair Tying*\n"
        "If you have long hair, please tie it up neatly. "
        "You can also ask someone to help if needed.\n\n"
        "📺 *Recap the Drum Score*\n"
        "Make sure to go through the performance videos again and recap the score before the show. Stay sharp!"
    )
    
    WELCOME_MESSAGE_TEMPLATE = (
        "Hi {name}, thanks for your request to join NTU Chinese Drums Welcome Tea Session!\n\n"
        "Please fill up the registration form:\n{form_link}\n\n"
        "Any issues feel free to contact chairperson Brandon @Brandonkjj or "
        "vice-chairperson Pip Hui @pip_1218 on telegram!"
    )
    
    # Error messages
    ERROR_MESSAGES = {
        'invalid_format': (
            "❌ Invalid input format. Use:\n*Event // Date // Location // Info (optional)*\n\n"
            "Example:\n`NTU Welcome Tea // 23 JUN 2025 8:00pm // NYA // Formal wear required`"
        ),
        'invalid_date_single': (
            "❌ Invalid *date/time* format.\n"
            "👉 Use formats like:\n"
            "- `23aug25 8:30pm`\n"
            "- `23aug 1430`\n"
            "- `23aug25`\n"
            "- `23aug`\n"
            "\n⚠️ Make sure your input uses letters for month (e.g. `aug`, not `08`)."
        ),
        'invalid_date_multiple': (
            "❌ One or more *date/time* entries are invalid.\n"
            "👉 Use correct comma `,` between entries and formats like:\n"
            "- `23aug 8pm, 24aug 9pm`\n"
            "- `23aug25 1430, 24aug25`\n"
            "\n⚠️ Use *letter months*, not numeric (e.g. `aug`, not `08`)."
        ),
        'thread_not_registered': "❌ This thread is not registered in the sheet.",
        'performance_rejected': "❌ This performance is already REJECTED. You cannot modify it.",
        'admin_only': "⛔ Only admins can use this command.",
        'topic_only': "⛔ This command must be used inside a topic thread.",
    }
    
    @staticmethod
    def format_date_lines(date_string: str) -> str:
        """Format date string into bulleted lines"""
        date_lines = []
        for entry in date_string.splitlines():
            entry = entry.strip()
            if not entry:
                continue
            try:
                # Try to parse and reformat
                dt = datetime.strptime(entry, "%d %b %Y | %I:%M%p")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                date_lines.append(formatted)
            except:
                # Fallback: just add bullet
                date_lines.append(f"• {entry}")
        return "\n".join(date_lines)
    
    @staticmethod
    def format_performance_opportunity(event: str, dates: str, location: str, info: str) -> str:
        """Format performance opportunity message"""
        formatted_dates = MessageFormatter.format_date_lines(dates)
        return MessageFormatter.PERFORMANCE_OPPORTUNITY_TEMPLATE.format(
            event=event,
            formatted_dates=formatted_dates,
            location=location,
            info=info.strip()
        )
    
    @staticmethod
    def format_performance_summary(event: str, confirmed_dates: str, location: str, info: str) -> str:
        """Format performance summary message"""
        formatted_dates = MessageFormatter.format_date_lines(confirmed_dates)
        return MessageFormatter.PERFORMANCE_SUMMARY_TEMPLATE.format(
            event=event,
            formatted_dates=formatted_dates,
            location=location,
            info=info.strip()
        )
    
    @staticmethod
    def format_reminder_message(event: str, confirmed_dates: str, location: str) -> str:
        """Format performance reminder message"""
        date_lines = "\n".join([f"• {d.strip()}" for d in confirmed_dates.splitlines() if d.strip()])
        return MessageFormatter.REMINDER_TEMPLATE.format(
            event=event,
            date_lines=date_lines,
            location=location
        )
    
    @staticmethod
    def format_welcome_message(name: str, form_link: str) -> str:
        """Format welcome message for new join requests"""
        return MessageFormatter.WELCOME_MESSAGE_TEMPLATE.format(
            name=name,
            form_link=form_link
        )
    
    @staticmethod
    def get_error_message(error_type: str) -> str:
        """Get predefined error message"""
        return MessageFormatter.ERROR_MESSAGES.get(error_type, "❌ An error occurred.")
    
    @staticmethod
    def format_poll_question_single_date(date: str) -> str:
        """Format poll question for single date"""
        return f"Are you interested in the performance on {date}?"
    
    @staticmethod
    def format_poll_question_multiple_dates() -> str:
        """Format poll question for multiple dates"""
        return "Which dates are you interested in?"
    
    @staticmethod
    def format_training_poll_question(date: str) -> str:
        """Format training attendance poll question"""
        return f"Are you joining the training on {date}?"

class KeyboardBuilder:
    """Builds inline keyboards for various bot interactions"""
    
    @staticmethod
    def build_topic_type_keyboard(thread_id: int):
        """Build keyboard for topic type selection"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("PERF", callback_data=f"topic_type|PERF|{thread_id}")],
            [InlineKeyboardButton("OTHERS", callback_data=f"topic_type|OTHERS|{thread_id}")]
        ])
    
    @staticmethod
    def build_confirmation_keyboard(thread_id: int):
        """Build keyboard for performance confirmation"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ACCEPT", callback_data=f"CONFIRM|{thread_id}|ACCEPT")],
            [InlineKeyboardButton("❌ REJECT", callback_data=f"CONFIRM|{thread_id}|REJECT")],
            [InlineKeyboardButton("🚫 CANCEL", callback_data=f"CONFIRM|{thread_id}|CANCEL")]
        ])
    
    @staticmethod
    def build_modify_field_keyboard(available_fields: List[str]):
        """Build keyboard for field modification selection"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        emoji_map = {
            "EVENT": "📍",
            "CONFIRMED DATE | TIME": "📅",
            "PROPOSED DATE | TIME": "📅",
            "LOCATION": "📌",
            "PERFORMANCE INFO": "📝",
            "STATUS": "📊",
        }
        
        buttons = []
        for field in available_fields:
            emoji = emoji_map.get(field, "")
            buttons.append([InlineKeyboardButton(f"{emoji} {field.title()}", callback_data=f"MODIFY|{field}")])
        
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="MODIFY|CANCEL")])
        return InlineKeyboardMarkup(buttons)
    
    @staticmethod
    def build_date_selection_keyboard(dates: List[str], thread_id: int, selected_indices: List[str] = None):
        """Build keyboard for date selection with checkboxes"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        if selected_indices is None:
            selected_indices = []
        
        buttons = []
        for i, date in enumerate(dates):
            is_selected = str(i) in selected_indices
            label = f"✅ {date}" if is_selected else date
            buttons.append([InlineKeyboardButton(label, callback_data=f"FINALDATE|{thread_id}|{i}")])
        
        buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data=f"FINALDATE|{thread_id}|CONFIRM")])
        return InlineKeyboardMarkup(buttons)

# Example usage in your handlers:
# from utils.formatters import MessageFormatter, KeyboardBuilder

# # In your performance handler:
# message_text = MessageFormatter.format_performance_opportunity(
#     event="NTU Welcome Tea",
#     dates="23 AUG 2025 | 8:00pm\n24 AUG 2025 | 9:00pm",
#     location="NYA",
#     info="Formal wear required"
# )

# keyboard = KeyboardBuilder.build_confirmation_keyboard(thread_id=123)
# await update.message.reply_text(message_text, reply_markup=keyboard, parse_mode="Markdown")