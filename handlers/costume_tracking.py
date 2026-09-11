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
from services.costume_sheets import (Size, LedgerStatus, get_stock_rows,
                                     outstanding_by_performance,
                                     IssueEntry, Closure, get_running_inventory,
                                     get_open_holdings, get_costume_sizes,
                                     append_issues, close_rows,
                                     pant_size_label, NONE_SIZE,
                                     set_row_performance, ITEM_FIELDS, items_out,
                                     record_sizes_from_issues,
                                     describe_items, release_items, issue_items_to,
                                     shirt_size_for)
from handlers.costume_common import (_read, _edit, _fail, _set_icon, _holding_label,
                                     _compact_size,
                                     _holding_label_full, _item_button_label,
                                     _picked,
                                     _held_by_name, _stage, _stage_for, _clear_stage,
                                     _stage_key, _tag_group, divider_row,
                                     tagged_rows, short_colour, _DIVIDER_MIN,
                                     _PAGE, _ROSTER_PAGE, _ISSUE_PAGE, _SIZE_OPTS)


_ACT_LABEL = {"issue": "📤 Issue", "return": "↩️ Return", "transfer": "🔁 Transfer"}


_ACCESSORIES = (("waist", "Waist Wrap"), ("wrist", "Wrist Wrap"), ("head", "Head Band"))




async def _render_home(query, context=None, banner: str = "") -> None:
    """The Costume Tracker home — the four daily actions, not a menu of menus.

    Issue / Return / Transfer / Outstanding are the work; Overview and Size are
    occasional reference. Grouping the four behind a "Costume Tracking" screen
    charged a tap on every entry, so they sit here, and that screen's stock
    summary came up with them: it is the context you want BEFORE choosing, not
    after. Both reads behind it are trust-cached, so a warm open costs no API
    calls at all.
    """
    ok, sections = await _read(get_running_inventory)
    ok2, holdings = await _read(get_open_holdings)
    holdings = holdings if ok2 else []

    lines = ([banner, ""] if banner else []) + ["\U0001f455 *Costume Tracker*", ""]
    if ok:
        for sec in sections:
            for item in sec.items:
                if not item.sized:           # headline items only: shirts + pants
                    continue
                out = f"  \u00b7  {item.out_all} out" if item.out_all else ""
                lines.append(f"\u2022 {item.color} {item.name} \u2014 "
                             f"*{item.on_hand_all}* left{out}")
        lines += ["", f"\U0001f464 Holding now: *{len(holdings)}*"]
    else:
        lines.append("_Couldn't read the running table just now._")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4e4 Issue", callback_data="LOGI|ACT|issue"),
         InlineKeyboardButton("\u21a9\ufe0f Return", callback_data="LOGI|ACT|return")],
        [InlineKeyboardButton("\U0001f501 Transfer", callback_data="LOGI|ACT|transfer"),
         InlineKeyboardButton("\U0001f4cb Outstanding", callback_data="LOGI|OUT")],
        [InlineKeyboardButton("\U0001f4e6 Costume Overview", callback_data="LOGI|LIST"),
         InlineKeyboardButton("\U0001f4cf Costume Size", callback_data="LOGI|SIZE|0")],
        [InlineKeyboardButton("\U0001f989 Exit to Cockpit",
                              callback_data="DASH_VIEW|HOME")],
    ])
    await _edit(query, "\n".join(lines), kb)


async def _render_outstanding_list(query) -> None:
    """Every performance that still has costumes out, worst first."""
    ok, perfs = await _read(outstanding_by_performance)
    if not ok:
        return await _fail(query, "the ledger", perfs, back="LOGI|TRACK")
    live = [p for p in perfs if p.rows_out]

    lines = ["\U0001f4cb *Outstanding*", ""]
    if not live:
        lines.append("\U0001f389 _Nothing is out. Every costume issued has been "
                     "returned or handed on._")
    else:
        total = sum(p.rows_out for p in live)
        lines.append(f"*{total}* costume(s) still out across "
                     f"*{len(live)}* performance(s).")
    rows = [[InlineKeyboardButton(
        f"{p.performance} \u2014 {p.people_out} still holding",
        callback_data=f"LOGI|OUTP|{p.performance}|0")] for p in live]
    done = [p for p in perfs if not p.rows_out]
    if done:
        lines += ["", "_All returned: " + ", ".join(p.performance for p in done) + "_"]
    rows.append([InlineKeyboardButton("\U0001f519 Back", callback_data="LOGI|HOME")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_outstanding(query, perf: str, page: int) -> None:
    """Who still has not returned for ONE performance, and what they hold."""
    ok, holdings = await _read(get_open_holdings)
    if not ok:
        return await _fail(query, "the holders list", holdings, back="LOGI|OUT")
    mine = [h for h in holdings if (h.event or "").strip() == perf.strip()]
    ok2, perfs = await _read(outstanding_by_performance)
    stat = next((p for p in (perfs if ok2 else []) if p.performance == perf), None)

    if not mine:
        return await _edit(query,
                           f"\U0001f4cb *Outstanding \u2014 {perf}*\n\n"
                           f"\U0001f389 _Everything issued for this performance is "
                           f"back._",
                           InlineKeyboardMarkup([[InlineKeyboardButton(
                               "\U0001f519 Back", callback_data="LOGI|OUT")]]))

    by_name: dict[str, list] = {}
    for h in mine:
        by_name.setdefault(h.name, []).append(h)
    names = sorted(by_name)
    chunk, page, pages = paginate(names, page, _PAGE)

    head = f"\U0001f4cb *Outstanding \u2014 {perf}*"
    if stat:
        head += (f"\n_{stat.people_out} of "
                 f"{len({h.name for h in mine}) + (stat.rows_issued - stat.rows_out)} "
                 f"issued still holding \u00b7 {stat.rows_out} costume(s) out_")
    lines = [head, ""]
    for name in chunk:
        for h in by_name[name]:
            lines.append(f"\u23f3 {name} \u2014 {_holding_label_full(h)}")

    rows = []
    nav = pagination_row(page, pages, lambda p: f"LOGI|OUTP|{perf}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("\u21a9\ufe0f Return", callback_data="LOGI|ACT|return"),
                 InlineKeyboardButton("\U0001f501 Transfer",
                                      callback_data="LOGI|ACT|transfer")])
    rows.append([InlineKeyboardButton("\U0001f519 Back", callback_data="LOGI|OUT")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_pick_perf(query, page: int = 0) -> None:
    ok, events = await _read(_run_sync_events)
    if not ok:
        return await _fail(query, "the performance list", events, back="LOGI|TRACK")
    ok2, holdings = await _read(get_open_holdings)
    held_names = {h.name for h in holdings} if ok2 else set()

    live = [(tid, name, performers) for tid, name, performers in events if performers]
    chunk, page, pages = paginate(live, page, _ISSUE_PAGE)

    rows = [[InlineKeyboardButton(
        f"\U0001f4e4 {name}  \u00b7  {sum(1 for n in performers if n in held_names)}"
        f"/{len(performers)} holding",
        callback_data=f"LOGI|EV|{tid}")] for tid, name, performers in chunk]
    nav = pagination_row(page, pages, lambda p: f"LOGI|EVP|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("\U0001f519 Back", callback_data="LOGI|HOME")])
    text = "\U0001f4e4 *Issue* \u2014 pick a performance:"
    if not live:
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


async def _render_pick_performer(query, context, eid: str, banner: str = "",
                                 page: int = 0) -> None:
    """The show's roster, ten a page.

    Each performer takes a line of text as well as a button — what they already
    hold, or what is staged for them — so this pages shorter than a plain grid
    of names. Anyone staged is listed above the page, whichever page they are
    on: that list is what you check before submitting, and it is useless if it
    is scattered across pages.
    """
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

    # Ordered and tagged like Take Attendance, so one member list behaves the
    # same wherever it appears: seniors before juniors, then year category,
    # then name. Only the performers marked for THIS show, in that order.
    ok3, roster = await _read(get_member_roster, True)
    order = {n.strip().lower(): i for i, (n, _tag) in enumerate(roster or [])}
    tags = {n.strip().lower(): tag for n, tag in (roster or [])}
    performers = sorted(performers,
                        key=lambda n: (order.get(n.strip().lower(), 10 ** 6), n.lower()))

    chunk, page, pages = paginate(performers, page, _ISSUE_PAGE)
    lines = ([banner, ""] if banner else []) + [
        f"\U0001f4e4 *Issue \u2014 {name}*",
        f"_{sum(1 for n in performers if n in held)}/{len(performers)} already holding "
        f"\u00b7 {len(staged)} staged_", ""]

    if staged_by_nick:
        for nick, mine in staged_by_nick.items():
            for it in mine:
                tag = "swap\u2192" if it.get("swap_row") else "\u2192"
                bits = [b for b in (
                    f"{it['shirt_colour']} shirt {it['shirt']}" if it.get("shirt") else "",
                    f"{it['pant_colour']} pants "
                    f"{pant_size_label(Size(it['pants']))}" if it.get("pants") else "",
                ) if b]
                lines.append(f"\U0001f4dd {nick}  {tag} " +
                             (", ".join(bits) or "accessories only"))
        lines.append("")

    def labelled(nick):
        tag = tags.get(nick.strip().lower(), "")
        return f"({tag}) {nick}" if tag else nick

    for nick in chunk:
        if nick in staged_by_nick:
            continue                     # already spelled out above
        if nick in held:
            sets = "; ".join(_holding_label_full(h) for h in held[nick])
            plural = "sets" if len(held[nick]) > 1 else "set"
            lines.append(f"\u2705 {labelled(nick)} \u2014 holding {len(held[nick])} "
                         f"{plural}: {sets}")
        else:
            lines.append(f"\u23f3 {labelled(nick)} \u2014 not issued")

    def mark(nick):
        return ("\U0001f4dd " if nick in staged_by_nick
                else "\U0001f501 " if nick in held else "")

    rows = tagged_rows(
        [(n, tags.get(n.strip().lower(), "")) for n in chunk],
        lambda n, tg: mark(n) + labelled(n),
        lambda n: f"LOGI|PF|{eid}|{n}",
        "LOGI|NOP",
        total=len(performers))

    nav = pagination_row(page, pages, lambda p: f"LOGI|PFP|{eid}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    if staged:
        rows.append([InlineKeyboardButton(f"\U0001f4cb Review & Submit ({len(staged)})",
                                          callback_data="LOGI|REV")])
    rows.append([InlineKeyboardButton("\U0001f519 Back", callback_data="LOGI|ACT|issue")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


def _stock_options(rows) -> dict:
    """Everything the config screen needs about stock, from one read.

    Sizes are keyed by `item|colour`, not by item: a colour the club bought in
    three sizes should not offer the two it never had. Colours, sizes and
    pairings all come from Main Inventory, so a newly bought colour appears in
    the pickers with no code change — and re-renders work from this snapshot
    instead of hitting Google on every tap.
    """
    colours, sizes, pairs = {}, {}, {}
    for item, colour, size, _qty, pair in rows:
        colours.setdefault(item, [])
        if colour and colour not in colours[item]:
            colours[item].append(colour)
        if size and size != NONE_SIZE:
            key = f"{item}|{colour}"
            sizes.setdefault(key, [])
            if size not in sizes[key]:
                sizes[key].append(size)
        if pair:
            pairs[f"{item}|{colour}"] = pair
    return {"colours": colours, "sizes": sizes, "pairs": pairs}


def _size_value(label: str) -> str:
    """`'M - 170'` -> `'M'`. The ledger stores the full label for pants; the
    draft carries the bare size so one set of Size values covers both items."""
    return label.split("-")[0].strip()


async def _draft_for(ud: dict, eid: str, nick: str, event_name: str, held,
                     swap_row=None, opts: dict | None = None) -> dict:
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

    opts = opts or ud.get("logi_opts") or {"colours": {}, "sizes": {}, "pairs": {}}
    first = lambda item: (opts["colours"].get(item) or [NONE_SIZE])[0]   # noqa: E731

    staged = _stage(ud)["items"].get(_stage_key(nick, swap_row))
    if staged:
        base = dict(staged)
    elif held:
        base = {"shirt_colour": held.shirt_colour,
                "shirt": held.shirt_size.value if held.shirt_size else "",
                "pant_colour": held.pant_colour,
                "pants": held.pant_size.value if held.pant_size else "",
                "waist": held.waist, "wrist": held.wrist, "head": held.head}
    else:
        ok, sizes = await _read(get_costume_sizes)
        entry = (sizes or {}).get(nick) if ok else None
        shirt_colour = first("Shirt")
        # No recorded size stays BLANK rather than defaulting to M: a silent M
        # is indistinguishable from a real M once it is in the ledger, and the
        # size drives which column counts as issued.
        seeded_shirt = shirt_size_for(entry, shirt_colour)
        seeded_pants = (entry or {}).get("pants")
        # Seed a colour only alongside a size. A sized item needs both to count
        # as issued, so a lone colour would tick a row that sends out nothing.
        base = {"shirt_colour": shirt_colour if seeded_shirt else NONE_SIZE,
                "shirt": seeded_shirt.value if seeded_shirt else "",
                "pant_colour": first("Pants") if seeded_pants else NONE_SIZE,
                "pants": seeded_pants.value if seeded_pants else "",
                # Accessories start unset. Which wrap goes with which shirt is
                # exactly the "set" knowledge this model drops, so the bot greys
                # out what does not pair rather than guessing on your behalf.
                "waist": NONE_SIZE, "wrist": NONE_SIZE, "head": NONE_SIZE}
        by_colour = {st: sz.value for st, sz in ((entry or {}).get("shirt") or {}).items() if sz}
        base["shirt_by_colour"] = by_colour

    for key in ("nick", "event", "swap_row", "swap_label", "swap_event"):
        base.pop(key, None)
    base.setdefault("shirt_by_colour", {})
    base.setdefault("all_colours", False)
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
    """Pick what goes out, item by item: shirt, pants, then each accessory.

    Every item carries its own colour, so the screen asks for a colour and (for
    shirts and pants) a size. Accessory colours that belong with a DIFFERENT
    shirt are shown struck through and inert — the pairing is a guard rail, and
    "Show all colours" lifts it for a deliberate mix.
    """
    ud = context.user_data
    swap_row = int(swap_row) if str(swap_row or "").isdigit() else None
    existing = ud.get("logi_draft")
    fresh = not (existing and (existing.get("eid"), existing.get("nick"),
                               existing.get("swap_row")) == (eid, nick, swap_row))

    # Only the FIRST render of a draft needs the sheets. Every tap re-renders
    # this screen, and PERF TABULATION is read uncached — fetching here
    # unconditionally meant one admin adjusting sizes could exhaust Google's
    # 60-reads-per-minute quota. A re-render works entirely from the draft.
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
        _ok_s, rows = await _read(get_stock_rows)
        ud["logi_opts"] = _stock_options(rows if _ok_s else [])
    else:
        event_name = existing.get("event", "")
    opts = ud.get("logi_opts") or {"colours": {}, "sizes": {}, "pairs": {}}

    d = await _draft_for(ud, eid, nick, event_name or "", held, swap_row, opts)

    if d["swap_row"]:
        head = (f"🔁 *Swap — {nick}*  ({d['event']})\n\n"
                f"_Currently holds {d['swap_label']} (from {d['swap_event']}). "
                f"Confirming closes that row as returned and issues the new one._")
        add_label = "✔️ Add swap to list"
    else:
        head = f"📤 *Issue — {nick}*  ({d['event']})"
        add_label = "✔️ Add to list"

    def line(icon, label, colour, size=None, needs_size=False):
        """A sized item counts as issued only with BOTH a colour and a size —
        its `Out` formula filters on both columns, so a colour alone sends out
        nothing. Say so plainly rather than showing "Red -"."""
        if colour in ("", NONE_SIZE) or (needs_size and not size):
            return f"{icon} {label}: *—*"
        return f"{icon} {label}: *{colour}{(' ' + size) if size else ''}*"

    pant_txt = pant_size_label(Size(d["pants"])) if d["pants"] else ""
    text = "\n".join([
        head, "",
        line("👕", "Shirt", d["shirt_colour"], d["shirt"], needs_size=True),
        line("👖", "Pants", d["pant_colour"], pant_txt, needs_size=True),
        line("🧣", "Waist wrap", d["waist"]),
        line("🤲", "Wrist wrap", d["wrist"]),
        line("🎀", "Head band", d["head"]),
    ])
    if d["shirt_colour"] in ("", NONE_SIZE) and d["pant_colour"] in ("", NONE_SIZE):
        text += f"\n\n⚠️ _Neither a shirt nor pants selected._"

    rows_kb = []

    def size_buttons(item, colour, picked_colour, picked_size, cb):
        """Sizes for ONE colour, on that colour's own row.

        Tapping a size picks the colour with it — on a row that already says
        "Red shirt", tapping M can only mean a red M, and making that two taps
        was the thing that made this screen long.
        """
        out = []
        for label in opts["sizes"].get(f"{item}|{colour}", []):
            value = _size_value(label)
            chosen = colour == picked_colour and value == picked_size
            out.append(InlineKeyboardButton(
                _picked(_compact_size(label), chosen),
                callback_data=f"{cb}|{colour}|{value}"))
        return out

    def item_rows(item, icon, colour_cb, size_cb, picked_colour, picked_size=None,
                  accessory=False, label=""):
        """A sized item gets one row per colour, its sizes alongside.

        The colour is a LABEL on those rows, not a button: picking a size on the
        row that says "Red shirt" already says which shirt, so a separate colour
        tap would only be a way to get the two out of step. An accessory has no
        sizes, so its colours ARE the choice and share a single row.
        """
        hidden, row = 0, []
        for colour in opts["colours"].get(item, []):
            pair = opts["pairs"].get(f"{item}|{colour}", "")
            blocked = (accessory and pair and d["shirt_colour"] not in ("", NONE_SIZE)
                       and pair.strip().lower() != d["shirt_colour"].strip().lower()
                       and not d.get("all_colours"))
            if blocked:
                hidden += 1
                row.append(InlineKeyboardButton(f"🚫 {colour}",
                                                callback_data="LOGI|NOP"))
                continue
            if accessory:
                # The row is labelled at the left like the shirt and pant rows,
                # so the icon does not repeat on every colour button.
                row.append(InlineKeyboardButton(
                    _picked(colour, colour == picked_colour),
                    callback_data=f"{colour_cb}|{colour}"))
            else:
                # Abbreviate only when the row is actually tight. Telegram gives
                # every button in a row the same width, so a shirt's five sizes
                # leave the label a sixth of the row and "White" clips — but
                # pants have three sizes and "Black" fits with room to spare.
                sizes = opts["sizes"].get(f"{item}|{colour}", [])
                text = (short_colour(colour, opts["colours"].get(item, []))
                        if len(sizes) >= 4 else colour)
                label = InlineKeyboardButton(f"{icon} {text}",
                                             callback_data="LOGI|NOP")
                rows_kb.append([label] + size_buttons(item, colour, picked_colour,
                                                      picked_size, size_cb))
        if row:
            rows_kb.append([InlineKeyboardButton(f"{icon} {label}",
                                                 callback_data="LOGI|NOP")] + row)
        return hidden

    item_rows("Shirt", "👕", None, "LOGI|DSH",
              d["shirt_colour"], d["shirt"])
    item_rows("Pants", "👖", None, "LOGI|DPS",
              d["pant_colour"], d["pants"])
    blocked_count = (
        item_rows("Waist Wrap", "🧣", "LOGI|DACC|waist", None,
                  d["waist"], accessory=True, label="Waist")
        + item_rows("Wrist Wrap", "🤲", "LOGI|DACC|wrist", None,
                    d["wrist"], accessory=True, label="Wrist")
        + item_rows("Head Band", "🎀", "LOGI|DACC|head", None,
                    d["head"], accessory=True, label="Head"))

    if blocked_count or d.get("all_colours"):
        rows_kb.append([InlineKeyboardButton(
            "\U0001f648 Only matching colours" if d.get("all_colours")
            else f"\U0001f441\ufe0f Show all colours ({blocked_count} hidden)",
            callback_data="LOGI|DALL")])
    rows_kb.append([InlineKeyboardButton(add_label, callback_data="LOGI|ADD")])
    rows_kb.append([InlineKeyboardButton("\U0001f519 Back",
                                         callback_data=f"LOGI|EV|{eid}")])

    await _edit(query, text, InlineKeyboardMarkup(rows_kb))


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
                               "🔙 Back", callback_data="LOGI|HOME")]]))

    partial = bool(ud.get("logi_partial")) and action == "return"
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
    if action == "return":
        lines += ["", ("_Pick-the-pieces mode is ON._" if partial
                       else "Tap a name to return the whole costume:")]
    else:
        lines += ["", "Tap a person:"]

    # Ordered and tagged like Take Attendance. Two people can share a first
    # name, and one person can have two open rows, so a button that is only a
    # name would be ambiguous — those few get the costume spelled out and a row
    # to themselves, since a long label paired with another is clipped.
    ok3, roster = await _read(get_member_roster, True)
    order = {n.strip().lower(): i for i, (n, _t) in enumerate(roster or [])}
    tags = {n.strip().lower(): tg for n, tg in (roster or [])}
    holdings = sorted(holdings, key=lambda h: (order.get(h.name.strip().lower(), 10 ** 6),
                                               h.name.lower(), h.row))
    seen = {}
    for h in holdings:
        seen[h.name] = seen.get(h.name, 0) + 1
    chunk, page, pages = paginate(holdings, page, _PAGE)

    # Same rule as everywhere else: signpost a long list, leave a short one
    # alone. The holders list is short after a small show and long after a big
    # one, so it needs the test rather than a fixed answer.
    show_dividers = len(holdings) >= _DIVIDER_MIN
    rows, buf, prev_group = [], [], None
    for h in chunk:
        tag = tags.get(h.name.strip().lower(), "")
        group = _tag_group(tag)
        if show_dividers and prev_group is not None and group != prev_group:
            if buf:
                rows.append(buf)
                buf = []
            rows.append(divider_row(group, "LOGI|NOP"))
        prev_group = group
        mark = "\U0001f4dd " if str(h.row) in staged else ""
        name = f"({tag}) {h.name}" if tag else h.name
        # A per-person ✂️ button would take half of every row — Telegram splits a
        # row evenly between its buttons and offers no width control — so the
        # partial case is a MODE at the top instead. Names then sit two to a row,
        # which halves the length of the list as well.
        if action == "return":
            cb = (f"LOGI|HSEL|return|{h.row}" if partial
                  else f"LOGI|HALL|{h.row}")
        else:
            cb = f"LOGI|HSEL|{action}|{h.row}"
        if seen[h.name] > 1:
            if buf:
                rows.append(buf)
                buf = []
            rows.append([InlineKeyboardButton(
                f"{mark}{name} \u00b7 {_holding_label_full(h)}", callback_data=cb)])
            continue
        buf.append(InlineKeyboardButton(mark + name, callback_data=cb))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)

    if action == "return":
        rows.insert(0, [InlineKeyboardButton(
            "\u2702\ufe0f Returning part of a costume \u2014 tap to switch off" if partial
            else "\u2702\ufe0f Only part of a costume? Tap here first",
            callback_data="LOGI|HPART")])

    nav = pagination_row(page, pages, lambda p: f"LOGI|HP|{action}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    if staged:
        rows.append([InlineKeyboardButton(f"📋 Review & Submit ({len(staged)})",
                                          callback_data="LOGI|REV")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|HOME")])
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
             f"_for {holding.event}_", "",
             f"Which pieces are being {'returned' if action == 'return' else 'passed on'}?"]
    # 2x2 grid: four pieces is a shape you read at a glance, where four stacked
    # full-width buttons is a list you have to scan.
    btns = [InlineKeyboardButton(
        _picked(_item_button_label(holding, key, label), key in chosen),
        callback_data=f"LOGI|RIT|{action}|{row}|{key}")
        for key, _cc, _sc, label, _ca, _sa in ITEM_FIELDS if key in available]
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

    lines = [f"\U0001f501 *Transfer \u2014 {holder}*", "",
             f"Held: {label} (from {event})", "", "Hand it to:"]
    # Same list, order, tags and dividers as Take Attendance: one member list
    # behaves the same everywhere. The tag is display only — the ledger is
    # written by nickname, so that is what the callback carries.
    rows = tagged_rows(chunk,
                       lambda n, tg: f"({tg}) {n}" if tg else n,
                       lambda n: f"LOGI|TRS|{row}|{n}",
                       "LOGI|NOP")
    nav = pagination_row(page, pages, lambda p: f"LOGI|TRT|{row}|{p}", "LOGI|NOP")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="LOGI|ACT|transfer")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _render_review(query, context) -> None:
    st = _stage(context.user_data)
    if not st["key"] or not st["items"]:
        return await _render_home(query)
    action, scope = st["key"]
    lines = [f"📋 *Review* — {_ACT_LABEL[action]}", ""]
    for _key, it in st["items"].items():
        if action == "issue":
            swap = " _(swap — old row closed)_" if it.get("swap_row") else ""
            bits = []
            if it.get("shirt"):
                bits.append(f"{it['shirt_colour'].lower()} shirt {it['shirt']}")
            if it.get("pants"):
                bits.append(f"{it['pant_colour'].lower()} pants "
                            f"{pant_size_label(Size(it['pants']))}")
            for key, label in _ACCESSORIES:
                if it.get(key) not in ("", NONE_SIZE, None):
                    bits.append(f"{it[key].lower()} {label.lower()}")
            lines.append(f"• {it['nick']} — " + (", ".join(bits) or "nothing picked") + swap)
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
        return await _render_home(query)
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
                                  shirt_colour=it["shirt_colour"] if it["shirt"] else NONE_SIZE,
                                  shirt_size=Size(it["shirt"]) if it["shirt"] else None,
                                  pant_colour=it["pant_colour"] if it["pants"] else NONE_SIZE,
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
