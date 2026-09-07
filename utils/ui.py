"""Shared inline-keyboard building blocks.

Pagination was written twice with two different looks (attendance used
`◀ Prev | Page 1/3 | Next ▶`, the Costume Tracker used bare `◀️`/`▶️` arrows).
Anything that pages a list should import from here instead, so every screen in
the bot navigates identically and a change to the look happens once.
"""
from telegram import InlineKeyboardButton


def paginate(items, page: int, size: int):
    """Return `(chunk, page, total_pages)` with `page` clamped into range.

    Clamping here means callers never have to guard against a stale page number
    from an old message whose list has since shrunk.
    """
    total_pages = max(1, -(-len(items) // size))
    page = max(0, min(page, total_pages - 1))
    start = page * size
    return items[start:start + size], page, total_pages


def pagination_row(page: int, total_pages: int, callback_for_page, noop: str,
                   unit: str = "Page"):
    """`◀ Prev | <unit> n/N | Next ▶` as a keyboard row.

    Returns an EMPTY list when there is only one page, so callers can always
    `rows += pagination_row(...)` without checking first.

    `callback_for_page` maps a 0-based page number to its callback_data; `noop`
    is the callback the inert page indicator carries (each handler has its own,
    since the global button gate answers by prefix).
    """
    if total_pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀ Prev", callback_data=callback_for_page(page - 1)))
    row.append(InlineKeyboardButton(f"{unit} {page + 1}/{total_pages}", callback_data=noop))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ▶", callback_data=callback_for_page(page + 1)))
    return row


def grid_row(labels, noop: str):
    """A row of inert cells, used to draw a table with buttons as columns.

    Telegram has no table markup and splits a row's width evenly between its
    buttons, so a keyboard is the only way to get truly aligned columns. Blank cells
    become '·' because Telegram rejects an empty button label.
    """
    return [InlineKeyboardButton(str(x) if str(x).strip() else "·", callback_data=noop)
            for x in labels]
