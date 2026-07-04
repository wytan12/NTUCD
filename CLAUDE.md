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
| Persistence | `PicklePersistence` → `bot_data.pkl` (WT job tracking, dietary-capture state, group chat data survive restarts) |
| Database | Google Sheets via `gspread` + `oauth2client` |
| Timezone | Asia/Singapore (`pytz`) |
| Scheduler | `JobQueue` (APScheduler): daily 7-day performance reminder scan (09:00 SGT), daily auto-poll (09:00 SGT), and a **30-second Welcome Tea heartbeat** (`welcome_tea_scheduler_tick`) that reads job datetimes from the sheet and fires each WT job once when due. Per-admin DM menus registered as a detached `asyncio.create_task` on startup |
| Deployment | Heroku worker dyno (`Procfile`: `worker: python main.py`) |

---

## Directory Structure

```
telegram-bot/
├── main.py                  # Entry point — handlers, global button security gate, startup data load
├── config.py                # Env-loaded token/creds, sheet names, chat/thread IDs, admin role keywords
├── requirements.txt
├── Procfile                 # Heroku worker: python main.py
├── bot_data.pkl             # PicklePersistence store (created at runtime)
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
│   ├── verification_handlers.py # join-request router; ACTIVE /verify | /verification matric conversation
│   └── welcome_tea_handlers.py  # Welcome Tea automation — sheet-driven scheduler, RSVP buttons,
│                                #   dietary capture, /attd event-day check-in, 8 scheduled jobs
├── services/
│   ├── google_sheets.py         # All Sheets ops; cached client + Drive-modifiedTime smart cache;
│   │                            #   role tiers; WT settings/rows; PERF TABULATION; member info upserts
│   └── date_parser.py           # Date/time parser → `DD Mon YYYY  H:MM AM/PM`; supports ranges
└── utils/
    ├── constants.py             # Conversation states, in-memory sets and dicts
    ├── decorators.py            # is_admin() group check, check_is_authenticated_admin(), @admin_only
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
| `SHEET_COLUMNS` | **10 columns** — includes `SUMMARY MSG ID` (the old "known divergence" is fixed; the pinned-summary message id is now persisted and used to delete/replace the old pin) |

> There is **no** `WELCOME_TEA_EVENT_DATE` / `*_DAYS_BEFORE` / `*_TIME` config any
> more — every Welcome Tea date/time lives in the WELCOME TEA sheet tab and is
> re-read every 30 s, so admins reschedule jobs by editing the sheet, no deploy
> needed. `EXEMPTED_THREAD_IDS` is also gone (per-thread access control removed).

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
| **Row 1** | | | `POLL ID` | `poll_id\|message_id` per date |
| **Row 2** | `NAME` | `Tabulation` | `TRAINING DATE [REG]` | one training date per column |
| **Rows 3+** | nickname | `=SUM(D3:3)` | | `1` / blank per member |

- **Col B** `Tabulation` is a sheet-side formula — the bot only *reads* it (to sort members most-frequent-first).
- **Row 1** now stores **`poll_id|message_id`** (`format_training_poll_ref`), so the
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
- `sync_welcome_tea_member(matric, tele_id)`: header-matched **upsert** of the
  latest Welcome Tea form response into this tab (used by `/verification`);
  dedupes rows sharing the same Tele ID / Matric No
- `update_member_join_in_info` / `update_member_leave_in_info`: join/leave stamps
  (Join → Join Date now + Leave Date cleared; Leave → Leave Date + Status Left);
  new members get a minimal row in the first empty slot inside the formula region
- Role tiers are read from `Role/Position` + `Tele ID` (see Admin tiers below)

### `MEMBER INFO AY25/26` tab
Read-only Tele-ID lookup (`is_member_in_ay2526`) for the returning-member
auto-approve gate.

### `NTUFD Welcome Tea Registration 2026 (Responses)` sheet
Google Form responses; matched by `Matric No` (latest response wins). Read by the
**active** `/verification` flow (`matric_valid` / `get_welcome_tea_registration`).

### `WELCOME TEA` tab (config `WELCOME_TEA_ID_TAB`) — settings + user grid
**Top settings block** (read every 30 s by `get_welcome_tea_settings`):
- `B1` event date · `B2` WT group invite link · `B3` main-group invite link · `B4` signup-form link
- `C1` optional final-cleanup time override (default 19:30 on event day)
- Job schedule: **row 3 = time, row 4 = date**, columns `I..O` in order:
  I=Details, J=Reminder, K=Approval, L=Pre-Cutoff, M=Cutoff, N=WTD Reminder, O=Follow-up

**User grid** (header row auto-detected within the first 6 rows via
`_welcome_tea_layout`; data typically starts row 6; falls back to fixed A–N):

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
| I–N | `SENT` markers: Details / Reminder / Approval / Pre-Cutoff / Cutoff / WTD Reminder |

`append_welcome_tea_id` upserts by Tele ID (existing status preserved);
`update_welcome_tea_status`, `update_wt_dietary`, `update_wt_attendance`,
`update_wt_msg_id`, `mark_wt_user_col_sent` write single cells;
`get_welcome_tea_recipients(statuses)` filters rows (non-numeric Tele IDs skipped).

---

## Feature Flows

### New Member Onboarding (`join_request_handler`)
Routing order for an incoming join request (all invite links need
"Request Admin Approval" enabled):
1. **Main-group post-Welcome-Tea link** (`link url == B3` of the WT sheet) →
   verification prompt: request stays pending in `pending_users`, user is DM'd to
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

**`/verification` matric flow** (ACTIVE, not legacy): asks for the matric number,
looks it up in the Welcome Tea form responses, **copies the full registration
into MEMBER INFO AY26/27** (`sync_welcome_tea_member` — header-matched upsert +
duplicate-row cleanup + Status=Join/Join Date), then approves the pending
request. Friendly retry messages on matric-not-found / sheet failure.

**Join/leave tracking** (`handle_member_status` + `handle_new_member` safety
net): join → Join Date stamped, Leave Date cleared, Status=Join; leave/kick →
Leave Date, Status=Left. **The Welcome Tea group is excluded** from tracking.
Both paths invalidate the sheet cache.

### Welcome Tea Automation (`welcome_tea_handlers.py`)
Entirely **sheet-driven**: a 30-second `run_repeating` heartbeat
(`welcome_tea_scheduler_tick`) reads the settings block and fires each job
**once per scheduled datetime** (tracked in `bot_data["wt_jobs_sent"]`, persisted
via PicklePersistence; changing a time in the sheet re-arms that job; a new
event date in B1 resets all tracking).

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
6. **WTD Reminder** — event-day reminder: confirmed (`Attend`+`STILL_COMING`) get
   dinner-included text; `Not Confirm` get eat-beforehand text (col N)
7. **Follow-up** — post the thank-you + main-group invite link into the WT group
8. **Final cleanup** (event day 19:30, or C1 override) — silently strip the
   still-coming buttons for `Not Confirm`/`STILL_COMING` users

**Join-request catch-up**: `handle_welcome_tea_join_request` registers the user
(status `Not Confirm`), DMs the "Yay!" message, then — based on where "now" falls
in the schedule (`_detect_wt_window`: before-details / W1 / W2_W3 / W4) — also
sends whatever the user missed (Details, Details+Reminder, or the Cutoff DM).
The request stays pending until the Approval job (or an event-day check-in).

**Buttons**: ✅ Confirm → status `Attend` (+ immediate approve if the approval
time already passed) + **dietary question** (reply captured by
`handle_dietary_reply`, a group -2 private-message intercept keyed on
`bot_data["wt_pending_dietary"]`, written to col E). ❌ Reject → status `Reject`
+ join request declined immediately. 🎉 I'll Be There!! → `STILL_COMING`.
😔 Can't Make It → `Reject` + decline.

**`/attd` event-day check-in** (registered at group -3, for **non-admins**;
admins are redirected to `/start`): only works on the event date; marks col F,
approves the still-pending WT join request (unless status `Reject`), strips
leftover buttons, and DMs every MAIN admin a food heads-up for walk-ins whose
meal wasn't catered.

`handle_welcome_tea_qr` (`/start welcome_tea` deep-link) still exists but is not
wired to any handler.

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
   first): field menu shows a **public summary preview + internal registry
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
The member-toggle UI is shared: **paginated 10 per page** (`ATTD_PAGE|`), a live
**"✅ Present: n / total" counter**, toggle buttons two-per-row, CONFIRM
batch-writes the whole column, and every terminal outcome re-renders the list
with a banner (never a dead-end).
- **Performance branch**: event picker from the PERF tab
  (`get_perf_event_list`, sorted latest perf date first, present count in
  brackets) → find-or-create the thread-id column in PERF TABULATION → roster
  from col A pre-ticked → CONFIRM (`commit_perf_column`). No dates/polls here.
- **Regular branch**: date picker lists only **polled** dates
  (`get_training_date_columns`, latest first, present count) → toggles → CONFIRM
  (`commit_attendance_column`). Includes the ✏️ Modify-a-Date sub-flow (below).
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
gone).

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

`bot_data` (persisted in `bot_data.pkl`): `wt_jobs_sent` (job → fired-at
datetime string), `wt_last_event_date`, `wt_pending_dietary`,
`welcome_tea_group_chat_id`, `group_chat_data`.

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
which pass through for regular users.

Other pre-gate handlers: `handle_dietary_reply` (group -2) intercepts private
text from users in dietary-capture mode and stops propagation.

### Scheduled jobs
- **`daily_reminder_cron_job`** — 09:00 SGT daily; 7-day PERF scan (checklist / admin nudge).
- **`auto_poll_check`** — 09:00 SGT daily; training-poll window check.
- **`welcome_tea_scheduler_tick`** — every 30 s; fires the 8 WT jobs from sheet-defined datetimes.

> Testing: commented `run_once`/`run_repeating` lines sit next to both daily
> registrations in `main.py`/`on_startup` for quick local testing.

---

## Admin DM Dashboard (Private Chat)

### Two admin tiers (role-driven from `MEMBER INFO AY26/27`)
Resolved from `Role/Position` + `Tele ID` (case-insensitive contains-match).
**No per-user role cache**: `_get_user_role` re-derives the role from the
smart-cached MEMBER INFO values on every call, so a role added/removed in the
Google Sheet web UI takes effect on the user's next interaction (~30 s worst
case — the Drive modifiedTime window), with no ♻️ Refresh or bot write needed:
- **MAIN** — `chairperson` (covers Vice), `secretary`, `sde` → full dashboard,
  receive all alert/reminder DMs (`get_alert_admin_ids`), may create topics
  (wizard **and** manual in-group creation), pass `is_main_admin`.
- **SECONDARY** — `treasurer`, `logistic`, `business`, `publications`, `coach` →
  dashboard access only; **topic creation is blocked** for them
  (`MAIN_ADMIN_TOPIC_ONLY_TEXT`); no alert DMs.
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
`/start` opens the **"NTUFD Command Centre"** — an 8-button grid + ♻️ Refresh
Data. Buttons emit `DASH_VIEW|<target>` → `handle_dashboard_navigation`;
`DASH_REFRESH` → `handle_dashboard_refresh`. Everything renders as a single
edited bubble with success banners and 🦅 Exit to Cockpit buttons.

| Button | `DASH_VIEW\|…` | Action |
|---|---|---|
| 🎪 New Topic | `LAUNCH_NEW` | PERF/OTHERS picker (**MAIN admins only**) |
| 🛠️ Edit Performance | `LAUNCH_MODIFY` | tappable event list → staged-edit field menu |
| ✅ Take Attendance | `LAUNCH_ATTD` | attendance category menu |
| ⏰ Reminders | `LAUNCH_REMIND` | manual remind portal (with escrow) |
| 📣 Broadcast | `LAUNCH_ANNOUNCE` | announce portal (General + PERF + OTHERS targets) |
| 📊 Performance Ledger | `LEDGER` | status list (✅/⏳/❌, latest first, `+n more` date collapsing) **with a 👤 performers line per event** from PERF TABULATION |
| 🧵 Thread Index | `THREADS` | thread-id directory (General 0, Voting 53 hardcoded, PERF, OTHERS) |
| 👥 Member Roster | `MEMBERS` | grouped member menu: 🎩 Graduates (`Year == "-"`, top) → 🌏 Exchange (98) → 🎓 Year N → other named groups → ❓ Unassigned; counts on buttons, drill-down per group |

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
`(sheet_name, tab_name)`, shared across admins.
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
- `bot_data.pkl` is created/updated at runtime by PicklePersistence (it is
  currently committed to the repo — consider gitignoring it).
- The `venv/` directory is in the repo root but should be in `.gitignore`.
- All handlers are `async` — do not introduce synchronous blocking calls in
  handler code (the one existing exception is the `threading.Timer` night-before
  reminder in `auto_poll_check`).

### ⚠️ Known code issues
- **Orphaned `@admin_only` decorator in `admin_handlers.py` (~line 268)**: the
  function it originally decorated (`manual_test_reminder_trigger`) is commented
  out, so Python attaches it to the next `def`, `initiate_remind_portal_via_dm`.
  Harmless in practice (always called from admin DM contexts) but unintended —
  safe to delete. (A second orphaned decorator that broke the 10 PM
  night-before training reminder by wrapping `send_reminder` was removed on
  4 Jul 2026.)
- A stale comment in `replace_training_date_column` (google_sheets.py) says the
  auto-poll fires at "2 days away"; the actual window everywhere is ≤ 4 days.
- `handle_welcome_tea_qr` and `mark_welcome_tea_setting_sent` are unwired/legacy.
