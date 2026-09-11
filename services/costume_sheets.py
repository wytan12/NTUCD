"""Costume Tracker data layer — the three COSTUME tabs of the main database.

These lived in a separate `Logistics AY 26/27` spreadsheet at first and were
folded back into `NTUFD AY26/27 Database`; nothing here assumes either, since
the file and tab names come from config and every position is detected.

Three tabs, three jobs:

* **COSTUME OVERVIEW** — all inventory *state*, in two blocks:
  - `A:F` **Main Inventory**, the manually-maintained stock totals, one row per
    `(Item, Set, Size)`. The only place the bot ever *writes* a stock number
    (✏️ Update Stock).
  - `H:P` the **RUNNING INVENTORY** matrix. Every cell is a `SUMIFS` pulling
    Total from Main Inventory beside it and Out from the COSTUME TRACKING
    ledger, so Total / Out / On hand can never drift from the records they
    summarise. The bot **reads this and never writes to it** — the same rule
    the ATTENDANCE `Tabulation` column follows.
* **COSTUME TRACKING** — inventory *events*: a flat ledger, header on row 1,
  one row per (performance, member) issue, closed by stamping a Returned or
  Transferred date. Nothing above it, so it stays sortable and filterable.
* **COSTUME SIZE** — per-member default sizes, one column per set
  (`Red Shirt Size` / `White Shirt Size`) plus `Pant Size`. Issuing writes back
  into it: what a member was handed IS their size.

A **transfer** closes the giver's row *and opens one for the receiver*, so a
costume handed from A to B stays counted as out — the giver's row is
released and a fresh one opened for the receiver.

Row/column positions are *detected*, never hardcoded: the running block is found
by its `Metric` header cells and the ledger by its `Performances` header, so
inserting a section or reordering ledger columns in the sheet does not break the
bot. All reads go through the shared `get_cached_values` smart cache, keyed by
`(sheet_name, tab_name)`, and every write invalidates it. Since these tabs now
sit in the main database, a costume write drops that whole file's cached tabs —
which is what Drive's per-FILE `modifiedTime` would have done anyway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config import (LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB, COSTUME_TRACKING_TAB,
                    COSTUME_SIZE_TAB, sg_tz)
from services.google_sheets import (get_cached_values, get_gspread_sheet,
                                    invalidate_sheet_cache)


# ---------------------------------------------------------------------------
# Categories — the sheet's vocabulary, spelled exactly once
# ---------------------------------------------------------------------------
# Every value below is the literal string stored in the sheet. The running
# table's SUMIFS match on these strings, so a typo here silently zeroes a
# column; keeping them in enums means the rest of the codebase can never
# introduce one.

# NOTE: the Overview `Colour` column IS matched on — `adjust_stock` finds its row
# by (Item, Colour, Size) and every running-table SUMIFS filters on it — so a
# colour's spelling must agree between the sheet and anything the bot writes.
# What the bot never does is *invent* a colour: option lists come from the sheet.
class Colour(str, Enum):
    """Colours we happen to ship with. NOT a closed category — see `list_colours`.

    Which colours exist is a purchasing decision, so a colour travels through
    the code as the plain string stored in the sheet. These members remain only
    as named constants for values the code defaults to; they are deliberately
    NOT used to validate, or a newly bought colour could be stocked and never
    issued.
    """
    RED = "Red"
    WHITE = "White"
    BLACK = "Black"
    YELLOW = "Yellow"

    @property
    def dot(self) -> str:
        return {"Red": "🔴", "White": "⚪", "Black": "⚫", "Yellow": "🟡"}.get(self.value, "▪️")


class Size(str, Enum):
    XS = "XS"
    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


class Item(str, Enum):
    """`Item` column of Costume Overview. Open for the same reason as CostumeSet:
    a new set may bring a garment type nobody has coded for, so callers pass the
    sheet's string and these members are just the known names."""
    SHIRT = "Shirt"
    PANTS = "Pants"
    WAIST_WRAP = "Waist Wrap"
    WRIST_WRAP = "Wrist Wrap"
    HEAD_BAND = "Head Band"

    @property
    def sized(self) -> bool:
        return self in (Item.SHIRT, Item.PANTS)


class LedgerStatus(str, Enum):
    ISSUED = "Issued"
    RETURNED = "Returned"
    TRANSFERRED = "Transferred"


NONE_SIZE = "-"          # "no item of this type on this row" — matched by nothing


def pant_size_label(size: Size) -> str:
    """The sheet's own label for a pant size, e.g. Size.M -> `'M - 170'`.

    Looked up in Main Inventory rather than hardcoded, so a re-labelled run needs
    no code change. This label is the **key** the running table's pants `Out`
    filters on, so the ledger must store it verbatim — writing a bare `'M'`
    would leave that column counting nothing. Falls back to the bare size when
    no matching row exists.
    """
    want = (size.value if isinstance(size, Size) else str(size)).strip().upper()
    try:
        for item, _colour, label, _qty, _pairs in get_stock_rows():
            if (item.strip().lower().startswith(Item.PANTS.value.lower())
                    and label.strip().upper().split("-")[0].strip() == want):
                return label.strip()
    except Exception as e:                       # noqa: BLE001 — labelling is cosmetic
        print(f"[LOGI][WARN] pant label lookup failed: {e}")
    return want


def parse_size(raw) -> Size | None:
    """`'M'`, `'m'`, `'M - 170'` -> Size.M. Unknown/blank -> None."""
    text = str(raw or "").strip().upper()
    if not text:
        return None
    head = text.split("-")[0].strip()
    try:
        return Size(head)
    except ValueError:
        return None


def _enum_or_none(cls, raw):
    text = str(raw or "").strip()
    for member in cls:
        if member.value.lower() == text.lower():
            return member
    return None


# ---------------------------------------------------------------------------
# Running inventory (read-only)
# ---------------------------------------------------------------------------

@dataclass
class InventoryItem:
    name: str                       # display name, e.g. "Wrist Wrap (pairs)"
    color: str
    sized: bool
    sizes: list[str]                # size header labels for this section
    total: list[int] = field(default_factory=list)
    out: list[int] = field(default_factory=list)
    on_hand: list[int] = field(default_factory=list)
    total_all: int = 0
    out_all: int = 0
    on_hand_all: int = 0


@dataclass
class InventorySection:
    title: str
    sizes: list[str]
    items: list[InventoryItem] = field(default_factory=list)


_SIZE_SPAN = 5          # five size columns per section


def _cell(row, idx) -> str:
    return (row[idx] if idx < len(row) else "").strip()


def _int(row, idx) -> int:
    text = _cell(row, idx).replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return 0


def _invalidate_costume():
    """Drop the costume snapshots after a write — and only those.

    Invalidating by spreadsheet would now throw away ATTENDANCE and MEMBER INFO
    too, since these tabs share a file with them. All three costume tabs go
    together because a ledger write also changes the Overview running table:
    its Out cells are formulas over the ledger.
    """
    for tab in (COSTUME_TRACKING_TAB, COSTUME_OVERVIEW_TAB, COSTUME_SIZE_TAB):
        invalidate_sheet_cache(LOGISTICS_SHEET, tab)


def _cvals(tab: str, force: bool = False):
    """Every Costume Tracker read goes through here.

    `trust_cache` means: serve the snapshot, ask Google nothing. Costume data
    only changes when this module writes it — and every write invalidates the
    cache — so re-checking on each screen would spend API calls to be told
    nothing changed. It also stops an unrelated write elsewhere in the database
    (attendance, a member joining) from invalidating these tabs, which is what
    sharing a spreadsheet with them would otherwise cost.

    A hand edit in the web UI is picked up by ♻️ Refresh Data, by the next
    bot write, or by the 10-minute ceiling in `_TRUSTED_CACHE_TTL_SECONDS`.
    """
    return get_cached_values(LOGISTICS_SHEET, tab, force=force, trust_cache=not force)


def _tracking_values(force=False):
    return _cvals(COSTUME_TRACKING_TAB, force=force)


def _overview_values(force=False):
    return _cvals(COSTUME_OVERVIEW_TAB, force=force)


def _running_header(values) -> int | None:
    """Column index of the running block's `Colour` header, or None if absent.

    `Total` must follow `Colour | Size`, which is what separates this from MAIN
    INVENTORY's own `Colour | Size | Quantity` header sitting in the same rows.
    """
    def at(row, i):
        return (row[i] if i < len(row) else "").strip().lower()

    for row in values:
        for idx, cell in enumerate(row):
            if ((cell or "").strip().lower() == "colour"
                    and at(row, idx + 1) == "size" and at(row, idx + 2) == "total"):
                return idx
    return None


def get_running_inventory(force=False) -> list[InventorySection]:
    """Parse the RUNNING INVENTORY block into one section per ITEM.

    Grouped by item: a banner naming it, a `Colour | Size | Total | Out |
    On hand` header, then one row per colour and size — the colour written only
    on its first row. Located by that header rather than by row number, so a
    colour, size or item added by `rebuild_running_block` needs no change here.
    """
    values = _cvals(COSTUME_OVERVIEW_TAB, force=force)
    head = _running_header(values)
    if head is None:
        return []
    c_colour, c_size, c_total, c_out, c_hand = head, head + 1, head + 2, head + 3, head + 4

    sections: list[InventorySection] = []
    current: InventorySection | None = None
    per_colour: dict[str, dict] = {}
    colour = ""

    def flush():
        """Turn the rows collected for one item into its InventoryItem list."""
        if current is None:
            return
        for name, data in per_colour.items():
            sized = data["sizes"] != [NONE_SIZE]
            current.items.append(InventoryItem(
                name=current.title.title(), color=name, sized=sized,
                sizes=data["sizes"] if sized else [],
                total=data["total"] if sized else [],
                out=data["out"] if sized else [],
                on_hand=data["hand"] if sized else [],
                total_all=sum(data["total"]), out_all=sum(data["out"]),
                on_hand_all=sum(data["hand"]),
            ))
        if not current.sizes:
            current.sizes = next((d["sizes"] for d in per_colour.values()
                                  if d["sizes"] != [NONE_SIZE]), [])

    for row in values:
        first = _cell(row, c_colour)
        size = _cell(row, c_size)
        if first.lower() == "colour" and size.lower() == "size":
            continue                              # the table header itself
        if first and not size and not _cell(row, c_total):
            if first.lower().startswith("running"):
                continue                          # the block's own title row
            flush()                               # a banner starts a new item
            per_colour = {}
            current = InventorySection(title=first, sizes=[])
            sections.append(current)
            colour = ""
            continue
        if current is None or not size:
            continue
        colour = first or colour                  # blank = same colour as above
        data = per_colour.setdefault(colour, {"sizes": [], "total": [], "out": [],
                                              "hand": []})
        data["sizes"].append(size)
        data["total"].append(_int(row, c_total))
        data["out"].append(_int(row, c_out))
        data["hand"].append(_int(row, c_hand))
    flush()
    return [sec for sec in sections if sec.items]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

LEDGER_HEADERS = ["Performances", "Name", "Shirt Colour", "Shirt Size",
                  "Pant Colour", "Pant Size", "Waist Wrap", "Wrist Wrap",
                  "Head Band", "Status", "Issued date", "Returned date",
                  "Transferred date", "Transferred To", "Remarks"]


@dataclass
class Holding:
    """One open ledger row: someone holding a costume, not yet returned."""
    row: int
    name: str
    event: str
    # Every item carries its OWN colour, because a costume is not a bundle: a
    # red shirt is worn with a yellow waist wrap and a black wrist wrap. Colours
    # are raw sheet strings — a newly bought colour must not need a code change.
    shirt_colour: str                # "-" when no shirt went out
    pant_colour: str
    shirt_size: Size | None
    pant_size: Size | None
    waist: str                       # colour, or "-"
    wrist: str
    head: str


def _ledger_layout(values) -> tuple[int, dict[str, int]]:
    """Return `(header_row_number, {header_lower: col_index})` for the ledger."""
    for i, row in enumerate(values):
        if _cell(row, 0).lower() == "performances":
            return i + 1, {(c or "").strip().lower(): n for n, c in enumerate(row) if (c or "").strip()}
    raise LookupError("ledger header row ('Performances') not found in Costume Tracking")


def get_ledger(force=False) -> tuple[int, dict[str, int], list[tuple[int, list]]]:
    """`(header_row, col_map, [(row_number, raw_row), ...])` for ledger data rows."""
    values = _tracking_values(force=force)
    header_row, cols = _ledger_layout(values)
    name_col = cols["name"]
    rows = [(i + 1, r) for i, r in enumerate(values)
            if i + 1 > header_row and _cell(r, name_col)]
    return header_row, cols, rows


@dataclass
class Outstanding:
    """One performance's return status."""
    performance: str
    people_out: int          # distinct members still holding something
    rows_out: int            # open ledger rows
    rows_issued: int         # every row ever written for this performance
    last_issued: str         # the most recent Issued date seen, for ordering


def outstanding_by_performance(force=False) -> list[Outstanding]:
    """Per performance: how many people have not returned yet.

    Counted from the ledger rather than from PERF TABULATION, because the
    question is about costumes, not attendance — someone marked for a show who
    was never issued anything cannot be outstanding, and someone issued for a
    show they did not end up performing in still has to bring it back.

    A row is open on exactly the same test the running table's `Out` uses: no
    Returned date and no Transferred date. Ordered by most recently issued, so
    the show you are running now is at the top.
    """
    _hdr, cols, rows = get_ledger(force=force)

    def col(row, key):
        idx = cols.get(key)
        return _cell(row, idx) if idx is not None else ""

    seen: dict[str, dict] = {}
    for _n, row in rows:
        perf = col(row, "performances") or "(no performance)"
        name = col(row, "name")
        if not name:
            continue
        data = seen.setdefault(perf, {"people": set(), "out": 0, "total": 0, "last": ""})
        data["total"] += 1
        data["last"] = max(data["last"], col(row, "issued date") or "")
        if not col(row, "returned date") and not col(row, "transferred date"):
            data["out"] += 1
            data["people"].add(name.strip().lower())

    out = [Outstanding(performance=perf, people_out=len(d["people"]), rows_out=d["out"],
                       rows_issued=d["total"], last_issued=d["last"])
           for perf, d in seen.items()]
    # Dates are DD/MM/YYYY, so sort on the parts rather than the string.
    def key(o):
        bits = o.last_issued.split("/")
        return tuple(int(b) for b in reversed(bits)) if len(bits) == 3 else (0, 0, 0)
    out.sort(key=key, reverse=True)
    return out


def get_open_holdings(force=False) -> list[Holding]:
    """Everyone currently holding — no returned date and no transferred date.

    Deliberately keyed on the dates rather than on `Status`, because that is
    exactly what the running table's `Out` formulas count. If the two disagreed,
    the bot's holder list and the sheet's numbers would tell different stories.
    """
    _, cols, rows = get_ledger(force=force)

    def col(row, key, default=""):
        idx = cols.get(key)
        return _cell(row, idx) if idx is not None else default

    holdings = []
    for number, row in rows:
        if col(row, "returned date") or col(row, "transferred date"):
            continue
        holdings.append(Holding(
            row=number,
            name=col(row, "name"),
            event=col(row, "performances"),
            shirt_colour=col(row, "shirt colour") or NONE_SIZE,
            pant_colour=col(row, "pant colour") or NONE_SIZE,
            shirt_size=parse_size(col(row, "shirt size")),
            pant_size=parse_size(col(row, "pant size")),
            waist=col(row, "waist wrap") or NONE_SIZE,
            wrist=col(row, "wrist wrap") or NONE_SIZE,
            head=col(row, "head band") or NONE_SIZE,
        ))
    return holdings


def _today() -> str:
    return datetime.now(sg_tz).strftime("%d/%m/%Y")


def _col_letter(idx: int) -> str:
    letter, idx = "", idx + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letter = chr(65 + rem) + letter
    return letter


@dataclass
class IssueEntry:
    """One costume going out — every item named by its own colour.

    `NONE_SIZE` ("-") in any field means that item was not issued. A pants-only
    or shirt-only loan is ordinary, and each item's `Out` formula filters on its
    own column, so a "-" simply stops that item counting.
    """
    name: str
    event: str
    shirt_colour: str = NONE_SIZE
    shirt_size: Size | None = None
    pant_colour: str = NONE_SIZE
    pant_size: Size | None = None
    waist: str = NONE_SIZE
    wrist: str = NONE_SIZE
    head: str = NONE_SIZE
    remarks: str = ""


def append_issues(entries: list[IssueEntry]) -> int:
    """Append issue rows to the ledger in a single write. Returns rows written."""
    if not entries:
        return 0
    header_row, cols, rows = get_ledger(force=True)
    first_free = (rows[-1][0] if rows else header_row) + 1
    width = max(cols.values()) + 1
    today = _today()

    block = []
    for e in entries:
        row = [""] * width
        for key, val in (("performances", e.event), ("name", e.name),
                         ("shirt colour", e.shirt_colour or NONE_SIZE),
                         ("shirt size", e.shirt_size.value if e.shirt_size else NONE_SIZE),
                         ("pant colour", e.pant_colour or NONE_SIZE),
                         ("pant size",
                          pant_size_label(e.pant_size) if e.pant_size else NONE_SIZE),
                         ("waist wrap", e.waist or NONE_SIZE),
                         ("wrist wrap", e.wrist or NONE_SIZE),
                         ("head band", e.head or NONE_SIZE),
                         ("status", LedgerStatus.ISSUED.value),
                         ("issued date", today), ("remarks", e.remarks)):
            if key in cols:
                row[cols[key]] = val
        block.append(row)

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_TRACKING_TAB)
    end = _col_letter(width - 1)
    ws.update(values=block, range_name=f"A{first_free}:{end}{first_free + len(block) - 1}",
              value_input_option="USER_ENTERED")
    _invalidate_costume()
    return len(block)


@dataclass
class Closure:
    """One ledger row being closed. `to` is only meaningful for a transfer."""
    row: int
    status: LedgerStatus
    remarks: str = ""
    to: str = ""


def set_row_performance(row: int, performance: str) -> bool:
    """Retag an open ledger row with the performance it is now being kept for.

    A costume kept across shows never physically moves, so nothing is closed and
    nothing is appended — only this cell changes, and `Out` is untouched. Safe
    because `Performances` is the one ledger column no running-table formula
    filters on, and the `Issued date` still records when it first went out.

    Without this the tag goes stale the moment a costume is kept for a second
    show, and "still holding for the next performance" becomes indistinguishable
    from "never gave it back".
    """
    _hdr, cols, _rows = get_ledger(force=True)
    if "performances" not in cols:
        print("[LOGI][WARN] ledger has no Performances column")
        return False
    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_TRACKING_TAB)
    ws.update_cell(row, cols["performances"] + 1, performance)
    _invalidate_costume()
    return True


def close_rows(closures: list[Closure]) -> int:
    """Stamp Returned/Transferred date + Status on existing ledger rows.

    All cells go up in one `batch_update`, so a 20-person return costs one write
    rather than twenty.
    """
    if not closures:
        return 0
    _, cols, _ = get_ledger(force=True)
    today = _today()
    payload = []

    for c in closures:
        date_key = ("returned date" if c.status is LedgerStatus.RETURNED
                    else "transferred date")
        cells = {"status": c.status.value, date_key: today}
        if c.remarks:
            cells["remarks"] = c.remarks
        if c.to:
            cells["transferred to"] = c.to
        for key, val in cells.items():
            if key in cols:
                a1 = f"{_col_letter(cols[key])}{c.row}"
                payload.append({"range": a1, "values": [[val]]})

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_TRACKING_TAB)
    ws.batch_update(payload, value_input_option="USER_ENTERED")
    _invalidate_costume()
    return len(closures)


def costume_size_layout(force=False) -> tuple[dict[str, int], int | None]:
    """`({set_name: column_index}, pant_size_column)` read from the header row.

    A member's shirt size differs per costume set ("Red Shirt Size", "White Shirt
    Size"), and more columns appear when a set is bought — so the columns are
    matched by header, never by position. Reading them positionally is what made
    the pant default silently pick up the White Shirt column.
    """
    values = _cvals(COSTUME_SIZE_TAB, force=force)
    header = values[0] if values else []
    shirt_cols: dict[str, int] = {}
    pant_col = None
    for idx, raw in enumerate(header):
        name = (raw or "").strip()
        low = name.lower()
        if low.endswith("shirt size"):
            set_name = name[: -len("Shirt Size")].strip()
            if set_name:
                shirt_cols[set_name] = idx
        elif low.startswith("pant"):
            pant_col = idx
    return shirt_cols, pant_col


def get_costume_sizes(force=False) -> dict[str, dict]:
    """`{nickname: {'shirt': {set: Size|None}, 'pants': Size|None}}`.

    Shirt sizes are keyed by costume set, so the issue screen can seed the size
    for whichever set is actually selected.
    """
    values = _cvals(COSTUME_SIZE_TAB, force=force)
    shirt_cols, pant_col = costume_size_layout(force=force)
    sizes: dict[str, dict] = {}
    for row in values[1:]:
        nick = _cell(row, 0)
        if not nick or nick.lower() == "nickname":
            continue
        sizes[nick] = {
            "shirt": {st: parse_size(_cell(row, c)) for st, c in shirt_cols.items()},
            "pants": parse_size(_cell(row, pant_col)) if pant_col is not None else None,
        }
    return sizes


def record_sizes_from_issues(entries) -> int:
    """Learn a member's sizes from what they were actually issued.

    Handing someone an M shirt IS the measurement — more reliable than whatever
    was typed into Costume Size months ago — so an issue writes back the sizes it
    used, and the next issue screen pre-fills them. Shirt goes to the column of
    the set it was issued for (`Red Shirt Size` / `White Shirt Size`), matched by
    header so a newly bought set lands in its own column.

    Only cells that actually differ are written, and everything goes up in one
    `batch_update`: a 30-performer show would otherwise cost 60 single-cell
    writes against a 60-per-minute quota. Returns the number of cells written.
    """
    if not entries:
        return 0
    shirt_cols, pant_col = costume_size_layout(force=True)
    values = _cvals(COSTUME_SIZE_TAB, force=True)
    rows = {_cell(r, 0).strip().lower(): i + 1 for i, r in enumerate(values) if _cell(r, 0)}
    width = max([*shirt_cols.values()] + [pant_col if pant_col is not None else 0]) + 1

    updates, appended = [], []

    def current(row_no, col0):
        row = values[row_no - 1] if row_no - 1 < len(values) else []
        return _cell(row, col0).strip()

    for e in entries:
        nick = (e.name or "").strip()
        if not nick:
            continue
        wanted = []
        col = next((c for st, c in shirt_cols.items()
                    if st.lower() == (e.shirt_colour or "").strip().lower()), None)
        if e.shirt_size and col is not None:
            wanted.append((col, e.shirt_size.value))
        if e.pant_size and pant_col is not None:
            wanted.append((pant_col, e.pant_size.value))
        if not wanted:
            continue

        row_no = rows.get(nick.lower())
        if row_no is None:                       # member not on the size sheet yet
            new_row = next((r for n, r in appended if n == nick.lower()), None)
            if new_row is None:
                new_row = [""] * width
                new_row[0] = nick
                appended.append((nick.lower(), new_row))
            for col0, val in wanted:
                new_row[col0] = val
            continue
        for col0, val in wanted:
            if current(row_no, col0) != val:     # skip what already matches
                updates.append({'range': f"{_col_letter(col0)}{row_no}", 'values': [[val]]})

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_SIZE_TAB)
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    if appended:
        ws.append_rows([r for _n, r in appended], value_input_option="USER_ENTERED")
    if updates or appended:
        _invalidate_costume()
    return len(updates) + sum(1 for _ in appended)


def shirt_size_for(entry: dict | None, colour: str, strict: bool = False) -> Size | None:
    """That member's shirt size for one colour.

    `strict=False` falls back to any colour they have a size for — right when
    SEEDING a screen the admin then looks at and can correct. `strict=True`
    refuses to substitute, for callers that write without showing anything:
    a red M is not evidence of a white M, and once it is in the ledger a
    borrowed size is indistinguishable from a measured one.
    """
    if not entry:
        return None
    per_colour = entry.get("shirt") or {}
    for name, size in per_colour.items():
        if size and name.lower() == str(colour).strip().lower():
            return size
    if strict:
        return None
    return next((s for s in per_colour.values() if s), None)


def set_costume_size(nickname: str, garment: str, size: Size) -> bool:
    """Write one member's default size, appending a row if they're new.

    `garment` is `"pants"` or `"shirt:<set>"` (e.g. `"shirt:Red"`), resolved
    against the header row so a newly added set's column is written correctly.
    """
    shirt_cols, pant_col = costume_size_layout(force=True)
    if garment.startswith("shirt:"):
        wanted = garment.split(":", 1)[1].strip().lower()
        col0 = next((c for st, c in shirt_cols.items() if st.lower() == wanted), None)
    else:
        col0 = pant_col
    if col0 is None:
        print(f"[LOGI][WARN] no Costume Size column for {garment!r}")
        return False

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_SIZE_TAB)
    values = _cvals(COSTUME_SIZE_TAB, force=True)
    for i, row in enumerate(values):
        if _cell(row, 0).lower() == nickname.lower():
            ws.update_cell(i + 1, col0 + 1, size.value)
            _invalidate_costume()
            return True

    new_row = [""] * (max(col0, 0) + 1)
    new_row[0] = nickname
    new_row[col0] = size.value
    ws.append_row(new_row, value_input_option="USER_ENTERED")
    _invalidate_costume()
    return True


# ---------------------------------------------------------------------------
# Stock corrections (the one place the bot writes a quantity)
# ---------------------------------------------------------------------------

def add_stock_row(item: str, colour: str, size: str, quantity: int,
                  pairs_with: str = "", notes: str = "") -> tuple[bool, str]:
    """Append one `(Item, Colour, Size)` row to Main Inventory.

    Written into the first blank row INSIDE the formula region, never at the
    bottom of the sheet: every Total is a SUMIFS bounded at `INV_LAST_ROW`, so a
    row below that bound is stocked but invisible to every count.

    Refuses a duplicate `(Item, Colour, Size)` — two rows for one thing would
    make `adjust_stock` edit whichever it found first and the SUMIFS quietly add
    them together. Returns `(ok, message)`.
    """
    item, colour = item.strip(), colour.strip()
    size = (size or NONE_SIZE).strip() or NONE_SIZE
    if not item or not colour:
        return False, "an item and a colour are both needed"

    values = _cvals(COSTUME_OVERVIEW_TAB, force=True)
    header_row, cols = _stock_layout(values)
    for existing in get_stock_rows(force=False):
        if (existing[0].strip().lower(), existing[1].strip().lower(),
                existing[2].strip().lower()) == (item.lower(), colour.lower(),
                                                 size.lower()):
            return False, f"{item} {colour} {size} is already in Main Inventory"

    target = None
    for n in range(header_row + 1, INV_LAST_ROW + 1):
        row = values[n - 1] if n - 1 < len(values) else []
        if not _cell(row, cols["item"]):
            target = n
            break
    if target is None:
        return False, (f"no blank row left between {header_row + 1} and "
                       f"{INV_LAST_ROW} — the formula region is full")

    width = max(cols.values()) + 1
    line = [""] * width
    for key, value in (("item", item), ("colour", colour), ("size", size),
                       ("quantity", quantity), ("pairs with", pairs_with),
                       ("notes", notes)):
        if key in cols:
            line[cols[key]] = value

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    ws.update(values=[line],
              range_name=f"A{target}:{_col_letter(width - 1)}{target}",
              value_input_option="USER_ENTERED")
    _invalidate_costume()
    return True, f"row {target}"


def adjust_stock(item, set_or_type: str, size_label: str, delta: int) -> int | None:
    """Add `delta` to one Costume Overview `(Item, Set, Size)` row's Quantity.

    Returns the new quantity, or None when no matching row exists. Never goes
    below zero. This writes to Costume Overview, so the running table's Total
    (and therefore On hand) picks the change up on its own.
    """
    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    values = _cvals(COSTUME_OVERVIEW_TAB, force=True)
    for i, row in enumerate(values):
        want_item = (item.value if isinstance(item, Item) else str(item)).strip().lower()
        if (_cell(row, 0).lower() == want_item
                and _cell(row, 1).lower() == str(set_or_type).lower()
                and _cell(row, 3).lower() == str(size_label).lower()):
            new = max(0, _int(row, 4) + delta)
            ws.update_cell(i + 1, 5, new)
            _invalidate_costume()
            return new
    return None


def get_stock_rows(force=False) -> list[tuple[str, str, str, int, str]]:
    """`[(item, colour, size, quantity, pairs_with), ...]` from Main Inventory.

    `(item, colour, size)` is the identity of a thing the club owns — a Red
    shirt in M, a Yellow waist wrap. `pairs_with` names the shirt colour an
    accessory belongs with; it drives one UI hint and gates nothing.

    Columns are matched by header, and the table stops at the first blank Item:
    column A continues below with free-text bookkeeping ("Last Updated",
    "Signed Off"), which would otherwise be served as inventory.
    """
    values = _cvals(COSTUME_OVERVIEW_TAB, force=force)
    header_row, cols = _stock_layout(values)
    rows = []
    for row in values[header_row:]:
        item = _cell(row, cols["item"])
        if not item:
            break
        rows.append((item, _cell(row, cols["colour"]), _cell(row, cols["size"]),
                     _int(row, cols["quantity"]),
                     _cell(row, cols["pairs with"]) if "pairs with" in cols else ""))
    return rows


def _stock_layout(values) -> tuple[int, dict[str, int]]:
    """`(header_row_number, {header_lower: col_index})` for Main Inventory.

    Located by the row whose first cell is `Item`, so the block can move and
    columns can be reordered without touching the code.
    """
    for i, row in enumerate(values):
        if _cell(row, 0).strip().lower() == "item":
            cols = {(c or "").strip().lower(): n for n, c in enumerate(row[:8]) if (c or "").strip()}
            if "colour" not in cols and "color" in cols:
                cols["colour"] = cols["color"]
            return i + 1, cols
    raise LookupError("Main Inventory header row ('Item') not found in Costume Overview")


# ---------------------------------------------------------------------------
# Categories that actually exist, read from the sheet
# ---------------------------------------------------------------------------
# Main Inventory is the register of what the club owns, so it — not an enum —
# decides which sets and pant types the UI may offer.

def list_items(force=False) -> list[str]:
    """Item names present in Main Inventory, in sheet order."""
    seen = []
    for item, _c, _s, _q, _p in get_stock_rows(force=force):
        if item not in seen:
            seen.append(item)
    return seen


def list_colours(item: str, force=False) -> list[str]:
    """Colours Main Inventory stocks for one item, in sheet order.

    This is the option list every colour picker is built from, which is what
    lets a newly bought colour appear in the bot the moment it is stocked —
    no enum, no deploy.
    """
    want = str(item).strip().lower()
    seen = []
    for it, colour, _s, _q, _p in get_stock_rows(force=force):
        if it.strip().lower() == want and colour and colour not in seen:
            seen.append(colour)
    return seen


def sizes_for(item: str, force=False) -> list[str]:
    """Size labels stocked for one item, in sheet order. Empty for unsized items."""
    want = str(item).strip().lower()
    seen = []
    for it, _c, size, _q, _p in get_stock_rows(force=force):
        if it.strip().lower() == want and size and size != NONE_SIZE and size not in seen:
            seen.append(size)
    return seen


def pairs_with(item: str, colour: str, force=False) -> str:
    """The shirt colour this item+colour belongs with, or "" for "anything".

    Read from Main Inventory's `Pairs with` column. It exists to grey out the
    wraps that do not go with the shirt just chosen — a guard rail on the issue
    screen, never a rule: it gates no write, so a blank or a typo costs a hint
    and nothing more.
    """
    want_i, want_c = str(item).strip().lower(), str(colour).strip().lower()
    for it, col, _s, _q, pair in get_stock_rows(force=force):
        if it.strip().lower() == want_i and col.strip().lower() == want_c:
            return pair
    return ""



# ---------------------------------------------------------------------------
# Rebuilding the running block
# ---------------------------------------------------------------------------
# The running block is generated, not hand-written. Adding a colour, a size or a
# whole item to Main Inventory and pressing rebuild is all it takes — the shape,
# the formulas and the colour coding are all derived from what the sheet says
# it owns. Hand-editing 40 SUMIFS is how a column ends up pointing at the wrong
# range and silently reading zero.
#
# This is the ONE place the bot writes into the running block, and it writes
# FORMULAS, never numbers: the sheet still computes every figure. Values stay
# read-only for the same reason as ATTENDANCE's Tabulation column.

RUNNING_FIRST_COL = 7        # H — the block starts here, beside Main Inventory
INV_LAST_ROW = 60            # Main Inventory rows every Total SUMIFS spans. Bounded
                             # ABOVE the free-text bookkeeping, and deliberately
                             # past today's last row so new stock can be appended
                             # without falling outside the formulas.
LEDGER_LAST_ROW = 994        # bound every Out COUNTIFS spans
_SIZE_SLOTS = 5              # size columns per section; the widest item wins


def _ledger_columns_for(item: str, cols: dict) -> tuple[str | None, str | None]:
    """`(colour_column, size_column)` in the ledger for one item, by header.

    Matched by name so a newly stocked item needs no code: a `Sash` item finds
    `Sash Colour`/`Sash Size`, or a plain `Sash` column when it has no sizes.
    Plural is tried both ways, since the sheet says `Pants` but `Pant Size`.
    """
    base = item.strip().lower()
    names = [base] + ([base[:-1]] if base.endswith("s") else [base + "s"])
    for name in names:
        if f"{name} colour" in cols and f"{name} size" in cols:
            return _col_letter(cols[f"{name} colour"]), _col_letter(cols[f"{name} size"])
    for name in names:
        if name in cols:
            return _col_letter(cols[name]), None
    return None, None


def rebuild_running_block(force=True) -> tuple[bool, str]:
    """Regenerate the RUNNING INVENTORY block from Main Inventory and the ledger.

    One group per item: a banner naming it, then `Colour | Size | Total | Out |
    On hand` with one row per colour and size. Metrics are COLUMNS rather than
    three stacked rows, so an item with no sizes is one line instead of three
    rows trailing five empty size columns.

    The colour is written only on its first row, the way you would write it by
    hand. Returns `(ok, message)`.
    """
    values = _cvals(COSTUME_OVERVIEW_TAB, force=force)
    try:
        inv_header, inv_cols = _stock_layout(values)
    except LookupError as e:
        return False, str(e)
    _lh, ledger_cols = _ledger_layout(_tracking_values(force=force))

    stock = get_stock_rows(force=force)
    if not stock:
        return False, "Main Inventory is empty — nothing to build from."

    rng = lambda letter: f"${letter}${inv_header}:${letter}${INV_LAST_ROW}"   # noqa: E731
    qty = rng(_col_letter(inv_cols["quantity"]))
    i_col = rng(_col_letter(inv_cols["item"]))
    c_col = rng(_col_letter(inv_cols["colour"]))
    z_col = rng(_col_letter(inv_cols["size"]))

    tab = COSTUME_TRACKING_TAB
    lcol = lambda header: _col_letter(ledger_cols[header])                    # noqa: E731
    open_row = (f"'{tab}'!${lcol('returned date')}$2:${lcol('returned date')}${LEDGER_LAST_ROW},\"\","
                f"'{tab}'!${lcol('transferred date')}$2:${lcol('transferred date')}${LEDGER_LAST_ROW},\"\","
                f"'{tab}'!${lcol('name')}$2:${lcol('name')}${LEDGER_LAST_ROW},\"<>\"")

    grouped: dict[str, list[tuple[str, str]]] = {}
    for item, colour, size, _q, _p in stock:
        grouped.setdefault(item, []).append((colour, size or NONE_SIZE))

    grid = [["RUNNING INVENTORY", "",
             f"auto-calculated from Main Inventory + {tab} — do not type here",
             "", ""], ["", "", "", "", ""]]
    banners, row_no, skipped = [], 2, []

    for item, pairs in grouped.items():
        col_colour, col_size = _ledger_columns_for(item, ledger_cols)
        if col_colour is None:
            skipped.append(item)          # nothing in the ledger records it
            continue
        grid.append([item.upper(), "", "", "", ""])
        banners.append(row_no + 1)
        grid.append(["Colour", "Size", "Total", "Out", "On hand"])
        row_no += 2

        last_colour = None
        for colour, size in pairs:
            row_no += 1
            esc, z_esc = colour.replace('"', '""'), str(size).replace('"', '""')
            total = (f'=SUMIFS({qty},{i_col},"{item}",{c_col},"{esc}",'
                     f'{z_col},"{z_esc}")')
            if col_size and size != NONE_SIZE:
                out = (f"=COUNTIFS('{tab}'!${col_colour}$2:${col_colour}${LEDGER_LAST_ROW},"
                       f"\"{esc}\",'{tab}'!${col_size}$2:${col_size}${LEDGER_LAST_ROW},"
                       f"\"{z_esc}\",{open_row})")
            else:
                out = (f"=COUNTIFS('{tab}'!${col_colour}$2:${col_colour}${LEDGER_LAST_ROW},"
                       f"\"{esc}\",{open_row})")
            grid.append([colour if colour != last_colour else "", size, total, out,
                         f"=J{row_no}-K{row_no}"])
            last_colour = colour
        grid.append(["", "", "", "", ""])
        row_no += 1

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    _unmerge_running(ws)
    first = _col_letter(RUNNING_FIRST_COL)
    ws.batch_clear([f"{first}1:{_col_letter(RUNNING_FIRST_COL + 8)}"
                    f"{max(len(grid) + 40, 80)}"])
    ws.update(values=grid,
              range_name=f"{first}1:{_col_letter(RUNNING_FIRST_COL + 4)}{len(grid)}",
              value_input_option="USER_ENTERED")
    _style_running(ws, banners, len(grid))
    _invalidate_costume()

    note = f"{len(banners)} item(s), {sum(len(v) for v in grouped.values())} row(s)"
    if skipped:
        note += f" — skipped {', '.join(skipped)} (no matching ledger column)"
    return True, note


def _unmerge_running(ws) -> None:
    """Drop every merge inside the block before rewriting it.

    A write into a merged range lands ONLY in its top-left cell and the rest is
    dropped without an error — that silently swallowed two whole Out rows once.
    """
    meta = ws.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets(properties(title,sheetId),merges)"})
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == ws.title)
    last_col = RUNNING_FIRST_COL + 9
    # Any merge that OVERLAPS the block, not just one that starts inside it: a
    # merge straddling in from the left swallows the write just as completely,
    # and this failed once already with three legacy 5-column merges sitting on
    # one row — the sections after it landed sideways instead of below.
    reqs = [{"unmergeCells": {"range": m}} for m in sheet.get("merges", [])
            if m.get("startColumnIndex", 0) < last_col
            and m.get("endColumnIndex", last_col) > RUNNING_FIRST_COL]
    if reqs:
        ws.spreadsheet.batch_update({"requests": reqs})


def _style_running(ws, banners, last_row) -> None:
    """Bold the banners, and colour every On hand cell red / amber / green.

    The thresholds are the ones the bot's stock dot uses (0 -> red, under a
    third of Total -> amber, else green), so the sheet and the bot never
    disagree about what "running low" means.
    """
    sid = ws.id
    total_col = _col_letter(RUNNING_FIRST_COL + 2)
    hand_col = _col_letter(RUNNING_FIRST_COL + 4)
    cells = {"sheetId": sid, "startRowIndex": 2, "endRowIndex": last_row,
             "startColumnIndex": RUNNING_FIRST_COL + 4,
             "endColumnIndex": RUNNING_FIRST_COL + 5}

    meta = ws.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets(properties(sheetId),conditionalFormats)"})
    reqs = []
    for sheet in meta["sheets"]:
        if sheet["properties"]["sheetId"] != sid:
            continue
        for i in range(len(sheet.get("conditionalFormats", [])) - 1, -1, -1):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})

    def rule(formula, colour):
        return {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [cells],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": colour}}}}}

    # Added at index 0 each time, so the LAST rule added is checked first: green
    # is the fallback, red wins outright. Row 3 is the first row the range
    # covers, so these relative references shift correctly down the block.
    guard = f'ISNUMBER(${hand_col}3),${total_col}3>0'
    reqs += [
        rule(f'=AND({guard},${hand_col}3/${total_col}3>=0.34)',
             {"red": 0.85, "green": 0.94, "blue": 0.83}),
        rule(f'=AND({guard},${hand_col}3/${total_col}3<0.34)',
             {"red": 1.0, "green": 0.9, "blue": 0.7}),
        rule(f'=AND(ISNUMBER(${hand_col}3),${hand_col}3<=0)',
             {"red": 0.96, "green": 0.8, "blue": 0.8}),
    ]
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": last_row,
                  "startColumnIndex": RUNNING_FIRST_COL,
                  "endColumnIndex": RUNNING_FIRST_COL + 5},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": False}}},
        "fields": "userEnteredFormat.textFormat.bold"}})
    for n in banners:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": n - 1, "endRowIndex": n + 1,
                      "startColumnIndex": RUNNING_FIRST_COL,
                      "endColumnIndex": RUNNING_FIRST_COL + 5},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93}}},
            "fields": "userEnteredFormat(textFormat.bold,backgroundColor)"}})
        reqs.append({"mergeCells": {"mergeType": "MERGE_ALL", "range": {
            "sheetId": sid, "startRowIndex": n - 1, "endRowIndex": n,
            "startColumnIndex": RUNNING_FIRST_COL,
            "endColumnIndex": RUNNING_FIRST_COL + 5}}})
    ws.spreadsheet.batch_update({"requests": reqs})


# ---------------------------------------------------------------------------
# Partial return / transfer
# ---------------------------------------------------------------------------
# A row bundles a whole costume, but people hand back or pass on one piece at a
# time. Rather than splitting into one row per item — which would mean ~120 rows
# for a 30-performer show — an item is released by clearing ITS OWN field on the
# row. That works because every item's Out already filters on its own field:
# shirt on Shirt Size, pants on Pant Size, each accessory on its count column.
# So clearing one stops that item counting while the rest keep counting.

# (key, colour column, size column or None, label, Holding colour attr, size attr)
# Every item now looks the same: a colour column, optionally a size column, and
# "-" in the colour column meaning "not issued". That uniformity is what lets
# release / describe / items_out treat all five items with one rule.
ITEM_FIELDS = (
    ("shirt", "shirt colour", "shirt size", "Shirt", "shirt_colour", "shirt_size"),
    ("pants", "pant colour", "pant size", "Pants", "pant_colour", "pant_size"),
    ("waist", "waist wrap", None, "Waist Wrap", "waist", None),
    ("wrist", "wrist wrap", None, "Wrist Wrap", "wrist", None),
    ("head", "head band", None, "Head Band", "head", None),
)


def item_colour(h, key: str) -> str:
    """The colour of one item on a holding, or "-" when it is not out."""
    attr = next((f[4] for f in ITEM_FIELDS if f[0] == key), None)
    return (getattr(h, attr, "") or NONE_SIZE) if attr else NONE_SIZE


def items_out(h) -> list[str]:
    """Which item keys are still out on this holding.

    One rule for all five: an item is out when its colour column holds a real
    colour rather than "-".
    """
    return [key for key, *_rest in ITEM_FIELDS
            if item_colour(h, key) not in ("", NONE_SIZE)]


def describe_items(h, keys) -> str:
    """`'red shirt M, black wrist wrap'` — what a Remarks note and the UI say."""
    bits = []
    for key, _cc, _sc, label, _ca, size_attr in ITEM_FIELDS:
        if key not in keys:
            continue
        colour = item_colour(h, key)
        if colour in ("", NONE_SIZE):
            continue
        size = getattr(h, size_attr, None) if size_attr else None
        if key == "pants" and size:
            bits.append(f"{colour.lower()} {label.lower()} {pant_size_label(size)}")
        elif size:
            bits.append(f"{colour.lower()} {label.lower()} {size.value}")
        else:
            bits.append(f"{colour.lower()} {label.lower()}")
    return ", ".join(bits)


def release_items(h, keys, action: str, to: str = "") -> tuple[bool, bool]:
    """Release some items from an open row. Returns `(ok, row_closed)`.

    `action` is "returned" or "transferred". Two very different shapes:

    * **Everything goes back** (nothing left out) — the row is closed the plain
      way: `Returned`/`Transferred date` + `Status`, and **the row keeps what it
      says it issued**. Once a date is stamped the row no longer counts towards
      `Out`, so blanking the sizes would only destroy the record of what the
      person actually had.
    * **Only some of it goes back** — the row must stay open, so each released
      item is cleared in ITS OWN field (a size becomes `-`, an accessory count
      `0`). Every item's `Out` filters on its own column, so the released item
      stops counting while the rest keeps counting. A dated note goes in
      **Remarks**; the date columns stay empty, because the costume has not
      fully come back.
    """
    keys = [k for k in keys if any(f[0] == k for f in ITEM_FIELDS)]
    if not keys:
        return False, False

    _hdr, cols, rows = get_ledger(force=True)
    raw = next((r for n, r in rows if n == h.row), None)
    if raw is None:
        return False, False

    stamp = _today()
    closed = not [k for k in items_out(h) if k not in keys]
    updates = []

    if closed:
        status = (LedgerStatus.RETURNED.value if action == "returned"
                  else LedgerStatus.TRANSFERRED.value)
        date_col = "returned date" if action == "returned" else "transferred date"
        cells = {date_col: stamp, "status": status}
        if to:
            cells["transferred to"] = to
        for header, value in cells.items():
            if header in cols:
                updates.append({'range': f"{_col_letter(cols[header])}{h.row}",
                                'values': [[value]]})
    else:
        # Clear the item's OWN columns — its colour, and its size when it has
        # one. Every column blanks to "-" now, so there is no per-item special
        # case left: the item stops counting, the rest of the row keeps counting.
        for key, colour_col, size_col, _label, _ca, _sa in ITEM_FIELDS:
            if key not in keys:
                continue
            for header in (colour_col, size_col):
                if header and header in cols:
                    updates.append({'range': f"{_col_letter(cols[header])}{h.row}",
                                    'values': [[NONE_SIZE]]})
        note = f"{action.capitalize()} {describe_items(h, keys)}"
        if to:
            note += f" to {to}"
        note += f" on {stamp}"
        if "remarks" in cols:
            prev = _cell(raw, cols["remarks"])
            updates.append({'range': f"{_col_letter(cols['remarks'])}{h.row}",
                            'values': [[f"{prev}; {note}" if prev else note]]})

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_TRACKING_TAB)
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    _invalidate_costume()
    return True, closed


def issue_items_to(h, keys, to: str) -> int:
    """Open a row for `to` carrying only `keys` from `h` — the receiving half of
    a partial transfer. Items not passed on stay with the original holder."""
    if not keys:
        return 0
    entry = IssueEntry(
        name=to,
        event=h.event,
        shirt_colour=h.shirt_colour if "shirt" in keys else NONE_SIZE,
        pant_colour=h.pant_colour if "pants" in keys else NONE_SIZE,
        shirt_size=h.shirt_size if "shirt" in keys else None,
        pant_size=h.pant_size if "pants" in keys else None,
        waist=h.waist if "waist" in keys else NONE_SIZE,
        wrist=h.wrist if "wrist" in keys else NONE_SIZE,
        head=h.head if "head" in keys else NONE_SIZE,
        remarks=f"Transferred from {h.name}",
    )
    return append_issues([entry])
