<div align="center">

# 📦 Telegram Archiver

**Copy an entire Telegram group or channel — every message, from the very first to the last — into a brand new chat on your own account.**

No *"Forwarded from"* tags. Topics, albums, replies and pins preserved. Resumable. Fast.

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telethon-MTProto-2CA5E0?logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

🇮🇷 **[نسخهٔ فارسی این راهنما ← README.fa.md](README.fa.md)**

</div>

---

## 📖 What is this?

`telegram_archiver.py` is a single-file, interactive terminal tool built on [Telethon](https://docs.telethon.dev/).
It logs in with **your own Telegram account** (not a bot), lets you pick one of your groups/channels from an arrow-key menu, creates a **new chat of the same type and name**, and then copies the entire history into it — message by message, in the original order.

Typical uses: backing up a chat you might lose access to, migrating a community, or archiving a huge topic-based group one topic at a time.

---

## ✨ Features

| | |
|---|---|
| 👤 **Multi-account** | Each account gets its own session and progress files |
| 🗂 **Topics (forums)** | Recreates topics with the same titles; archive all of them or pick specific ones |
| 🖼 **Real albums** | Grouped photos/videos stay albums instead of becoming separate messages |
| ⚡ **Send by reference** | Media is re-sent without downloading whenever Telegram allows — no disk usage, much faster |
| 🎭 **Fidelity kept** | Stickers stay stickers, voice notes stay voice notes, round videos stay round, GIFs stay animated |
| 💬 **Replies preserved** | A reply in the source becomes a reply to the *copied* message in the destination |
| 📌 **Pins** | Pinned messages in the source get pinned in the destination |
| 🗳 **Everything else** | Polls, locations/venues, contacts and dice are copied too |
| 🔗 **Smart link previews** | Preview images aren't mistaken for real attachments |
| ✍️ **Formatting-safe** | Text is copied with real entities, so `*`, `_` and backticks can't mangle the copy |
| 🏷 **Sender prefix** | Optionally prefix each message with the original sender's name in **bold** — great for topic archives |
| ▶️ **Resume anywhere** | Ctrl+C, network drop or a killed Colab session? Run it again and it continues from the next message |
| ♻️ **Reuses destinations** | Detects an already-created destination chat (via a hidden marker in its bio) instead of making duplicates |
| 🔁 **Retry failed** | Failed messages are recorded and can be re-attempted later from the main menu |
| ✅ **Delivery verified** | After each send it double-checks with Telegram that the message really landed |
| 🚀 **Parallel transfers** | Multi-connection download/upload instead of the ~1 MB/s single-stream limit |
| 💾 **Disk friendly** | Only ever needs space for one file at a time; temp files are deleted right after upload |
| 📊 **Live progress** | Progress bars, speed, ETA and an overall `1,240 / 8,300 · 15% · ~47m left` line |
| 🌐 **RTL-aware UI** | Persian/Arabic chat and topic names display correctly in the terminal |

---

## 🧰 Requirements

- Python **3.9+**
- A Telegram account (phone number for login)
- `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)

---

## ⚙️ Installation

```bash
git clone https://github.com/Space316/Telegram-Archiver.git
cd Telegram-Archiver

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

<details>
<summary><b>New to Python? What those two venv lines do</b></summary>

`python3 -m venv .venv` creates an isolated copy of Python in a hidden `.venv` folder next to the project, and `source .venv/bin/activate` switches your terminal over to it. Everything you `pip install` afterwards lands inside that folder instead of your system Python, so this project can never break another one (and some Linux distros refuse a system-wide `pip install` altogether with an `externally-managed-environment` error).

When it's active your prompt starts with `(.venv)`. You need to run the `activate` line again in every new terminal. To remove everything later, just delete the `.venv` folder. It is git-ignored, so it never ends up on GitHub.

The step is optional — `pip install -r requirements.txt` on its own works too.

</details>

<details>
<summary><b>What each package is for</b></summary>

| Package | Required? | Why |
|---|---|---|
| `telethon` | ✅ required | Telegram MTProto client |
| `rich` | ✅ required | Panels, tables and progress bars |
| `questionary` | ✅ required | Arrow-key menus |
| `cryptg` | 💡 strongly recommended | Moves AES to C — without it transfers are CPU-bound around 1 MB/s |
| `arabic-reshaper`, `python-bidi` | 🌐 optional | Correct display of Persian/Arabic names in the terminal |
| `pyfiglet` | 🎨 optional | Big ASCII-art title |

</details>

---

## 🔑 Getting your API credentials

1. Go to **https://my.telegram.org** and log in with your phone number
2. Open **API development tools** and create an app (any name works)
3. Copy the `api_id` and `api_hash`

Then provide them in **any one** of these ways (first one found wins):

```bash
# 1) environment variables
export TG_API_ID=123456
export TG_API_HASH=abcdef0123456789abcdef0123456789
```

```bash
# 2) a config.py sitting next to the script (the usual way)
cp config.example.py config.py
$EDITOR config.py
```

> 3) Or just run the script — on the first run it asks for them and offers to write a private `config.py` (chmod 600) for you.

> ⚠️ **`config.py` is git-ignored on purpose. Never commit your credentials or your `*.session` files.**

---

## ▶️ Running it

```bash
python3 telegram_archiver.py
```

That's the whole command — there are no command-line flags. Everything is chosen from interactive menus, so you can just follow the questions.

### First run

You'll be asked for a **name for this account** (any label you like, e.g. `main`), your **phone number**, the **login code** Telegram sends you, and your **2FA password** if you have one. The session is stored in `accounts/<name>.session`, so from the second run on you go straight to the menu.

Before the menu appears, the script prints a settings summary and the free disk space it can see at `DOWNLOAD_DIR` — handy for confirming a mounted drive is really there.

### The main menu

```
◆ M A I N   M E N U ◆
        account main

» 📦  Pick a group/channel to copy
  👤  Copy only one person's messages from a topic
  🔁  Retry failed messages
  🧹  Reset a chat's progress
  🔄  Switch account
  ❌  Exit
```

The label after `account` is simply the name you chose for the logged-in account on the first run.

Move with the ↑ ↓ arrow keys and confirm with **Enter**. Almost every screen also has a **⬅ Back** entry that steps one question backwards instead of cancelling the whole job.

| Option | What it does | When to use it |
|---|---|---|
| 📦 **Pick a group/channel to copy** | The normal full archive. Creates the destination chat and copies the history of the chat (or of the topics you select). | Your main starting point |
| 👤 **Copy only one person's messages from a topic** | Same as above, but only messages from **one sender** are copied. The list includes every member plus a special *🎭 Anonymous admin* entry for messages posted as the group itself. | Extracting one person's posts, e.g. an announcements author |
| 🔁 **Retry failed messages** | Re-sends only the messages that failed earlier (recorded in `progress.json`). Duplicate protection is on for these. | After a run finished with some skipped/failed items |
| 🧹 **Reset a chat's progress** | Deletes the saved progress entry for a chat, so the next run starts from scratch with a **fresh destination chat**. The already-copied chat is not deleted. | A run went wrong and you want a clean start |
| 🔄 **Switch account** | Returns to the account picker; each account keeps its own session and its own `progress.json`. | Archiving with a different Telegram account |
| ❌ **Exit** | Closes the connection cleanly. | Done |

### The questions during a copy run

After picking a chat you'll be asked a short series of questions:

**1. Which chat?** — a searchable list of your groups and channels (just start typing to filter). A summary of the chosen chat is printed, including whether it is a forum, whether content protection is on, and how many pinned messages it has.

**2. Which topics?** *(forums only)* — either **all topics**, or a multi-select list where you tick the ones you want. Message counts are shown next to each topic name.

**3. How much should be copied?**

| Choice | Meaning |
|---|---|
| 📚 **Everything (full history)** | From the very first message onwards |
| 🔢 **Only the most recent messages** | Asks for a number, e.g. `200`. With multiple selected topics it means the last N **of each topic** |

**4. Where should copying start?**

| Choice | Meaning |
|---|---|
| ⏮ **From the beginning (or where it stopped last time)** | Normal behaviour, resumes automatically |
| 🔗 **From a specific message — paste its link** | In Telegram: right-click / long-press a message → *Copy Message Link*. Private-chat `t.me/c/...` links work, and a bare message id works too. The message you point at is shown as a preview so a wrong link is obvious immediately |

**5. Prefix every copied message with the sender's name?** — `Yes` puts the original sender's name in **bold** on the first line (`«Ali»`). Very useful for topic archives where every post would otherwise look like it came from you. For multi-topic runs you can decide this per topic.

Then the copying starts, with a live progress bar per file and a running counter such as `✔ 1,240 copied so far (album of 3, id=90218)`.

### Resuming after an interruption

Progress is written to `progress.json` (atomically, with a `.bak` copy). If a run is interrupted — Ctrl+C, connection drop, closed laptop — just run the script again and pick the same chat. It continues from the next message and remembers the old-id → new-id mapping, so replies keep pointing at the right messages across runs.

---

## 🖥 Running long jobs with tmux (recommended on a server)

**The problem:** if you run the script over SSH, your terminal is the parent of the process. The moment your SSH session drops — Wi-Fi hiccup, laptop sleep, closing the window — the process is killed with it. On a big group a full archive can take hours, so this happens more often than you'd think.

**The fix:** `tmux` keeps a terminal running **on the server itself**. You attach to it to watch, detach to leave it running, and reattach later from anywhere — even from a different computer. The script keeps working while you're gone.

(`nohup` and `&` are the classic alternatives, but they don't work here: this tool is interactive and needs a real terminal for its menus.)

### Install it

```bash
sudo apt install tmux        # Debian / Ubuntu
sudo dnf install tmux        # Fedora
brew install tmux            # macOS
```

### Use it

```bash
# 1) create a named session
tmux new -s archiver

# 2) inside it, start the script as usual
cd ~/Telegram-Archiver
source .venv/bin/activate
python3 telegram_archiver.py

# 3) leave it running and go away:  press  Ctrl+b  then  d

# 4) come back later (even after a reboot of your own laptop)
tmux attach -t archiver
```

### The handful of commands worth knowing

| Command / keys | What it does |
|---|---|
| `tmux new -s archiver` | Create a session named `archiver` |
| **`Ctrl+b`** then **`d`** | **Detach** — leaves everything running in the background |
| `tmux attach -t archiver` | Re-attach to that session |
| `tmux ls` | List running sessions |
| **`Ctrl+b`** then **`[`** | Scroll mode — ↑ ↓ / PageUp to read back through the log, `q` to quit scrolling |
| `tmux kill-session -t archiver` | Destroy the session (stops the script) |

> 💡 `Ctrl+b` is tmux's "prefix" key: you press and release it, *then* press the second key. It is not a combination held together.

> ⚠️ Detaching is `Ctrl+b` `d`. Pressing **`Ctrl+c`** would stop the script itself — though thanks to `progress.json` you could simply start it again and it would resume.

---

## 🎛 Tuning

All knobs live at the top of `telegram_archiver.py`, under `# ------------------ settings ------------------`:

| Setting | Default | Meaning |
|---|---|---|
| `ADAPTIVE_DELAY` | `True` | Speeds up while Telegram is happy, slows down on FloodWait |
| `MIN_MESSAGE_DELAY` / `MAX_MESSAGE_DELAY` | `0.3` / `15.0` | Bounds for the pause between messages |
| `FAST_TRANSFER` | `True` | Parallel-chunk download/upload |
| `TRANSFER_CONNECTIONS` | `8` | Parallel workers per file (4–16 is sane; too high ⇒ FloodWait) |
| `PREFER_DIRECT_MEDIA` | `True` | Re-send media by reference instead of download + upload |
| `MIN_FREE_DISK_GB` | `3` | Refuse to download when free space drops below this |
| `MAX_FILE_SIZE_GB` | `None` | Skip files larger than this (`None` = no limit) |
| `SPLIT_LONG_TEXT` | `True` | Send text over 4096 chars as follow-up replies instead of truncating |
| `VERIFY_SENT` | `"media"` | `always` / `media` / `never` — re-check the message really landed |
| `DOWNLOAD_DIR`, `ACCOUNTS_DIR` | `downloads`, `accounts` | Where temp files and sessions live |

<details>
<summary><b>💾 Low on server disk space? Mount Google Drive with rclone</b></summary>

The script deletes each temp file right after uploading it, so it only ever needs room for **one** file at a time. If even that is too much:

```bash
curl https://rclone.org/install.sh | sudo bash
rclone config                      # add a remote named e.g. "gdrive"
mkdir -p ~/gdrive
rclone mount gdrive: ~/gdrive --vfs-cache-mode writes --daemon
```

Then set inside the script:

```python
DOWNLOAD_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/downloads"
ACCOUNTS_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/accounts"
```

On startup the script prints the free space it sees at `DOWNLOAD_DIR`, so you can confirm the mount is really active.

</details>

---

## 🚧 Limitations

- Telegram doesn't support topics in **basic groups** — if the source is a basic group, a regular supergroup (no topics) is created instead.
- Creating many topics back-to-back can hit flood limits, so a short delay is added between each one.
- Chats with **"restrict saving content"** enabled can't be re-sent by reference; the script detects this up front and falls back to download + re-upload where possible.
- Upload size cap follows your account: **2 GB** normally, **4 GB** with Telegram Premium (detected automatically).
- If a destination chat is reused without a matching `progress.json`, the script can't know how far the previous run got and starts from the beginning — which may create duplicates.

---

## 🧯 Troubleshooting

| Symptom | Fix |
|---|---|
| Transfers stuck around 1 MB/s | `pip install cryptg` |
| Persian/Arabic names look reversed | `pip install arabic-reshaper python-bidi` |
| Frequent `FloodWaitError` | Lower `TRANSFER_CONNECTIONS`, raise `MIN_MESSAGE_DELAY` |
| `Please install these two libraries first` | `pip install rich questionary` |
| Out of disk space | Raise `MIN_FREE_DISK_GB`, set `MAX_FILE_SIZE_GB`, or use the rclone setup above |
| The run dies whenever SSH drops | Use the tmux setup above |
| Some messages failed | Main menu → **Retry failed messages** |
| A run copied 0 messages | Main menu → **Reset a chat's progress**, then try again |

---

## ⚖️ Legal & privacy

This tool acts as **your own Telegram client** through the official MTProto API. Use it only for chats you are allowed to archive, and respect Telegram's [Terms of Service](https://telegram.org/tos) as well as the privacy of other members. Your `*.session` files grant full access to your account — treat them like passwords.

---

## 📄 License

[MIT](LICENSE)
