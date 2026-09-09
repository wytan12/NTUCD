"""Member-facing costume screen — `/costume`.

Ordinary members are not dashboard admins and never see the Cockpit, but a
costume passed from one member to another happens without an admin present, so
it is the one event the bot otherwise cannot see. This gives members a way to
record it themselves.

**Transfer only, by design.** A transfer is net-zero for stock — it closes the
giver's row and opens the receiver's, so the costume stays counted as out and a
mistake is self-correcting. A *return* decreases `Out` and would make the sheet
claim stock that isn't physically back, so returns stay with logistics, who are
standing there when the costume is handed over anyway.

Callback namespace `MYCOS|…`, whitelisted in `global_button_security_check`
(main.py) — without that entry every button here answers "Access Denied",
because the gate blocks all callbacks from non-dashboard-admins.

Every callback re-checks that the ledger row actually belongs to the caller.
Callback data is client-supplied and a crafted `MYCOS|T|<row>` would otherwise
let one member transfer another member's costume.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import sg_tz

from utils.ui import paginate, pagination_row
from services.costume_sheets import (get_open_holdings, get_costume_sizes,
                                     ITEM_FIELDS, items_out, describe_items,
                                     release_items, issue_items_to)
from handlers.costume_common import (_read, _edit, _holding_label_full, _picked,
                                     _item_button_label, _PAGE)

_CELL = "MYCOS|NOP"


async def _my_nickname(user_id: int) -> str | None:
    """Tele ID -> nickname, the key the costume ledger is written in."""
    from services.google_sheets import get_nickname_by_user_id
    ok, nick = await _read(get_nickname_by_user_id, user_id)
    return nick if ok else None


async def _my_holdings(user_id: int):
    """`(nickname, [Holding, ...])` for the caller. Nickname is None if unknown."""
    nick = await _my_nickname(user_id)
    if not nick:
        return None, []
    ok, holdings = await _read(get_open_holdings)
    if not ok:
        return nick, []
    return nick, [h for h in holdings if h.name.strip().lower() == nick.strip().lower()]


def _row_of(holdings, row: str):
    return next((h for h in holdings if str(h.row) == str(row)), None)


def _home_text(nick: str, holdings) -> tuple[str, InlineKeyboardMarkup]:
    if not holdings:
        return ("👕 *My Costume*\n\n"
                f"Hi {nick}! You have no costume on loan right now.\n\n"
                "_If you think this is wrong, check with the logistics team._",
                InlineKeyboardMarkup([]))

    lines = [f"👕 *My Costume* — {nick}", "",
             f"You currently have *{len(holdings)}* "
             f"{'sets' if len(holdings) > 1 else 'set'}:", ""]
    rows = []
    for h in holdings:
        lines.append(f"• {_holding_label_full(h)}")
        lines.append(f"  _issued for {h.event}_")
        rows.append([InlineKeyboardButton(
            f"🔁 Pass on my {h.costume_set or ''} set".replace("  ", " "),
            callback_data=f"MYCOS|T|{h.row}")])
    lines += ["", "_Passed it to someone else? Record it here so the club knows "
                  "who has it._", "",
              "_Returning it? Hand it to the logistics team — they'll mark it._"]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_my_costume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/costume` — DM only, shows what this member currently has on loan."""
    message = update.effective_message
    if not message or update.effective_chat.type != "private":
        return
    nick, holdings = await _my_holdings(update.effective_user.id)
    if nick is None:
        await message.reply_text(
            "👕 *My Costume*\n\nI couldn't find you in the member list, so I can't "
            "look up your costume.\n\n_Ask the logistics team to add your Telegram "
            "ID to MEMBER INFO._", parse_mode="Markdown")
        return
    text, kb = _home_text(nick, holdings)
    sent = await message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    context.user_data["mycos_msg_id"] = sent.message_id


def _picked_items(ud: dict, row: str, holding) -> list[str]:
    """Which pieces this member is passing on — nothing until they say so.

    Same rule as the admin transfer screen: handing over a whole set is the
    exception, and a pre-ticked list would sign away pieces they still have.
    """
    sel = ud.setdefault("mycos_items", {})
    if row not in sel:
        sel[row] = []
    return sel[row]


async def _render_items(query, context, row: str, holding) -> None:
    """People hand over a shirt or a pair of pants, not always the whole set."""
    chosen = _picked_items(context.user_data, row, holding)
    available = items_out(holding)
    lines = ["🔁 *Pass on*",
             f"_{holding.costume_set or 'Costume'} set · issued for {holding.event}_", "",
             "What are you passing on?"]
    btns = [InlineKeyboardButton(
        _picked(_item_button_label(holding, key, label), key in chosen),
        callback_data=f"MYCOS|I|{row}|{key}")
        for key, _hdr, label in ITEM_FIELDS if key in available]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if chosen:
        rows.append([InlineKeyboardButton("➡️ Who has it?",
                                          callback_data=f"MYCOS|IA|{row}")])
        if len(chosen) < len(available):
            lines += ["", "_The rest stays with you._"]
    else:
        rows.append([InlineKeyboardButton("⚠️ Pick at least one item",
                                          callback_data="MYCOS|NOP")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="MYCOS|HOME")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_recipients(query, row: str, holding, page: int,
                             what: str = "") -> None:
    ok, sizes = await _read(get_costume_sizes)
    names = sorted(n for n in (sizes or {}) if n.strip().lower() != holding.name.strip().lower())
    chunk, page, pages = paginate(names, page, _PAGE)

    lines = ["🔁 *Pass on*", "", f"*{what or _holding_label_full(holding)}*", "",
             f"_issued for {holding.event}_", "", "Who has it now?"]
    btns = [InlineKeyboardButton(n, callback_data=f"MYCOS|TO|{row}|{n}") for n in chunk]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = pagination_row(page, pages, lambda p: f"MYCOS|TP|{row}|{p}", _CELL)
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="MYCOS|HOME")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


def _hand_over(holding, keys, to: str) -> bool:
    """Close the member's claim on `keys` and open the receiver's, in one thread.

    Two writes, not one: releasing alone would drop the pieces out of `Out` as
    though they came back to the club, when they only changed hands.
    """
    release_items(holding, keys, "transferred", to)
    issue_items_to(holding, keys, to)
    return True


async def _notify_logistics(context, giver: str, to: str, what: str, event: str) -> None:
    """DM the logistics team that a member moved a costume between themselves.

    A member hand-over is the one costume event no admin witnesses — the sheet
    records it, but nobody is watching the sheet. Without this the first anyone
    hears of it is when the wrong person turns up at collection.

    Never allowed to break the transfer: the write already succeeded by the time
    this runs, so a failed DM (Telegram forbids messaging someone who has never
    started the bot) is logged and dropped, not raised.
    """
    from services.google_sheets import get_logistics_admin_ids
    stamp = datetime.now(sg_tz).strftime("%d/%m/%Y")
    text = (f"🔁 *Costume hand-over*\n\n"
            f"*{giver}*  ➜  *{to}*\n"
            f"{what}\n\n"
            f"_for {event} · {stamp}_\n"
            f"_Recorded by {giver} with /costume._")
    try:
        ids = await asyncio.to_thread(get_logistics_admin_ids)
    except Exception as e:
        print(f"[LOGI][WARN] could not resolve logistics admins: {e}")
        return
    for uid in ids:
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown")
        except Exception as e:
            print(f"[LOGI][WARN] hand-over notice to {uid} failed: {e}")


async def member_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    verb = parts[1] if len(parts) > 1 else "HOME"
    if verb == "NOP":
        await query.answer()
        return
    await query.answer()

    nick, holdings = await _my_holdings(update.effective_user.id)
    if nick is None:
        return await _edit(query, "👕 *My Costume*\n\nI couldn't find you in the "
                                  "member list.", InlineKeyboardMarkup([]))

    if verb == "HOME":
        text, kb = _home_text(nick, holdings)
        return await _edit(query, text, kb)

    # Everything below acts on a specific row — it must be one of THEIRS.
    row = parts[2] if len(parts) > 2 else ""
    holding = _row_of(holdings, row)
    if holding is None:
        text, kb = _home_text(nick, holdings)
        return await _edit(query, "⚠️ _That costume is no longer yours to pass on._\n\n"
                                  + text, kb)

    if verb == "T":
        return await _render_items(query, context, row, holding)
    if verb == "I":                      # I|<row>|<item> — tick one piece
        key = parts[3]
        chosen = _picked_items(context.user_data, row, holding)
        chosen.remove(key) if key in chosen else chosen.append(key)
        return await _render_items(query, context, row, holding)
    if verb == "IA":
        if not _picked_items(context.user_data, row, holding):
            return await _render_items(query, context, row, holding)
        return await _render_recipients(
            query, row, holding, 0,
            describe_items(holding, _picked_items(context.user_data, row, holding)))
    if verb == "TP":
        return await _render_recipients(
            query, row, holding, int(parts[3]),
            describe_items(holding, _picked_items(context.user_data, row, holding)))
    if verb == "TO":                     # confirm before writing
        to = parts[3]
        keys = _picked_items(context.user_data, row, holding)
        what = describe_items(holding, keys)
        rest = ("" if len(keys) == len(items_out(holding))
                else "\n\n_The rest stays with you._")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Yes, {to} has it", callback_data=f"MYCOS|OK|{row}|{to}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"MYCOS|T|{row}")],
        ])
        return await _edit(query, f"🔁 *Confirm*\n\nPass to *{to}*?\n\n"
                                  f"*{what}*\n_from {_holding_label_full(holding)}_"
                                  f"{rest}\n\n"
                                  f"_{to} becomes responsible for it. "
                                  f"This doesn't return it to the club._", kb)
    if verb == "OK":
        to = parts[3]
        await _edit(query, f"⏳ Recording…", InlineKeyboardMarkup([]))
        keys = list(_picked_items(context.user_data, row, holding))
        ok, _res = await _read(_hand_over, holding, keys, to)
        if not ok:
            return await _edit(query, "⚠️ Couldn't record that just now — please try "
                                      "again in a moment.",
                               InlineKeyboardMarkup([[InlineKeyboardButton(
                                   "🔙 Back", callback_data="MYCOS|HOME")]]))
        await _notify_logistics(context, nick, to, describe_items(holding, keys),
                                holding.event)
        context.user_data.get("mycos_items", {}).pop(row, None)
        _nick2, fresh = await _my_holdings(update.effective_user.id)
        text, kb = _home_text(nick, fresh)
        return await _edit(query, f"✅ *Done* — *{to}* now has it.\n\n" + text, kb)

    text, kb = _home_text(nick, holdings)
    return await _edit(query, text, kb)
