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

# NOTE: the Overview `Set` column IS matched on — `adjust_stock` finds its row by
# (Item, Set, Size) and every running-table SUMIFS filters on it — so a set's
# spelling must agree between the sheet and anything the bot writes. What the bot
# never does is *invent* a set: the option lists come from the sheet.
class CostumeSet(str, Enum):
    """The sets we ship with. NOT a closed category — see `list_costume_sets`.

    How many costume sets exist is a purchasing decision, so a set travels
    through the code as the plain string stored in the sheet and the options
    come from Main Inventory. These members stay as named constants for the
    values the code needs to default to; they are deliberately NOT used to
    validate, or a newly bought set could be stocked but never issued.
    """
    RED = "Red"
    WHITE = "White"

    @property
    def dot(self) -> str:
        return "🔴" if self is CostumeSet.RED else "⚪"

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
        for item, _st, _color, label, _qty in get_stock_rows():
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


def _tracking_values(force=False):
    return get_cached_values(LOGISTICS_SHEET, COSTUME_TRACKING_TAB, force=force)


def _overview_values(force=False):
    return get_cached_values(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB, force=force)


def _metric_column(values) -> int | None:
    """Column index of the running block's `Metric` header, or None if absent.

    Found by content rather than assumed, so the whole block can be moved to a
    different column (as it was, from Costume Tracking to Costume Overview)
    without touching this module.
    """
    for row in values:
        for idx, cell in enumerate(row):
            if (cell or "").strip().lower() == "metric":
                return idx
    return None


def get_running_inventory(force=False) -> list[InventorySection]:
    """Parse the RUNNING INVENTORY block into sections of Total/Out/On-hand rows.

    Sections are located by their `Metric` header cell rather than by row number,
    so adding an item or a whole section in the sheet needs no code change.
    """
    values = _overview_values(force=force)
    metric_col = _metric_column(values)
    if metric_col is None or metric_col < 2:
        return []
    name_col, color_col = metric_col - 2, metric_col - 1
    size_cols = [metric_col + 1 + n for n in range(_SIZE_SPAN)]
    total_col = metric_col + 1 + _SIZE_SPAN

    sections: list[InventorySection] = []
    current: InventorySection | None = None
    last_title = ""

    for i, row in enumerate(values):
        metric = _cell(row, metric_col).lower()
        head = _cell(row, name_col)

        if metric == "metric":
            current = InventorySection(title=last_title,
                                       sizes=[_cell(row, c) for c in size_cols])
            sections.append(current)
            continue

        if metric == "total" and current is not None and head:
            out_row = values[i + 1] if i + 1 < len(values) else []
            hand_row = values[i + 2] if i + 2 < len(values) else []
            sized = any(_cell(row, c) != "" for c in size_cols)
            current.items.append(InventoryItem(
                name=head, color=_cell(row, color_col), sized=sized, sizes=current.sizes,
                total=[_int(row, c) for c in size_cols] if sized else [],
                out=[_int(out_row, c) for c in size_cols] if sized else [],
                on_hand=[_int(hand_row, c) for c in size_cols] if sized else [],
                total_all=_int(row, total_col),
                out_all=_int(out_row, total_col),
                on_hand_all=_int(hand_row, total_col),
            ))
            continue

        if head and metric == "" and not head.lower().startswith("running"):
            last_title = head          # section banner, one row above its header

    return [s for s in sections if s.items]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

LEDGER_HEADERS = ["Performances", "Name", "Costume Set", "Shirt Size",
                  "Pant Size", "Waist Wrap", "Wrist Wrap", "Head Band",
                  "Status", "Issued date", "Returned date", "Transferred date",
                  "Transferred To", "Remarks", "Accessory Set"]


@dataclass
class Holding:
    """One open ledger row: someone holding a costume, not yet returned."""
    row: int
    name: str
    event: str
    costume_set: str                 # raw sheet value; may be a set we don't know
    accessory_set: str               # whose wraps these are (may differ from costume_set)
    shirt_size: Size | None
    pant_size: Size | None
    waist: int
    wrist: int
    head: int


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
            costume_set=col(row, "costume set"),
            accessory_set=col(row, "accessory set") or col(row, "costume set"),
            shirt_size=parse_size(col(row, "shirt size")),
            pant_size=parse_size(col(row, "pant size")),
            waist=_int(row, cols.get("waist wrap", -1)) if "waist wrap" in cols else 0,
            wrist=_int(row, cols.get("wrist wrap", -1)) if "wrist wrap" in cols else 0,
            head=_int(row, cols.get("head band", -1)) if "head band" in cols else 0,
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
    """One costume going out. Accessory counts default to a full bundle."""
    name: str
    event: str
    costume_set: str
    shirt_size: Size
    pant_size: Size
    waist: int = 1
    wrist: int = 1
    head: int = 0
    remarks: str = ""
    # Whose wraps were taken. Blank means "this set's own" — the common case; it
    # differs only when a set wears another set's accessories.
    accessory_set: str = ""


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
                         ("costume set", str(e.costume_set)),
                         ("accessory set", str(e.accessory_set or e.costume_set)),
                         ("shirt size", e.shirt_size.value if e.shirt_size else NONE_SIZE),
                         ("pant size",
                          pant_size_label(e.pant_size) if e.pant_size else NONE_SIZE),
                         ("waist wrap", e.waist),
                         ("wrist wrap", e.wrist), ("head band", e.head),
                         ("status", LedgerStatus.ISSUED.value),
                         ("issued date", today), ("remarks", e.remarks)):
            if key in cols:
                row[cols[key]] = val
        block.append(row)

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_TRACKING_TAB)
    end = _col_letter(width - 1)
    ws.update(values=block, range_name=f"A{first_free}:{end}{first_free + len(block) - 1}",
              value_input_option="USER_ENTERED")
    invalidate_sheet_cache(LOGISTICS_SHEET)
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
    invalidate_sheet_cache(LOGISTICS_SHEET)
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
    invalidate_sheet_cache(LOGISTICS_SHEET)
    return len(closures)


def costume_size_layout(force=False) -> tuple[dict[str, int], int | None]:
    """`({set_name: column_index}, pant_size_column)` read from the header row.

    A member's shirt size differs per costume set ("Red Shirt Size", "White Shirt
    Size"), and more columns appear when a set is bought — so the columns are
    matched by header, never by position. Reading them positionally is what made
    the pant default silently pick up the White Shirt column.
    """
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=force)
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
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=force)
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
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=True)
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
                    if st.lower() == (e.costume_set or "").strip().lower()), None)
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
        invalidate_sheet_cache(LOGISTICS_SHEET)
    return len(updates) + sum(1 for _ in appended)


def shirt_size_for(entry: dict | None, costume_set: str) -> Size | None:
    """That member's shirt size for one set, falling back to any set they have."""
    if not entry:
        return None
    per_set = entry.get("shirt") or {}
    for st, size in per_set.items():
        if size and st.lower() == str(costume_set).strip().lower():
            return size
    return next((s for s in per_set.values() if s), None)


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
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=True)
    for i, row in enumerate(values):
        if _cell(row, 0).lower() == nickname.lower():
            ws.update_cell(i + 1, col0 + 1, size.value)
            invalidate_sheet_cache(LOGISTICS_SHEET)
            return True

    new_row = [""] * (max(col0, 0) + 1)
    new_row[0] = nickname
    new_row[col0] = size.value
    ws.append_row(new_row, value_input_option="USER_ENTERED")
    invalidate_sheet_cache(LOGISTICS_SHEET)
    return True


# ---------------------------------------------------------------------------
# Stock corrections (the one place the bot writes a quantity)
# ---------------------------------------------------------------------------

def adjust_stock(item, set_or_type: str, size_label: str, delta: int) -> int | None:
    """Add `delta` to one Costume Overview `(Item, Set, Size)` row's Quantity.

    Returns the new quantity, or None when no matching row exists. Never goes
    below zero. This writes to Costume Overview, so the running table's Total
    (and therefore On hand) picks the change up on its own.
    """
    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB, force=True)
    for i, row in enumerate(values):
        want_item = (item.value if isinstance(item, Item) else str(item)).strip().lower()
        if (_cell(row, 0).lower() == want_item
                and _cell(row, 1).lower() == str(set_or_type).lower()
                and _cell(row, 3).lower() == str(size_label).lower()):
            new = max(0, _int(row, 4) + delta)
            ws.update_cell(i + 1, 5, new)
            invalidate_sheet_cache(LOGISTICS_SHEET)
            return new
    return None


def get_stock_rows(force=False) -> list[tuple[str, str, str, str, int]]:
    """`[(item, set, color, size, quantity), ...]` from Main Inventory.

    Stops at the first blank Item cell once the table has started. Column A
    continues below the table with free-text bookkeeping ("Last Updated",
    "Signed Off", the update note); without this bound those lines are served
    as inventory rows and show up in both the Main Inventory table and the
    Update Stock picker.
    """
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB, force=force)
    rows = []
    started = False
    for row in values[2:]:
        item = _cell(row, 0)
        if not item or item.lower() == "item":
            if started:
                break
            continue
        started = True
        rows.append((item, _cell(row, 1), _cell(row, 2), _cell(row, 3), _int(row, 4)))
    return rows


# ---------------------------------------------------------------------------
# Categories that actually exist, read from the sheet
# ---------------------------------------------------------------------------
# Main Inventory is the register of what the club owns, so it — not an enum —
# decides which sets and pant types the UI may offer.

def list_costume_sets(force=False) -> list[str]:
    """Costume sets present in Main Inventory, in sheet order (pants excluded)."""
    seen = []
    for item, st, _c, _z, _q in get_stock_rows(force=force):
        if item.strip().lower() != Item.PANTS.value.lower() and st and st not in seen:
            seen.append(st)
    return seen


ACCESSORY_ITEMS = (Item.WAIST_WRAP.value, Item.WRIST_WRAP.value, Item.HEAD_BAND.value)


def set_accessories(set_name: str, force=False) -> list[str]:
    """Accessory item names this set owns stock of, as spelled in the sheet.

    Matched by prefix, not equality: Main Inventory qualifies some names
    ("Wrist Wrap (pairs)"), and the running table's SUMIFS already use the same
    `"Wrist Wrap*"` wildcard, so this keeps the two consistent.
    """
    found = []
    for item, st, _c, _z, _q in get_stock_rows(force=force):
        if st.lower() != str(set_name).lower() or item in found:
            continue
        if any(item.lower().startswith(a.lower()) for a in ACCESSORY_ITEMS):
            found.append(item)
    return found


def list_accessory_owners(force=False) -> list[str]:
    """Sets that own accessory stock, i.e. the sets another set can borrow from.

    A set created with shirts only (new colourway, existing wraps) owns none, so
    at issue time the admin picks whose accessories are being taken and that goes
    in the ledger's `Accessory Set` column.
    """
    return [s for s in list_costume_sets(force=force) if set_accessories(s)]


# ---------------------------------------------------------------------------
# Adding a whole new costume set
# ---------------------------------------------------------------------------

INV_FIRST_ROW = 3            # Main Inventory data starts here
INV_LAST_ROW = 60            # upper bound baked into the running table's SUMIFS
_LEDGER_FIRST, _LEDGER_LAST = 2, 1001

# Which ledger column counts as "out" for each item. Shirt consumes the row's
# Quantity; the accessories have their own count columns.
_OUT_COLUMN_FOR = {Item.SHIRT.value: "quantity", Item.WAIST_WRAP.value: "waist wrap",
                   Item.WRIST_WRAP.value: "wrist wrap", Item.HEAD_BAND.value: "head band"}

# (item, colour, sized) — what a costume set normally contains.
DEFAULT_SET_ITEMS = [(Item.SHIRT.value, "", True), (Item.WAIST_WRAP.value, "", False),
                     (Item.WRIST_WRAP.value, "", False), (Item.HEAD_BAND.value, "", False)]


def _first_blank_inventory_row(values) -> int:
    """Row number just past the last Main Inventory row."""
    row = INV_FIRST_ROW
    while row - 1 < len(values) and _cell(values[row - 1], 0):
        row += 1
    return row


def _running_block_end(values, name_col: int) -> int:
    """Last used row of the running block, so a new section appends below it."""
    last = 0
    for i, row in enumerate(values, start=1):
        if _cell(row, name_col) or _cell(row, name_col + 2):
            last = i
    return last


def add_costume_set(set_name: str, items=None, sizes=None) -> tuple[bool, str]:
    """Register a brand-new costume set: stock rows + its running-table section.

    Two writes are needed because the sheet holds two different things. Main
    Inventory gets a zero-quantity row per (item, size) so the admin can count
    the new stock in; the running table gets a section of SUMIFS so the set
    reports Total / Out / On hand like every other. Without the second half the
    set would be stockable but invisible on the Costume Overview screen.

    Everything else adapts on its own — the set pickers and the running-table
    parser both read the sheet — so this is the only step a new set needs.

    Returns `(ok, message)`.
    """
    name = str(set_name).strip()
    if not name:
        return False, "Set name is required."
    items = items or DEFAULT_SET_ITEMS
    sizes = sizes or [s.value for s in Size]

    if name.lower() in {s.lower() for s in list_costume_sets(force=True)}:
        return False, f"A set called '{name}' already exists in Main Inventory."

    values = _overview_values(force=True)
    metric_col = _metric_column(values)
    if metric_col is None or metric_col < 2:
        return False, "Couldn't find the running table's 'Metric' header."
    name_col = metric_col - 2

    # --- 1. Main Inventory rows (quantity 0; the admin counts stock in) ------
    start = _first_blank_inventory_row(values)
    new_rows = []
    for item, color, sized in items:
        for size in (sizes if sized else ["-"]):
            new_rows.append([item, name, color, size, 0])
    end = start + len(new_rows) - 1
    if end > INV_LAST_ROW:
        return False, (f"Main Inventory would overflow row {INV_LAST_ROW} "
                       f"(needs {end}). Widen the running table's SUMIFS first.")

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    ws.update(values=new_rows, range_name=f"A{start}:E{end}",
              value_input_option="USER_ENTERED")

    # --- 2. Running-table section -------------------------------------------
    _hdr, cols, _rows = get_ledger(force=True)
    tr = "'" + COSTUME_TRACKING_TAB + "'!"

    def led(key):
        letter = _col_letter(cols[key])
        return f"{tr}${letter}${_LEDGER_FIRST}:${letter}${_LEDGER_LAST}"

    open_cond = f'{led("returned date")},"",{led("transferred date")},"",{led("name")},"<>"'
    inv_item = f"$A${INV_FIRST_ROW}:$A${INV_LAST_ROW}"
    inv_set = f"$B${INV_FIRST_ROW}:$B${INV_LAST_ROW}"
    inv_size = f"$D${INV_FIRST_ROW}:$D${INV_LAST_ROW}"
    inv_qty = f"$E${INV_FIRST_ROW}:$E${INV_LAST_ROW}"

    block_start = _running_block_end(values, name_col) + 2
    grid = []
    title = f"{name.upper()} COSTUME SET"
    grid.append([title] + [""] * (3 + len(sizes)))
    grid.append(["Item", "Color", "Metric"] + list(sizes) + ["Total"])
    hdr_row = block_start + 1
    r = block_start + 2
    for item, color, sized in items:
        total_c = [item, color, "Total"]
        out_c = ["", "", "Out"]
        hand_c = ["", "", "On hand"]
        out_key = _OUT_COLUMN_FOR.get(item)
        if sized:
            for n in range(len(sizes)):
                letter = _col_letter(metric_col + 1 + n)
                total_c.append(f'=SUMIFS({inv_qty},{inv_item},"{item}*",{inv_set},'
                               f'"{name}",{inv_size},{letter}${hdr_row})')
                if out_key == "quantity":
                    out_c.append(f'=SUMIFS({led("quantity")},{led("costume set")},"{name}",'
                                 f'{led("shirt size")},{letter}${hdr_row},{open_cond})')
                elif out_key:
                    out_c.append(f'=SUMIFS({led(out_key)},{led("accessory set")},"{name}",{open_cond})')
                else:
                    out_c.append(0)      # no ledger column tracks this item yet
                hand_c.append(f"={letter}{r}-{letter}{r + 1}")
            first = _col_letter(metric_col + 1)
            last = _col_letter(metric_col + len(sizes))
            total_c.append(f"=SUM({first}{r}:{last}{r})")
            out_c.append(f"=SUM({first}{r + 1}:{last}{r + 1})")
        else:
            total_c += [""] * len(sizes)
            out_c += [""] * len(sizes)
            hand_c += [""] * len(sizes)
            total_c.append(f'=SUMIFS({inv_qty},{inv_item},"{item}*",{inv_set},"{name}")')
            out_c.append(f'=SUMIFS({led(out_key)},{led("accessory set")},"{name}",{open_cond})'
                         if out_key else 0)
        tc = _col_letter(metric_col + 1 + len(sizes))
        hand_c.append(f"={tc}{r}-{tc}{r + 1}")
        grid += [total_c, out_c, hand_c]
        r += 3

    first_col = _col_letter(name_col)
    last_col = _col_letter(metric_col + 1 + len(sizes))
    ws.update(values=grid,
              range_name=f"{first_col}{block_start}:{last_col}{block_start + len(grid) - 1}",
              value_input_option="USER_ENTERED")

    invalidate_sheet_cache(LOGISTICS_SHEET)
    return True, (f"Added '{name}': {len(new_rows)} inventory rows (quantity 0) and a "
                  f"running-table section. Count the stock in with Update Stock.")


def remove_costume_set(set_name: str) -> tuple[bool, str]:
    """Undo `add_costume_set` — the safety net for a typo'd name.

    Main Inventory is rewritten WITHOUT the set rather than having its rows
    deleted: deleting rows inside `A3:A60` would make Google shrink every
    running-table SUMIFS range with them, so repeated add/remove would quietly
    eat the headroom the ranges were widened to provide. Rewriting keeps the
    rows contiguous (which `get_stock_rows` relies on) and the ranges untouched.

    The set's running-table section is cleared in place; the resulting gap is
    harmless because the block is parsed by its `Metric` headers, not by row.

    Returns `(ok, message)`.
    """
    name = str(set_name).strip()
    if not name:
        return False, "Set name is required."

    values = _overview_values(force=True)
    metric_col = _metric_column(values)
    if metric_col is None or metric_col < 2:
        return False, "Couldn't find the running table's 'Metric' header."
    name_col = metric_col - 2

    keep, dropped = [], 0
    for row in values[INV_FIRST_ROW - 1:]:
        item = _cell(row, 0)
        if not item:
            break
        if _cell(row, 1).lower() == name.lower():
            dropped += 1
            continue
        keep.append([item, _cell(row, 1), _cell(row, 2), _cell(row, 3), _int(row, 4)])
    if not dropped:
        return False, f"No inventory rows found for '{name}'."

    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_OVERVIEW_TAB)
    last_written = INV_FIRST_ROW + len(keep) - 1
    ws.update(values=keep, range_name=f"A{INV_FIRST_ROW}:E{last_written}",
              value_input_option="USER_ENTERED")
    ws.batch_clear([f"A{last_written + 1}:E{last_written + dropped}"])

    # Clear the section whose banner names this set.
    title = f"{name.upper()} COSTUME SET"
    start = next((i for i, row in enumerate(values, start=1)
                  if _cell(row, name_col).upper() == title), None)
    cleared = 0
    if start:
        end = start
        for i in range(start + 1, len(values) + 1):
            row = values[i - 1] if i - 1 < len(values) else []
            if _cell(row, name_col).upper().endswith("COSTUME SET") and i != start:
                break
            if not any(_cell(row, c) for c in range(name_col, metric_col + 7)):
                if i > start + 1:
                    break
            end = i
        first_col = _col_letter(name_col)
        last_col = _col_letter(metric_col + 6)
        ws.batch_clear([f"{first_col}{start}:{last_col}{end}"])
        cleared = end - start + 1

    invalidate_sheet_cache(LOGISTICS_SHEET)
    return True, (f"Removed '{name}': {dropped} inventory rows and "
                  f"{cleared} running-table rows cleared.")


# ---------------------------------------------------------------------------
# Partial return / transfer
# ---------------------------------------------------------------------------
# A row bundles a whole costume, but people hand back or pass on one piece at a
# time. Rather than splitting into one row per item — which would mean ~120 rows
# for a 30-performer show — an item is released by clearing ITS OWN field on the
# row. That works because every item's Out already filters on its own field:
# shirt on Shirt Size, pants on Pant Size, each accessory on its count column.
# So clearing one stops that item counting while the rest keep counting.

ITEM_FIELDS = (
    ("shirt", "shirt size", "Shirt"),
    ("pants", "pant size", "Pants"),
    ("waist", "waist wrap", "Waist Wrap"),
    ("wrist", "wrist wrap", "Wrist Wrap"),
    ("head", "head band", "Head Band"),
)


def items_out(h) -> list[str]:
    """Which item keys are still out on this holding."""
    out = []
    if h.shirt_size:
        out.append("shirt")
    if h.pant_size:
        out.append("pants")
    for key, attr in (("waist", "waist"), ("wrist", "wrist"), ("head", "head")):
        if getattr(h, attr, 0):
            out.append(key)
    return out


def describe_items(h, keys) -> str:
    """`'shirt M, pants M - 170, waist wrap'` for the Remarks note."""
    bits = []
    for key in keys:
        if key == "shirt" and h.shirt_size:
            bits.append(f"shirt {h.shirt_size.value}")
        elif key == "pants" and h.pant_size:
            bits.append(f"pants {pant_size_label(h.pant_size)}")
        elif key == "waist":
            bits.append("waist wrap")
        elif key == "wrist":
            bits.append("wrist wrap")
        elif key == "head":
            bits.append("head band")
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
    keys = [k for k in keys if k in dict((f[0], f) for f in ITEM_FIELDS)]
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
        for key, header, _label in ITEM_FIELDS:
            if key not in keys or header not in cols:
                continue
            blank = NONE_SIZE if header.endswith("size") else 0
            updates.append({'range': f"{_col_letter(cols[header])}{h.row}",
                            'values': [[blank]]})
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
    invalidate_sheet_cache(LOGISTICS_SHEET)
    return True, closed


def issue_items_to(h, keys, to: str) -> int:
    """Open a row for `to` carrying only `keys` from `h` — the receiving half of
    a partial transfer. Items not passed on stay with the original holder."""
    if not keys:
        return 0
    entry = IssueEntry(
        name=to,
        event=h.event,
        costume_set=h.costume_set or CostumeSet.RED.value,
        shirt_size=h.shirt_size if "shirt" in keys else None,
        pant_size=h.pant_size if "pants" in keys else None,
        waist=1 if "waist" in keys else 0,
        wrist=1 if "wrist" in keys else 0,
        head=1 if "head" in keys else 0,
        remarks=f"Transferred from {h.name}",
    )
    return append_issues([entry])
