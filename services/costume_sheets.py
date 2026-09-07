"""Costume Tracker data layer — the separate `Logistics AY 26/27` spreadsheet.

Three tabs, three jobs:

* **Costume Overview** — all inventory *state*, in two blocks:
  - `A:F` **Main Inventory**, the manually-maintained stock totals, one row per
    `(Item, Set, Size)`. The only place the bot ever *writes* a stock number
    (✏️ Update Stock).
  - `H:P` the **RUNNING INVENTORY** matrix. Every cell is a `SUMIFS` pulling
    Total from Main Inventory beside it and Out from the Costume Tracking
    ledger, so Total / Out / On hand can never drift from the records they
    summarise. The bot **reads this and never writes to it** — the same rule
    the ATTENDANCE `Tabulation` column follows.
* **Costume Tracking** — inventory *events*: a flat ledger, header on row 1,
  one row per (performance, member) issue, closed by stamping a Returned or
  Transferred date. Nothing above it, so it stays sortable and filterable.
* **Costume Size** — `Nickname | Shirt Size | Pant Size` defaults.

A **transfer** closes the giver's row *and opens one for the receiver*, so a
costume handed from A to B stays counted as out — see `apply_transfers`.

Row/column positions are *detected*, never hardcoded: the running block is found
by its `Metric` header cells and the ledger by its `Performances` header, so
inserting a section or reordering ledger columns in the sheet does not break the
bot. All reads go through the shared `get_cached_values` smart cache, keyed by
`(sheet_name, tab_name)` — so this spreadsheet caches independently of the main
database, and every write invalidates it.
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

class PantType(str, Enum):
    """Pants are stocked on their own axis, independent of the costume set.

    Open like `CostumeSet`: Old/New may be merged into a single pant type once
    the old stock is retired, so the code reads the available types from Main
    Inventory (`list_pant_types`) rather than assuming these two. These members
    remain as named constants for defaults.
    """
    OLD = "Old"
    NEW = "New"


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


def pant_size_label(pant_type, size: Size) -> str:
    """The sheet's own label for a pant size — `'M'` for Old, `'M - 170'` for New.

    Looked up in Main Inventory rather than hardcoded. The height suffix is a
    property of the stock, so if the pant types merge or a new run is labelled
    differently, the sheet is already the source of truth and nothing here has
    to change. Falls back to the bare size when there is no matching row.
    """
    want = (size.value if isinstance(size, Size) else str(size)).strip().upper()
    try:
        for item, st, _color, label, _qty in get_stock_rows():
            if (item.strip().lower() == Item.PANTS.value.lower()
                    and st.strip().lower() == str(pant_type).strip().lower()
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

LEDGER_HEADERS = ["Performances", "Name", "Costume Set", "Pant Type", "Shirt Size",
                  "Pant Size", "Quantity", "Waist Wrap", "Wrist Wrap", "Head Band",
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
    pant_type: str                   # raw sheet value ("Old"/"New"/whatever)
    shirt_size: Size | None
    pant_size: Size | None
    quantity: int
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
        pant_type = col(row, "pant type")
        holdings.append(Holding(
            row=number,
            name=col(row, "name"),
            event=col(row, "performances"),
            costume_set=col(row, "costume set"),
            accessory_set=col(row, "accessory set") or col(row, "costume set"),
            pant_type=pant_type,
            shirt_size=parse_size(col(row, "shirt size")),
            pant_size=parse_size(col(row, "pant size")),
            quantity=_int(row, cols.get("quantity", -1)) if "quantity" in cols else 0,
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
    pant_type: str
    shirt_size: Size
    pant_size: Size
    quantity: int = 1
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
                         ("pant type", str(e.pant_type)),
                         ("shirt size", e.shirt_size.value),
                         ("pant size", pant_size_label(e.pant_type, e.pant_size)),
                         ("quantity", e.quantity), ("waist wrap", e.waist),
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


def apply_transfers(transfers: list[tuple[Holding, str, str]]) -> int:
    """Hand costumes from one member to another. `[(holding, to_name, remarks)]`.

    A transfer is two ledger movements, not one: the giver's row is closed
    (Transferred + date + Transferred To) **and a fresh open row is created for
    the receiver** carrying the same set, sizes and accessories.

    Both halves matter. Closing the giver's row alone would drop the item out of
    the `Out` count even though it never came back — the sheet would show stock
    on hand that is physically in someone's bag. Opening the receiver's row keeps
    `Out` constant across the hand-over and makes the chain traceable when the
    costume is passed on again.

    The close batch runs first: if the append then fails, the item shows as
    returned rather than silently issued to two people at once, and re-running
    the transfer fixes it.
    """
    if not transfers:
        return 0

    close_rows([Closure(row=h.row, status=LedgerStatus.TRANSFERRED,
                        remarks=remarks, to=to) for h, to, remarks in transfers])

    entries = [IssueEntry(
        name=to,
        event=h.event,
        costume_set=h.costume_set or CostumeSet.RED.value,
        accessory_set=h.accessory_set or h.costume_set or CostumeSet.RED.value,
        pant_type=h.pant_type or PantType.NEW.value,
        shirt_size=h.shirt_size or Size.M,
        pant_size=h.pant_size or Size.M,
        quantity=h.quantity or 1,
        waist=h.waist, wrist=h.wrist, head=h.head,
        remarks=f"Transferred from {h.name}",
    ) for h, to, _remarks in transfers]
    append_issues(entries)
    return len(transfers)


# ---------------------------------------------------------------------------
# Costume Size defaults
# ---------------------------------------------------------------------------

def get_costume_sizes(force=False) -> dict[str, dict[str, Size | None]]:
    """`{nickname: {'shirt': Size|None, 'pants': Size|None}}` from Costume Size."""
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=force)
    sizes: dict[str, dict[str, Size | None]] = {}
    for row in values[1:]:
        nick = _cell(row, 0)
        if not nick or nick.lower() == "nickname":
            continue
        sizes[nick] = {"shirt": parse_size(_cell(row, 1)), "pants": parse_size(_cell(row, 2))}
    return sizes


def set_costume_size(nickname: str, garment: str, size: Size) -> bool:
    """Write one member's default shirt/pant size, appending a row if they're new."""
    ws = get_gspread_sheet(LOGISTICS_SHEET, COSTUME_SIZE_TAB)
    values = get_cached_values(LOGISTICS_SHEET, COSTUME_SIZE_TAB, force=True)
    col = 2 if garment == "shirt" else 3
    for i, row in enumerate(values):
        if _cell(row, 0).lower() == nickname.lower():
            ws.update_cell(i + 1, col, size.value)
            invalidate_sheet_cache(LOGISTICS_SHEET)
            return True
    ws.append_row([nickname, size.value if col == 2 else "", size.value if col == 3 else ""],
                  value_input_option="USER_ENTERED")
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


def list_pant_types(force=False) -> list[str]:
    """Pant types present in Main Inventory ("Old"/"New", or one if they merge)."""
    seen = []
    for item, st, _c, _z, _q in get_stock_rows(force=force):
        if item.strip().lower() == Item.PANTS.value.lower() and st and st not in seen:
            seen.append(st)
    return seen


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
