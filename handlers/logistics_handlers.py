"""Logistics → Costume Tracker: entry point and callback router.

The Costume Tracker is split across four modules; this one is the public face
that `main.py` and `private_handlers` import, so the split is invisible outside:

    costume_common.py     shared render/read helpers + staging buffer
    costume_inventory.py  what the club owns  — Overview, tables, Update Stock,
                          New Costume Set, Costume Size
    costume_tracking.py   who is holding what — Issue / Return / Transfer
    logistics_handlers.py this file: home screen + `logistics_callback`

Backed by the `Logistics AY 26/27` spreadsheet via `services.costume_sheets`.
There is no dummy data and no module-level mutable state: every screen reads the
sheet through the shared smart cache, so two admins on the panel at once always
see the same numbers.

Access: `services.google_sheets.is_logistics_admin` (MAIN admins + anyone whose
Role/Position contains "logistic"), enforced at the Cockpit entry and again at
the top of `logistics_callback`.

Callback namespace `LOGI|<verb>|...`, registered in main.py on `^LOGI\|`. Every
screen edits the one dashboard bubble.

WHERE THE NUMBERS COME FROM
  • Stock totals      Costume Overview A:F        (admin-maintained; Update Stock writes here)
  • Out / On hand     Costume Overview H:P        (SUMIFS — read-only for the bot)
  • Who holds what    Costume Tracking ledger     (open row = no returned/transferred date)
  • Default sizes     Costume Size
  • Events/performers PERF tab + PERF TABULATION  (shared with the attendance flow)

The bot never computes an aggregate the sheet already computes. Issue/return/
transfer only append or close ledger rows; Out and On hand then move on their
own, so the panel and the spreadsheet cannot disagree.

MODEL — holding is per *ledger row*, not per person or per performance:
  • A costume issued for one show stays with that member until they return it.
  • One member may hold SEVERAL sets at once (e.g. two shirts in different
    sizes) — that is simply several open rows, so tapping a current holder
    offers "swap this set" per row *or* "issue an additional set".
  • TRANSFER records WHO it went to and opens a row for the receiver, so a
    hand-over never makes the costume look returned.

Sets, pant types and items are read from the sheet, not from the enums — a newly
bought costume set needs no code change (see `add_costume_set`).

Callback map:
    HOME · LIST · INV|<page> · RUN|<section>
    SKP · SKC|<category> · SKS|<set> · SKI|<set>|<item> · SKZ|<set>|<item>|<size>
    SKADJ|<set>|<item>|<size>|<delta>
    SIZE|<page> · SZE|<nick> · SZ|<nick>|<garment>|<size>
    TRACK · ACT|<action> · EV|<eid> · PF|<eid>|<nick>
    PFN|<eid>|<nick> · PFS|<eid>|<nick>|<row>
    DSET/DSH/DPS/DACC · ADD
    HP|<action>|<page> · HSEL|<action>|<row>
    RIT|<action>|<row>|<item> · RIA|<action>|<row>
    TRT|<row>|<page> · TRS|<row>|<name>
    REV · SUB · CLR · NOP
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services.costume_sheets import Item, Size
from handlers.costume_common import (_read, _edit, _fail, _clear_stage, _held_by_name,
                                     _holding_label, _holding_label_full,
                                     _stage, _stage_for, _stage_key)
from handlers.costume_inventory import (
    _render_list, _render_main_inventory, _render_running_table,
    _render_stock_pick, _render_stock_sets, _render_stock_items,
    _render_stock_sizes, _render_stock_leaf, _render_sizes, _render_size_edit)
from handlers.costume_tracking import (
    _render_track, _render_pick_perf, _render_pick_performer, _render_holder_choice,
    _render_config, _render_holders, _render_transfer_to, _render_review, _submit,
    _render_item_pick, _item_keys,
    event_name_for)
from services.costume_sheets import (get_open_holdings, adjust_stock,
                                     set_costume_size, set_row_performance,
                                     describe_items, items_out)

__all__ = ["render_logistics_home", "logistics_callback"]


async def render_logistics_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👕 *Costume Tracker*\n\n"
        "• *Costume Overview* — stock on hand, out, and totals\n"
        "• *Costume Tracking* — issue / return / transfer\n"
        "• *Costume Size* — default sizes per member\n\n"
        "_Live from the Logistics sheet._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Costume Overview", callback_data="LOGI|LIST")],
        [InlineKeyboardButton("📝 Costume Tracking", callback_data="LOGI|TRACK")],
        [InlineKeyboardButton("📏 Costume Size", callback_data="LOGI|SIZE|0")],
        [InlineKeyboardButton("🦅 Exit to Cockpit", callback_data="DASH_VIEW|HOME")],
    ])
    await _edit(update.callback_query, text, kb)


async def logistics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    from services.google_sheets import is_logistics_admin
    if not is_logistics_admin(update.effective_user.id):
        await query.answer("🔒 Costume Tracker is for Logistics & Main admins only.",
                           show_alert=True)
        return
    await query.answer()

    parts = data.split("|")
    verb = parts[1] if len(parts) > 1 else "HOME"
    ud = context.user_data

    if verb == "NOP":
        return
    if verb == "HOME":
        return await render_logistics_home(update, context)

    # --- Costume List / stock ---
    if verb == "LIST":
        return await _render_list(query)
    if verb == "INV":
        return await _render_main_inventory(query, int(parts[2]))
    if verb == "RUN":
        return await _render_running_table(query, int(parts[2]))
    if verb == "SKP":                    # SKP|<page> — page kept for old buttons
        return await _render_stock_pick(query)
    if verb == "SKC":                    # SKC|costume|pants
        return await _render_stock_sets(query, parts[2])
    if verb == "SKS":                    # SKS|<set>
        return await _render_stock_items(query, parts[2])
    if verb == "SKI":                    # SKI|<set>|<item>
        return await _render_stock_sizes(query, parts[2], parts[3])
    if verb == "SKZ":                    # SKZ|<set>|<item>|<size>
        return await _render_stock_leaf(query, parts[2], parts[3], parts[4])
    if verb == "SKADJ":                  # SKADJ|<set>|<item>|<size>|<delta>
        st, item, size, delta = parts[2], parts[3], parts[4], int(parts[5])
        # Pass the sheet's own item name straight through. Forcing it through the
        # Item enum broke as soon as the sheet renamed a row ("Wrist Wrap (pairs)").
        ok, new = await _read(adjust_stock, item, st, size, delta)
        banner = ("✅ Updated." if ok and new is not None
                  else "⚠️ Couldn't write that row — check Main Inventory.")
        return await _render_stock_leaf(query, st, item, size, banner=banner)

    # --- Costume Size ---
    if verb == "SIZE":
        return await _render_sizes(query, int(parts[2]) if len(parts) > 2 else 0)
    if verb == "SZE":
        return await _render_size_edit(query, parts[2])
    if verb == "SZ":
        nick, garment, val = parts[2], parts[3], parts[4]
        await _read(set_costume_size, nick, garment, Size(val))
        return await _render_size_edit(query, nick)

    # --- Costume Tracking ---
    if verb == "TRACK":
        return await _render_track(query)
    if verb == "ACT":
        action = parts[2]
        cur = _stage(ud)["key"]
        if cur and cur[0] != action:
            _clear_stage(ud)
        ud.pop("logi_draft", None)
        if action == "issue":
            return await _render_pick_perf(query)
        return await _render_holders(query, context, action)
    if verb == "EV":                     # EV|<eid>
        ud.pop("logi_draft", None)
        return await _render_pick_performer(query, context, parts[2])
    if verb == "PF":                     # PF|<eid>|<nick> — route via the holder choice
        eid, nick = parts[2], parts[3]
        ok, holdings = await _read(get_open_holdings)
        mine = _held_by_name(holdings if ok else []).get(nick, [])
        if mine:
            return await _render_holder_choice(query, eid, nick, mine,
                                               await event_name_for(eid))
        return await _render_config(query, context, eid, nick)
    if verb == "PFK":                    # PFK|<eid>|<row> — retag, issue nothing
        eid, row = parts[2], int(parts[3])
        name = await event_name_for(eid)
        if not name:
            return await _render_pick_performer(query, context, eid)
        ok, _res = await _read(set_row_performance, row, name)
        banner = (f"↪️ _Kept for *{name}* — row retagged, nothing issued._" if ok
                  else "⚠️ _Couldn't retag that row._")
        return await _render_pick_performer(query, context, eid, banner=banner)
    if verb == "PFN":                    # PFN|<eid>|<nick> — an additional set
        ud.pop("logi_draft", None)
        return await _render_config(query, context, parts[2], parts[3])
    if verb == "PFS":                    # PFS|<eid>|<nick>|<row> — swap that row
        ud.pop("logi_draft", None)
        return await _render_config(query, context, parts[2], parts[3], swap_row=parts[4])
    if verb in ("DSET", "DSH", "DPS", "DACC"):
        d = ud.get("logi_draft")
        if not d:
            return await _render_track(query)
        if verb == "DACC":
            d[parts[2]] = 0 if d[parts[2]] else 1
        else:
            key = {"DSET": "set", "DSH": "shirt", "DPS": "pants"}[verb]
            d[key] = parts[2]
            if verb == "DSET":
                # Shirt size differs per set, so follow the member's record for
                # the set just picked; keep the current size if they have none.
                d["shirt"] = (d.get("shirt_by_set") or {}).get(parts[2], d["shirt"])
        return await _render_config(query, context, d["eid"], d["nick"],
                                    swap_row=d.get("swap_row"))
    if verb == "ADD":
        d = ud.get("logi_draft")
        if not d:
            return await _render_track(query)
        _stage_for(ud, "issue", d["eid"])["items"][_stage_key(d["nick"], d["swap_row"])] = {
            k: d.get(k) for k in ("nick", "event", "set", "shirt", "pants",
                                  "waist", "wrist", "head", "swap_row")}
        ud.pop("logi_draft", None)
        return await _render_pick_performer(query, context, d["eid"])
    if verb == "HP":                     # HP|<action>|<page> — holders list paging
        return await _render_holders(query, context, parts[2], int(parts[3]))
    if verb in ("HSEL", "RIT", "RIA"):
        # HSEL|<action>|<row> opens the item picker; RIT toggles one piece;
        # RIA accepts the selection. All three need the live row.
        action, row = parts[2], parts[3]
        ok, holdings = await _read(get_open_holdings)
        if not ok:
            return await _fail(query, "the holders list", holdings, back="LOGI|TRACK")
        h = next((x for x in holdings if str(x.row) == row), None)
        if h is None:
            return await _render_holders(query, context, action)
        st = _stage_for(ud, action, "*")
        if verb == "HSEL":
            if row in st["items"]:        # tapping a staged person un-stages them
                del st["items"][row]
                ud.get("logi_items", {}).pop(row, None)
                return await _render_holders(query, context, action)
            return await _render_item_pick(query, context, action, row, h)
        if verb == "RIT":
            key = parts[4]
            chosen = _item_keys(ud, row, h)
            chosen.remove(key) if key in chosen else chosen.append(key)
            return await _render_item_pick(query, context, action, row, h)
        keys = _item_keys(ud, row, h)     # RIA
        if not keys:
            return await _render_item_pick(query, context, action, row, h)
        if action == "return":
            st["items"][row] = {"name": h.name, "label": _holding_label_full(h),
                                "desc": describe_items(h, keys), "keys": list(keys),
                                "full": len(keys) == len(items_out(h))}
            return await _render_holders(query, context, "return")
        # Show what is actually changing hands, not the whole set.
        return await _render_transfer_to(query, row, h.name, describe_items(h, keys),
                                         h.event, 0)
    if verb == "TRT":                    # TRT|<row>|<page> — recipient picker paging
        row = parts[2]
        ok, holdings = await _read(get_open_holdings)
        h = next((x for x in (holdings if ok else []) if str(x.row) == row), None)
        if h is None:
            return await _render_holders(query, context, "transfer")
        return await _render_transfer_to(query, row, h.name,
                                         describe_items(h, _item_keys(ud, row, h)),
                                         h.event, int(parts[3]))
    if verb == "TRS":                    # TRS|<row>|<recipient>
        row, to = parts[2], parts[3]
        ok, holdings = await _read(get_open_holdings)
        h = next((x for x in (holdings if ok else []) if str(x.row) == row), None)
        if h is None:
            return await _render_holders(query, context, "transfer")
        keys = _item_keys(ud, row, h)
        _stage_for(ud, "transfer", "*")["items"][row] = {
            "name": h.name, "label": _holding_label_full(h), "to": to,
            "desc": describe_items(h, keys), "keys": list(keys),
            "full": len(keys) == len(items_out(h))}
        return await _render_holders(query, context, "transfer")
    if verb == "REV":
        return await _render_review(query, context)
    if verb == "SUB":
        return await _submit(query, context)
    if verb == "CLR":
        _clear_stage(ud)
        return await _render_track(query)

    return await render_logistics_home(update, context)
