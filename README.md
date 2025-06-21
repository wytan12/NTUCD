# NTUCD Telegram Bot

A custom Telegram bot built for NTU Chinese Dance (NTUCD) to manage training polls, reminders, and communication within group topics. This bot supports automated workflows like attendance polling and scheduled notifications.

---

## Features

### `/start`
- Check if the bot is running.
- Works in any group or topic.
  
### `/threadid`
- Get the thread ID for a topic.
- Required for other commands like `/poll` and `/remind`.

### `/poll`
- Create a weekly **Tuesday training poll**.
- Automatically schedules a **Monday 10PM reminder** for that topic.
- Thread ID required.

### `/remind`
- Manually send performance-related reminders.
- Thread ID required (typically for the performance sub-group).

---

## Getting Started

To deploy your own version of the bot, follow the steps below.

### 1. Prerequisites

You will need:
- A [Telegram bot token](https://t.me/BotFather)
- A Google Sheets API token (for logging or poll storage, if needed)

---

### 2. Environment Setup

#### a. Clone the repository
```bash
git clone https://github.com/your-username/ntucd-bot.git
cd ntucd-bot
```
#### b. Create and activate a virtual environment

```bash
python -m venv venv
```

**Activate on macOS/Linux:**

```bash
source venv/bin/activate
```

**Activate on Windows (cmd):**

```bash
venv\Scripts\activate
```

**Activate on Windows (PowerShell):**

```powershell
.\venv\Scripts\Activate.ps1
```

> **Remember to activate your `venv` before running the bot!**

---

### c. Install required packages

```bash
pip install -r requirements.txt
```

---

### d. Create a `.env` file

Inside the root project folder, add the following:

```ini
TELEGRAM_BOT_TOKEN = your_telegram_bot_token
GOOGLE_SHEET_CREDENTIALS_JSON = path_or_raw_json_credentials
```

---

### 3. Running the Bot

```bash
python bot.py
```

Once started, your bot will respond to the following commands:

* `/start`
* `/threadid`
* `/poll`
* `/remind`

---

