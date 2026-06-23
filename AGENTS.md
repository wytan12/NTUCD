# NTUCD Telegram Bot — Project Overview

## Purpose

This bot operationalizes administrative processes for **NTU Chinese Drums (NTUCD)**, a university CCA at Nanyang Technological University. It manages:

- New member onboarding and verification
- Performance opportunity lifecycle (creation → confirmation → reminder)
- Weekly training attendance polls
- Group topic access control
- Member join/leave tracking in Google Sheets

---

## Architecture

| Layer | Technology |
|---|---|
| Bot framework | `python-telegram-bot` v21.5 (async, polling) |
| Database | Google Sheets via `gspread` + `oauth2client` |
| Timezone | Asia/Singapore (`pytz`) |
| Scheduler | `JobQueue` (APScheduler): daily 7-day performance reminder scan (09:00 SGT) + daily auto-poll (09:00 SGT) + one-off Welcome Tea details/reminder/approval jobs; per-admin DM menus registered as a detached `asyncio.create_task` on startup |
| Deployment | Heroku worker dyno (`Procfile`: `worker: python main.py`) |

---

## Directory Structure

```
telegram-bot/
├── main.py                  # Entry point — registers all handlers, loads startup data
├── config.py                # Bot token, sheet names, chat/thread IDs, admin user IDs
├── requirements.txt
├── Procfile                 # Heroku worker: python main.py
├── handlers/
│   ├── admin_handlers.py        # /start, /threadid, remind portal, daily_reminder_cron_job (7-day scan), send_reminder()
│   ├── conversation_handlers.py # Topic type selection, parse_perf_input, /confirmation flow
│   ├── member_handlers.py       # handle_new_member, handle_member_status (join/leave tracking)
│   ├── message_handlers.py      # Group message access control (per-thread rules)
│   ├── modify_handlers.py       # /modify flow — edit performance sheet fields
│   ├── attendance_handlers.py   # Admin DM `attd` flow — mark regular-training attendance
│   ├── poll_handlers.py         # auto_poll_check (daily), handle_poll_answer → attendance, send_interest_poll
│   ├── private_handlers.py      # Admin DM dashboard (/new PERF/OTHERS, list, modify, announce, info, attd, remind portal)
│   ├── verification_handlers.py # join-request router (Welcome Tea vs Tele-ID gate); legacy /verify matric conversation
│   └── welcome_tea_handlers.py  # Welcome Tea automation — QR/join-request capture, Confirm/Reject buttons, scheduled details/reminder/approval jobs
├── services/
│   ├── google_sheets.py         # All Google Sheets read/write ops; cached gspread client + spreadsheet handles, plus a Drive-modifiedTime smart data cache (get_cached_records/values, invalidate_sheet_cache)
│   └── date_parser.py           # Flexible date/time string parser → normalised format
└── utils/
    ├── constants.py             # Conversation states, in-memory sets and dicts
    ├── decorators.py            # is_admin() group-admin check, check_is_authenticated_admin(), @admin_only (DM-only gate)
    └── helpers.py               # Date helpers, delete_topic_with_delay()
```

---

## Key Config Values (`config.py`)

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token (loaded from env or hardcoded for debug) |
| `GOOGLE_CREDENTIALS_JSON` | Service account credentials dict for Google Sheets |
| `CHAT_ID` | The Telegram supergroup ID the bot manages |
| `SHEET_NAME` | Main Google Sheet name (e.g. `"NTUCD AY25/26 Timeline"`) |
| `SHEET_TAB_NAME` | Performance list tab (`"PERF"`) |
| `ATTENDANCE_TAB` | Regular-training attendance tab (`"Attendance"`) |
| `ATT_NAME_COL` / `ATT_TOTAL_COL` / `ATT_LABEL_COL` / `ATT_FIRST_DATE_COL` | Attendance column layout (A / B / C / D). `ATT_TOTAL_COL` (B) is the sheet-maintained `Tabulation` count — read-only for the bot |
| `ATT_POLL_ROW` / `ATT_DATE_ROW` / `ATT_FIRST_MEMBER_ROW` | Attendance row layout (1 / 2 / 3) |
| `WELCOME_TEA_SHEET` / `WELCOME_TEA_TAB` | Legacy Welcome Tea registration form responses sheet (used by the old `/verify` matric path) |
| `WELCOME_TEA_ID_TAB` | `WELCOME TEA ID` tab — QR/join-request registrations + Confirm/Reject status (headerless: A=Tele ID, B=username, C=timestamp, D=status) |
| `WELCOME_TEA_STATUS_NOT_CONFIRM` / `_ATTEND` / `_REJECT` | Canonical status strings (`"Not Confirm"` / `"Attend"` / `"Reject"`) written to col D |
| `WELCOME_TEA_EVENT_DATE` | Welcome Tea event date; the 3 automation jobs are scheduled relative to it |
| `WELCOME_TEA_DETAILS_DAYS_BEFORE` / `_TIME` (4 / 22:15) | Details-DM job offset + time (Fri before a Tue event) |
| `WELCOME_TEA_REMINDER_DAYS_BEFORE` / `_TIME` (3 / 22:16) | Reminder-DM job offset + time (Sat) |
| `WELCOME_TEA_APPROVAL_DAYS_BEFORE` / `_TIME` (2 / 22:17) | Join-request approval job offset + time (Sun) |
| `WELCOME_TEA_GROUP_CHAT_ID` / `WELCOME_TEA_JOIN_REQUEST_LINK` | Welcome Tea group + invite link used to detect tea join requests |
| `TOPIC_VOTING_ID` | Thread ID where attendance polls are posted |
| `EXEMPTED_THREAD_IDS` | List of thread IDs exempt from access control (General + Voting) |
| `MAIN_ADMIN_ROLE_KEYWORDS` / `SECONDARY_ADMIN_ROLE_KEYWORDS` | Role/Position keywords (contains-match) defining the two admin tiers |
| `ADMIN_DM_USER_IDS` | Emergency-fallback Telegram IDs for dashboard/alerts (used only when the role lookup yields nothing) |
| `JOIN_CONTACT_ADMIN_ID` | Last-resort "contact our admin" Tele ID for the join flow |
| `WELCOME_TEA_LINK_KEYWORD` | Invite-link name keyword that routes a join request to the Welcome Tea flow |
| `sg_tz` | Singapore timezone object |

---

## Google Sheets Schema

### `PERF` tab
Columns: `THREAD ID | EVENT TYPE | EVENT NAME | REHEARSAL DATE | TIME | PERF DATE | TIME | LOCATION | OTHER INFO | REMUNATION | STATUS`

- Each row = one performance forum topic
- `EVENT TYPE`: `EXT` (External) or `INT` (Internal) — stored in sheet but **not shown** in the pinned summary
- `REHEARSAL DATE | TIME`: formatted multi-line date string (one date per line), or `-` if no rehearsal
- `STATUS`: blank (pending) → `ACCEPTED` or `REJECTED`

### `Attendance` tab (regular trainings only)
Grid layout (no Telegram IDs stored here — names are resolved from the `MEMBER INFO AY26/27` tab):

| | A | B | C | D, E, F… |
|---|---|---|---|---|
| **Row 1** | | | `POLL ID` | poll id per date |
| **Row 2** | `NAME` | `Tabulation` | `TRAINING DATE [REG]` | one training date per column |
| **Rows 3+** | nickname | `=SUM(D3:3)` | | `1` / blank per member |

- **Col A** = member nickname (matches `Nickname` in the `MEMBER INFO AY26/27` tab)
- **Col B** = `Tabulation` — the per-member count. **The bot never writes this column.** It is a sheet-side formula maintained by the admin (e.g. `=SUM(D3:3)` or `=COUNTIF(D3:3, 1)`, dragged down per row). The bot only *reads* it to sort members "most frequent first" in the modify UI.
- **Col C** = label column only (`POLL ID` / `TRAINING DATE [REG]`)
- **Cols D onward** = one training date each. Admin pre-fills row 2 with all term dates (excluding public holidays). Bot writes the poll id into row 1 and `1` into a member's cell when they vote Yes. New date columns are placed in the **first empty slot from col D** (the bot scans from D and reuses a column whose date matches, else fills the first gap — it never appends past unrelated columns farther right, so a `Tabulation`/analysis column can safely live to the right of the date region).
- Training dates accept flexible formats: `12/8`, `12/8/26`, `12 Aug`, `12 Aug 2025` (`parse_sheet_date`).

### `PERF TABULATION` tab (performance-event attendance)
Same grid shape as the `Attendance` tab, but each event column is keyed by the
performance topic's **THREAD ID** (not a poll id), and there are no dates/polls:

| | A | B | C | D, E, F… |
|---|---|---|---|---|
| **Row 1** | | | label | `THREAD ID` per event |
| **Row 2** | `NAME` | `Tabulation` | label | `EVENT NAME` per event |
| **Rows 3+** | nickname | `=SUM(...)` | | `1` / blank per member |

- **Col A** = member nickname (admin **pre-fills the roster** — there's no poll to auto-add voters).
- **Col B** = `Tabulation` (sheet formula, bot never writes; read to sort members most-frequent-first).
- **Cols D onward** = one performance event each; row 1 = thread id, row 2 = event name.
- The bot **finds-or-creates** an event column by thread id when the admin marks that event (`get_perf_event_column(..., create=True)`); members are read from col A (`get_perf_attendees`), and CONFIRM batch-writes the column (`commit_perf_column`). No modify-date, no auto-poll. Config: `PERF_TAB_NAME` + `PERF_*` layout constants.

### `OTHERS` tab
Columns: `THREAD ID | EVENT NAME`
- Each row = one OTHERS (bonding/misc) topic
- Both thread ID and topic title are written when the topic is created

### `STANDARD TOPIC Rules` tab
Columns: `THREAD ID | NAME | RULE PROFILE`
- `RULE PROFILE`: comma-separated permission tags (e.g. `TEXT,MEDIA,POLLS`)

### `PERFORMER Info` sheet (retired)
The legacy member-timeline spreadsheet is no longer used. The legacy-named
functions (`copy_user_to_timeline`, `user_already_in_timeline`,
`mark_user_left_in_sheet`) now delegate to the `MEMBER INFO AY26/27` tab
(config `MEMBER_INFO_TAB`), matched by `Tele ID`.

### `MEMBER INFO AY26/27` tab
The active member roster used for the attendance Tele ID → nickname lookup.
Columns include `Full Name (as per Matric Card)` (A), `Nickname` (B) … `Tele ID` (P).
`get_nickname_by_user_id` matches the `Tele ID` column against the poll voter's
Telegram id and returns their `Nickname` (header lookup is case-insensitive and
index-based, so column order can change without breaking it).

### `NTUCD Welcome Tea Registration` sheet (legacy)
- `Matriculation Number`, `Attendance` (1 = attended), `User ID`
- Used only by the **legacy `/verify` matric path** (`matric_valid` /
  `update_user_id_in_sheet`), which is no longer the active Welcome Tea flow.

### `WELCOME TEA ID` tab (active Welcome Tea flow)
**Headerless** grid (`WELCOME_TEA_ID_TAB`):
| A | B | C | D |
|---|---|---|---|
| Tele ID | username | timestamp (`YYYY-MM-DD HH:MM:SS`) | status |

- `status` ∈ `Not Confirm` / `Attend` / `Reject` (blank → treated as `Not Confirm`).
- `append_welcome_tea_id` upserts a row (status reset to `Not Confirm`) when a user
  scans the QR / sends the tea join request; `update_welcome_tea_status` writes
  col D on a Confirm/Reject tap; `get_welcome_tea_recipients(statuses)` filters
  rows by status for the scheduled jobs (non-numeric Tele IDs skipped).

---

## Feature Flows

### New Member Onboarding
Two flows, routed in `join_request_handler` by **which join request** it is (both
invite links must have "Request Admin Approval" enabled):
- a request to the **Welcome Tea group** — detected by `chat.id ==
  WELCOME_TEA_GROUP_CHAT_ID`, **or** the link url == `WELCOME_TEA_JOIN_REQUEST_LINK`,
  **or** the link **name contains "tea"** (`WELCOME_TEA_LINK_KEYWORD`) → the
  **Welcome Tea automation flow** (step 3 below).
- **any other link** (the unique returning-member link; unnamed links default
  here) → the **Tele-ID gate** (step 2).

1. User sends join request → `join_request_handler` fires and routes
2. **Tele-ID gate** (member link): the requester's Telegram id is checked against the `Tele ID`
   column of `MEMBER INFO AY25/26` (`is_member_in_ay2526`, smart-cached):
   - **found** → the request is **auto-approved** with a welcome-back DM — sent
     **before** `approve()`, because the join-request DM window (which lets the
     bot message a user who never started it) closes the moment the request is
     processed; a DM failure never blocks the approval. Right after approving,
     the bot **stamps `MEMBER INFO AY26/27` immediately** (`Status = Join`,
     `Join Date` = today, `Leave Date` cleared); the subsequent join event
     re-stamps the same values via `handle_member_status` (safe double-write).
   - **not found** → the request is left **pending** (Telegram's "waiting
     room"; stored in `pending_users`), the user is DM'd to contact the admin
     (role-driven contact, `JOIN_CONTACT_ADMIN_ID` in config as last resort;
     clickable `tg://user?id=…` link),
     and the **admin is proactively alerted** with the requester's name/handle/
     Tele ID. The user-DM works even if they never started the bot — a join
     request opens a ~5-minute window in which the bot may message the
     requester (the bot sends immediately). Requires the invite link to have
     **"Request Admin Approval"** enabled.
3. **Welcome Tea automation flow** (`welcome_tea_handlers.py`): on the tea join
   request, `handle_welcome_tea_join_request` stashes the request in
   `welcome_tea_pending_requests` / `welcome_tea_join_chats`, registers the user
   in `WELCOME TEA ID` (`append_welcome_tea_id`, status `Not Confirm`), and DMs a
   "thanks for registering" message. The request stays **pending** (waiting room).
   Three **one-off jobs** (`schedule_welcome_tea_jobs`, computed from
   `WELCOME_TEA_EVENT_DATE` minus the config day-offsets, registered on startup;
   past times are skipped) then drive it:
   - **Details job** (D-4, 22:15): DMs the event details + **Confirm / Reject**
     buttons (`WELCOME_TEA_CONFIRM` / `WELCOME_TEA_REJECT`) to every `Not Confirm`
     user.
   - **Reminder job** (D-3, 22:16): re-DMs the still-`Not Confirm` users.
   - **Approval job** (D-2, 22:17): **approves** the pending join requests of
     `Attend` + `Not Confirm` users, **declines** `Reject` users.
   Tapping **Confirm** → status `Attend`; **Reject** → status `Reject` **and** the
   join request is declined immediately (`handle_welcome_tea_confirmation`).
   (Legacy: the old `/verify` matric conversation — `start_verification` /
   `handle_matric` against the Welcome Tea responses sheet — is still registered
   but is no longer part of this flow. `handle_welcome_tea_qr` for `/start
   welcome_tea` deep-links exists but is not currently wired to a handler.)
4. **MEMBER INFO AY26/27 join/leave tracking** (`update_member_join_in_info` / `update_member_leave_in_info`, matched by `Tele ID`): on **join or rejoin** → `Join Date` = now (`DD Mon YYYY HH:MM`, SGT), **`Leave Date` cleared**, `Status` = `Join` (a rejoin reuses the member's old row, so the stale leave date is wiped and the join date refreshed; the roster treats both `Join` and `Active` as active members); if no row matches, a minimal row (Full Name + Tele ID + Join Date + Active) is written into the first empty row inside the formula region so the C:K lookups auto-fill. On **leave/kick** → `Leave Date` = today, `Status` = `Left` (Join Date kept as history). Both invalidate the sheet cache so the roster updates immediately.

### Performance Topic Lifecycle
1. **Create**: Admin sends `/new` in DM → picks type. **The DM wizard offers only
   PERF and OTHERS** (no STANDARD path in `private_handlers`).
   - **PERF** — 6-step conversational flow (`handle_confirm_new_perf` callbacks):
     1. Select **EXT** or **INT** event type (inline buttons, `PERF_EVENT_TYPE|`)
     2. Type **Event Name** (free text)
     3. Type **Rehearsal Date & Time** (comma format `31 aug, 9pm`), or *Skip*
     4. Type **Performance Date & Time** (required)
     5. Type **Location** (required)
     6. Type **Other Info**, or *Skip*
     → Summary preview with **CONFIRM** (`CONFIRM_NEW_PERF`) / **CANCEL** /
       reset (`PERF_RESET|`) buttons
     → On confirm: forum topic `PERF - {event_name}` is created, a row is
       appended to the PERF tab, pinned summary posted
   - **OTHERS**: admin enters topic title → confirm (`CONFIRM_NEW_OTHERS`) →
     creates topic, logs thread ID + event name to `OTHERS` tab
   - **STANDARD**: handled by the in-group conversation/callback path
     (`TYPE_SELECTED|`, `TOGGLE_RULE|`, `CONFIRM_STANDARD`), **not** the DM `/new`
     wizard. Saves rules to `STANDARD TOPIC Rules`.
2. **Confirm**: Admin runs `/confirmation` in the topic thread → selects ACCEPT/REJECT
   - ACCEPT: `STATUS` set to `ACCEPTED`, pinned summary refreshed
   - REJECT: `STATUS` set to `REJECTED` (topic is **not** deleted)
3. **Remind**: two paths —
   - **Automatic**: `daily_reminder_cron_job` (09:00 SGT) finds performances
     exactly 7 days away; ACCEPTED → broadcasts the preparation checklist
     (glasses/contacts, shave, costume, barefoot, hair, recap drum score) to the
     topic; blank STATUS → DMs admins a nudge.
   - **Manual**: `/remind` (or `remind`) in DM opens a portal
     (`initiate_remind_portal_via_dm`) with topic-picker buttons
     (`MANUAL_REMIND_TID|`); validates `STATUS == ACCEPTED` before broadcasting.
     `/remind` typed in a group is passive (no-op).
4. **Modify**: `modify <id>` (or `/modify`) in **DM only** → inline field menu →
   updates sheet + refreshes pinned summary, looping back to the menu for
   multi-edit. Group use of `/modify` is fully passive (left as a normal
   message, no deletion, no reply) — same as any `/something` typed in a topic.

> **Known divergence**: code references a `SUMMARY MSG ID` column
> (`publish_performance_summary` does `SHEET_COLUMNS.index("SUMMARY MSG ID")`) but
> that column is **not** in `config.SHEET_COLUMNS` (9 cols). The lookup raises
> `ValueError`, caught by try/except, so the summary message ID is silently not
> persisted. Add the column to `SHEET_COLUMNS` (and the sheet) if message-ID
> editing is needed.

### Attendance (`attd` / Cockpit → Attendance Portal)
Opens a **top-level category menu** (`_build_home_keyboard`): **🏋️ For Regular
Training** (`ATTD_CAT|REGULAR`) vs **🎭 For Performance** (`ATTD_CAT|PERF`), plus
**🦅 Exit to Cockpit** (`DASH_VIEW|HOME`). The active category is tracked in
`user_data["attd_mode"]` ("REG"/"PERF"). The whole flow runs as a **single edited
"bubble"** (`user_data["attd_bubble_id"]` via `_update_attendance_bubble`, which
edits `query.message` in place and **never spawns a new message** — a "message is
not modified" no-op is swallowed, so back buttons stay in the one bubble). Both
categories share the member-toggle UI and CONFIRM batch-write; only the target
tab differs. All `ATTD_*` callbacks route to `attendance_callback`
(`main.py` pattern `^ATTD_`).
- **🎭 Performance** branch → the `PERF TABULATION` tab, columns keyed by **thread
  id**. The event picker is sourced from the `PERF` tab (`get_perf_event_list`,
  newest first, present count in brackets) → pick an event (`ATTD_PEVT|<tid>`) →
  the bot find-or-creates that thread id's column (`get_perf_event_column`), shows
  the roster from col A (`get_perf_attendees`) pre-ticked, toggle (`ATTD_TOGGLE|`)
  → **CONFIRM** (`ATTD_CONFIRM`) writes via `commit_perf_column`. **No dates, no
  polls, no modify-date** here. Back from the event **list** → category menu
  (`ATTD_HOME`); back from the member-**toggle** → event list (`ATTD_BACK_PERF`).
- **📋 Regular Training** branch (`ATTD_DATE|<col>`; toggle back = `ATTD_BACK_REG`
  → date list; date-list back = `ATTD_HOME` → category) → everything below (dates,
  polls, modify-date). The ✏️ Modify-a-Date sub-flow (`ATTD_MODMENU` /
  `ATTD_MODDATE|`) and the typed-date capture (`handle_moddate_text`, which
  deletes the user's message and edits the bubble) live here.
- **Banners, never dead-ends**: every terminal outcome re-renders the relevant
  list with a banner prepended — `ATTD_CONFIRM` (attendance saved 🟢),
  `ATTD_MODDATE_CONFIRM` (date changed 🟢, or ⚠️ if the date turned out past),
  and **Modify-a-Date with no upcoming dates** (⚠️ shown on the REG date list).
  **❌/Exit** returns to the Cockpit (`DASHBOARD_TEXT`).

### Training Attendance Poll (regular trainings)
- **Auto-poll**: a daily `JobQueue` job (`auto_poll_check`, runs 09:00 SGT) scans row 2 of the `Attendance` tab. For any training date **4 days away or sooner (down to today), but not in the past**, with no poll id yet, it posts a non-anonymous poll (options `✅ Count me in!` / `❌ Can't make it`; **option index 0 must stay the "yes" option** — `handle_poll_answer` reads `0 in selected_options`) to the Voting topic and writes the poll id into row 1. (Tuesday training → poll normally fires the previous Friday 9 AM.) The "≤ 4 days" window (rather than "exactly 4 days") means a date moved *inside* the window by a manual sheet edit — e.g. an admin edits a date from 7 days to 1 day away — is still caught on the next daily run instead of being skipped forever. A reminder is scheduled for 10 PM the day before training. The admin pre-fills all training dates in row 2; the bot only ever fills in the poll id (row 1). There is **no manual `/poll` command** — poll creation is fully automatic (or triggered by the date-modify sub-flow below).
- **Yes-vote capture** (`handle_poll_answer`): on a training poll, the voter's `user_id` is mapped to a nickname via the `MEMBER INFO AY26/27` tab (`Tele ID` → `Nickname`, falling back to full name); the date column is found by matching the poll id in row 1; `1` is written to that member's cell (a new row is created if the nickname isn't listed yet). Changing the vote to No / retracting clears the cell. The `Tabulation` column (B) updates itself via its sheet formula — the bot does not touch it. Poll-type detection falls back to the sheet (`is_training_poll_id`) so votes are still captured after a bot restart.
- **Modify attendance** (Admin DM, `attd` command → `attendance_handlers.py`): lists training dates **that have a poll id**, **latest date first**, each button showing the **present count in brackets** e.g. `15/6 (3)` (`get_training_date_columns` returns `(col, date_str, present_count)` only for columns with both a date in row 2 and a poll id in row 1 — so un-polled prefilled dates and any analysis column such as `Tabulation` are excluded — sorted by parsed date descending) → admin picks one → bot shows every attendee sorted by the `Tabulation` count (most frequent first), pre-ticked ✅ if already marked → admin toggles names (cached in `user_data`, no live writes) → **CONFIRM** batch-writes the whole date column in one `batch_update`. The `Tabulation` column recalculates itself from its sheet formula.
- **Modify a training date** (Admin DM, `attd` → **✏️ Modify a Date**): the date picker has a "Modify a Date" button → it lists only **upcoming** polled dates (today or later — `_future_dates` filters out past trainings, which can't be re-dated; the mark-attendance picker still shows past dates) → pick the date to change → type the new date in **standardised format** (`12 June`, `12 Jun`, `12 June 2026`; parsed by `parse_date_line` from `date_parser.py`, which **rejects slash formats like `12/6`** and stores the canonical `DD Mon YYYY` — e.g. `12 Jun 2026` — into the sheet, still readable by `parse_sheet_date`; a **past typed date is rejected the moment it's entered** — a year-less date is judged in the *current* year, so `2 June` typed on 5 June is flagged as past rather than rolled to next year; type the year explicitly to schedule across years. The past check is also re-enforced at CONFIRM) → confirm. On confirm `replace_training_date_column` writes the new date (row 2) **into the same column, replacing the old value, and clears that column's existing ticks** (the old marks belonged to the old date). The poll follows the **same window as `auto_poll_check`**: the bot posts a fresh poll to the Voting topic and writes its id (row 1) **if the new date is 4 days away or sooner (down to today)**; for a further-out date the poll id is left blank so the daily `auto_poll_check` posts it when the date is due. The typed date is consumed by `handle_moddate_text`, hooked into the DM dispatcher (`handle_private_command`).

### Group Access Control (`message_handlers.py`)
The current `handle_message` is intentionally **minimal and passive**:
- **Total passivity for text**: any message starting with `/` or matching a known
  command word (`testremind`, `remind`, `modify`, `new`, `list`, `announce`,
  `threadid`) returns immediately with **no deletion** — group chatter and stray
  admin commands are never touched.
- **Manual topic creation blocked**: when a `forum_topic_created` event arrives,
  the topic is deleted and its creator DMed to use `/new`, **unless** the thread
  ID is already in `OTHERS_THREAD_IDS` or `initialized_topics` (i.e. created via
  the bot).
- Per-message permission enforcement for STANDARD topics is **not** active in
  this handler version — `handle_message` does not gate individual messages
  against any rule cache (the old `TOPIC_RULES_CACHE`/`load_topic_rules` have been
  removed as dead code).

---

## In-Memory State (`utils/constants.py`)

| Name | Type | Purpose |
|---|---|---|
| `initialized_topics` | `set` | Thread IDs of topics created via bot (prevents auto-delete on creation) |
| `OTHERS_THREAD_IDS` | `set` | Thread IDs of OTHERS topics (loaded from sheet on startup) |
| `pending_questions` | `dict` | Tracks bot prompt message IDs for cleanup |
| `active_polls` | `dict` | `{poll_id: "training" | "interest"}` |
| `yes_voters` | `set` | User IDs who voted Yes in current training poll |
| `interest_votes` | `dict` | `{poll_id: {user_id: [option_ids]}}` |
| `pending_users` | `dict` | `{user_id: ChatJoinRequest}` for the legacy `/verify` flow |
| `welcome_tea_pending_requests` | `dict` | `{user_id: ChatJoinRequest}` awaiting the Sunday Welcome Tea approval/decline job |
| `welcome_tea_join_chats` | `dict` | `{user_id: chat_id}` so a Welcome Tea request can be approved/declined even without the cached request object |

---

## Bot Commands (registered in `main.py`)

Most admin actions now live in the **Admin DM Dashboard** (private chat). In
group topics the bot is deliberately **passive** — `/modify`, `/remind`, etc.
either no-op or only print a thread ID. The per-admin DM command menu
(`register_private_admin_menus`) is set with `BotCommandScopeChat`; the public
default menu is cleared (`clear_all_public_menus`). The menu now lists **only
`start`** — every other action lives inside the unified `/start` Cockpit
dashboard, so the other `BotCommand` entries are commented out in `admin_commands`
(re-enable a line there to resurface a command in the Telegram menu).

| Command | Handler | Behaviour |
|---|---|---|
| `/start` | `admin_handlers.start` | Admin DM dashboard / bot test |
| `/threadid` | `admin_handlers.thread_id_command` | Prints the current topic's thread ID |
| `/confirmation` | `conversation_handlers.confirmation` | Accept/reject a performance in its topic |
| `/modify` | `modify_handlers.start_modify` | **DM-only** — edit performance fields (group use is fully passive, message left untouched) |
| `/remind` | `admin_handlers.remind_command` | Group: passive. DM: opens the remind portal |
| `/testremind` | `admin_handlers.manual_test_reminder_trigger` | Manually fires the reminder scan cycle |
| `/verify` | `verification_handlers.start_verification` | (DM) new-member matric verification conversation |

### Scheduled jobs (`JobQueue`, registered at startup)
- **`daily_reminder_cron_job`** — 09:00 SGT daily. Scans PERF for performances
  exactly **7 days** away: ACCEPTED → broadcasts the preparation checklist to the
  topic; blank STATUS → DMs admins a nudge to confirm.
- **`auto_poll_check`** — 09:00 SGT daily. Posts a training attendance poll for
  any date 4 days away or sooner that has no poll id yet (see Attendance flow).
- **Welcome Tea jobs** (`schedule_welcome_tea_jobs`, `welcome_tea_handlers.py`) —
  **one-off** `run_once` jobs computed from `WELCOME_TEA_EVENT_DATE` minus the
  config day-offsets (D-4 details / D-3 reminder / D-2 approval; any time already
  in the past is skipped on registration). See the Welcome Tea automation flow.

> **Scheduling note (testing):** the daily-reminder line in `on_startup` has a
> commented `run_once(..., when=10)` alongside `run_daily`; `auto_poll_check`
> similarly has a commented `run_repeating` test line. Use `/testremind` (DM) to
> fire the 7-day scan on demand without touching the schedule.

## Admin DM Dashboard (Private Chat)

### Two admin tiers (role-driven from `MEMBER INFO AY26/27`)
Admin access is resolved from the **`Role/Position` + `Tele ID`** columns of the
MEMBER INFO tab (`get_admin_role_ids` in `google_sheets.py`, keyword
contains-match, case-insensitive, smart-cached):
- **MAIN admins** — roles matching `chairperson` (covers Vice Chairperson),
  `secretary`, `sde` → full dashboard access **and** receive every admin
  alert/reminder DM (`get_alert_admin_ids`): the daily 7-day pending-status
  nudges and the join-request waiting-room alerts.
- **SECONDARY admins** — roles matching `treasurer`, `logistic`, `business`,
  `pnp` → dashboard access only (`get_dashboard_admin_ids`); they get **no**
  alert/reminder DMs (group-wide broadcasts reach them like any member).
- `ADMIN_DM_USER_IDS` in `config.py` is an **emergency fallback only**: it
  grants dashboard access / receives alerts **only when the role lookup yields
  nothing at all** (sheet unreachable or Role column wiped). While any role is
  resolvable, config-listed ids without an admin role are **blocked** like any
  member.
- The join-flow **"contact our admin" link** is also role-driven
  (`get_join_contact_admin_id`: first main admin in priority order chairperson →
  secretary → sde); `JOIN_CONTACT_ADMIN_ID` in `config.py` is the
  last-resort fallback only.
All gates route through `is_dashboard_admin()` (used by `/start`, the DM
dispatcher, attendance callbacks, refresh, `@admin_only` via
`check_is_authenticated_admin`); the startup per-admin menu bind
(`register_private_admin_menus`) covers both tiers. Unauthorized DMs get total
silence. Commands **require a leading slash** (`/modify`, `/list`, `/attd`, …);
`_parse_command_payload` only recognises a command when the text starts with
`/`. Plain text without a slash is treated as flow input (event names, dates,
announcement text) or ignored — typing `modify` or `Modify start` will **not**
trigger the command:

| Command | Action |
|---|---|
| `/new` | Step-through wizard to create a **PERF** or **OTHERS** topic (no STANDARD in the DM wizard) |
| `/list` | List all thread IDs and performance statuses; rows are tappable to jump into `/modify` |
| `/modify <id>` | Edit a performance entry by thread ID — inline field menu, **multi-edit loop** (returns to the menu after each edit) |
| `/attd` / `/attendance` | Mark attendance — top menu picks **Regular Training** (pick date → tick → confirm) or **Performance** (pick event by thread id → tick → confirm, writes to `PERF TABULATION`) |
| `/announce <id> <msg>` | Send a message to a specific topic thread (with target-picker buttons) |
| `/info <id>` | Preview the formatted performance summary card in DM |
| `/remind` / `/testremind` | Open the remind portal / fire the reminder scan manually |
| `/threadid` / `/threads` / `/thread` | Print the group topic directory |

### The Cockpit (`/start` button dashboard)
`/start` opens the **"NTUFD Command Centre"** (`DASHBOARD_TEXT` +
`_dashboard_keyboard` in `private_handlers.py`) — an 8-button inline grid plus a
**♻️ Refresh Data** button. Every button emits a `DASH_VIEW|<target>`
callback routed by **`handle_dashboard_navigation`** (registered in `main.py`
with pattern `^DASH_VIEW\|`); `DASH_REFRESH` is handled by
`handle_dashboard_refresh`. Most flows now run as a **single edited "bubble"**
rather than new messages, and carry a **🦅 Exit to Cockpit** button
(`DASH_VIEW|HOME`) to return to the dashboard. Many actions edit in place and
re-render with a green **success banner** prepended (e.g. after a modify, an
announce, or saving attendance).

| Button | `DASH_VIEW\|…` | Action |
|---|---|---|
| 🎪 New Topic | `LAUNCH_NEW` | PERF/OTHERS type picker (`TYPE_SELECTED\|`) |
| 🛠️ Edit Performance | `LAUNCH_MODIFY` | `render_modify_list` (tappable rows → field menu) |
| ✅ Take Attendance | `LAUNCH_ATTD` | attendance category menu (`_build_home_keyboard`) |
| 📊 Status Ledger | `LEDGER` | read-only status list (✅/⏳/❌ per event, IDs stripped) |
| 📣 Broadcast | `LAUNCH_ANNOUNCE` | `initiate_announce_portal_via_dm` (target picker) |
| ⏰ Reminders | `LAUNCH_REMIND` | `initiate_remind_portal_via_dm` (topic picker) |
| 🧵 Thread Index | `THREADS` | thread-id directory (PERF + OTHERS) |
| 👥 Member Roster | `MEMBERS` | group-button menu of active members from `MEMBER INFO AY26/27` (`get_active_members` → `(sort_key, token, label, name)` via `_classify_member_group`: `Year == "-"` → **🎩 Graduates** (top); a number anywhere in the cell → 🎓 **Year N** (e.g. `postgraduate/1` → Year 1); other text → its **own named group** (e.g. `exchange` → 🌏 **Exchange**); blank → 🎓 **Year —**. Buttons in seniority order with counts; tap → `MEMBERS_<token>` drill-down, back → group menu). `Status` ∈ Active/Join; nickname with full-name fallback |

**Only the latest panel is live**, enforced two ways:
1. **Neutralise-on-open (primary):** opening a fresh panel **edits the previous
   one to a "🛑 This dashboard has been closed" message with its keyboard
   stripped**, so whatever sub-section it was showing can no longer be operated.
   `/start` (`admin_handlers.start`) and `/attd` (`start_attendance_modify`) both
   do this using the prior `master_dash_id`, then stamp the new bubble as the new
   `master_dash_id`. Because everything renders as one in-place bubble, the old
   master *is* the sub-section, so stripping its buttons kills it outright — no
   reliance on every handler remembering to guard.
2. **Click-time guard (backstop):** callback handlers also reject clicks on any
   bubble whose id ≠ `master_dash_id` with an "expired panel" notice —
   `handle_dashboard_navigation` (`DASH_VIEW|`), `attendance_callback` (`ATTD_*`),
   modify handlers (`MODIFY|`) — covering any stray older bubble.

> **Note (config):** `BOT_TOKEN` / `GOOGLE_CREDENTIALS_JSON` are currently
> commented out in `config.py` (load-from-env lines). The bot won't import/run
> until they're provided (uncomment + set Heroku config vars).

### Data freshness (smart cache + 🔄 Refresh)
List/menu reads (`/list`, `/modify`, `/info`, announce/remind portals) go through
**`get_cached_records()` / `get_cached_values()`** in `google_sheets.py`. Before
serving, these do a tiny Drive **`modifiedTime`** check (`get_lastUpdateTime()` —
just the timestamp, not the cells): unchanged → serve the in-memory snapshot;
changed → re-download and re-stamp. Keyed by `(sheet_name, tab_name)` at module
level (shared across admins).
- **Bot writes self-heal instantly**: every PERF-sheet write (`apply_modify_value`,
  EVENT TYPE / STATUS selects, `/new` append, `/confirmation` ACCEPT/REJECT,
  legacy in-group append) calls `invalidate_sheet_cache()` right after, so the
  next render re-pulls fresh without waiting for Drive's (laggy) `modifiedTime`.
- **Manual web-UI edits**: caught on the next list open once Drive's `modifiedTime`
  catches up (a few seconds), or immediately via the **♻️ Refresh Data**
  button (`DASH_REFRESH` → `handle_dashboard_refresh` → `invalidate_sheet_cache()`).
- **Back navigation**: multi-layer inline flows carry Back/Home buttons; the
  `/modify` field menu's **🔙 Back to List** edits the message in place
  (`render_modify_list(..., incoming_query=query)`) — one Telegram call, no
  "⏳ Fetching…" placeholder.

### `/modify` editable fields
`EVENT TYPE` (EXT/INT buttons), `EVENT NAME`, `REHEARSAL DATE | TIME`,
`PERF DATE | TIME`, `LOCATION`, `OTHER INFO`, `REMUNATION`, `STATUS`
(ACCEPTED/REJECTED/PENDING buttons). Date fields use the comma format
(`31 aug, 9pm`). Editing most fields refreshes the pinned summary;
`REMUNATION`/`STATUS` update the sheet only. A REJECTED status updates logs but
does **not** delete or close the topic.

---

## Date Format Convention

The `date_parser.py` service normalises free-text date/time input. Each line is
**one date, comma-separated from its time(s)**: `DATE , TIME [TIME …]`. Output is
`DD Mon YYYY  TIME1 / TIME2` (e.g. `31 Aug 2025  9:00 PM`).

### Input format per line
Each line = **one date**. A **comma** separates the date from one or more
space-separated times:
```
31 aug, 9pm
1 sep, 7pm 9pm
```

### Output format
```
31 Aug 2025  9:00 PM
01 Sep 2025  7:00 PM / 9:00 PM
```

### Rules
- Date part: `DD MON [YY|YYYY]` — **space-separated** day/month/year. Year is
  optional; smart roll-forward applies if omitted (see below).
- Month: 3–9 letter abbreviation or full name (case-insensitive): `aug`,
  `august` both work.
- Time: am/pm suffix or 24-hour 4-digit format: `8am`, `10:30pm`, `0000`, `1230`.
  Output is canonicalised to `H:MM AM/PM`.
- **Comma is mandatory** to separate date from time. Multiple times after the
  comma are space/`/`-separated and joined in output with ` / `. A line with no
  comma is treated as date-only (no time).
- **Smart roll-forward** (`_parse_date_only`): if the year is omitted and the
  month/day has already passed this year, the next year is assumed.
- **Year window**: parsed year must fall within `2025–2029` (`n-1 … n+3`), else
  a re-enter error is raised. 2-digit years expand as `20YY`.
- Multiple dates: enter each on a new line (Shift+Enter in Telegram);
  `parse_and_format_dates` processes the block line-by-line.

### Example prompt shown to user
```
Single date Single time: 31 aug, 9pm
Single date Multiple times: 1 sep, 7pm 9pm
Multiple dates:
31 aug, 9pm
1 sep, 7pm 9pm
```

### Sheet storage
Multi-date values are stored as a single cell with each date on a new line (`\n`-separated). The pinned summary renders each line as a bullet point.

> **Note**: the legacy in-group `parse_perf_input` flow (`conversation_handlers.py`)
> still uses a `//` field separator and is distinct from the comma-based date
> parser used by the DM `/new` wizard.

---

## Deployment

- Hosted on **Heroku** as a worker dyno (not web dyno — no HTTP server)
- `Procfile`: `worker: python main.py`
- Credentials and tokens should be set as **Heroku Config Vars** (environment variables), not committed to the repo
- Bot runs in polling mode (`app.run_polling(allowed_updates=Update.ALL_TYPES)`)

---

## Development Notes

- The bot currently has two configs in `config.py`: a **debug group** (active) and a **main group** (commented out). Switch by toggling `CHAT_ID` and the thread ID constants.
- `GOOGLE_CREDENTIALS_JSON` is imported directly as a dict in `config.py` — for production, load from `os.environ.get("GOOGLE_CREDENTIALS_JSON")` and `json.loads()`.
- The `venv/` directory is in the repo root but should be in `.gitignore`.
- All handlers use `async/await` — do not introduce synchronous blocking calls in handler code.
