"""Costume Tracker — inventory: what the club owns.

Reads Costume Overview (Main Inventory + the running table) and is the only
place the bot writes a stock quantity. Also owns Costume Size defaults and
registering a brand-new costume set.

Nothing here knows who is holding a costume — that is `costume_tracking`.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.ui import paginate, pagination_row, grid_row
import types

from services.costume_sheets import (Item, Size, add_stock_row,
                                     get_running_inventory, get_stock_rows,
                                     get_costume_sizes, set_costume_size, adjust_stock)
from handlers.costume_common import (_read, _edit, _fail, _edit_dashboard, _PAGE,
                                     _picked,
                                     _compact_size, _emoji_for, _set_icon,
                                     _ITEM_EMOJI, _SECTION_EMOJI, _stock_dot,
                                     _SIZE_OPTS)


_CELL = "LOGI|NOP"


_INV_PAGE = 10


_PANTS = "Pants"


def _cells(*labels) -> list:
    return grid_row(labels, _CELL)


def _wide(label: str) -> list:
    """A single full-width cell — used for item names.

    Telegram divides a row's width evenly between its buttons, so a name sharing
    a 7-column row was clipped to "Pan…". Giving the name its own row lets the
    metric rows use short labels that fit the narrow column.
    """
    return [InlineKeyboardButton(label, callback_data=_CELL)]


async def _render_list(query, banner: str = "") -> None:
    """Costume Overview is a menu, not a report.

    The per-item listing that used to live here is gone: both tables now have
    their own screen, so printing them again on the way in was duplication. This
    screen also makes no sheet reads at all.
    """
    text = ((f"{banner}\n\n" if banner else "")
            + "📦 *Costume Overview*\n\n"
            "• *Main Inventory* — stock totals; tap a quantity to change it\n"
            "• *Running Table* — Total / Out / On hand, per size\n\n"
            "_Added a new colour or item to the sheet? Rebuild the running "
            "table so it picks them up._")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Main Inventory", callback_data="LOGI|INV|0"),
         InlineKeyboardButton("📊 Running Table", callback_data="LOGI|RUN|0")],
        [InlineKeyboardButton("🔄 Rebuild running table", callback_data="LOGI|RBLD")],
        [InlineKeyboardButton("🔙 Back", callback_data="LOGI|HOME")],
    ])
    await _edit(query, text, kb)


async def _render_rebuild_confirm(query) -> None:
    """Rebuilding rewrites the whole block, so it asks first.

    It only ever writes FORMULAS — every number stays the sheet's own — but it
    does overwrite anything typed into those cells by hand, which is exactly
    what it is for.
    """
    ok, rows = await _read(get_stock_rows)
    items = len({i for i, _c, _z, _q, _p in rows}) if ok else 0
    lines = ["🔄 *Rebuild running table*", "",
             "This regenerates the whole block from Main Inventory:", "",
             f"• *{items}* item group(s), every colour and size it stocks",
             "• Total / Out / On hand formulas, freshly written",
             "• On hand colour-coded 🔴 none · 🟡 low · 🟢 fine", "",
             "_Use it after adding a colour, a size or a new item to the sheet._",
             "_It writes formulas only — the numbers stay the sheet's own._"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Rebuild it", callback_data="LOGI|RBLDOK")],
        [InlineKeyboardButton("🔙 Back", callback_data="LOGI|LIST")],
    ])
    await _edit(query, "\n".join(lines), kb)


async def _render_main_inventory(query, page: int, banner: str = "") -> None:
    """The stock list, with every Qty cell tappable.

    The other three cells stay inert — they are a layout device, since Telegram
    has no table markup. The quantity is the one thing on this screen you would
    ever want to change, so it is the one cell that does something: tapping it
    goes straight to that row's counter instead of walking back through
    item -> colour -> size to reach a row already in front of you.
    """
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "Main Inventory", rows, back="LOGI|LIST")
    chunk, page, pages = paginate(rows, page, _INV_PAGE)

    grid = [_cells("Item", "Colour", "Size", "Qty")]
    for item, colour, size, qty, _pairs in chunk:
        grid.append(_cells(item, colour, size)
                    + [InlineKeyboardButton(
                        f"{qty} \u270f\ufe0f",
                        callback_data=f"LOGI|SKZ|{item}|{colour}|{size}|{page}")])
    nav = pagination_row(page, pages, lambda p: f"LOGI|INV|{p}", _CELL)
    if nav:
        grid.append(nav)
    grid.append([InlineKeyboardButton("\u2795 Add item", callback_data="LOGI|ADDI"),
                 InlineKeyboardButton("\U0001f519 Back", callback_data="LOGI|LIST")])

    text = ((f"{banner}\n\n" if banner else "")
            + f"\U0001f4cb *Main Inventory*\n\n"
            + f"Stock totals exactly as typed in the sheet \u2014 {len(rows)} rows.\n"
            + "_Tap a quantity to change it._")
    await _edit(query, text, InlineKeyboardMarkup(grid))


async def _render_add_item(query, context, banner: str = "") -> None:
    """Ask for the new row as one typed line.

    One line rather than a four-step wizard: a new item's name and colour cannot
    come from buttons — there is nothing to pick them from yet — and typing
    "Sash, Gold, -, 12" is quicker than four screens for the one case buttons
    cannot cover.
    """
    ok, rows = await _read(get_stock_rows)
    items = ", ".join(dict.fromkeys(i for i, _c, _z, _q, _p in rows)) if ok else ""
    context.user_data["logi_add_item"] = True
    lines = [f"{banner}\n" if banner else "",
             "\u2795 *Add to Main Inventory*", "",
             "Send it as one line:", "",
             "`Item, Colour, Size, Quantity`", "",
             "Examples:",
             "`Sash, Gold, -, 12`",
             "`Shirt, Blue, M, 6`", "",
             f"_Already stocked: {items}_",
             "_Use `-` for the size when the item has no sizes._",
             "_A new ITEM also needs a matching column in COSTUME TRACKING "
             "before it can be issued._"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "\U0001f519 Cancel", callback_data="LOGI|INV|0")]])
    await _edit(query, "\n".join(x for x in lines if x != ""), kb)


async def handle_add_item_text(update, context) -> bool:
    """Consume a typed `Item, Colour, Size, Quantity` line. True if handled."""
    if not context.user_data.get("logi_add_item"):
        return False
    text = (update.effective_message.text or "").strip()
    if text.startswith("/"):
        return False
    context.user_data.pop("logi_add_item", None)

    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2:
        await update.effective_message.reply_text(
            "\u26a0\ufe0f I need at least an item and a colour, like "
            "`Sash, Gold, -, 12`.", parse_mode="Markdown")
        return True
    item, colour = parts[0], parts[1]
    size = parts[2] if len(parts) > 2 and parts[2] else "-"
    try:
        qty = int(parts[3]) if len(parts) > 3 and parts[3] else 0
    except ValueError:
        await update.effective_message.reply_text(
            f"\u26a0\ufe0f `{parts[3]}` is not a number \u2014 nothing was added.",
            parse_mode="Markdown")
        return True

    ok, note = await _read(add_stock_row, item, colour, size, qty)
    added, detail = note if ok and isinstance(note, tuple) else (False, note)
    try:
        await update.effective_message.delete()
    except Exception:                    # noqa: BLE001 — deletion is cosmetic
        pass

    banner = (f"\u2705 *Added* {item} {colour} {size} \u00d7 {qty} ({detail})."
              if added else f"\u26a0\ufe0f Couldn't add it: {detail}")
    if added:
        banner += "\n_Tap \U0001f504 Rebuild running table so it starts counting._"
    dash = context.user_data.get("master_dash_id")
    if dash:
        class _Q:                        # the inventory screen edits a message
            message = types.SimpleNamespace(message_id=dash,
                                            chat=update.effective_chat)
            from_user = update.effective_user
            async def answer(self, *a, **k):
                pass
        _Q.message.chat_id = update.effective_chat.id
        try:
            await _render_main_inventory(_Q(), 0, banner=banner)
            return True
        except Exception as e:           # noqa: BLE001 — fall back to a reply
            print(f"[LOGI][WARN] add-item redraw failed: {e}")
    await update.effective_message.reply_text(banner, parse_mode="Markdown")
    return True


async def _render_running_table(query, idx: int) -> None:
    ok, sections = await _read(get_running_inventory)
    if not ok:
        return await _fail(query, "the running table", sections, back="LOGI|LIST")
    if not sections:
        return await _fail(query, "the running table", "no sections found", back="LOGI|LIST")
    _chunk, idx, total = paginate(sections, idx, 1)
    sec = sections[idx]

    # New-pants labels are "M - 170"; the height alone is unambiguous under a
    # section already titled NEW PANTS, and "M-170" would clip in a 7th of a row.
    # Keep the size letter with the height ("S-160"): the number alone loses which
    # size it is, and the item name now has its own full-width row so the size
    # columns have room for five characters.
    labels = [_compact_size(s) for s in sec.sizes]
    grid = []
    for item in sec.items:
        # "Wrist Wrap (pairs)" -> "Wrist Wrap": the qualifier adds nothing here,
        # and the slot is better spent on the colour — two sets' wraps differ by
        # colour, not by name, so "Waist Wrap" alone is ambiguous across sections.
        # The section already names the item, so the full-width row names the
        # COLOUR — that is what identifies a thing now, and two colours of the
        # same item are what the reader is telling apart.
        colour = item.color.strip()
        icon = _set_icon(colour)
        name = item.name.split("(")[0].strip()
        grid.append(_wide(f"{icon} {colour}" if colour else f"• {name}"))
        if item.sized:
            # Size header repeats under each item's name rather than sitting once
            # at the top: it reads as that item's own column labels, and a
            # free-size item in between never inherits a header it can't use.
            grid.append(_cells("", *labels, "TTL"))
            grid.append(_cells("tot", *item.total, item.total_all))
            grid.append(_cells("out", *item.out, item.out_all))
            grid.append(_cells("left", *item.on_hand, item.on_hand_all))
        else:
            # Free-size items have nothing to spread across size columns.
            grid.append(_cells(f"tot {item.total_all}", f"out {item.out_all}",
                               f"left {item.on_hand_all}"))

    nav = pagination_row(idx, total, lambda p: f"LOGI|RUN|{p}", _CELL, unit="Section")
    if nav:
        grid.append(nav)
    grid.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|LIST")])

    title = sec.title.title()
    text = (f"📊 *Running Table* — {_emoji_for(sec.title, _SECTION_EMOJI, '📦')} *{title}*\n\n"
            f"*tot* = initial stock · *out* = issued out · "
            f"*left* = what's available.\n"
            f"_Auto-calculated; the bot never writes these._")
    await _edit(query, text, InlineKeyboardMarkup(grid))


def _stock_qty(rows, item: str, colour: str, size: str) -> int | None:
    for i, c, z, q, _p in rows:
        if i == item and c == colour and z == size:
            return q
    return None


def _colours_of(rows, item: str) -> list[str]:
    return list(dict.fromkeys(c for i, c, _z, _q, _p in rows if i == item))


def _sizes_of(rows, item: str, colour: str) -> list[tuple[str, int]]:
    return [(z, q) for i, c, z, q, _p in rows if i == item and c == colour]


async def _render_stock_leaf(query, item: str, colour: str, size: str,
                             banner: str = "", from_page: str | None = None) -> None:
    """The +/- counter for one (item, colour, size).

    Only ever reached by tapping a quantity in Main Inventory, so Back returns
    to the page that was being read — `from_page` carries it through the
    adjustments so a run of +1s does not lose your place.
    """
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "the stock list", rows, back="LOGI|INV|0")
    qty = _stock_qty(rows, item, colour, size)
    if qty is None:                      # the row was deleted in the sheet
        return await _render_main_inventory(query, int(from_page or 0),
                                            banner=f"⚠️ _{item} {colour} {size} is no "
                                                   f"longer in Main Inventory._")

    back = f"LOGI|INV|{from_page or 0}"
    tail = f"|{from_page}" if from_page is not None else ""
    step = lambda d: f"LOGI|SKADJ|{item}|{colour}|{size}|{d}{tail}"      # noqa: E731
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u221210", callback_data=step(-10)),
         InlineKeyboardButton("\u22125", callback_data=step(-5)),
         InlineKeyboardButton("\u22121", callback_data=step(-1))],
        [InlineKeyboardButton("+1", callback_data=step(1)),
         InlineKeyboardButton("+5", callback_data=step(5)),
         InlineKeyboardButton("+10", callback_data=step(10))],
        [InlineKeyboardButton("\U0001f519 Back", callback_data=back)],
    ])
    icon = _emoji_for(item, _ITEM_EMOJI, "\u2022")
    size_line = "" if str(size).strip() in ("", "-") else f" \u00b7 Size *{_compact_size(size)}*"
    head = f"{banner}\n\n" if banner else ""
    await _edit(query, f"{head}\u270f\ufe0f *Edit quantity*\n{icon} *{item}* \u00b7 "
                       f"{_set_icon(colour)} *{colour}*{size_line}\n\n"
                       f"Quantity in stock: *{qty}*\n\n_Tap to adjust._", kb)


async def _render_sizes(query, page: int) -> None:
    ok, sizes = await _read(get_costume_sizes)
    if not ok:
        return await _fail(query, "costume sizes", sizes)
    nicks = sorted(sizes)
    chunk, page, pages = paginate(nicks, page, _PAGE)
    filled = sum(1 for n in nicks
                 if any((sizes[n]["shirt"] or {}).values()) or sizes[n]["pants"])
    lines = [f"📏 *Default Costume Sizes*  _({filled}/{len(nicks)} filled)_", ""]
    for nick in chunk:
        sz = sizes[nick]
        shirts = " · ".join(f"{st} *{v.value}*" for st, v in (sz["shirt"] or {}).items() if v)
        p = sz["pants"].value if sz["pants"] else "—"
        lines.append(f"• {nick} — {shirts or 'no shirt size'} · Pants *{p}*")
    btns = [InlineKeyboardButton(f"✏️ {n}", callback_data=f"LOGI|SZE|{n}") for n in chunk]
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = pagination_row(page, pages, lambda p: f"LOGI|SIZE|{p}", _CELL)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|HOME")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(kb))


async def _render_size_edit(query, nick: str) -> None:
    ok, sizes = await _read(get_costume_sizes)
    if not ok:
        return await _fail(query, "costume sizes", sizes)
    sz = sizes.get(nick) or {"shirt": {}, "pants": None}
    per_set, p = sz.get("shirt") or {}, sz.get("pants")

    # One shirt row per costume set — a member's shirt size differs between them.
    lines = [f"📏 *{nick}*"]
    rows = []
    for st, cur in per_set.items():
        lines.append(f"👕 {st} Shirt: *{cur.value if cur else '—'}*")
        rows.append([InlineKeyboardButton(_picked(o.value, o is cur),
                                          callback_data=f"LOGI|SZ|{nick}|shirt:{st}|{o.value}")
                     for o in _SIZE_OPTS])
    lines.append(f"👖 Pants: *{p.value if p else '—'}*")
    rows.append([InlineKeyboardButton(_picked(o.value, o is p),
                                      callback_data=f"LOGI|SZ|{nick}|pants|{o.value}")
                 for o in _SIZE_OPTS])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|SIZE|0")])
    await _edit(query, "\n".join(lines) + "\n\nTap a size to change it:",
                InlineKeyboardMarkup(rows))
