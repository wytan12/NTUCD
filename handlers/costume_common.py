"""Shared plumbing for the Costume Tracker screens.

Everything here is used by BOTH `costume_inventory` and `costume_tracking`, or
is small enough that keeping it beside them avoids a circular import. Nothing in
this module talks to the sheet directly — it wraps, renders and formats.

Split out of the old single `logistics_handlers` module, which had grown to
~1200 lines covering two unrelated jobs (what the club owns vs who is holding
what).
"""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services.costume_sheets import Size, pant_size_label

_PAGE = 16
_SIZE_OPTS = list(Size)          # shared by the size editor and the issue config


async def _read(fn, *args, **kwargs):
    """Run a blocking sheets call in a thread. Returns (ok, value_or_error)."""
    try:
        return True, await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as e:                       # noqa: BLE001 — surfaced to the admin
        print(f"[LOGI][ERROR] {getattr(fn, '__name__', fn)} failed: {e}")
        return False, str(e)


async def _edit(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"[LOGI] edit skipped: {e}")


async def _fail(query, what: str, err: str, back: str = "LOGI|HOME") -> None:
    await _edit(query, f"⚠️ Couldn't load {what}.\n\n`{err[:200]}`\n\n_Try again in a moment._",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=back)]]))


async def _edit_dashboard(context, chat_id, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the one dashboard bubble from a *text* handler (no callback query).

    Falls back to a fresh message if the bubble is gone, so a typed name never
    disappears into nothing.
    """
    dash_id = context.user_data.get("master_dash_id")
    if dash_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=dash_id,
                                                text=text, reply_markup=kb,
                                                parse_mode="Markdown")
            return
        except Exception as e:
            print(f"[LOGI] dashboard edit failed: {e}")
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb,
                                   parse_mode="Markdown")


def _compact_size(label: str) -> str:
    """`'M - 170'` -> `'M-170'`; plain sizes pass through."""
    return "-".join(part.strip() for part in label.split("-")) if "-" in label else label


_SECTION_EMOJI = (("red", "🔴"), ("white", "⚪"), ("pants", "👖"),
                  ("old", "👖"), ("new", "✨"))


_ITEM_EMOJI = (("shirt", "👕"), ("pants", "👖"), ("waist", "🧣"),
               ("wrist", "🤲"), ("head", "🎀"))


def _emoji_for(text: str, table, default: str) -> str:
    low = text.lower()
    return next((e for key, e in table if key in low), default)


def _stock_dot(on_hand: int, total: int) -> str:
    """🟢 comfortable · 🟡 running low · 🔴 nothing left — same scale as Attendance Rate."""
    if on_hand <= 0:
        return "🔴"
    if total and on_hand / total < 0.34:
        return "🟡"
    return "🟢"


def _picked(label: str, selected: bool) -> str:
    """Mark a chosen option unmistakably.

    A leading "•" or trailing "✓" was too easy to miss in a row of identical
    buttons, so a selected option gets the same ✅ the accessory toggles use and
    an unselected one is padded to keep the row from jumping as it changes.
    """
    return f"✅ {label}" if selected else f"◻️ {label}"


def _set_icon(name: str) -> str:
    """Icon for a costume set. Sets come from the sheet, so an unknown one must
    still render — never borrow another category's icon."""
    return {"Red": "🔴", "White": "⚪", "Old": "👖", "New": "✨"}.get(str(name), "📦")


def _held_by_name(holdings) -> dict[str, list]:
    """`{name: [Holding, ...]}` — a member may legitimately hold several sets."""
    by_name: dict[str, list] = {}
    for h in holdings:
        by_name.setdefault(h.name, []).append(h)
    return by_name


def _holding_label(h) -> str:
    """Compact form — `Red · SM · PM - 170`.

    For admin lists and buttons, where several holdings appear at once and the
    shorthand is read by people who use this screen daily.
    """
    bits = [h.costume_set or "?"]
    if h.shirt_size:
        bits.append(f"S{h.shirt_size.value}")
    if h.pant_size:
        bits.append(f"P{pant_size_label(h.pant_size)}")
    return " · ".join(bits)


def _holding_label_full(h) -> str:
    """Spelled out — `Red costume set · Shirt M · Pants M - 170`.

    For the member-facing screen, where someone sees this once or twice a term
    and `SM`/`PM` reads like nothing at all.
    """
    bits = [f"{h.costume_set} costume set" if h.costume_set else "Costume set"]
    bits.append(f"Shirt {h.shirt_size.value}" if h.shirt_size else "no shirt")
    bits.append(f"Pants {pant_size_label(h.pant_size)}" if h.pant_size else "no pants")
    return " · ".join(bits)


def _stage(ud: dict) -> dict:
    return ud.setdefault("logi_stage", {"key": None, "items": {}})


def _stage_for(ud: dict, action: str, scope: str) -> dict:
    st = _stage(ud)
    if st["key"] != (action, scope):
        st = ud["logi_stage"] = {"key": (action, scope), "items": {}}
    return st


def _clear_stage(ud: dict) -> None:
    ud["logi_stage"] = {"key": None, "items": {}}
    ud.pop("logi_draft", None)
    ud.pop("logi_items", None)      # per-row item ticks belong to the staged list


def _stage_key(nick: str, swap_row) -> str:
    """Staging key. A swap and an extra set for the same person are separate."""
    return f"{nick}#{swap_row or 'new'}"
