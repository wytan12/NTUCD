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
from services.costume_sheets import (Item, Size, get_running_inventory, get_stock_rows,
                                     get_costume_sizes, set_costume_size, adjust_stock)
from handlers.costume_common import (_read, _edit, _fail, _edit_dashboard, _PAGE,
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


async def _render_list(query) -> None:
    """Costume Overview is a menu, not a report.

    The per-item listing that used to live here is gone: both tables now have
    their own screen, so printing them again on the way in was duplication. This
    screen also makes no sheet reads at all.
    """
    text = ("📦 *Costume Overview*\n\n"
            "• *Main Inventory* — stock totals exactly as typed in the sheet\n"
            "• *Running Table* — Total / Out / On hand, per size\n\n"
            "_Both come straight from the Logistics sheet._")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Main Inventory", callback_data="LOGI|INV|0"),
         InlineKeyboardButton("📊 Running Table", callback_data="LOGI|RUN|0")],
        [InlineKeyboardButton("✏️ Update Stock", callback_data="LOGI|SKP|0")],
        [InlineKeyboardButton("🔙 Back", callback_data="LOGI|HOME")],
    ])
    await _edit(query, text, kb)


async def _render_main_inventory(query, page: int) -> None:
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "Main Inventory", rows, back="LOGI|LIST")
    chunk, page, pages = paginate(rows, page, _INV_PAGE)

    grid = [_cells("Item", "Set", "Size", "Qty")]
    for item, st, _color, size, qty in chunk:
        grid.append(_cells(item, st, size, qty))
    nav = pagination_row(page, pages, lambda p: f"LOGI|INV|{p}", _CELL)
    if nav:
        grid.append(nav)
    grid.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|LIST")])

    text = (f"📋 *Main Inventory*\n\n"
            f"Stock totals exactly as typed in the sheet — {len(rows)} rows.\n"
            f"_Tap ✏️ Update Stock on the previous screen to change a quantity._")
    await _edit(query, text, InlineKeyboardMarkup(grid))


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
    labels = [_compact_size(s).split("-")[-1].strip() for s in sec.sizes]
    grid = []
    for item in sec.items:
        # "Wrist Wrap (pairs)" -> "Wrist Wrap": the qualifier adds nothing here.
        name = item.name.split("(")[0].strip()
        icon = _emoji_for(item.name, _ITEM_EMOJI, "•")
        grid.append(_wide(f"{icon} {name}"))
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

    title = sec.title.replace(" COSTUME SET", " SET").title()
    text = (f"📊 *Running Table* — {_emoji_for(sec.title, _SECTION_EMOJI, '📦')} *{title}*\n\n"
            f"*tot* = initial stock · *out* = issued out · "
            f"*left* = what's available.\n"
            f"_Auto-calculated; the bot never writes these._")
    await _edit(query, text, InlineKeyboardMarkup(grid))


def _stock_groups(rows, category: str):
    """`{set: {item: [(size, qty), ...]}}` for one category, in sheet order."""
    groups: dict[str, dict[str, list]] = {}
    for item, st, _color, size, qty in rows:
        is_pants = item.lower() == _PANTS.lower()
        if (category == "pants") != is_pants:
            continue
        groups.setdefault(st, {}).setdefault(item, []).append((size, qty))
    return groups


def _stock_qty(rows, st: str, item: str, size: str) -> int | None:
    for i, s, _c, z, q in rows:
        if i == item and s == st and z == size:
            return q
    return None


def _stock_category(item: str) -> str:
    return "pants" if item.lower() == _PANTS.lower() else "costume"


def _stock_back(rows, st: str, item: str, from_size_screen: bool = False) -> str:
    """Back target that skips any step which had only one option.

    Single-option screens are skipped on the way in (a pants set has only
    "Pants"; a free-size item has only "-"), so a Back button pointing at one
    would bounce the admin straight forward again and look broken.
    """
    items = {i for i, s, _c, _z, _q in rows if s == st}
    sizes = [z for i, s, _c, z, _q in rows if s == st and i == item]
    if not from_size_screen and len(sizes) > 1:
        return f"LOGI|SKI|{st}|{item}"
    if len(items) > 1:
        return f"LOGI|SKS|{st}"
    return f"LOGI|SKC|{_stock_category(item)}"


async def _render_stock_pick(query, _page: int = 0, banner: str = "") -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👕 Costume Set", callback_data="LOGI|SKC|costume")],
        [InlineKeyboardButton("👖 Pants", callback_data="LOGI|SKC|pants")],
        [InlineKeyboardButton("🔙 Back", callback_data="LOGI|LIST")],
    ])
    head = f"{banner}\n\n" if banner else ""
    await _edit(query, f"{head}✏️ *Update Stock*\n\nWhat are you counting?\n\n"
                       "• *Costume Set* — shirt, waist wrap, wrist wrap, head band\n"
                       "• *Pants* — stocked separately as Old / New\n"
                       "• *New Costume Set* — register a set the club just bought\n\n"
                       "_Writes to Main Inventory; On hand recalculates itself._", kb)


async def _render_stock_sets(query, category: str) -> None:
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "the stock list", rows, back="LOGI|LIST")
    groups = _stock_groups(rows, category)
    if not groups:
        return await _fail(query, "that category", "no matching inventory rows",
                           back="LOGI|SKP|0")
    btns = [InlineKeyboardButton(f"{_set_icon(st)} {st}",
                                 callback_data=f"LOGI|SKS|{st}") for st in groups]
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|SKP|0")])
    word = "costume set" if category == "costume" else "pants"
    await _edit(query, f"✏️ *Update Stock*\n\nWhich {word}?", InlineKeyboardMarkup(kb))


async def _render_stock_items(query, st: str) -> None:
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "the stock list", rows, back="LOGI|SKP|0")
    items = {}
    for item, s, _c, size, qty in rows:
        if s == st:
            items.setdefault(item, []).append((size, qty))
    if not items:
        return await _render_stock_pick(query)

    # A set with a single item (pants) has nothing to choose — skip this screen.
    if len(items) == 1:
        only = next(iter(items))
        return await _render_stock_sizes(query, st, only)

    category = _stock_category(next(iter(items)))
    btns = []
    for item, sizes in items.items():
        icon = _emoji_for(item, _ITEM_EMOJI, "•")
        total = sum(q for _s, q in sizes)
        btns.append(InlineKeyboardButton(f"{icon} {item} ({total})",
                                         callback_data=f"LOGI|SKI|{st}|{item}"))
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"LOGI|SKC|{category}")])
    await _edit(query, f"✏️ *Update Stock* — *{st}*\n\nWhich item?",
                InlineKeyboardMarkup(kb))


async def _render_stock_sizes(query, st: str, item: str) -> None:
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "the stock list", rows, back="LOGI|SKP|0")
    sizes = [(z, q) for i, s, _c, z, q in rows if s == st and i == item]
    if not sizes:
        return await _render_stock_items(query, st)
    # Free-size items ("-") have nothing to choose — go straight to the counter.
    if len(sizes) == 1:
        return await _render_stock_leaf(query, st, item, sizes[0][0])

    btns = [InlineKeyboardButton(f"{_compact_size(z)} ({q})",
                                 callback_data=f"LOGI|SKZ|{st}|{item}|{z}")
            for z, q in sizes]
    kb = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    kb.append([InlineKeyboardButton("🔙 Back",
                                    callback_data=_stock_back(rows, st, item, from_size_screen=True))])
    icon = _emoji_for(item, _ITEM_EMOJI, "•")
    await _edit(query, f"✏️ *Update Stock* — {icon} *{item}* · *{st}*\n\nWhich size?",
                InlineKeyboardMarkup(kb))


async def _render_stock_leaf(query, st: str, item: str, size: str, banner: str = "") -> None:
    ok, rows = await _read(get_stock_rows)
    if not ok:
        return await _fail(query, "the stock list", rows, back="LOGI|SKP|0")
    qty = _stock_qty(rows, st, item, size)
    if qty is None:
        return await _render_stock_items(query, st)

    back = _stock_back(rows, st, item)
    step = lambda d: f"LOGI|SKADJ|{st}|{item}|{size}|{d}"      # noqa: E731
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("−10", callback_data=step(-10)),
         InlineKeyboardButton("−5", callback_data=step(-5)),
         InlineKeyboardButton("−1", callback_data=step(-1))],
        [InlineKeyboardButton("+1", callback_data=step(1)),
         InlineKeyboardButton("+5", callback_data=step(5)),
         InlineKeyboardButton("+10", callback_data=step(10))],
        [InlineKeyboardButton("🔙 Back", callback_data=back)],
    ])
    icon = _emoji_for(item, _ITEM_EMOJI, "•")
    size_line = "" if str(size).strip() in ("", "-") else f" · Size *{_compact_size(size)}*"
    head = f"{banner}\n\n" if banner else ""
    await _edit(query, f"{head}✏️ *Update Stock*\n{icon} *{item}* · *{st}*{size_line}\n\n"
                       f"Quantity in stock: *{qty}*\n\n_Tap to adjust._", kb)


async def _render_sizes(query, page: int) -> None:
    ok, sizes = await _read(get_costume_sizes)
    if not ok:
        return await _fail(query, "costume sizes", sizes)
    nicks = sorted(sizes)
    chunk, page, pages = paginate(nicks, page, _PAGE)
    filled = sum(1 for n in nicks if sizes[n]["shirt"] or sizes[n]["pants"])
    lines = [f"📏 *Default Costume Sizes*  _({filled}/{len(nicks)} filled)_", ""]
    for nick in chunk:
        sz = sizes[nick]
        s = sz["shirt"].value if sz["shirt"] else "—"
        p = sz["pants"].value if sz["pants"] else "—"
        lines.append(f"• {nick} — Shirt *{s}* · Pants *{p}*")
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
    sz = sizes.get(nick, {"shirt": None, "pants": None})
    s, p = sz["shirt"], sz["pants"]
    text = (f"📏 *{nick}*\nShirt: *{s.value if s else '—'}*    "
            f"Pants: *{p.value if p else '—'}*\n\nTap a size to change it:")
    shirt_row = [InlineKeyboardButton(("• " if o is s else "") + o.value,
                                      callback_data=f"LOGI|SZ|{nick}|shirt|{o.value}")
                 for o in _SIZE_OPTS]
    pants_row = [InlineKeyboardButton(("• " if o is p else "") + o.value,
                                      callback_data=f"LOGI|SZ|{nick}|pants|{o.value}")
                 for o in _SIZE_OPTS]
    kb = InlineKeyboardMarkup([shirt_row, pants_row,
                               [InlineKeyboardButton("🔙 Back", callback_data="LOGI|SIZE|0")]])
    await _edit(query, text, kb)
