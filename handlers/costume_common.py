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

from services.costume_sheets import (Size, pant_size_label, item_colour,
                                     NONE_SIZE)

_PAGE = 16
_ROSTER_PAGE = 20        # member lists page like Take Attendance does
_ISSUE_PAGE = 10         # the Issue lists carry a line of text each, so
                         # they page shorter than a plain button grid
_DIVIDER_MIN = 10        # below this many people, dividers cost more rows
                         # than they save: on a six-name list you are
                         # looking for ONE known name, and three headings
                         # just push the names further apart
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


# Sections are items now ("SHIRT", "WAIST WRAP"), so the section and item
# tables became the same lookup.
_SECTION_EMOJI = (("shirt", "👕"), ("pants", "👖"), ("waist", "🧣"),
                  ("wrist", "🤲"), ("head", "🎀"))


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
    """Dot for a colour. Colours come from the sheet, so an unknown one must
    still render — never borrow another colour's dot."""
    return {"Red": "🔴", "White": "⚪", "Black": "⚫", "Yellow": "🟡",
            "Blue": "🔵", "Green": "🟢"}.get(str(name).strip(), "▪️")


def _tag_group(tag: str) -> str:
    """Category of a tag: `SU3` -> `SU`, `JEG` -> `JEG`, `` -> ``.

    The year is kept in the bracket beside a name but dropped from the divider,
    so Year 3 and Year 4 seniors sit under one heading instead of two.
    """
    return (tag or "").rstrip("0123456789")


def divider_row(group: str, noop: str) -> list:
    """The `─── JU ───` separator Take Attendance uses, so the two rosters read
    the same way. Inert: it is a heading drawn with a button."""
    from telegram import InlineKeyboardButton
    return [InlineKeyboardButton(f"─── {group or '—'} ───", callback_data=noop)]


def tagged_rows(people, label_for, callback_for, noop: str, per_row: int = 2,
                total: int | None = None):
    """Lay out `(name, tag)` pairs the way Take Attendance lays out its roster.

    Two to a row, with a `─── SU ───` divider when the category changes — but
    only once the list is long enough to be worth signposting (`total`, which
    defaults to the number of people passed, is the WHOLE list rather than this
    page, so a long roster keeps its dividers on every page).

    Shared so the Issue roster, the holders list and both recipient pickers
    cannot drift into three slightly different looks.
    """
    from telegram import InlineKeyboardButton
    show_dividers = (len(people) if total is None else total) >= _DIVIDER_MIN
    rows, buf, prev = [], [], None
    for name, tag in people:
        group = _tag_group(tag)
        if show_dividers and prev is not None and group != prev:
            if buf:
                rows.append(buf)
                buf = []
            rows.append(divider_row(group, noop))
        prev = group
        buf.append(InlineKeyboardButton(label_for(name, tag),
                                        callback_data=callback_for(name)))
        if len(buf) == per_row:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return rows


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
    bits = []
    if h.shirt_size:
        bits.append(f"{h.shirt_colour} S{h.shirt_size.value}")
    if h.pant_size:
        bits.append(f"{h.pant_colour} P{pant_size_label(h.pant_size)}")
    return " · ".join(bits) or "accessories only"


def _holding_label_full(h) -> str:
    """Spelled out — `Red costume set · Shirt M · Pants M - 170`.

    For the member-facing screen, where someone sees this once or twice a term
    and `SM`/`PM` reads like nothing at all.
    """
    bits = [f"{h.shirt_colour} shirt {h.shirt_size.value}" if h.shirt_size else "no shirt",
            f"{h.pant_colour} pants {pant_size_label(h.pant_size)}"
            if h.pant_size else "no pants"]
    for label, colour in (("waist wrap", h.waist), ("wrist wrap", h.wrist),
                          ("head band", h.head)):
        if colour and colour != NONE_SIZE:
            bits.append(f"{colour.lower()} {label}")
    return " · ".join(bits)


def _item_button_label(h, key: str, fallback: str) -> str:
    """`Shirt M`, `Pants L - 180`, `Waist Wrap` — the piece AND its size.

    The size belongs on the button rather than in a header line above it: the
    header would repeat sizes for pieces the person is not even choosing, and
    the tap target is where you actually need to know which shirt you mean.
    """
    colour = item_colour(h, key)
    tag = "" if colour in ("", NONE_SIZE) else f"{colour} "
    if key == "shirt" and h.shirt_size:
        return f"{tag}Shirt {h.shirt_size.value}"
    if key == "pants" and h.pant_size:
        return f"{tag}Pants {pant_size_label(h.pant_size)}"
    return f"{tag}{fallback}"


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
