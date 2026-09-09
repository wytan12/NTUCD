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
from services.google_sheets import get_member_roster
from services.costume_sheets import (CostumeSet, Size, LedgerStatus,
                                     IssueEntry, Closure, get_running_inventory,
                                     get_open_holdings, get_costume_sizes,
                                     append_issues, close_rows,
                                     pant_size_label, list_costume_sets, NONE_SIZE,
                                     set_row_performance, ITEM_FIELDS, items_out,
                                     record_sizes_from_issues,
                                     describe_items, release_items, issue_items_to,
                                     shirt_size_for)
from handlers.costume_common import (_read, _edit, _fail, _set_icon, _holding_label,
                                     _holding_label_full, _item_button_label,
                                     _picked,
                                     _held_by_name, _stage, _stage_for, _clear_stage,
                                     _stage_key, _PAGE, _ROSTER_PAGE, _SIZE_OPTS)


_ACT_LABEL = {"issue": "📤 Issue", "return": "↩️ Return", "transfer": "🔁 Transfer"}


_ACCESSORIES = (("waist", "Waist Wrap"), ("wrist", "Wrist Wrap"), ("head", "Head Band"))




async def _render_track(query) -> None:
    ok, sections = await _read(get_running_inventory)
    if not ok:
        return await _fail(query, "the inventory", sections)
    ok2, holdings = await _read(get_open_holdings)
    holdings = holdings if ok2 else []

    lines = ["📝 *Costume Tracking*", ""]
    for sec in sections:
        for item in sec.items:
            if not item.sized:                    # headline items only: shirts + pants
                continue
            # Section name qualifies the item ("Red" + "Shirt"), but once Old/New
            # pants merged the section IS the item — so drop the qualifier rather
            # than printing "Pants Pants".
            label = sec.title.replace("COSTUME SET", "").strip().title()
            name = item.name if label.lower() == item.name.lower() else f"{label} {item.name}"
            out = f"  ·  {item.out_all} out" if item.out_all else ""
            lines.append(f"• {name} — *{item.on_hand_all}* left{out}")
    lines += ["", f"👤 Holding now: *{len(holdings)}*", "", "What are you doing?"]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Issue", callback_data="LOGI|ACT|issue")],
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
        rows.append([InlineKeyboardButton(f"📤 {name}  ·  {held}/{len(performers)} holding",
                                          callback_data=f"LOGI|EV|{tid}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|TRACK")])
    text = "📤 *Issue* — pick a performance:"
    if len(rows) == 1:
        text += "\n\n_No performance has performers marked in PERF TABULATION yet._"
    await _edit(query, text, InlineKeyboardMarkup(rows))


def _run_sync_events():
    """Sync wrapper so `_read` can push both PERF reads onto one thread.

    Both read from the cache without re-checking Drive: PERF and PERF TABULATION
    change when the bot writes them (which invalidates the cache), and paging
    around the Costume Tracker should not keep asking Google whether an
    unrelated tab moved.
    """
    from services.google_sheets import get_perf_event_list, get_all_perf_performers
    events = get_perf_event_list(trust_cache=True)
    performers = get_all_perf_performers(trust_cache=True)
    return [(tid, name, performers.get(str(tid), [])) for tid, name, _cnt in events]


async def event_name_for(eid: str) -> str:
    """Display name of one performance, or "" if it can't be resolved."""
    ok, events = await _read(_run_sync_events)
    if not ok:
        return ""
    name, _performers = _event_by_id(events, eid)
    return name or ""


def _event_by_id(events, eid: str):
    for tid, name, performers in events:
        if str(tid) == str(eid):
            return name, performers
    return None, []


async def _render_pick_performer(query, context, eid: str, banner: str = "") -> None:
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

    lines = ([banner, ""] if banner else []) + [
        f"📤 *Issue — {name}*",
        f"_{sum(1 for n in performers if n in held)}/{len(performers)} already holding "
        f"· {len(staged)} staged_", ""]
    for nick in performers:
        mine = staged_by_nick.get(nick, [])
        if mine:
            for it in mine:
                tag = "swap→" if it.get("swap_row") else "→"
                lines.append(f"📝 {nick}  {tag} {it['set']} S{it['shirt']}/P{it['pants']}")
        elif nick in held:
            sets = "; ".join(_holding_label_full(h) for h in held[nick])
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
                "shirt": (held.shirt_size or Size.M).value,
                "pants": (held.pant_size or Size.M).value,
                "waist": held.waist, "wrist": held.wrist, "head": held.head}
    else:
        ok, sizes = await _read(get_costume_sizes)
        entry = (sizes or {}).get(nick) if ok else None
        default_set = CostumeSet.RED.value
        # No recorded size stays BLANK rather than defaulting to M: a silent M
        # is indistinguishable from a real M once it is in the ledger, and the
        # size drives which column counts as issued.
        seeded_shirt = shirt_size_for(entry, default_set)
        seeded_pants = (entry or {}).get("pants")
        base = {"set": default_set,
                "shirt": seeded_shirt.value if seeded_shirt else "",
                "pants": seeded_pants.value if seeded_pants else "",
                "waist": 1, "wrist": 1, "head": 0}
        # Cache every set's shirt size on the draft, so switching set re-seeds
        # the size without another sheet read (re-renders are read-free).
        by_set = {st: sz.value for st, sz in ((entry or {}).get("shirt") or {}).items() if sz}
        base["shirt_by_set"] = by_set

    for key in ("nick", "event", "swap_row", "swap_label", "swap_event"):
        base.pop(key, None)
    base.setdefault("shirt_by_set", {})
    # Snapshot what is being swapped, so re-renders never need the Holding object
    # (and therefore never need another sheet read) to describe it.
    d = {"eid": eid, "nick": nick, "event": event_name, "swap_row": swap_row,
         "swap_label": _holding_label_full(held) if held else "",
         "swap_event": held.event if held else "", **base}
    ud["logi_draft"] = d
    return d


async def _render_holder_choice(query, eid: str, nick: str, holdings,
                                event_name: str = "") -> None:
    """Someone already holding can keep it, swap a set, or take an extra one.

    Two shirts in different sizes is just two open ledger rows, so "additional"
    needs no special data handling. **Keep** exists because a costume worn across
    several shows otherwise stays tagged with the first one: once that show is
    over, "still holding for the next performance" and "never returned it" look
    identical in the sheet.
    """
    mine = holdings
    lines = [f"🔁 *{nick}* already holds {len(mine)} "
             f"{'sets' if len(mine) > 1 else 'set'}:", ""]
    rows = []
    for h in mine:
        lines.append(f"• {_holding_label_full(h)}  ·  from {h.event}")
        # Nothing to retag if it is already tagged with this performance.
        if event_name and h.event != event_name:
            rows.append([InlineKeyboardButton(f"↪️ Keep it for {event_name}",
                                              callback_data=f"LOGI|PFK|{eid}|{h.row}")])
        rows.append([InlineKeyboardButton(f"🔁 Swap {_holding_label(h)}",
                                          callback_data=f"LOGI|PFS|{eid}|{nick}|{h.row}")])
    lines += ["", "_Keep_ just retags the row for this show — nothing is issued "
                  "or returned. _Swap_ exchanges the set; _Additional_ gives "
                  "another on top."]
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
        sets = sets or [CostumeSet.RED.value, CostumeSet.WHITE.value]
        ud["logi_opts"] = {"sets": sets}
    opts = ud.get("logi_opts") or {"sets": [CostumeSet.RED.value, CostumeSet.WHITE.value]}

    d = await _draft_for(ud, eid, nick, event_name or "", held, swap_row)
    cset = d["set"]

    back = [InlineKeyboardButton("🔙 Back", callback_data=f"LOGI|EV|{eid}")]

    if d["swap_row"]:
        head = (f"🔁 *Swap — {nick}*  ({d['event']})\n\n"
                f"_Currently holds {d['swap_label']} (from {d['swap_event']}). "
                f"Confirming closes that row as returned and issues the new one._")
        add_label = "✔️ Add swap to list"
    else:
        head = f"📤 *Issue — {nick}*  ({d['event']})\n\n_Defaults are their Costume Size._"
        add_label = "✔️ Add to list"

    # The accessory counts aren't restated here — the ✅/⬜ toggles below already
    # show them, and repeating them made the bubble read twice as long.
    # An unpicked size is written to the ledger as "-" — "no item of this type on
    # this row" — so a shirt-only or pants-only issue is a normal outcome rather
    # than an error. One row is one costume out; there is no quantity column.
    shirt_txt = d["shirt"] or NONE_SIZE
    pant_txt = pant_size_label(Size(d["pants"])) if d["pants"] else NONE_SIZE
    text = (f"{head}\n\nSet: *{cset}*\n"
            f"Shirt: *{shirt_txt}*   Pant: *{pant_txt}*")
    if not d["shirt"] and not d["pants"]:
        text += "\n\n⚠️ _Neither a shirt nor pants selected._"

    add_row = [InlineKeyboardButton(add_label, callback_data="LOGI|ADD")]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_picked(f"{_set_icon(o)} {o}", o == cset),
                              callback_data=f"LOGI|DSET|{o}") for o in opts["sets"]],
        [InlineKeyboardButton("👕", callback_data="LOGI|NOP")] +
        [InlineKeyboardButton(_picked(o.value, o.value == d["shirt"]),
                              callback_data=f"LOGI|DSH|{o.value}") for o in _SIZE_OPTS],
        [InlineKeyboardButton("👖", callback_data="LOGI|NOP")] +
        [InlineKeyboardButton(_picked(o.value, o.value == d["pants"]),
                              callback_data=f"LOGI|DPS|{o.value}") for o in _SIZE_OPTS],
        [InlineKeyboardButton(("✅ " if d[k] else "⬜ ") + lab,
                              callback_data=f"LOGI|DACC|{k}") for k, lab in _ACCESSORIES],
        add_row,
        back,
    ])
    await _edit(query, text, kb)


async def _render_holders(query, context, action: str, page: int = 0,
                          banner: str = "") -> None:
    """The list of open holdings to act on.

    Deliberately does NOT print a line per holder: after a big show 40 people
    are holding something, and 40 text lines plus 40 buttons is a wall nobody
    reads (and runs into Telegram's message length). The buttons carry the
    names, the detail appears once a person is tapped, and only the people
    already STAGED are spelled out — that is the part the admin still has to
    check before submitting.
    """
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
    lines = ([banner, ""] if banner else []) + [
        f"{_ACT_LABEL[action]} — currently holding (*{len(holdings)}*)"]
    if staged:
        lines += ["", f"_Staged for {verb}:_"]
        for h in holdings:
            it = staged.get(str(h.row))
            if not it:
                continue
            what = it.get("desc") or it.get("label", "")
            to = f"  ➜  *{it['to']}*" if action == "transfer" and it.get("to") else ""
            tag = "" if it.get("full", True) else "  _(partial)_"
            lines.append(f"📝 {h.name} — {what}{to}{tag}")
    lines += ["", "Tap a person:"]

    # Two people can share a first name, and one person can have two open rows,
    # so a button that is only a name would be ambiguous. Those few get the
    # costume spelled out and a row to themselves — paired with another button
    # the label would be clipped mid-word.
    seen = {}
    for h in holdings:
        seen[h.name] = seen.get(h.name, 0) + 1
    chunk, page, pages = paginate(holdings, page, _PAGE)

    rows, pending = [], []
    for h in chunk:
        mark = "📝 " if str(h.row) in staged else ""
        cb = f"LOGI|HSEL|{action}|{h.row}"
        if seen[h.name] > 1:
            rows.append([InlineKeyboardButton(
                f"{mark}{h.name} · {_holding_label_full(h)}", callback_data=cb)])
            continue
        pending.append(InlineKeyboardButton(mark + h.name, callback_data=cb))
        if len(pending) == 2:
            rows.append(pending)
            pending = []
    if pending:
        rows.append(pending)

    nav = pagination_row(page, pages, lambda p: f"LOGI|HP|{action}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    if staged:
        rows.append([InlineKeyboardButton(f"📋 Review & Submit ({len(staged)})",
                                          callback_data="LOGI|REV")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|TRACK")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


def _item_keys(ud: dict, row: str, holding, action: str = "return") -> list[str]:
    """Which items are ticked for this row.

    A return starts with everything ticked — people hand the whole costume back
    and ticking four boxes every time would be busywork. A transfer starts with
    NOTHING ticked: passing on a whole set is the exception, and a pre-ticked
    list would quietly move pieces the giver still has.
    """
    sel = ud.setdefault("logi_items", {})
    if row not in sel:
        sel[row] = items_out(holding) if action == "return" else []
    return sel[row]


async def _render_item_pick(query, context, action: str, row: str, holding) -> None:
    """Tick the pieces involved. People hand back or pass on one item at a time,
    and each item's Out counts from its own field, so a partial action clears
    just that field and leaves the rest counted."""
    ud = context.user_data
    chosen = _item_keys(ud, row, holding, action)
    available = items_out(holding)

    lines = [f"{_ACT_LABEL[action]} — *{holding.name}*",
             f"_{holding.costume_set or 'Costume'} set · for {holding.event}_", "",
             f"Which pieces are being {'returned' if action == 'return' else 'passed on'}?"]
    # 2x2 grid: four pieces is a shape you read at a glance, where four stacked
    # full-width buttons is a list you have to scan.
    btns = [InlineKeyboardButton(
        _picked(_item_button_label(holding, key, label), key in chosen),
        callback_data=f"LOGI|RIT|{action}|{row}|{key}")
        for key, _hdr, label in ITEM_FIELDS if key in available]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if chosen:
        nxt = "✔️ Add to list" if action == "return" else "➡️ Choose who has it"
        rows.append([InlineKeyboardButton(nxt, callback_data=f"LOGI|RIA|{action}|{row}")])
    else:
        rows.append([InlineKeyboardButton("⚠️ Pick at least one piece",
                                          callback_data="LOGI|NOP")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"LOGI|ACT|{action}")])
    if chosen and len(chosen) < len(available):   # nothing picked yet is not "partial"
        lines += ["", "_Partial — the rest stays out, and the row stays open._"]
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_transfer_to(query, row: str, holder: str, label: str, event: str,
                              page: int) -> None:
    """Pick who the costume is being handed to.

    The recipient is the point of a transfer: without it the costume vanishes
    from `Out` even though nobody returned it. The list is the same one Take
    Attendance shows — active members, year-category order, `(SU3)` tags,
    20 a page — rather than the Costume Size sheet, which is a size record and
    not a roster.
    """
    ok, roster = await _read(get_member_roster, True)
    if not ok:
        return await _fail(query, "the member list", roster, back="LOGI|ACT|transfer")
    people = [(n, tag) for n, tag in roster if n != holder]
    chunk, page, pages = paginate(people, page, _ROSTER_PAGE)

    lines = [f"🔁 *Transfer — {holder}*", "",
             f"Held: {label} (from {event})", "", "Hand it to:"]
    # Same list, order and 20-a-page as Take Attendance: one member list behaves
    # the same everywhere. The tag is display only — the ledger is written by
    # nickname, so that is what the callback carries.
    btns = [InlineKeyboardButton(f"({tag}) {n}" if tag else n,
                                 callback_data=f"LOGI|TRS|{row}|{n}") for n, tag in chunk]
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
            swap = " _(swap — old row closed)_" if it.get("swap_row") else ""
            acc = "+".join(lab for k, lab in _ACCESSORIES if it[k])
            shirt = f"Shirt {it['shirt']}" if it['shirt'] else "no shirt"
            pants = (f"Pants {pant_size_label(Size(it['pants']))}" if it['pants']
                     else "no pants")
            lines.append(f"• {it['nick']} — {it['set']} costume set · {shirt} · {pants}"
                         f"{' · ' + acc if acc else ''}{swap}")
        elif action == "return":
            lines.append(f"• {it['name']} — returning {it.get('desc') or it['label']}"
                         + ("" if it.get("full", True) else "  _(partial)_"))
        else:
            lines.append(f"• {it['name']} — {it.get('desc') or it['label']}  ➜  *{it['to']}*"
                         + ("" if it.get("full", True) else "  _(partial)_"))
    if action == "transfer":
        lines += ["", "_Each transfer closes the giver's row and opens one for the "
                      "receiver, so the costume stays counted as out._"]

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
                                  shirt_size=Size(it["shirt"]) if it["shirt"] else None,
                                  pant_size=Size(it["pants"]) if it["pants"] else None,
                                  waist=it["waist"], wrist=it["wrist"], head=it["head"])
                       for it in items.values()]
            await asyncio.to_thread(append_issues, entries)
            # Handing someone a costume is the measurement — write it back so
            # Costume Size stays current and the next issue pre-fills.
            learned = await asyncio.to_thread(record_sizes_from_issues, entries)
            msg = f"🟢 *Issued {n} costume(s) — saved to the sheet.*"
            if learned:
                word = "entry" if learned == 1 else "entries"
                msg += f"\n📏 _Costume Size updated ({learned} {word})._"
            if swaps:
                msg += f"\n🔁 _{len(swaps)} old row(s) closed as returned._"
        else:
            ok, holdings = await _read(get_open_holdings)
            if not ok:
                raise RuntimeError(holdings)
            by_row = {str(h.row): h for h in holdings}
            if any(k not in by_row for k in items):
                raise RuntimeError("a row changed while you were staging — reopen the list")
            # One row at a time: a partial release clears only the items chosen,
            # and the row closes itself when nothing is left out.
            closed = 0
            for k, it in items.items():
                h = by_row[k]
                keys = it.get("keys") or items_out(h)
                _ok, was_closed = await asyncio.to_thread(
                    release_items, h, keys,
                    "returned" if action == "return" else "transferred",
                    it.get("to", ""))
                closed += int(was_closed)
                if action == "transfer":
                    await asyncio.to_thread(issue_items_to, h, keys, it["to"])
            if action == "return":
                msg = f"🟢 *Logged {n} return(s) — saved to the sheet.*"
            else:
                msg = (f"🟢 *Logged {n} transfer(s) — saved to the sheet.*\n"
                       f"_Receivers now hold them; Out is unchanged._")
            if n - closed:
                msg += (f"\n📝 _{n - closed} row(s) stayed open — some pieces "
                        f"are still out; the details are in Remarks._")

    except Exception as e:                       # noqa: BLE001 — surfaced to the admin
        print(f"[LOGI][ERROR] submit ({action}) failed: {e}")
        return await _edit(
            query,
            f"⚠️ Couldn't write to the ledger — *nothing was saved*.\n\n`{str(e)[:200]}`\n\n"
            f"_Your list is still staged; try Submit again._",
            InlineKeyboardMarkup([[InlineKeyboardButton("📋 Back to review",
                                                        callback_data="LOGI|REV")]]))

    # Same shape as Take Attendance's CONFIRM: clear the staging buffer, then
    # re-render the list we came from with the result as a banner. A terminal
    # "done" bubble is a dead end — after writing one group there is usually
    # another to do, and the list is where you see the write took effect.
    _clear_stage(ud)
    if action == "issue":
        return await _render_pick_performer(query, context, _scope, banner=msg)
    return await _render_holders(query, context, action, banner=msg)
