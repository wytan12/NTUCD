# NTUFD Telegram Bot — Project Overview

## Purpose

This bot operationalizes administrative processes for **NTU Festive Drums (NTUFD)**
(formerly documented as NTUCD), a university CCA at Nanyang Technological
University. It manages:

- New member onboarding: Welcome Tea recruitment automation + matric-number verification into the main group
- Performance opportunity lifecycle (creation → status → reminders → announcements)
- Weekly training attendance polls and attendance marking (regular + performance)
- Manual-topic-creation control in the group forum
- Member join/leave tracking in Google Sheets

---

## Architecture

| Layer | Technology |
|---|---|
| Bot framework | `python-telegram-bot` v21.5 (async, polling) |
| Persistence | `PicklePersistence` → `bot_data.pkl` (WT job tracking, dietary-capture state, pending verification / member-write queues, group chat data). **A cache in front of Sheets, not a database** — Heroku's filesystem is ephemeral, so the file is discarded on every deploy, crash and daily dyno cycle. It survives a crash *within* a dyno's life, nothing more. Anything durable must live in the sheet: this is why the WT jobs re-check per-user `SENT` columns rather than trusting `wt_jobs_sent`, and why `/verification` writes MEMBER INFO inline instead of trusting the queue |
| Database | Google Sheets via `gspread` + `oauth2client`. **Two spreadsheets**: `SHEET_NAME` (main) and `LOGISTICS_SHEET` (Costume Tracker), cached independently since the smart cache is keyed by `(sheet_name, tab_name)` |
| Timezone | Asia/Singapore (`pytz`) |
| Scheduler | `JobQueue` (APScheduler): daily 7-day performance reminder scan (09:00 SGT), daily auto-poll (09:00 SGT), and a **7-second MEMBER INFO write drain** (`drain_member_writes_job`). The Welcome Tea 30-second heartbeat exists but is **currently disabled** (see below). Per-admin DM menus registered as a detached `asyncio.create_task` on startup |
| Deployment | Heroku worker dyno (`Procfile`: `worker: python main.py`) |

> ⚠️ **The Welcome Tea scheduler is switched off.** `schedule_welcome_tea_jobs(application)`
> is commented out in `on_startup` (main.py) — WT broadcast jobs are run manually.
> `welcome_tea_scheduler_tick`, the eight job callbacks, and all sheet-driven
> datetimes still exist and work; nothing fires them automatically right now.
> `/attd` and `/verification` read the WT settings directly and are unaffected.

---

## Directory Structure

```
telegram-bot/
├── main.py                  # Entry point — handlers, global button security gate, startup data load
├── config.py                # Env-loaded token/creds, sheet names, chat/thread IDs, admin role keywords
├── requirements.txt
├── Procfile                 # Heroku worker: python main.py
├── bot_data.pkl             # PicklePersistence store (created at runtime; gitignored via *.pkl)
├── handlers/
│   ├── admin_handlers.py        # /start (Cockpit open), /threadid (DM directory), 7-day reminder scan,
│   │                            #   manual remind portal + REMIND_ESCROW approve-and-broadcast,
│   │                            #   announce portal + broadcast, send_reminder (night-before training DM)
│   ├── conversation_handlers.py # ONLY build_performance_summary + publish_performance_summary
│   │                            #   (pinned-summary builder; the old //-separator flow and /confirmation are gone)
│   ├── member_handlers.py       # handle_new_member (safety net), handle_member_status (join/leave tracking;
│   │                            #   Welcome Tea group excluded)
│   ├── message_handlers.py      # Group messages: ONLY blocks manual forum-topic creation
│   │                            #   (MAIN admins exempt); everything else is untouched
│   ├── modify_handlers.py       # Edit-performance flow — STAGED edits, "Save & Push Updates" commit
│   ├── attendance_handlers.py   # Cockpit → Take Attendance (REG dates / PERF events), pagination,
│   │                            #   present counter, Modify-a-Date sub-flow with old-poll cleanup
│   ├── poll_handlers.py         # auto_poll_check (daily), handle_poll_answer → attendance, send_interest_poll
│   ├── private_handlers.py      # Cockpit dashboard, DM dispatcher (only /start), PERF/OTHERS creation wizard,
│   │                            #   Performance Ledger (+performers), Thread Index, Member Roster
│   ├── verification_handlers.py # join-request router; ACTIVE /verify | /verification matric conversation;
│   │                            #   drain_member_writes_job (background MEMBER INFO write queue)
│   ├── welcome_tea_handlers.py  # Welcome Tea automation — RSVP buttons, dietary capture, /attd event-day
│   │                            #   check-in, 8 job callbacks + paced/batched broadcasting helpers
│   ├── logistics_handlers.py    # Costume Tracker ENTRY POINT — render_logistics_home +
│   │                            #   logistics_callback router (^LOGI\|). Imported by main.py;
│   │                            #   the 3-module split below is invisible from outside
│   ├── costume_common.py        # Shared by the two halves: _read/_edit/_fail/_edit_dashboard,
│   │                            #   emoji + label helpers, the user_data staging buffer
│   ├── costume_inventory.py     # What the club OWNS — Costume Overview menu, Main Inventory +
│   │                            #   Running tables (button grids), Update Stock, Costume Size
│   └── costume_tracking.py      # Who is HOLDING what — Issue / Return / Transfer, review, submit
├── services/
│   ├── google_sheets.py         # All Sheets ops; cached client + Drive-modifiedTime smart cache;
│   │                            #   role tiers; WT settings/rows/batch writes; PERF TABULATION; member upserts
│   ├── costume_sheets.py        # Costume Tracker data layer — the separate Logistics spreadsheet
│   └── date_parser.py           # Date/time parser → `DD Mon YYYY  H:MM AM/PM`; supports ranges
└── utils/
    ├── constants.py             # Conversation states, in-memory sets and dicts
    ├── decorators.py            # is_admin() group check, check_is_authenticated_admin(), @admin_only
    ├── ui.py                    # Shared keyboard building blocks: paginate(), pagination_row()
    │                            #   (`◀ Prev | Page n/N | Next ▶`), grid_row(). Used by the
    │                            #   attendance toggles AND every paginated Costume Tracker screen
    └── helpers.py               # get_next_tuesday, delete_topic_with_delay(), date label helpers
```

---

## Key Config Values (`config.py`)

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` / `GOOGLE_CREDENTIALS_JSON` | **Loaded from env vars** (`os.environ.get`). Heroku Config Vars in prod; `GOOGLE_CREDENTIALS_JSON` may be a JSON string (Heroku) or dict (local) — `_get_client()` handles both |
| `SHEET_NAME` | Main Google Sheet: `"NTUFD AY26/27 Database"` (debug sheet line commented out) |
| `SHEET_TAB_NAME` | Performance list tab (`"PERF"`) |
| `ATTENDANCE_TAB` | Regular-training attendance tab (`"ATTENDANCE"`) |
| `MEMBER_INFO_TAB` | `"MEMBER INFO AY26/27"` — active roster (Nickname col B, Tele ID col P) |
| `ATT_NAME_COL` / `ATT_TOTAL_COL` / `ATT_LABEL_COL` / `ATT_FIRST_DATE_COL` | Attendance column layout (A / B / C / D). `ATT_TOTAL_COL` (B) is the sheet-maintained `Tabulation` formula — read-only for the bot |
| `ATT_POLL_ROW` / `ATT_DATE_ROW` / `ATT_FIRST_MEMBER_ROW` | Attendance row layout (1 / 2 / 3) |
| `PERF_TAB_NAME` + `PERF_*` constants | `PERF TABULATION` tab layout (performance attendance; row 1 = thread id, row 2 = event name) |
| `CHAT_ID` | **Main group** AY26/27 is active (`-1004406293828`); debug group commented out |
| `WELCOME_TEA_GROUP_CHAT_ID` | Welcome Tea group (main active, debug commented) |
| `TOPIC_VOTING_ID` | Thread where attendance polls are posted (53 main / 5 debug) |
| `WELCOME_TEA_SHEET` / `WELCOME_TEA_TAB` | `"NTUFD Welcome Tea Registration 2026 (Responses)"` / `"Form Responses 1"` — the Google-Form responses read by the **active** `/verification` matric flow |
| `WELCOME_TEA_ID_TAB` | `"WELCOME TEA"` tab in the main sheet — settings block + per-user RSVP/tracking grid (see schema) |
| `WELCOME_TEA_STATUS_*` | Canonical statuses: `Not Confirm` / `Attend` / `Reject` / `STILL_COMING` |
| `MAIN_ADMIN_ROLE_KEYWORDS` | `("chairperson", "secretary", "sde")` — contains-match |
| `SECONDARY_ADMIN_ROLE_KEYWORDS` | `("treasurer", "logistic", "business", "publications", "coach")` |
| `ADMIN_DM_USER_IDS` | Emergency fallback IDs (used only when the role lookup yields nothing) |
| `JOIN_CONTACT_ADMIN_ID` | Last-resort "contact our admin" Tele ID for the join flow |
| `WELCOME_TEA_LINK_KEYWORD` | `"tea"` — still defined, but the keyword route in `join_request_handler` is **commented out**; routing now uses chat id + links stored in the WELCOME TEA sheet settings |
| `sg_tz` | Singapore timezone object |
| `SHEET_COLUMNS` | **10 columns** — includes `SUMMARY MSG ID`, the pinned-summary message id used to delete/replace the old pin |

> There is **no** `WELCOME_TEA_EVENT_DATE` / `*_DAYS_BEFORE` / `*_TIME` config any
> more — every Welcome Tea date/time lives in the WELCOME TEA sheet tab, so admins
> reschedule jobs by editing the sheet, no deploy needed. `EXEMPTED_THREAD_IDS` is
> also gone (per-thread access control removed).

---

## Google Sheets Schema

### `PERF` tab
Columns: `THREAD ID | EVENT TYPE | EVENT NAME | REHEARSAL DATE | TIME | PERF DATE | TIME | LOCATION | OTHER INFO | REMUNATION | STATUS | SUMMARY MSG ID`

- Each row = one performance forum topic
- `EVENT TYPE`: `EXT` or `INT` — stored but **not shown** in the pinned summary
- `PERF DATE | TIME` may be `TBD` (the wizard's Skip button)
- `STATUS`: blank (pending) → `ACCEPTED` or `REJECTED`
- `SUMMARY MSG ID`: message id of the current pinned summary; `publish_performance_summary`
  unpins/deletes the old message and stores the new id here

### `ATTENDANCE` tab (regular trainings only)
Grid layout (names resolved from `MEMBER INFO AY26/27`):

| | A | B | C | D, E, F… |
|---|---|---|---|---|
| **Row 1** | | | `POLL ID` | `poll_id|message_id` per date |
| **Row 2** | `NAME` | `Tabulation` | `TRAINING DATE [REG]` | one training date per column |
| **Rows 3+** | nickname | `=SUM(D3:3)` | | `1` / blank per member |

- **Col B** `Tabulation` is a sheet-side formula — the bot only *reads* it (to sort members most-frequent-first).
- **Row 1** stores **`poll_id|message_id`** (`format_training_poll_ref`), so the
  Modify-a-Date flow can delete or close the superseded poll message in the Voting topic.
- New date columns go into the **first empty slot from col D** (a `Tabulation`/analysis
  column may safely live to the right of the date region).
- Training dates accept flexible formats: `12/8`, `12/8/26`, `12 Aug`, `12 Aug 2025` (`parse_sheet_date`).

### `PERF TABULATION` tab (performance-event attendance)
Same grid shape, but event columns are keyed by the topic's **THREAD ID** (row 1)
with the **EVENT NAME** in row 2. Col A = admin-prefilled roster; col B =
Tabulation formula (read-only for the bot). The bot find-or-creates a column by
thread id (`get_perf_event_column(..., create=True)`) and batch-writes it on
CONFIRM (`commit_perf_column`). `get_all_perf_performers()` reads every marked
member per event — used by the Performance Ledger's performers line.

### `Logistics AY 26/27` sheet (Costume Tracker) — separate spreadsheet
Read/written by `services/costume_sheets.py`; config keys `LOGISTICS_SHEET`,
`COSTUME_OVERVIEW_TAB`, `COSTUME_TRACKING_TAB`, `COSTUME_SIZE_TAB`.

- **`Costume Overview`** — all inventory *state*, two blocks side by side:
  - `A:F` **Main Inventory** — `Item | Set | Item Color | Size | Quantity | Notes`,
    one row per `(Item, Set, Size)`, data rows **3–60**. Hand-maintained;
    ✏️ Update Stock is the only bot write in this whole spreadsheet. The range is
    bounded at row 60 (not at today's last row) so rows can be appended without
    falling outside every `SUMIFS`. Free-text bookkeeping (`Last Updated`,
    `Signed Off`, the update note) sits at **A62+**, deliberately below that
    bound and below a blank row — `get_stock_rows` stops at the first blank
    `Item`, so anything under it is ignored rather than served as stock.
  - `H:P` **RUNNING INVENTORY** — matrix of `Item | Color | Metric | 5 sizes | Total`
    in four sections (Red Set / White Set / Old Pants / New Pants), each item
    expanded into **Total / Out / On hand**. Every cell is a `SUMIFS` (Total from
    Main Inventory, Out from the ledger) — **read-only for the bot**, same rule as
    ATTENDANCE col B. Replaced the old hand-rolled `Summary` block.
- **`Costume Tracking`** — flat ledger, header row 1, one row per issue:
  `Performances | Name | Costume Set | Pant Type | Shirt Size | Pant Size | Quantity |
  Waist Wrap | Wrist Wrap | Head Band | Status | Issued date | Returned date |
  Transferred date | Transferred To | Remarks`. A row is **open** (= counted in
  `Out`) while Returned date and Transferred date are both blank.
- **`Costume Size`** — `Nickname | Shirt Size | Pant Size` defaults.

Notes that bite:
- Pants sit on their own axis (`Pant Type` Old/New), **not** the Red/White costume
  set; New pant sizes carry the height (`M - 170`). `pant_size_label()` **looks
  that label up in Main Inventory** rather than hardcoding it, so a relabelled
  run — or Old/New merging into one type — needs no code change.
- **Only three of the five categories are closed.** `Size` and `LedgerStatus` are
  fixed, so they validate strictly. `CostumeSet`, `PantType` and `Item` are
  **open** — how many sets exist is a purchasing decision, Old/New may merge, and
  a new set may bring a garment nobody coded for. Those three travel as the
  sheet's own **strings**; the enums remain only as named constants for defaults,
  and the UI's option lists come from `list_costume_sets()` / `list_pant_types()`.
  Validating against them would let a newly bought set be stocked but never issued.
- **The `Item` column is a key, not a label.** `adjust_stock` finds its row by
  `(Item, Set, Size)` and every `SUMIFS` filters on it. Putting display text in
  it breaks lookups — `Wrist Wrap (pairs)` once made wrist-wrap stock
  un-editable. Qualifiers belong in `Notes` (col F); the running table carries
  its own display name.
- A **transfer** closes the giver's row *and appends one for the receiver*
  (`apply_transfers`), so `Out` is unchanged by a hand-over. Closing alone would
  make a costume that never came back look returned.
- One member may hold several sets at once — that's several open rows, keyed by
  row number, not by name. Tapping a current holder offers *swap this set* per
  open row **or** *issue an additional set*.
- Block positions are detected (`Metric` cell, `Performances` header), not
  hardcoded, so either block can move without a code change — which is how the
  running table moved from Costume Tracking to Costume Overview for free.
- PERF TABULATION is **cached** (`get_perf_event_list`, `get_perf_attendees`,
  `get_all_perf_performers` all go through `get_cached_values`; both writers
  invalidate it). `_render_config` additionally fetches only on a draft's first
  render — before that, one admin adjusting sizes could exhaust Google's
  60-reads-per-minute quota on its own.

> **Pending redesign — item-first stock.** Recording wants to be item-first
> (a yellow waist wrap is one thing you own) while issuing wants to stay
> set-first. Today's `(Item, Set, Size)` rows force both, so two sets sharing one
> waist wrap can't be expressed without duplicating the row. The agreed direction
> is `Item | Variant | Size | Qty` stock plus a separate `Set | Item | Variant`
> recipe block, so two sets point at one stock row. Not started. An
> `Accessory Set` ledger column was tried as a patch and has been removed.

### `OTHERS` tab
`THREAD ID | EVENT NAME` — one row per OTHERS (bonding/misc) topic.

### `STANDARD TOPIC Rules` tab — effectively retired
`append_standard_topic_to_sheet` and the `TOGGLE_RULE|`/`CONFIRM_STANDARD`
callback patterns still exist, but **no UI path creates a STANDARD topic** any
more (the type picker offers only PERF and OTHERS) and no message-level rule
enforcement runs. Dead code.

### `MEMBER INFO AY26/27` tab
Active roster: `Full Name (as per Matric Card)`, `Nickname`, `Gender`,
`Nationality`, `Matric No`, `School`, `Course`, `Year`, `Contact`, `NTU Email`,
`Date of Birth`, … `Role/Position`, `Status`, `Join Date`, `Leave Date`, `Tele ID`.
All lookups are header-based and case-insensitive, so column order can change.
- `get_nickname_by_user_id`: Tele ID → Nickname (falls back to full name)
- `sync_welcome_tea_members(pairs)`: header-matched **upsert of many members in
  one `batch_update`** (~3 API calls for the whole batch, vs ~4 each). Dedupes
  rows sharing a Tele ID / Matric No, and tracks `claimed_rows` so two new
  members in the same batch can't be assigned the same blank row — `values` is a
  pre-write snapshot, so without that guard one would overwrite the other.
  Returns `{tele_id: (ok, info)}`; `info` separates permanent failures
  (`matric_not_found`, `header_mismatch`, `no_empty_member_row`,
  `member_info_missing_required_columns`) from retryable ones
  (`form_lookup_unavailable`, `member_info_read_failed`,
  `member_info_write_failed`)
- `sync_welcome_tea_member(matric, tele_id)`: single-member wrapper over the
  batch function, so the two can never drift apart. Returns `(ok, info)`
- `update_member_join_in_info` / `update_member_leave_in_info`: join/leave stamps
  (Join → Join Date now + Leave Date cleared; Leave → Leave Date + Status Left);
  new members get a minimal row in the first empty slot inside the formula region
- `get_active_members()` treats a `Status` of **`Active` or `Join`** as active
- Role tiers are read from `Role/Position` + `Tele ID` (see Admin tiers below)
- `_member_tag_map()` (google_sheets.py) builds the attendance-surface tag +
  sort key from `Seniority` (col Q, `S`/`J`) + `Year`: `Year == "-"` → `G`,
  `"exchange"` → `EG`, text with `post`/`pg`/`grad` + a number → `P<n>`, a bare
  number → `U<n>`. Tag = `<S|J><code>` (e.g. `SU3`, `JEG`, `SG`, `SP1`); bare
  `<code>` when Seniority is blank; `""` when neither is set. Sort key
  `(cat_rank, -year)` orders Graduate → Exchange → Postgrad (yr desc) →
  Undergrad (yr desc) → unknown. Used by the Attendance Rate panel and both
  Take-Attendance rosters (`get_attendees_for_date`, `get_perf_attendees`).

### `MEMBER INFO AY25/26` tab
Read-only Tele-ID lookup (`is_member_in_ay2526`) for the returning-member
auto-approve gate.

### `NTUFD Welcome Tea Registration 2026 (Responses)` sheet
Google Form responses; matched by `Matric No` (latest response wins, since
`_find_matric_in_records` scans in reverse). Read by the **active**
`/verification` flow via `lookup_welcome_tea_registration`, which is
**cache-first with a miss-driven refresh** — it does *not* use the shared
modifiedTime cache, because Google writes Form submissions with its own
infrastructure and the linked sheet's `modifiedTime` can lag several minutes
behind a new response.

| Lookup outcome | Google calls | Meaning |
|---|---|---|
| `WT_LOOKUP_FOUND` from cache | **0** | matric already known — immune to a Google outage |
| `WT_LOOKUP_FOUND` after refresh | 1 | form was submitted since the last download |
| `WT_LOOKUP_NOT_FOUND` | 1 | re-read the sheet and it genuinely isn't there |
| `WT_LOOKUP_UNAVAILABLE` | 1 (failed) | Google wouldn't answer — **never** report this as "not found" |

A time-based TTL was tried first (60 s, commit `eb1a117`) and replaced: because
verifications arrive minutes-to-hours apart, the cache was always expired, so
**every** verification hit the network and was exposed to Google's sporadic
503s. That caused two silent onboarding failures for the same member on
19–20 Aug 2026. `refresh_welcome_tea_form_cache()` raises on API failure; the
caller decides what to do.

### `WELCOME TEA` tab (config `WELCOME_TEA_ID_TAB`) — settings + user grid
**Top settings block** (read via `get_welcome_tea_settings`, smart-cached):
- `B1` event date · `B2` WT group invite link · `B3` main-group invite link · `B4` signup-form link
- `C1` optional final-cleanup time override (default 19:30 on event day)
- Job schedule: **row 3 = time, row 4 = date**, columns `I..O` in order:
  I=Details, J=Reminder, K=Approval, L=Pre-Cutoff, M=Cutoff, N=WTD Reminder, **O=Follow-up**

**User grid** (header row auto-detected within the first 6 rows via
`_welcome_tea_layout`; data typically starts row 6; falls back to fixed A–O):

| Col | Field |
|---|---|
| A | Tele ID |
| B | Tele handle / username |
| C | Timestamp (`YYYY-MM-DD HH:MM:SS`) |
| D | Status (`Not Confirm` / `Attend` / `Reject` / `STILL_COMING`; blank ⇒ Not Confirm) |
| E | Dietary |
| F | Attendance (`1` = checked in on event day) |
| G | Cutoff Msg ID (the Details message, for button stripping) |
| H | Final Cutoff Msg ID (the "I'll Be There / Can't Make It" message) |
| I–O | `SENT` markers: Details / Reminder / Approval / Pre-Cutoff / Cutoff / WTD Reminder / **Follow-up** |

Single-cell writes: `append_welcome_tea_id` (upsert by Tele ID; existing status
preserved), `update_welcome_tea_status`, `update_wt_dietary`,
`update_wt_attendance`, `update_wt_msg_id`, `mark_wt_user_col_sent`.
**Bulk writes** (used by every broadcast job): `get_wt_write_context()` returns
`(worksheet, {tele_id: row_number})` for one read, and
`batch_update_wt_cells(updates, ctx=…)` writes many `(user_id, col, value)` cells
in a **single** `batch_update` call — this is what keeps a 100-member broadcast
inside Google's 60-requests-per-minute quota. `get_welcome_tea_recipients(statuses)`
filters rows (non-numeric Tele IDs skipped); calling it with no argument returns
every row.

---

## Feature Flows

### New Member Onboarding (`join_request_handler`)
Routing order for an incoming join request (all invite links need
"Request Admin Approval" enabled):
1. **Main-group post-Welcome-Tea link** (`link url == B3` of the WT sheet) →
   verification prompt: the request stays pending in `pending_users`, the user id
   is also persisted to `bot_data["pending_verification_ids"]` (the
   `ChatJoinRequest` object itself cannot be pickled), and the user is DM'd to
   type **`/verification`** (or `/verify`).
2. **Welcome Tea request** (`chat id == WELCOME_TEA_GROUP_CHAT_ID` or
   `link url == B2`) → Welcome Tea automation flow (below). *(The old link-NAME
   keyword route is commented out.)*
3. **Returning member** (`Tele ID` found in `MEMBER INFO AY25/26`) →
   welcome-back DM sent **before** `approve()` (the join-request DM window closes
   once the request is processed; a DM failure never blocks approval), then
   approve + `update_member_join_in_info` stamp.
4. **Any other request to the main group** (`chat id == CHAT_ID`) → same
   verification prompt as (1) — covers renamed/regenerated links.
5. **Anything else** → pending + "contact our admin" DM (role-driven contact via
   `get_join_contact_admin_id`, `JOIN_CONTACT_ADMIN_ID` fallback) + alert DM to
   all MAIN admins.

**`/verification` matric flow** (ACTIVE, not legacy) — **approve first, write later**:
1. Asks for the matric number, shows a typing indicator and pauses
   `VERIFICATION_THROTTLE_SECONDS` (2 s) purely for UX.
2. `lookup_welcome_tea_registration(matric)` — **cache first**: a matric already
   in the cached Form Responses costs zero API calls; only a **miss** re-reads
   the sheet (so a form submitted seconds ago is picked up). Three outcomes:
   - **UNAVAILABLE** (Google unreachable, typically a transient 503) → sleeps
     `VERIFICATION_RETRY_DELAY_SECONDS` (2 s) and retries **once**. Still
     unavailable → tells the member the *system* is busy and to send the number
     again shortly, and **stays in `ASK_MATRIC`** so re-sending just works.
     Never says "matric not found" here — we never got to check.
   - **NOT_FOUND** (fresh read, genuinely absent) → typo/registration-form
     message; also stays in `ASK_MATRIC`.
   - **FOUND** → continue to step 3.
   The retry sleeps in this async handler, never inside the sync sheets layer,
   so the event loop is never blocked.
3. Found → **approves the join request immediately** (falling back to
   `bot.approve_chat_join_request(CHAT_ID, user_id)` when the request object is
   gone after a restart), so the member never waits on Sheets.
4. Replies "welcome!" **first**, then writes MEMBER INFO **inline** via
   `asyncio.to_thread(sync_welcome_tea_member, …)` — the member is already
   approved and in the group, so the write never keeps them waiting, and it runs
   off the event loop because gspread is synchronous.
   The queue is only a **fallback**, entered in two cases:
   - the inline write failed (503, read/write error) → queued for retry;
   - `bot_data["pending_member_writes"]` is already non-empty → a burst is in
     progress, so join it and let the drain job batch everyone together.

   This ordering matters: the queue is an in-memory dict backed by
   `bot_data.pkl`, which Heroku's ephemeral filesystem discards on every dyno
   restart. Writing inline means the common case is confirmed immediately
   instead of sitting somewhere a restart would silently erase.

**`drain_member_writes_job`** (every 7 s) takes the **whole queue at once** and
passes it to `sync_welcome_tea_members`, which upserts every member in a
**single `batch_update`** — ~3 API calls for the entire batch instead of ~4 per
member. Results come back per member (`{tele_id: (ok, info)}`), so one bad
matric never sinks the rest: codes in `MEMBER_WRITE_DROP_CODES` are dropped with
a warning, everything else is retried on the next tick.

> Two failure modes this design deliberately avoids. **Head-of-line blocking**:
> the old one-entry-per-tick loop always took `next(iter(queue.items()))`, so a
> single stuck entry starved everyone behind it forever. **Silent data loss**:
> `sync_welcome_tea_members` returns `form_lookup_unavailable` /
> `member_info_read_failed` / `member_info_write_failed` — none of which are drop
> codes — so a transient Google 503 can never be mistaken for "this matric isn't
> registered" and throw a verified member's form data away.

**Join/leave tracking** (`handle_member_status` + `handle_new_member` safety
net): join → Join Date stamped, Leave Date cleared, Status=Join; leave/kick →
Leave Date, Status=Left. Bot accounts are skipped. **The Welcome Tea group is
excluded** from tracking. Both paths invalidate the sheet cache.

### Welcome Tea Automation (`welcome_tea_handlers.py`)
Entirely **sheet-driven**: `welcome_tea_scheduler_tick` reads the settings block
and fires each job **once per scheduled datetime** (tracked in
`bot_data["wt_jobs_sent"]`, persisted via PicklePersistence; changing a time in
the sheet re-arms that job; a new event date in B1 resets all tracking).

> ⚠️ The tick is **not currently registered** — `schedule_welcome_tea_jobs` is
> commented out in `on_startup`. Re-enable it (or invoke the job callbacks
> manually) to run the timeline.

Jobs, in timeline order:
1. **Details** — DM event details + ✅ Confirm / ❌ Reject buttons to `Not Confirm`
   users (msg id → col G; `SENT` → col I)
2. **Reminder** — re-DM still-`Not Confirm` users (col J)
3. **Approval** — approve pending WT join requests of `Attend` users, decline
   `Reject` users (col K)
4. **Pre-Cutoff nudge** — last-call DM to `Not Confirm` users (col L)
5. **Cutoff** — strip the Details buttons (via col G), DM the "RSVP closed"
   message with 🎉 I'll Be There!! / 😔 Can't Make It buttons (msg id → col H,
   `SENT` → col M)
6. **WTD Reminder** — event-day reminder: confirmed (`Attend` + `STILL_COMING`)
   get dinner-included text; `Not Confirm` get eat-beforehand text (col N)
7. **Follow-up** — **per-person DM** of the thank-you + main-group invite link to
   everyone with `Attendance = 1` (i.e. who actually ran `/attd` on event day),
   tracked in col O so the job is safely re-runnable. *(This used to be a single
   post into the WT group; it is now individually tracked DMs.)*
8. **Final cleanup** (event day 19:30, or C1 override) — silently strip the
   still-coming buttons for `Not Confirm`/`STILL_COMING` users

**Broadcast pacing and crash safety** (shared by every job above):
- `_paced_send(lambda: …)` wraps each Telegram call: a 50 ms gap after every send
  (`BROADCAST_SEND_DELAY_SECONDS`, ≈20 msg/s vs Telegram's 30/s cap) and one
  automatic retry after the stated wait on a `RetryAfter` 429. It takes a
  **callable**, not a coroutine, because a coroutine that already raised cannot
  be awaited twice.
- `_flush_writes(writes, ctx)` pushes buffered `SENT` markers to the sheet every
  `BROADCAST_FLUSH_EVERY_CELLS` (40) cells rather than once at the end, and the
  `finally` block always force-flushes the tail. A dyno restart mid-broadcast can
  therefore duplicate at most ~20 members' messages instead of the whole list
  (`wt_jobs_sent` is only stamped after the job completes).
- `_pace_batch(...)` sends in batches of `BROADCAST_BATCH_SIZE` (20) with a
  `BROADCAST_BATCH_PAUSE_SECONDS` (300 s) pause between them — **not** a Telegram
  limit, but a way to stagger when members *receive* the DM and therefore when
  they tap Confirm (each Confirm costs ~4 Google reads against a 60 reads/min
  quota). Markers are flushed *before* the sleep so a restart during the pause
  does not lose them.
- Every job skips rows whose `SENT` marker is already set, and only queues a
  marker **after** a successful send.

**Join-request catch-up**: `handle_welcome_tea_join_request` registers the user
(status `Not Confirm`), then:
- if their row already has `Attendance = 1` (they checked in via `/attd` first),
  it approves them immediately and sends nothing else;
- otherwise it DMs the "Yay!" message and — based on where "now" falls in the
  schedule (`_detect_wt_window`: before-details / W1 / W2_W3 / W4) — also sends
  whatever they missed (Details, Details + Reminder, or the Cutoff DM).

The request stays pending until the Approval job (or an event-day check-in).

**Buttons**: ✅ Confirm → status `Attend` (+ immediate approve if the approval
time has already passed) + **dietary question** (reply captured by
`handle_dietary_reply`, a group -2 private-message intercept keyed on
`bot_data["wt_pending_dietary"]`, written to col E; skipped when dietary is
already filled). ❌ Reject → status `Reject` + join request declined immediately.
🎉 I'll Be There!! → `STILL_COMING`. 😔 Can't Make It → `Reject` + decline.

**`/attd` event-day check-in** (registered at group -3, for **non-admins**;
admins are redirected to `/start`): only works on the event date. It marks col F,
approves the still-pending WT join request, and DMs the WT group link when there
is no pending request left to approve. A walk-in with no row at all is registered
on the spot and sent the link.

### Performance Topic Lifecycle
1. **Create** (MAIN admins only — both the Cockpit button and every creation
   callback check `is_main_admin`): Cockpit → 🎪 New Topic → PERF or OTHERS.
   - **PERF** — 6-step wizard in a single edited bubble with a live progress
     tracker: ① EXT/INT ② Event Name ③ Rehearsal Date & Time (**Skip** ⇒ `-`)
     ④ Performance Date & Time (**Skip** ⇒ `TBD`) ⑤ Location ⑥ Other Info
     (**Skip** ⇒ `-`) → summary preview with **per-field ✏️ Edit buttons**
     (summary mode returns straight to the preview after an edit) → CONFIRM
     creates topic `PERF - {name}`, appends the sheet row (10 cols), posts +
     pins the summary. Parse errors re-render in place with the previous value
     in a tap-to-copy code block.
   - **OTHERS** — title → confirm → topic created, row appended to `OTHERS`,
     thread id added to `OTHERS_THREAD_IDS` + `initialized_topics`.
2. **Status**: there is **no `/confirmation` command any more**. STATUS is
   changed via 🛠️ Edit Performance (ACCEPTED / REJECTED / PENDING buttons) or the
   remind-portal escrow (below). Setting REJECTED on a future-dated event posts a
   cancellation notice into the topic; the topic itself is never deleted.
3. **Remind**:
   - **Automatic**: `daily_reminder_cron_job` (09:00 SGT) scans for performances
     exactly 7 days away; ACCEPTED → posts the prep checklist (with a countdown
     line) into the topic; blank STATUS → DMs MAIN admins a nudge.
   - **Manual**: Cockpit → ⏰ Reminders → topic picker (sorted latest perf date
     first). Guards: past events and REJECTED are blocked (banner). A
     blank/PENDING event triggers the **escrow gate**
     (`REMIND_ESCROW_CONFIRM|YES/NO`): "Approve & Broadcast" flips STATUS to
     ACCEPTED in the sheet and immediately broadcasts the checklist.
4. **Modify** (Cockpit → 🛠️ Edit Performance → tappable event list, sorted latest
   first): the field menu shows a **public summary preview + internal registry
   preview** (Event Type / Remuneration / Status). Edits are **staged in
   `user_data` ("pending edits")** — nothing is written until 💾 **Save & Push
   Updates**, which batch-writes the changed cells, then:
   - renames the forum topic if EVENT NAME changed,
   - posts the cancellation notice if STATUS flipped to REJECTED (future events),
   - re-publishes the pinned summary **only if a public field changed and the
     event is not REJECTED** (internal-only edits don't ping the topic),
   - returns to the event list with a success banner.
5. **Announce**: Cockpit → 📣 Broadcast → target picker (General Chat = thread 0,
   all PERF topics sorted latest-first, all OTHERS). Past-dated and REJECTED
   performances are blocked. The bubble switches to text-capture mode
   (`modify_field = "ANNOUNCEMENT_TEXT_CAPTURE"`); the typed message is deleted
   from the DM and forwarded verbatim (Markdown) to the target thread.

### Attendance (Cockpit → ✅ Take Attendance)
Top-level category menu: **🏋️ Regular Training** vs **🎭 Performance**, tracked in
`user_data["attd_mode"]`. The whole flow runs in a single edited bubble
(`_update_attendance_bubble`; "message is not modified" no-ops are swallowed).
The member-toggle UI is shared: **paginated 20 per page** (`ATTD_PAGE|`), a live
**"✅ Present: n / total" counter**, toggle buttons two-per-row, CONFIRM
batch-writes the whole column, and every terminal outcome re-renders the list
with a banner (never a dead-end).
- **Performance branch**: event picker from the PERF tab
  (`get_perf_event_list`, sorted latest perf date first, present count in
  brackets) → find-or-create the thread-id column in PERF TABULATION → roster
  from col A pre-ticked → CONFIRM (`commit_perf_column`). No dates/polls here.
  `get_perf_attendees` applies the same **year-category** sort + tag prefix as the
  regular branch.
- **Regular branch**: date picker lists only **polled** dates
  (`get_training_date_columns`, latest first, present count) → toggles → CONFIRM
  (`commit_attendance_column`). Includes the ✏️ Modify-a-Date sub-flow (below).
  `get_attendees_for_date` sorts the toggle roster by **year category** then
  `Tabulation` desc then name A–Z, and prefixes the display label `(SU3) ` /
  `(JEG) ` etc. — see `_member_tag_map` below (display only — the toggle/commit
  key is the member row).
- Entry is via the Cockpit only (`DASH_VIEW|LAUNCH_ATTD`); there is no admin
  `/attd` DM command any more (`/attd` is the member-facing WT check-in).

### Training Attendance Poll (regular trainings)
- **Auto-poll** (`auto_poll_check`, daily 09:00 SGT): for any row-2 date **≤ 4
  days away (down to today)**, not past, with no poll ref yet → posts a
  non-anonymous poll (option 0 must stay the "yes" option) to the Voting topic,
  writes `poll_id|message_id` into row 1, and schedules a **10 PM night-before
  reminder** (a `threading.Timer` calling `send_reminder`). The ≤4-day window
  (not ==4) catches dates moved inside the window by manual sheet edits.
- **Vote capture** (`handle_poll_answer`): Tele ID → nickname, poll id → column,
  writes `1`/clears the cell; new voters get a row appended. Poll-type detection
  falls back to the sheet (`is_training_poll_id`) after restarts. The Tabulation
  column is never written.
- **Modify a training date** (REG date list → ✏️ Modify a Date): lists only
  **upcoming** polled dates; the current date is shown as a tap-to-copy code
  block; the new date must be in standardised format (`12 June`, `12 Jun 2026` —
  slashes rejected; a year-less past date is rejected immediately and the past
  check re-runs at CONFIRM). On confirm, `replace_training_date_column` rewrites
  row 2 in place and **clears the column's ticks**, and the **old poll message is
  deleted (or closed)** via the stored message id. A fresh poll is posted now if
  the new date is ≤ 4 days away, otherwise the poll ref is left blank for
  `auto_poll_check`. Typed dates are consumed by `handle_moddate_text` (hooked
  into the DM dispatcher; the typed message is deleted, the bubble is edited).

### Group Message Handling (`message_handlers.py`)
`handle_message` does **one thing**: when a `forum_topic_created` event arrives,
the topic is deleted and the creator DM'd to use the Command Centre — **unless**
the thread id is in `initialized_topics` (bot-created; PERF + OTHERS ids are
loaded into it at startup) **or the creator is a MAIN admin** (their manual topic
is permitted and cached). All other group traffic is completely untouched — no
command-word matching, no per-thread permission rules (the old rule cache is
gone). A superseded inline role-lookup block survives behind `if False and …` and
never executes.

---

## In-Memory / Persisted State

`utils/constants.py` (in-memory, reset on restart):

| Name | Type | Purpose |
|---|---|---|
| `initialized_topics` | `set` | Bot-created (or MAIN-admin-permitted) thread IDs; PERF + OTHERS ids loaded at startup |
| `OTHERS_THREAD_IDS` | `set` | OTHERS thread IDs (added on creation; startup load goes into `initialized_topics`) |
| `active_polls` | `dict` | `{poll_id: "training" \| "interest"}` |
| `yes_voters` / `interest_votes` | set / dict | poll bookkeeping |
| `pending_users` | `dict` | `{user_id: ChatJoinRequest}` awaiting `/verification` |
| `welcome_tea_pending_requests` / `welcome_tea_join_chats` | dicts | WT join requests awaiting the approval job (chat id also mirrored into `bot_data`) |
| `pending_questions` | `dict` | legacy prompt-cleanup tracking |

`bot_data` (persisted in `bot_data.pkl`):

| Key | Purpose |
|---|---|
| `wt_jobs_sent` | WT job name → fired-at datetime string |
| `wt_last_event_date` | resets `wt_jobs_sent` when B1 changes |
| `wt_pending_dietary` | `{str(user_id): True}` — users currently in dietary-capture mode |
| `welcome_tea_group_chat_id` | last-seen WT group id, for approve/decline fallbacks |
| `pending_verification_ids` | `set` of user ids with a pending main-group request — survives a restart, since the `ChatJoinRequest` object cannot be pickled |
| `pending_member_writes` | `{tele_id: matric}` queue drained by `drain_member_writes_job` |
| `group_chat_data` | per-group scratch store used by the pinned-summary publisher |

---

## Bot Commands & Handlers (registered in `main.py`)

The Telegram command menu (per-admin `BotCommandScopeChat`) lists **only
`/start`**; the public default menu is cleared. `/remind`, `/testremind` and
`/modify` command registrations are **commented out** — those flows live inside
the Cockpit. There is **no `/confirmation` handler** any more.

| Command | Handler | Behaviour |
|---|---|---|
| `/start` | `admin_handlers.start` | Opens the Cockpit (admins, DM only; silent otherwise) |
| `/threadid` | `admin_handlers.thread_id_command` | **DM only** — full forum directory chart (PERF + OTHERS). Passive in groups |
| `/attd` | `welcome_tea_handlers.handle_attd_checkin` (group -3) | **Member-facing WT event-day check-in**; admins are told to use `/start` |
| `/verify`, `/verification` | `verification_handlers` conversation | Matric verification for pending main-group join requests |

**Global button security gate** (`global_button_security_check`, `TypeHandler`
at group -1): every callback query from a non-dashboard-admin is answered with
"Access Denied" and killed via `ApplicationHandlerStop` — **except** the four
Welcome Tea RSVP callbacks (`WELCOME_TEA_CONFIRM/REJECT/STILL_COMING/CANT_MAKE_IT`),
which pass through for regular users. `DASH_VIEW|` and `DASH_REFRESH` clicks are
verified with `force=True` (live sheet read, throttled).

Other pre-gate handlers: `handle_dietary_reply` (group -2) intercepts private
text from users in dietary-capture mode and stops propagation.

### Scheduled jobs
- **`daily_reminder_cron_job`** — 09:00 SGT daily; 7-day PERF scan (checklist / admin nudge).
- **`auto_poll_check`** — 09:00 SGT daily; training-poll window check.
- **`drain_member_writes_job`** — every 7 s (first run at +10 s); drains the
  **entire** pending-write queue in one batched MEMBER INFO write. Only does
  anything when the queue is non-empty, i.e. when an inline write failed or a
  burst is in progress.
- **`welcome_tea_scheduler_tick`** — every 30 s **when enabled**; fires the 8 WT jobs
  from sheet-defined datetimes. Its registration is currently commented out.

> Testing: commented `run_once`/`run_repeating` lines sit next to both daily
> registrations in `main.py`/`on_startup` for quick local testing.

---

## Admin DM Dashboard (Private Chat)

### Two admin tiers (role-driven from `MEMBER INFO AY26/27`)
Resolved from `Role/Position` + `Tele ID` (case-insensitive contains-match).
**No per-user role cache** — `_get_user_role` re-derives the role from sheet data
on every call, with a **major-click freshness rule**:
- **MAJOR interactions** (`/start`, `/threadid`, every Cockpit `DASH_VIEW|`
  navigation click, `DASH_REFRESH`) pass `force=True` → the MEMBER INFO tab is
  re-downloaded live (bypassing the lazily-updated Drive modifiedTime), so a role
  granted/removed in the web UI applies on that click. Throttled to one real
  download per **30 s** (`_ROLE_FORCE_THROTTLE_SECONDS`).
- **In-task actions** (attendance toggles/paging, wizard steps, modify-field
  edits, announce picks) use the cached copy → instant; an admin mid-task is
  never slowed down and finishes their flow.
- **MAIN** — `chairperson` (covers Vice), `secretary`, `sde` → full dashboard,
  receive all alert/reminder DMs (`get_alert_admin_ids`), may create topics
  (wizard **and** manual in-group creation), pass `is_main_admin`.
- **SECONDARY** — `treasurer`, `logistic`, `business`, `publications`, `coach` →
  **read/broadcast access only**. `handle_dashboard_navigation` blocks
  `LAUNCH_NEW`, `LAUNCH_MODIFY`, `LAUNCH_ATTD` and `LAUNCH_REMIND` for them (and
  `handle_list_modify_callback` plus the creation callbacks re-check), so a
  SECONDARY admin can use 📣 Broadcast and the 🎭 Events / 👥 Members / 📦 Logistics
  hubs (Performance Ledger, Thread Index, Member Roster, Attendance Rate), and
  nothing else. No alert DMs.
- `ADMIN_DM_USER_IDS` is an emergency fallback used **only** when the role lookup
  yields nothing (sheet unreachable / Role column wiped).
- Join-flow contact admin: first MAIN admin in priority order chairperson →
  secretary → sde, else `JOIN_CONTACT_ADMIN_ID`.

All gates route through `is_dashboard_admin()` (used by `/start`, the DM
dispatcher, the global button gate, attendance callbacks, refresh). Unauthorized
DMs get total silence. The DM text dispatcher (`handle_private_command`) accepts
**only `/start`** as a command (typing `/start` mid-wizard cancels the active
flow); all other text is flow input (wizard steps, modify values, announcement
text, modify-date entry) or ignored.

### The Cockpit (`/start` dashboard)
`/start` opens the **"NTUFD Command Centre"** — a 7-button grid + ♻️ Refresh
Data. Buttons emit `DASH_VIEW|<target>` → `handle_dashboard_navigation`;
`DASH_REFRESH` → `handle_dashboard_refresh`. Everything renders as a single
edited bubble with success banners and 🦅 Exit to Cockpit buttons.

| Button | `DASH_VIEW\|…` | Action | Tier |
|---|---|---|---|
| 🎪 New Topic | `LAUNCH_NEW` | PERF/OTHERS picker | MAIN |
| 🛠️ Edit Performance | `LAUNCH_MODIFY` | tappable event list → staged-edit field menu | MAIN |
| ✅ Take Attendance | `LAUNCH_ATTD` | attendance category menu | MAIN |
| ⏰ Reminders | `LAUNCH_REMIND` | manual remind portal (with escrow) | MAIN |
| 📣 Broadcast | `LAUNCH_ANNOUNCE` | announce portal (General + PERF + OTHERS targets) | MAIN + SECONDARY |
| 🎭 Events | `EVENTS` | hub → 📊 Performance Ledger (`LEDGER`) · 🧵 Thread Index (`THREADS`) | MAIN + SECONDARY |
| 👥 Members | `PEOPLE` | hub → 👥 Member Roster (`MEMBERS`) · 📈 Attendance Rate (`ATTD_RATE`) | MAIN + SECONDARY |
| 📦 Logistics | `LOGISTICS` | Costume Tracker — 📦 Overview / 🎭 Costume Tracking / 📏 Costume Size. Gated by `is_logistics_admin` (MAIN + anyone whose role contains "logistic"), tighter than the other hubs | MAIN + Logistics |

**Hub sub-views** (reached from the two hubs above; each renders in the same
bubble with a 🔙 Back to its hub + 🦅 Exit to Cockpit):

| Sub-view | `DASH_VIEW\|…` | Action |
|---|---|---|
| 📊 Performance Ledger | `LEDGER` | status list (✅/⏳/❌, latest first, `+n more` date collapsing) **with a 👤 performers line per event** from PERF TABULATION |
| 🧵 Thread Index | `THREADS` | thread-id directory (General 0, Voting 53 hardcoded, PERF, OTHERS) |
| 👥 Member Roster | `MEMBERS` / `MEMBERS_<tok>` | grouped member menu: 🎩 Graduates (`Year == "-"`, top) → 🌏 Exchange (98) → 🎓 Year N → other named groups → ❓ Unassigned; counts on buttons, drill-down per group |
| 📈 Attendance Rate | `ATTD_RATE` / `ATTD_RATE_<n>` | per-member regular-training attendance rate = ATTENDANCE col B `Tabulation` ÷ polled-REG-date count. Active members only. Sorted by **year category** (see `_member_tag_map`) then rate-desc then nickname A–Z; names prefixed with the tag `(SU3)` / `(JEG)` / `(SG)` / `(SP1)` etc. Paginated **20/page**, rendered `🟢 *100%*  (SU3) Wei Yin  (12/15)` with a 🟢≥80 / 🟡≥50 / 🔴 dot. `%` clamped at 100. Banner when there are no polls yet / no active members. Data from `get_training_attendance_rates()` → `(nick, attended, polls, tag)` (smart-cached, 0 API calls warm) |

### Costume Tracker screens (📦 Logistics)
All render in the one dashboard bubble; callback namespace `LOGI|<verb>|…`.

| Screen | Callback | Notes |
|---|---|---|
| Costume Tracker home | `HOME` | 📦 Costume Overview · 🎭 Costume Tracking · 📏 Costume Size |
| 📦 Costume Overview | `LIST` | a **menu**, no sheet reads — two table buttons + ✏️ Update Stock |
| 📋 Main Inventory | `INV\|<page>` | `Item / Set / Size / Qty`, 10 rows a page |
| 📊 Running Table | `RUN\|<section>` | one section a screen; item name on its own full-width row, then `tot` / `out` / `left` across the size columns |
| ✏️ Update Stock | `SKP → SKC → SKS → SKI → SKZ → SKADJ` | drill-down Costume Set **or** Pants → set → item → size → `−10 −5 −1 / +1 +5 +10`. Single-option steps are skipped, and `_stock_back()` skips them on the way back too so Back never bounces forward |
| 📏 Costume Size | `SIZE\|<page>` · `SZE` · `SZ` | 71 members, 16 a page |
| 🎭 Costume Tracking | `TRACK` · `ACT\|<action>` | Issue / Return / Transfer |
| Issue | `EV → PF → PFN\|PFS → DSET/DPTY/DSH/DPS/DACC → ADD` | `PF` routes to the swap-or-additional choice when the performer already holds something |
| Return / Transfer | `HSEL` · `TRT\|<row>\|<page>` · `TRS` | Transfer picks a **recipient**; both act on open ledger rows |
| Review / commit | `REV` · `SUB` · `CLR` | nothing is written until `SUB` |

**Both tables are drawn as button grids** — Telegram has no table markup and
splits a row's width evenly between its buttons, so a keyboard is the only way
to get aligned columns. Every cell carries `LOGI|NOP` and does nothing; it is a
layout device, not a menu.

**Only the latest panel is live**, enforced two ways:
1. **Neutralise-on-open**: `/start` edits the previous `master_dash_id` bubble to
   a "🛑 closed" message with its keyboard stripped.
2. **Click-time guard**: `handle_dashboard_navigation`, `attendance_callback` and
   `handle_confirm_new_perf` reject clicks on any bubble whose id ≠
   `master_dash_id` with an "expired panel" notice.

### Data freshness (smart cache + ♻️ Refresh)
List/menu reads go through `get_cached_records()` / `get_cached_values()`:
a Drive `modifiedTime` check (itself cached 30 s, `_MODIFIED_TIME_TTL_SECONDS`)
decides whether to serve the snapshot or re-download. Keyed by
`(sheet_name, tab_name)`, shared across admins. `modifiedTime` is a per-FILE
property, so an edit to any tab refreshes every tab's cache for that spreadsheet.
- Every bot write calls `invalidate_sheet_cache()` → next read re-pulls fresh.
- Manual web-UI edits are caught when Drive's `modifiedTime` catches up, or
  instantly via ♻️ Refresh Data.

### Editable fields (`Edit Performance`)
`EVENT TYPE` (EXT/INT buttons), `EVENT NAME`, `REHEARSAL DATE | TIME`,
`PERF DATE | TIME`, `LOCATION`, `OTHER INFO`, `REMUNATION`, `STATUS`
(ACCEPTED/REJECTED/PENDING buttons). Date fields use the comma format and show
the previous value in a tap-to-copy block; `-` clears a date field. All edits
are **staged** until 💾 Save & Push Updates (see lifecycle §4).

---

## Date Format Convention (`date_parser.py`)

Each line = **one date**, comma-separated from its time(s):
`DATE , TIME [TIME …]`. Output: `DD Mon YYYY  H:MM AM/PM` (times joined with ` / `).

### Supported inputs per line
```
31 aug, 9pm                      → 31 Aug 2026  9:00 PM
1 sep, 7pm 9pm                   → 01 Sep 2026  7:00 PM / 9:00 PM
31 aug, 9am - 6pm                → 31 Aug 2026  9:00 AM - 6:00 PM   (time RANGES)
31 aug, 9am - 6pm / 8pm - 10pm   → two range slots joined with ' / '
31 aug                           → 31 Aug 2026                       (date only, no comma needed)
10oct                            → shorthand without space also accepted
```

### Rules
- Date part: `DD MON [YY|YYYY]`; month 3–9 letters, case-insensitive.
- Times: am/pm suffix, `10:30pm`, or 24-hour `0000`/`1230`. Ranges use `-`;
  distinct slots are separated by `/`.
- A line **without a comma** is treated as date-only unless it visibly contains
  a time (colon or am/pm suffix), in which case it is auto-split.
- **Smart roll-forward**: omitted year + already-passed month/day ⇒ next year.
- **Year window**: 2025–2029 hard limit; 2-digit years expand to `20YY`.
- Multiple dates: one per line (`parse_and_format_dates`).
- Sheet storage: multi-date values are one cell with `\n`-separated lines; the
  pinned summary renders each line as a bullet.
- `parse_date_line` is also used by the Modify-a-Date flow (date part only;
  slash formats like `12/6` are rejected there).

---

## Deployment & Development Notes

- Heroku worker dyno (`Procfile`: `worker: python main.py`), polling mode
  (`allowed_updates=Update.ALL_TYPES`).
- `BOT_TOKEN` and `GOOGLE_CREDENTIALS_JSON` come from env vars (Heroku Config
  Vars). Nothing is hardcoded in `config.py` any more.
- The **main group config is active**; the debug group lines are commented out
  in `config.py` (CHAT_ID, WELCOME_TEA_GROUP_CHAT_ID, TOPIC_VOTING_ID, SHEET_NAME).
- `.gitignore` covers `/venv`, `/.venv`, `__pycache__/`, `*.pyc` and `*.pkl`, so
  `bot_data.pkl` and the local virtualenv are no longer tracked.
- All handlers are `async` — do not introduce synchronous blocking calls in
  handler code (the one existing exception is the `threading.Timer` night-before
  reminder in `auto_poll_check`).
- The Google Sheets quota (60 reads/min, 60 writes/min per user) is the binding
  constraint on every bulk operation. New broadcast or bulk-write paths should
  reuse `get_wt_write_context` + `batch_update_wt_cells` (or an equivalent single
  `batch_update`) instead of looping single-cell writes.

### ⚠️ Known code issues
- **`/attd` status strings don't match the canonical statuses.**
  `handle_attd_checkin` branches on `"Confirm"`, `"I'll still be Coming"`,
  `"Last Min CMI"`, `"Reject"` and `"Waiting for reply"`, and writes
  `"I'll still be Coming"` (also the status it registers walk-ins with) — but
  `config.WELCOME_TEA_STATUS_*`, and everything else in the codebase, uses
  `Attend` / `Reject` / `Not Confirm` / `STILL_COMING`. Attendance is still
  marked (the fall-through `else` handles the mismatch, and
  `_normalize_welcome_tea_status` passes unknown strings through verbatim), but a
  user checked in this way ends up with a status no other flow recognises.
- **Orphaned `@admin_only` decorator in `admin_handlers.py`**: the function it
  originally decorated (`manual_test_reminder_trigger`) is commented out, so
  Python attaches it to the next `def`, `initiate_remind_portal_via_dm`. Harmless
  in practice (always called from admin DM contexts) but unintended — safe to delete.
- **Stale docstring**: `_get_user_role` still says the force throttle is 2 s;
  `_ROLE_FORCE_THROTTLE_SECONDS` is 30.
- **Stale comment** in `replace_training_date_column` (google_sheets.py) says the
  auto-poll fires at "2 days away"; the actual window everywhere is ≤ 4 days.
- **Dead branches in `handle_confirm_new_perf`** (private_handlers): its
  `MANUAL_REMIND_TID|` and `ANNOUNCE_TARGET|` branches are unreachable — neither
  prefix appears in the handler's registered pattern, and both callbacks are
  routed to `admin_handlers` instead. `private_handlers.initiate_announce_portal_via_dm`
  is likewise shadowed by the `admin_handlers` version everywhere except the
  `ANNOUNCE_BACK_MAPPED` path.
- **Dead code block** in `message_handlers.handle_message`: an inline role lookup
  guarded by `if False and …`, superseded by the `is_main_admin()` check above it.
- **Unwired / legacy functions**: `handle_welcome_tea_qr`,
  `mark_welcome_tea_setting_sent`, `append_standard_topic_to_sheet`,
  `send_interest_poll`, `record_training_poll`, `ensure_attendance_headers`,
  `matric_valid`, `update_user_id_in_sheet`, `copy_user_to_timeline`,
  `user_already_in_timeline`, `mark_user_left_in_sheet`, `get_next_monday_8pm`,
  `_date_label_from_display`, `utils.decorators.is_admin`, and
  `handle_modify_date_selection` (a stub that only answers "no longer available").
- **Unwired Costume Tracker functions**: `add_costume_set`, `remove_costume_set`,
  `set_accessories`, `list_accessory_owners`, plus the `accessory_set` field on
  `Holding` / `IssueEntry` in `services/costume_sheets.py`. The ➕ New Costume Set
  wizard that drove them was removed pending the item-first redesign, so nothing
  calls them. Harmless as they stand — `append_issues` skips the `accessory_set`
  write when the column is absent and `get_open_holdings` falls back to
  `Costume Set` — but delete or rewire them when that redesign lands.
- **`config.py` is currently a LOCAL DEV copy** — `BOT_TOKEN` and
  `GOOGLE_CREDENTIALS_JSON` are hardcoded instead of read from env vars, and
  `SHEET_NAME` / `CHAT_ID` / `WELCOME_TEA_GROUP_CHAT_ID` point at the **debug**
  sheet and groups. Do not commit it in this state: it would put the live bot
  token and Google private key into git history permanently, and deploying it
  would run production against the debug sheet. Only the four
  `LOGISTICS_SHEET` / `COSTUME_*_TAB` constants belong in a commit.
