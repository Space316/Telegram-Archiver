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

## ▶️ Usage

```bash
python3 telegram_archiver.py
```

The first run asks for your phone number and login code (plus your 2FA password if you have one). The session is saved under `accounts/`, so you only log in once per account.

### The flow

```
1. Choose account            →  multiple accounts supported, each isolated
2. Choose a group / channel  →  searchable arrow-key list, groups and channels separated
3. Choose topics             →  all topics, or just the ones you pick (forums only)
4. Choose start point        →  from the beginning, from a message link, or last N messages
5. Optional sender prefix    →  add the original sender's name to each message
6. Sit back                  →  destination chat is created and the history is copied
```

### The main menu also offers

- 🔁 **Retry failed messages** — re-attempt only the messages that failed earlier
- 🧹 **Reset a chat's progress** — start that chat over from scratch (a fresh destination is created)
- 👤 **Archive a single author** — filter the whole run by one sender

### Resuming

Progress is written to `progress.json` (atomically, with a `.bak` copy). If a run is interrupted for any reason, just run the script again and pick the same chat — it continues from the next message and remembers the old-id → new-id mapping, so replies keep working across runs.

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
| Some messages failed | Main menu → **Retry failed messages** |

---

## ⚖️ Legal & privacy

This tool acts as **your own Telegram client** through the official MTProto API. Use it only for chats you are allowed to archive, and respect Telegram's [Terms of Service](https://telegram.org/tos) as well as the privacy of other members. Your `*.session` files grant full access to your account — treat them like passwords.

---

## 📄 License

[MIT](LICENSE)
