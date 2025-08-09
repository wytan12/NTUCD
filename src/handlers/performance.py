from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from models.constants import ConversationStates
from utils.formatters import MessageFormatter

class PerformanceHandlers:
    def __init__(self, sheets_service):
        """Initialize with sheets service dependency"""
        self.sheets_service = sheets_service
        # Store any global variables that were in the original code
        self.pending_questions = {}
    
    async def topic_type_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle PERF/EVENT/OTHERS selection"""
        query = update.callback_query
        await query.answer()

        _, selection, thread_id = query.data.split("|")
        thread_id = int(thread_id)

        # Delete the initial prompt if it exists
        prompt_id = context.chat_data.pop(f"init_prompt_{thread_id}", None)
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=query.message.chat.id, message_id=prompt_id)
            except Exception as e:
                print(f"[DELETE ERROR] {e}")

        context.user_data["thread_id"] = thread_id
        context.user_data["topic_type"] = selection

        if selection == "PERF":
            prompt = await query.message.chat.send_message(
                "📝 Please enter: *Event // Date // Location // Info (If any)*",
                parse_mode="Markdown", message_thread_id=thread_id
            )
            self.pending_questions["perf_input"] = prompt.message_id
            return ConversationStates.DATE

        elif selection == "DATE_ONLY":
            prompt = await query.message.chat.send_message(
                "📅 Please enter the *Performance Date* (e.g. 12 MAR 2025):",
                parse_mode="Markdown", message_thread_id=thread_id
            )
            self.pending_questions["date"] = prompt.message_id
            return ConversationStates.DATE
        
        elif selection == "OTHERS":
            # ✅ Now we can use the sheets service
            self.sheets_service.append_to_others_list(thread_id)
        
        await query.message.edit_text(f"Topic marked as {selection}. No further action.")
        return ConversationHandler.END
    
    # === Conversation steps ===
    async def parse_perf_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.delete()
        sheet = get_gspread_sheet()

        try:
            if "perf_input" in pending_questions:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=pending_questions.pop("perf_input")
                )
        except Exception as e:
            print(f"[WARNING] Failed to delete perf input prompt: {e}")
        
        # Delete any previously shown error message from the bot
        try:
            if "last_error" in context.chat_data:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.chat_data.pop("last_error")
                )
        except Exception as e:
            print(f"[WARNING] Failed to delete previous error message: {e}")

        # === Case 1: Retrying Date Only ===
        if "perf_temp" in context.user_data:
            raw_date = update.message.text.strip()
            try:
                formatted_dates = parse_and_format_dates(raw_date)
                date = "\n".join(formatted_dates)
            except ValueError as e:
                # Clean up older messages if exist
                for key in ["last_error", "invalid_input"]:
                    msg_id = context.chat_data.pop(key, None)
                    if msg_id:
                        try:
                            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                        except Exception as ex:
                            print(f"[WARNING] Failed to delete previous {key} message: {ex}")

                error_msg = await update.effective_chat.send_message(
                    str(e),
                    parse_mode="Markdown",
                    message_thread_id=context.user_data["perf_temp"]["thread_id"]
                )
                context.chat_data["last_error"] = error_msg.message_id
                context.chat_data["invalid_input"] = update.message.message_id
                return DATE

            # Restore previous info
            temp = context.user_data.pop("perf_temp")
            event = temp["event"]
            location = temp["location"]
            info = temp["info"]
            thread_id = temp["thread_id"]

            # Update sheet
            all_rows = sheet.get_all_values()
            for idx, row in enumerate(all_rows, start=1):
                if row and str(row[0]) == str(thread_id):
                    sheet.update(values=[[event, date, location, info, "", ""]], range_name=f"B{idx}:G{idx}")
                    break

        # === Case 2: Full Input ===
        else:
            raw_text = update.message.text.strip()
            parts = [p.strip() for p in raw_text.split("//")]
            print(f"[DEBUG] Parsed parts: {len(parts)} - {parts}")

            thread_id = context.user_data.get("thread_id")  # ✅ Define this early

            if len(parts) < 3 or any(not p for p in parts[:3]):
                error_msg = await update.effective_chat.send_message(
                "❌ Invalid input format. Use:\n*Event // Date // Location // Info (optional)*\n\n"
                "Example:\n`NTU Welcome Tea // 23 JUN 2025 8:00pm // NYA // Formal wear required`",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )

                # Clean up older messages if exist
                for key in ["last_error", "invalid_input"]:
                    msg_id = context.chat_data.pop(key, None)
                    if msg_id:
                        try:
                            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                        except Exception as e:
                            print(f"[WARNING] Failed to delete previous {key} message: {e}")

                context.chat_data["last_error"] = error_msg.message_id
                context.chat_data["invalid_input"] = update.message.message_id
                return DATE

            event, date, location = parts[0], parts[1], parts[2]
            info = parts[3] if len(parts) >= 4 else ""
            thread_id = context.user_data.get("thread_id")

            # Try to format date
            try:
                raw_date = date
                formatted_dates = parse_and_format_dates(raw_date)
                date = "\n".join(formatted_dates)
            except ValueError as e:
                date = raw_date
                # Save temp and still append to sheet
                context.user_data["perf_temp"] = {
                    "event": event,
                    "location": location,
                    "info": info,
                    "thread_id": thread_id
                }
                try:
                    sheet.append_row([thread_id, event, date, location, info, "", ""])
                    print("[DEBUG] Row appended with invalid date")
                except Exception as e2:
                    print(f"[ERROR] Failed to append row with invalid date: {e}")
    
                # Clean up previous errors
                for key in ["last_error", "invalid_input"]:
                    msg_id = context.chat_data.pop(key, None)
                    if msg_id:
                        try:
                            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
                        except Exception as ex:
                            print(f"[WARNING] Failed to delete previous {key} message: {ex}")

                # Show proper error message from parse_and_format_dates()
                error_msg = await update.effective_chat.send_message(
                    str(e),
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )
                context.chat_data["last_error"] = error_msg.message_id
                context.chat_data["invalid_input"] = update.message.message_id
                return DATE

            # Valid date → Append to sheet
            try:
                sheet.append_row([thread_id, event, date, location, info, "", ""])
                print("[DEBUG] Row appended successfully")
            except Exception as e:
                print(f"[ERROR] Failed to append row: {e}")

        # Format multiline Date | Time block
        date_lines = []
        for entry in date.splitlines(): 
            entry = entry.strip()
            if not entry:
                continue
            try:
                dt = datetime.strptime(entry, "%d %b %Y %H%M")
                formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                date_lines.append(formatted)
            except:
                date_lines.append(f"• {entry}")  # fallback if not parseable
        formatted_dates = "\n".join(date_lines)

        # Then build the final message
        template = (
            f"\U0001F4E2 *Performance Opportunity*\n\n"
            f"\U0001F4CD *Event*\n"
            f"• {event}\n\n"
            f"\U0001F4C5 *Date | Time*\n"
            f"{formatted_dates}\n\n"
            f"\U0001F4CC *Location*\n"
            f"• {location}\n\n"
            f"*Performance Information:*\n"
            f"{info.strip()}"
        )
        msg = await update.effective_chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
        await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
        context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id
        # Send interest poll (single/multi-choice)
        poll = await send_interest_poll(context.bot, update.effective_chat.id, thread_id, sheet)
        context.chat_data[f"interest_poll_msg_{thread_id}"] = poll["message_id"] if poll else None
        print(f"[DEBUG] Saved new poll message ID: {poll['message_id']}")

        return ConversationHandler.END
    
    # Handle button selection
    async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        parts = query.data.split("|")
        if len(parts) != 3:
            return
        _, thread_id, action = parts
        thread_id = int(thread_id)

        # 🧹 Delete previous ACCEPT/REJECT prompt
        msg_id = context.chat_data.pop(f"confirm_prompt_{thread_id}", None)
        if msg_id:
            try:
                await context.bot.delete_message(chat_id=query.message.chat.id, message_id=msg_id)
            except:
                pass

        sheet = get_gspread_sheet()
        records = sheet.get_all_records()
        row_number, row_data = None, None
        for idx, row in enumerate(records):
            if str(row["THREAD ID"]) == str(thread_id):
                row_number = idx + 2
                row_data = row
                break
        if not row_data:
            return

        if action == "CANCEL":
            # Unpin summary (if exists), and send back Performance Opportunity
            # old_msg_id = context.chat_data.get(f"summary_msg_{thread_id}")
            # if old_msg_id:
            #     try:
            #         await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=old_msg_id)
            #     except:
            #         pass
            return

        if action == "REJECT":
            # === Update status in GSheet ===
            sheet = get_gspread_sheet()
            records = sheet.get_all_records()
            for idx, row in enumerate(records):
                if str(row["THREAD ID"]) == str(thread_id):
                    row_index = idx + 2
                    sheet.update_cell(row_index, 7, "REJECTED")
                    break

            # === Send rejection message
            await query.message.chat.send_message("❌ Performance rejected. This topic will now be closed.", message_thread_id=thread_id)

            # ✅ Delay + delete topic
            await delete_topic_with_delay(context, chat_id=query.message.chat.id, thread_id=thread_id)
        
        if action == "ACCEPT":
            all_dates = row_data["PROPOSED DATE | TIME"].splitlines()
            context.chat_data[f"final_row_number_{thread_id}"] = row_number
            context.chat_data[f"final_all_dates_{thread_id}"] = all_dates
            context.chat_data[f"selected_dates_{thread_id}"] = []

            buttons = [[InlineKeyboardButton(text=d, callback_data=f"FINALDATE|{thread_id}|{i}")]
                    for i, d in enumerate(all_dates)]
            buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data=f"FINALDATE|{thread_id}|CONFIRM")])
            prompt = await query.message.chat.send_message(
                "🗓️ Select final date(s) to confirm:",
                reply_markup=InlineKeyboardMarkup(buttons),
                message_thread_id=thread_id
            )
            context.chat_data[f"final_prompt_{thread_id}"] = prompt.message_id

    
    async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        chat_id = msg.chat_id
        thread_id = msg.message_thread_id

        print("[DEBUG] /remind triggered.")
        print(f"[DEBUG] Thread ID: {thread_id}")

        # Delete the command message
        try:
            await msg.delete()
            print("[DEBUG] Deleted /remind command message.")
        except Exception as e:
            print(f"[WARNING] Failed to delete /remind command message: {e}")

        # Guard clause: must be inside a topic
        if not msg.is_topic_message:
            print("[DEBUG] Not a topic message. Ignoring.")
            return

        # Guard clause: exempted thread
        if thread_id in EXEMPTED_THREAD_IDS:
            print("[DEBUG] Thread is exempted. Skipping.")
            return

        try:
            sheet = get_gspread_sheet()
            records = sheet.get_all_records()

            for row in records:
                if str(row["THREAD ID"]) == str(thread_id):
                    status = row.get("STATUS", "").strip().upper()
                    if status != "ACCEPTED":
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Reminder can only be used *after confirmation*.",
                            parse_mode="Markdown",
                            message_thread_id=thread_id
                        )
                        print("[DEBUG] Status not ACCEPTED. Reminder skipped.")
                        return

                    # === Compose reminder message ===
                    date_str = row.get("CONFIRMED DATE | TIME", "").strip()
                    date_lines = "\n".join([f"• {d.strip()}" for d in date_str.splitlines() if d.strip()])
                    template = (
                        f"📢 *Performance Reminder*\n\n"
                        f"📍 *Event*\n• {row['EVENT']}\n\n"
                        f"📅 *Date | Time*\n{date_lines}\n\n"
                        f"📌 *Location*\n• {row['LOCATION']}\n\n"
                        f"*📝 Final Preparation Notes*\n\n"
                        f"*👀 Glasses & Contact Lens*\n"
                        f"If you wear glasses, try your best to perform without them (e.g. wear contact lens). Default is *no glasses* on stage. Make sure you're comfortable before show day.\n\n"
                        f"*⬇️💪 Shave Your Armpits*\n"
                        f"We want the audience to focus on our performance, not our underarms 🪒😌 So please make sure to shave before the show!\n\n"
                        f"*🎽 Costume Tips*\n"
                        f"Our costumes are sleeveless and v-neck. Avoid wearing bright-colored bras (neon pink/yellow/rainbow 🌈). A black sports bra is best.\n\n"
                        f"*🦶 Barefoot Reminder*\n"
                        f"Everyone will be performing *barefoot*. Don't forget!\n\n"
                        f"*💇 Hair Tying*\n"
                        f"If you have long hair, please tie it up neatly. You can also ask someone to help if needed.\n\n"
                        f"*📺 Recap the Drum Score*\n"
                        f"Make sure to go through the performance videos again and recap the score before the show. Stay sharp!"
                    )
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=template,
                        parse_mode="Markdown",
                        message_thread_id=thread_id
                    )
                    print("[DEBUG] Reminder message sent.")
                    return

            # ❌ Not found
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ This thread has not been registered.",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )
            print("[DEBUG] Thread ID not found in GSheet.")

        except Exception as e:
            print(f"[ERROR] Failed to execute /remind: {e}")
            
    # Handle final date selection
    async def final_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data.split("|")
        if len(data) != 3:
            return

        _, thread_id, selection = data
        thread_id = int(thread_id)

        print(f"[DEBUG] Callback received for thread_id={thread_id}, selection={selection}")
        print(f"[DEBUG] chat_data keys: {list(context.chat_data.keys())}")

        row_number = context.chat_data.get(f"final_row_number_{thread_id}")
        all_dates = context.chat_data.get(f"final_all_dates_{thread_id}", [])
        selected = context.chat_data.setdefault(f"selected_dates_{thread_id}", [])

        print(f"[DEBUG] row_number={row_number}")
        print(f"[DEBUG] all_dates={all_dates}")
        print(f"[DEBUG] currently selected={selected}")

        if selection == "CONFIRM":
            if not selected:
                return await query.message.reply_text("⚠️ Please select at least one date before confirming.")

            # === Cleanup UI messages ===
            try:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
                print("[DEBUG] Deleted final selection message.")
            except Exception as e:
                print(f"[WARNING] Failed to delete final date selection message: {e}")

            try:
                msg_id = context.chat_data.pop("confirm_prompt_msg_id", None)
                if msg_id:
                    await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
                    print(f"[DEBUG] Deleted ACCEPT/REJECT message: {msg_id}")
            except Exception as e:
                print(f"[WARNING] Failed to delete ACCEPT/REJECT message: {e}")

            # === Update sheet ===
            value = "\n".join([all_dates[i] for i in sorted(map(int, selected))])
            sheet = get_gspread_sheet()
            sheet.update_cell(row_number, 6, value)  # Column F = 6
            sheet.update_cell(row_number, 7, "ACCEPTED")  # Column G = 7

            # === Refresh row ===
            updated_row = sheet.row_values(row_number)
            while len(updated_row) < 7:
                updated_row += [""] * (7 - len(updated_row))

            event, proposed, location, info, confirmed, status = updated_row[1:7]

            # === Format confirmed date
            date_lines = []
            for line in confirmed.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    dt = datetime.strptime(line, "%d %b %Y | %I:%M%p")
                    formatted = f"• {dt.strftime('%d %b %Y').upper()} | {dt.strftime('%I:%M%p').lower()}"
                except:
                    formatted = f"• {line}"
                date_lines.append(formatted)

            # === Format summary
            template = (
                f"📢 *Performance Summary*\n\n"
                f"📍 *Event*\n"
                f"• {event}\n\n"
                f"📅 *Confirmed Date | Time*\n"
                f"{chr(10).join(date_lines)}\n\n"
                f"📌 *Location*\n"
                f"• {location}\n\n"
                f"📝 *Performance Information:*\n"
                f"{info.strip()}"
            )

            # === Unpin previous summary (if any), then pin new summary
            try:
                old_summary_id = context.chat_data.pop(f"summary_msg_{thread_id}", None)
                if old_summary_id:
                    await context.bot.unpin_chat_message(chat_id=query.message.chat.id, message_id=old_summary_id)
                    print(f"[DEBUG] Unpinned old summary: {old_summary_id}")
            except Exception as e:
                print(f"[WARNING] Failed to unpin previous summary: {e}")

            # ✅ Send new summary
            msg = await query.message.chat.send_message(template, parse_mode="Markdown", message_thread_id=thread_id)
            await context.bot.pin_chat_message(chat_id=query.message.chat.id, message_id=msg.message_id, disable_notification=True)
            context.chat_data[f"summary_msg_{thread_id}"] = msg.message_id

            return

        # === Handle toggling checkboxes
        if selection not in selected:
            selected.append(selection)
        else:
            selected.remove(selection)

        new_buttons = []
        for i, d in enumerate(all_dates):
            is_selected = str(i) in selected
            label = f"✅ {d}" if is_selected else d
            new_buttons.append([InlineKeyboardButton(text=label, callback_data=f"FINALDATE|{thread_id}|{i}")])
        new_buttons.append([InlineKeyboardButton("✅ Confirm Selection", callback_data=f"FINALDATE|{thread_id}|CONFIRM")])

        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
