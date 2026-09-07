"""Costume Tracker — tracking: who is holding what.

Owns the issue / return / transfer flows against the Costume Tracking ledger,
plus the staged-edit buffer in `user_data`. Reads the running table only to show
a short "what is left" header.

Nothing here writes a stock quantity — that is `costume_inventory`.
"""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.ui import paginate, pagination_row
from services.costume_sheets import (CostumeSet, PantType, Size, LedgerStatus,
                                     IssueEntry, Closure, get_running_inventory,
                                     get_open_holdings, get_costume_sizes,
                                     append_issues, close_rows, apply_transfers,
                                     pant_size_label, list_costume_sets, list_pant_types)
from handlers.costume_common import (_read, _edit, _fail, _emoji_for, _set_icon,
                                     _ITEM_EMOJI, _stock_dot, _holding_label,
                                     _held_by_name, _stage, _stage_for, _clear_stage,
                                     _stage_key, _PAGE, _SIZE_OPTS)


_ACT_LABEL = {"issue": "✅ Issue", "return": "↩️ Return", "transfer": "🔁 Transfer"}


_ACCESSORIES = (("waist", "Waist Wrap"), ("wrist", "Wrist Wrap"), ("head", "Head Band"))




async def _render_track(query) -> None:
    ok, sections = await _read(get_running_inventory)
    if not ok:
        return await _fail(query, "the inventory", sections)
    ok2, holdings = await _read(get_open_holdings)
    holdings = holdings if ok2 else []

    lines = ["🎭 *Costume Tracking*", ""]
    for sec in sections:
        for item in sec.items:
            if not item.sized:                    # headline items only: shirts + pants
                continue
            label = sec.title.replace(" COSTUME SET", "").replace(" PANTS", "").title()
            dot = _stock_dot(item.on_hand_all, item.total_all)
            icon = _emoji_for(item.name, _ITEM_EMOJI, "•")
            out = f"  ·  {item.out_all} out" if item.out_all else ""
            lines.append(f"{dot} {icon} {label} {item.name} — *{item.on_hand_all}* left{out}")
    lines += ["", f"👤 Holding now: *{len(holdings)}*", "", "What are you doing?"]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Issue", callback_data="LOGI|ACT|issue")],
        [InlineKeyboardButton("↩️ Return", callback_data="LOGI|ACT|return")],
        [InlineKeyboardButton("🔁 Transfer", callback_data="LOGI|ACT|transfer")],
        [InlineKeyboardButton("🔙 Back", callback_data="LOGI|HOME")],
    ])
    await _edit(query, "\n".join(lines), kb)


async def _render_pick_perf(query) -> None:
    ok, events = await _read(_run_sync_events)
    if not ok:
        return await _fail(query, "the performance list", events, back="LOGI|TRACK")
    ok2, holdings = await _read(get_open_holdings)
    held_names = {h.name for h in holdings} if ok2 else set()

    rows = []
    for tid, name, performers in events:
        if not performers:
            continue
        held = sum(1 for n in performers if n in held_names)
        rows.append([InlineKeyboardButton(f"🎭 {name}  ·  {held}/{len(performers)} holding",
                                          callback_data=f"LOGI|EV|{tid}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|TRACK")])
    text = "✅ *Issue* — pick a performance:"
    if len(rows) == 1:
        text += "\n\n_No performance has performers marked in PERF TABULATION yet._"
    await _edit(query, text, InlineKeyboardMarkup(rows))


def _run_sync_events():
    """Sync wrapper so `_read` can push both PERF reads onto one thread."""
    from services.google_sheets import get_perf_event_list, get_all_perf_performers
    events = get_perf_event_list()
    performers = get_all_perf_performers()
    return [(tid, name, performers.get(str(tid), [])) for tid, name, _cnt in events]


def _event_by_id(events, eid: str):
    for tid, name, performers in events:
        if str(tid) == str(eid):
            return name, performers
    return None, []


async def _render_pick_performer(query, context, eid: str) -> None:
    ok, events = await _read(_run_sync_events)
    if not ok:
        return await _fail(query, "the performance list", events, back="LOGI|TRACK")
    name, performers = _event_by_id(events, eid)
    if name is None:
        return await _render_pick_perf(query)
    ok2, holdings = await _read(get_open_holdings)
    held = _held_by_name(holdings if ok2 else [])

    st = _stage_for(context.user_data, "issue", eid)
    staged = st["items"]
    staged_by_nick: dict[str, list] = {}
    for it in staged.values():
        staged_by_nick.setdefault(it["nick"], []).append(it)

    lines = [f"✅ *Issue — {name}*",
             f"_{sum(1 for n in performers if n in held)}/{len(performers)} already holding "
             f"· {len(staged)} staged_", ""]
    for nick in performers:
        mine = staged_by_nick.get(nick, [])
        if mine:
            for it in mine:
                tag = "swap→" if it.get("swap_row") else "→"
                lines.append(f"📝 {nick}  {tag} {it['set']} S{it['shirt']}/P{it['pants']}")
        elif nick in held:
            sets = "; ".join(_holding_label(h) for h in held[nick])
            plural = "sets" if len(held[nick]) > 1 else "set"
            lines.append(f"✅ {nick} — holding {len(held[nick])} {plural}: {sets}")
        else:
            lines.append(f"⏳ {nick} — not issued")

    pbtns = [InlineKeyboardButton(
        ("📝 " if n in staged_by_nick else "🔁 " if n in held else "") + n,
        callback_data=f"LOGI|PF|{eid}|{n}") for n in performers]
    rows = [pbtns[i:i + 2] for i in range(0, len(pbtns), 2)]
    if staged:
        rows.append([InlineKeyboardButton(f"📋 Review & Submit ({len(staged)})",
                                          callback_data="LOGI|REV")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|ACT|issue")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _draft_for(ud: dict, eid: str, nick: str, event_name: str, held,
                     swap_row=None) -> dict:
    """Working draft for the issue/swap config screen.

    Keyed on (event, performer, row being swapped) so switching between "swap
    this set" and "issue an additional set" reseeds instead of carrying the
    previous choice over. Seeds from the staged item, then the holding being
    swapped, then their Costume Size default.

    `swap_row` is passed in rather than read off `held`, because re-renders
    deliberately skip the sheet read and so have no `held` — deriving it here
    would make every size tap look like a different draft and silently reseed.
    """
    existing = ud.get("logi_draft")
    if existing and (existing.get("eid"), existing.get("nick"),
                     existing.get("swap_row")) == (eid, nick, swap_row):
        return existing

    staged = _stage(ud)["items"].get(_stage_key(nick, swap_row))
    if staged:
        base = dict(staged)
    elif held:
        base = {"set": held.costume_set or CostumeSet.RED.value,
                "ptype": held.pant_type or PantType.NEW.value,
                "shirt": (held.shirt_size or Size.M).value,
                "pants": (held.pant_size or Size.M).value,
                "waist": held.waist, "wrist": held.wrist, "head": held.head}
    else:
        ok, sizes = await _read(get_costume_sizes)
        sz = (sizes or {}).get(nick, {}) if ok else {}
        base = {"set": CostumeSet.RED.value, "ptype": PantType.NEW.value,
                "shirt": (sz.get("shirt") or Size.M).value,
                "pants": (sz.get("pants") or Size.M).value,
                "waist": 1, "wrist": 1, "head": 0}

    for key in ("nick", "event", "swap_row", "swap_label", "swap_event"):
        base.pop(key, None)
    # Snapshot what is being swapped, so re-renders never need the Holding object
    # (and therefore never need another sheet read) to describe it.
    d = {"eid": eid, "nick": nick, "event": event_name, "swap_row": swap_row,
         "swap_label": _holding_label(held) if held else "",
         "swap_event": held.event if held else "", **base}
    ud["logi_draft"] = d
    return d


async def _render_holder_choice(query, eid: str, nick: str, holdings) -> None:
    """Someone already holding can swap a specific set OR take an extra one.

    Two shirts in different sizes is just two open ledger rows, so "additional"
    needs no special data handling — only this choice, which the old flow
    skipped by assuming every re-issue was a swap.
    """
    mine = holdings
    lines = [f"🔁 *{nick}* already holds {len(mine)} "
             f"{'sets' if len(mine) > 1 else 'set'}:", ""]
    rows = []
    for h in mine:
        lines.append(f"• {_holding_label(h)}  ·  from {h.event}")
        rows.append([InlineKeyboardButton(f"🔁 Swap {_holding_label(h)}",
                                          callback_data=f"LOGI|PFS|{eid}|{nick}|{h.row}")])
    lines += ["", "Swap one of those, or give them another set on top?"]
    rows.append([InlineKeyboardButton("➕ Issue an additional set",
                                      callback_data=f"LOGI|PFN|{eid}|{nick}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"LOGI|EV|{eid}")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_config(query, context, eid: str, nick: str, swap_row=None) -> None:
    ud = context.user_data
    swap_row = int(swap_row) if str(swap_row or "").isdigit() else None
    existing = ud.get("logi_draft")
    fresh = not (existing and (existing.get("eid"), existing.get("nick"),
                               existing.get("swap_row")) == (eid, nick, swap_row))

    # Only the FIRST render of a draft needs the sheets. Every size/set/accessory
    # tap re-renders this screen, and PERF TABULATION is read uncached — fetching
    # here unconditionally meant one admin adjusting sizes could exhaust Google's
    # 60-reads-per-minute quota. Everything a re-render needs is already in the draft.
    held = None
    if fresh:
        ok, events = await _read(_run_sync_events)
        if not ok:
            return await _fail(query, "the performance list", events, back="LOGI|TRACK")
        event_name, _performers = _event_by_id(events, eid)
        if swap_row is not None:
            ok2, holdings = await _read(get_open_holdings)
            held = next((h for h in (holdings if ok2 else [])
                         if str(h.row) == str(swap_row)), None)
    else:
        event_name = existing.get("event", "")

    if fresh:
        _ok_s, sets = await _read(list_costume_sets)
        _ok_p, ptypes = await _read(list_pant_types)
        sets = sets or [CostumeSet.RED.value, CostumeSet.WHITE.value]
        ud["logi_opts"] = {"sets": sets,
                           "ptypes": ptypes or [PantType.OLD.value, PantType.NEW.value]}
    opts = ud.get("logi_opts") or {"sets": [CostumeSet.RED.value, CostumeSet.WHITE.value],
                                   "ptypes": [PantType.OLD.value, PantType.NEW.value]}

    d = await _draft_for(ud, eid, nick, event_name or "", held, swap_row)
    cset, ptype = d["set"], d["ptype"]

    back = [InlineKeyboardButton("🔙 Back", callback_data=f"LOGI|EV|{eid}")]

    if d["swap_row"]:
        head = (f"🔁 *Swap — {nick}*  ({d['event']})\n\n"
                f"_Currently holds {d['swap_label']} (from {d['swap_event']}). "
                f"Confirming closes that row as returned and issues the new one._")
        add_label = "✔️ Add swap to list"
    else:
        head = f"✅ *Issue — {nick}*  ({d['event']})\n\n_Defaults are their Costume Size._"
        add_label = "✔️ Add to list"

    acc = "  ".join(f"{label} *{d[key]}*" for key, label in _ACCESSORIES)
    text = (f"{head}\n\nSet: *{cset}*   Pants: *{ptype}*\n"
            f"Shirt: *{d['shirt']}*   Pant: *{pant_size_label(ptype, Size(d['pants']))}*\n"
            f"{acc}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_set_icon(o)} {o}" + (" ✓" if o == cset else ""),
                              callback_data=f"LOGI|DSET|{o}") for o in opts["sets"]],
        [InlineKeyboardButton(f"{o} pants" + (" ✓" if o == ptype else ""),
                              callback_data=f"LOGI|DPTY|{o}") for o in opts["ptypes"]],
        [InlineKeyboardButton("👕", callback_data="LOGI|NOP")] +
        [InlineKeyboardButton(("•" if o.value == d["shirt"] else "") + o.value,
                              callback_data=f"LOGI|DSH|{o.value}") for o in _SIZE_OPTS],
        [InlineKeyboardButton("👖", callback_data="LOGI|NOP")] +
        [InlineKeyboardButton(("•" if o.value == d["pants"] else "") + o.value,
                              callback_data=f"LOGI|DPS|{o.value}") for o in _SIZE_OPTS],
        [InlineKeyboardButton(("✅ " if d[k] else "⬜ ") + lab,
                              callback_data=f"LOGI|DACC|{k}") for k, lab in _ACCESSORIES],
        [InlineKeyboardButton(add_label, callback_data="LOGI|ADD")],
        back,
    ])
    await _edit(query, text, kb)


async def _render_holders(query, context, action: str) -> None:
    ud = context.user_data
    ok, holdings = await _read(get_open_holdings)
    if not ok:
        return await _fail(query, "the holders list", holdings, back="LOGI|TRACK")
    st = _stage_for(ud, action, "*")
    staged = st["items"]

    if not holdings:
        return await _edit(query, f"{_ACT_LABEL[action]}\n\n📭 Nobody is holding a costume "
                                  f"right now.",
                           InlineKeyboardMarkup([[InlineKeyboardButton(
                               "🔙 Back", callback_data="LOGI|TRACK")]]))

    verb = "returning" if action == "return" else "transferring"
    lines = [f"{_ACT_LABEL[action]} — currently holding ({len(holdings)})",
             f"_{len(staged)} staged for {verb}_", "", "Tap a person:"]
    for h in holdings:
        key = str(h.row)
        mark = "📝 " if key in staged else ""
        extra = ""
        if action == "transfer" and key in staged:
            extra = f" · _→ {staged[key]['to']}_"
        lines.append(f"{mark}{h.name} — {_holding_label(h)} · from {h.event}{extra}")

    btns = [InlineKeyboardButton(("📝 " if str(h.row) in staged else "") + h.name,
                                 callback_data=f"LOGI|HSEL|{action}|{h.row}")
            for h in holdings]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if staged:
        rows.append([InlineKeyboardButton(f"📋 Review & Submit ({len(staged)})",
                                          callback_data="LOGI|REV")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|TRACK")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_transfer_to(query, row: str, holder: str, label: str, event: str,
                              page: int) -> None:
    """Pick who the costume is being handed to.

    The recipient is the point of a transfer: without it the costume vanishes
    from `Out` even though nobody returned it. Names come from the Costume Size
    roster, which is the costume-side member list.
    """
    ok, sizes = await _read(get_costume_sizes)
    if not ok:
        return await _fail(query, "the member list", sizes, back="LOGI|ACT|transfer")
    names = [n for n in sorted(sizes) if n != holder]
    chunk, page, pages = paginate(names, page, _PAGE)

    lines = [f"🔁 *Transfer — {holder}*", "", f"Held: {label} (from {event})", "",
             "Hand it to:"]
    btns = [InlineKeyboardButton(n, callback_data=f"LOGI|TRS|{row}|{n}") for n in chunk]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = pagination_row(page, pages, lambda p: f"LOGI|TRT|{row}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|ACT|transfer")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_review(query, context) -> None:
    st = _stage(context.user_data)
    if not st["key"] or not st["items"]:
        return await _render_track(query)
    action, scope = st["key"]
    lines = [f"📋 *Review* — {_ACT_LABEL[action]}", ""]
    for _key, it in st["items"].items():
        if action == "issue":
            ptype = it["ptype"]
            swap = " _(swap — old row closed)_" if it.get("swap_row") else ""
            acc = "+".join(lab for k, lab in _ACCESSORIES if it[k])
            lines.append(f"• {it['nick']} — {it['set']} · S {it['shirt']} / "
                         f"P {pant_size_label(ptype, Size(it['pants']))}"
                         f"{' · ' + acc if acc else ''}{swap}")
        elif action == "return":
            lines.append(f"• {it['name']} — returning {it['label']}")
        else:
            lines.append(f"• {it['name']} — {it['label']}  ➜  *{it['to']}*")
    if action == "transfer":
        lines += ["", "_Each transfer closes the giver's row and opens one for the "
                      "receiver, so the costume stays counted as out._"]
    else:
        lines += ["", "_On submit the ledger updates; Out and On hand recalculate "
                      "in the sheet._"]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Submit {len(st['items'])}", callback_data="LOGI|SUB")],
        [InlineKeyboardButton("✏️ Back to edit",
                              callback_data=(f"LOGI|EV|{scope}" if action == "issue"
                                             else f"LOGI|ACT|{action}"))],
        [InlineKeyboardButton("🗑️ Clear list", callback_data="LOGI|CLR")],
    ])
    await _edit(query, "\n".join(lines), kb)


async def _submit(query, context) -> None:
    ud = context.user_data
    st = _stage(ud)
    if not st["key"] or not st["items"]:
        return await _render_track(query)
    action, _scope = st["key"]
    items = st["items"]
    n = len(items)

    await _edit(query, f"⏳ Writing {n} row(s) to the ledger…", InlineKeyboardMarkup([]))

    try:
        if action == "issue":
            swaps = [Closure(row=int(it["swap_row"]), status=LedgerStatus.RETURNED,
                             remarks="Swapped for a new set")
                     for it in items.values() if it.get("swap_row")]
            if swaps:
                await asyncio.to_thread(close_rows, swaps)
            entries = [IssueEntry(name=it["nick"], event=it["event"],
                                  costume_set=it["set"],
                                  pant_type=it["ptype"],
                                  shirt_size=Size(it["shirt"]), pant_size=Size(it["pants"]),
                                  waist=it["waist"], wrist=it["wrist"], head=it["head"])
                       for it in items.values()]
            await asyncio.to_thread(append_issues, entries)
            msg = f"✅ Issued *{n}* costume(s)."
            if swaps:
                msg += f"\n🔁 _{len(swaps)} old row(s) closed as returned._"
        elif action == "return":
            await asyncio.to_thread(
                close_rows,
                [Closure(row=int(k), status=LedgerStatus.RETURNED) for k in items])
            msg = f"↩️ Logged *{n}* return(s)."
        else:
            ok, holdings = await _read(get_open_holdings)
            if not ok:
                raise RuntimeError(holdings)
            by_row = {str(h.row): h for h in holdings}
            transfers = [(by_row[k], it["to"], "") for k, it in items.items() if k in by_row]
            if len(transfers) != n:
                raise RuntimeError("some rows were closed by someone else — reopen Transfer")
            await asyncio.to_thread(apply_transfers, transfers)
            msg = (f"🔁 Logged *{n}* transfer(s).\n"
                   f"_Receivers now hold them; Out is unchanged._")
    except Exception as e:                       # noqa: BLE001 — surfaced to the admin
        print(f"[LOGI][ERROR] submit ({action}) failed: {e}")
        return await _edit(
            query,
            f"⚠️ Couldn't write to the ledger — *nothing was saved*.\n\n`{str(e)[:200]}`\n\n"
            f"_Your list is still staged; try Submit again._",
            InlineKeyboardMarkup([[InlineKeyboardButton("📋 Back to review",
                                                        callback_data="LOGI|REV")]]))

    _clear_stage(ud)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Back to Tracking", callback_data="LOGI|TRACK")],
        [InlineKeyboardButton("📦 Costume Overview", callback_data="LOGI|LIST")],
        [InlineKeyboardButton("🦅 Costume Tracker home", callback_data="LOGI|HOME")],
    ])
    await _edit(query, msg, kb)
