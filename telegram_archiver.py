"""
Telegram Group/Channel Archiver (self-account, Telethon)
==========================================================
What it does:
1) Asks which account to use first (multiple accounts supported,
   each with its own session and progress file)
2) Lists your groups and channels separately, in a nice arrow-key
   menu in the terminal, so you can pick one
3) Creates a new group/channel of the same type, with the same name;
   if the source has topics (forum), it creates the same topics
   with the same names
4) Downloads and copies every message from the first post to the
   last (no "Forwarded from" tag), uploading each into the new chat
   and the matching topic
5) Optimized for large files (videos, etc.): high timeout, automatic
   retries, disk-space check before every download, temp file cleanup
   after upload, and a real progress bar for download/upload
6) If interrupted mid-run (Ctrl+C or a network drop), running it again
   resumes exactly from the next message (progress is saved to disk)
7) From the main menu you can also reset the progress of any chat so
   it starts over next time (a fresh destination chat will be created)
8) Pins in the source get pinned in the destination too
9) If the source has topics, you can archive all of them or pick
   specific ones
10) After creating a destination group, your account is set as an
    anonymous admin there (channels are already "anonymous" by nature)
11) If a group/channel with the exact same name already exists (e.g.
    from a previous, interrupted run - even in a different Colab
    session), it offers to reuse it instead of creating a duplicate.
    To tell it apart from some unrelated chat that just happens to
    share the same name, a small hidden marker (the source chat's id)
    is stored in the destination's bio; if exactly one candidate
    carries it, that one is reused automatically with no need to pick
    manually. Otherwise you're shown a list to choose from (or create
    new). It also reuses any topic that already has a matching title.
    Note: without a saved progress.json for that chat, it can't tell
    how far a previous run got, so it will start copying from the
    beginning again (which may create some duplicate messages if that
    chat/topic was already partly archived before).
12) If sending a message fails (e.g. a brief network hiccup), it's
    automatically retried a few times before being given up on. After
    every send, it also double-checks with Telegram that the message
    really landed in the destination (not just that send() didn't
    raise). Any message that still fails after all retries has its id
    recorded; use "Retry failed messages" from the main menu at any
    time afterward to re-attempt just those specific messages.
13) If a message in the source was a reply to another specific
    message, the copy is sent as a reply to that same message's copy
    in the destination too (not just dropped into the right topic).
    This needs an id mapping (old message id -> new message id),
    which is saved to progress.json as it goes, so it keeps working
    across resumed/interrupted runs.
14) Re-selecting a chat that's already been (fully or partially)
    archived offers to add more topics to this run - handy for going
    through a heavily-topic'd group one topic at a time (e.g. to stay
    under Google Colab's session limits). If you add topics after
    some progress was already made, it automatically does a "catch-up"
    pass first so the new topics' earlier messages aren't missed.

15) Albums (grouped photos/videos) are copied as real albums instead of
    being split into separate messages with a repeated caption.
16) Media is re-sent by reference whenever Telegram allows it: no download,
    no disk usage, much faster, and stickers stay stickers, voice notes stay
    voice notes, round videos stay round, gifs stay animated. Download +
    re-upload is only a fallback (and then the original file attributes are
    preserved).
17) Polls, locations/venues, contacts and dice are copied too (previously
    they were silently dropped).
18) Link previews are handled correctly: the picture Telegram shows for a
    plain link is part of the preview card, NOT an attachment, so it is no
    longer downloaded/re-uploaded as if the sender had attached a photo -
    the destination regenerates the preview from the link itself. A real
    photo sent together with a link is still uploaded normally.
19) Optional: prefix every copied message (or caption) with the original
    sender's name in bold - very handy for topic archives.
20) Text is copied with its real formatting entities instead of Markdown,
    so '*', '_' and backticks in the original can't mangle the copy.
21) Progress writes are batched/atomic instead of rewriting the whole JSON
    for every single message, and before retrying a message the tail of the
    destination is checked so a timed-out-but-delivered send can't create a
    duplicate.

Before running:
    pip install telethon rich questionary
    pip install pyfiglet   # optional, only used for the big ASCII-art title

You need API_ID and API_HASH from https://my.telegram.org
(your Telegram account -> API development tools -> create an app).
These aren't as sensitive as a password, but don't publish them
anywhere public.

Limitations:
- Telegram doesn't support topics for basic groups; if the source is
  a basic group, a regular supergroup (no topics) is created instead.
- Creating many topics back-to-back can hit Telegram's flood limits;
  a short delay is added between each topic.

Low on server disk space?
--------------------------
This script deletes each temp file right after uploading it, so it only
ever needs disk space for ONE file at a time (plus a small safety margin,
see MIN_FREE_DISK_GB below) - not the whole chat's total size. If even
that is too much for your server, you can mount your Google Drive as a
regular folder with rclone and point DOWNLOAD_DIR / ACCOUNTS_DIR at it:

    curl https://rclone.org/install.sh | sudo bash
    rclone config                      # add a remote named e.g. "gdrive"
    mkdir -p ~/gdrive
    rclone mount gdrive: ~/gdrive --vfs-cache-mode writes --daemon

Then set (further down in this file):
    DOWNLOAD_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/downloads"
    ACCOUNTS_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/accounts"

On startup the script prints the free space it sees at DOWNLOAD_DIR, so
you can confirm the mount is actually active before it starts running.
"""

import asyncio
import copy
import hashlib
import inspect
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.helpers import generate_random_long
from telethon.tl import functions, types
from telethon.tl.types import Channel, Chat, InputMessagesFilterPinned

try:
    import questionary
    from questionary import Style as QStyle
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import (
        Progress, TextColumn, BarColumn, DownloadColumn,
        TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn,
    )
    from rich import box
    from rich.text import Text
    from rich.align import Align
except ImportError:
    print("Please install these two libraries first:\n\n    pip install rich questionary\n")
    sys.exit(1)

try:
    import pyfiglet
    HAS_PYFIGLET = True
except ImportError:
    HAS_PYFIGLET = False

# Terminals draw characters strictly left-to-right, so Persian/Arabic (RTL) chat and
# topic names come out reversed and disjointed. These two libraries fix the DISPLAY:
#     pip install arabic-reshaper python-bidi
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_get_display
    HAS_BIDI = True
except ImportError:
    HAS_BIDI = False

# cryptg moves Telegram's AES from pure Python into C. Without it, transfers are
# often CPU-bound at roughly 1 MB/s no matter how fast the network is.
try:
    import cryptg  # noqa: F401
    HAS_CRYPTG = True
except ImportError:
    HAS_CRYPTG = False

console = Console()

# ------------------ settings ------------------
# ---- Telegram API credentials --------------------------------------------
# The credentials DO NOT live in this file. They are read in this order, and
# the first one found wins:
#   1. environment variables  TG_API_ID / TG_API_HASH
#        export TG_API_ID=123456
#        export TG_API_HASH=abcdef0123456789...
#   2. the config.py file sitting NEXT TO this script (the normal way):
#        API_ID = 123456
#        API_HASH = "abcdef..."
#   3. the two values below (kept only so the Colab notebook, which injects
#      them from its form fields, keeps working unchanged)
# On the very first run, if nothing is found, the script simply asks for them
# and offers to write config.py for you (created private, chmod 600).
API_ID = 0           # do not edit -- use config.py instead
API_HASH = ""        # do not edit -- use config.py instead
try:
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
except NameError:  # e.g. pasted into a notebook cell where __file__ doesn't exist
    CONFIG_FILE = os.path.abspath("config.py")

ACCOUNTS_DIR = "accounts"   # each account gets its own files here
# Low on server disk? Mount your Google Drive with rclone (see notes at the
# top of this file) and point these two at a folder inside the mount, e.g.:
#   DOWNLOAD_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/downloads"
#   ACCOUNTS_DIR = "/home/YOUR_USER/gdrive/telegram_archiver/accounts"

DOWNLOAD_DIR = "downloads"
DELAY_BETWEEN_MESSAGES = 1.5   # starting pause between messages (see ADAPTIVE_DELAY)
DELAY_BETWEEN_TOPICS = 2.0

# ---- adaptive speed ----
# Telegram only complains (FloodWait) when you push too hard. Instead of always
# waiting DELAY_BETWEEN_MESSAGES, the pause shrinks while everything goes well and
# grows again the moment Telegram pushes back. Set ADAPTIVE_DELAY = False to go
# back to a fixed pause.
ADAPTIVE_DELAY = True
MIN_MESSAGE_DELAY = 0.3        # never go faster than this
MAX_MESSAGE_DELAY = 15.0       # never crawl slower than this
SPEEDUP_AFTER = 12             # messages without a FloodWait before speeding up again
SPEEDUP_FACTOR = 0.8           # each speed-up multiplies the pause by this
SLOWDOWN_FACTOR = 2.0          # each FloodWait multiplies the pause by this

# ---- end-of-run behaviour ----
AUTO_RETRY_AT_END = True       # automatically re-try failed messages once when a run ends
NOTIFY_ON_FINISH = True        # send a summary to your own Saved Messages when a run ends
SHOW_OVERALL_PROGRESS = True   # "1,240 / 8,300 - 15% - ~47m left" line during a run
PROGRESS_LINE_EVERY = 20       # print that line at most every N messages (or every 30s)

MIN_FREE_DISK_GB = 3
DISK_SAFETY_MARGIN_GB = 1     # always keep this much free ON TOP of the file being downloaded
MAX_FILE_SIZE_GB = None       # skip files bigger than this (None = no limit)
UPLOAD_LIMIT_GB = 2           # filled in automatically after login (see detect_premium)
FREE_UPLOAD_LIMIT_GB = 2      # Telegram upload cap for a normal account
PREMIUM_UPLOAD_LIMIT_GB = 4   # Telegram upload cap for a Premium account

CONNECT_TIMEOUT = 60
REQUEST_RETRIES = 5

MESSAGE_RETRIES = 3          # how many times to retry a single message before giving up
MESSAGE_RETRY_BACKOFF = 5    # seconds to wait between retries

# ---- performance / fidelity knobs ----
PREFER_DIRECT_MEDIA = True   # re-send media by reference (no download/upload) when possible
PROTECTED_CHAT_AUTODETECT = True  # detect "restrict saving content" chats up front and skip the
                                  # reference-based paths that always fail there
PROGRESS_FLUSH_SECONDS = 5   # throttle progress.json writes (was: every single message)
# ---- progress-file safety net ----
# progress.json is the memory of the whole archive ("how far did we get"). Losing it
# means starting over, so it is written atomically, forced to disk, and a previous-good
# copy is kept next to it. If the main file is ever unreadable, the backup is used.
PROGRESS_BACKUPS = True
PROGRESS_BACKUP_SUFFIX = ".bak"
PROGRESS_BACKUP_SECONDS = 60  # refresh the backup copy at most this often
VERIFY_SENT = "media"        # "always" | "media" | "never": re-check the message really landed
DUPLICATE_SCAN_LIMIT = 30    # messages scanned in the destination before a retry, to avoid duplicates
ALBUM_MAX_ITEMS = 10         # Telegram's max items per album
CAPTION_LIMIT = 1024         # Telegram's caption length cap (media messages only)
TEXT_LIMIT = 4096            # Telegram's plain-text message length cap
SPLIT_LONG_TEXT = True       # send the overflow as follow-up replies instead of cutting it off

# ---- transfer speed ----
# Telegram throttles a SINGLE download/upload stream, so one connection rarely goes
# past ~1 MB/s. These settings download/upload one file in several parallel chunks.
FAST_TRANSFER = True           # set False to go back to plain single-stream transfers
TRANSFER_CONNECTIONS = 8       # parallel chunk workers per file (4-16 is sane; too high = FloodWait)
FAST_TRANSFER_MIN_MB = 5       # only bother with parallel transfer for files bigger than this
DOWNLOAD_REQUEST_SIZE_KB = 1024  # bytes per download request (max 1024, must divide by 4)
UPLOAD_PART_SIZE_KB = 512      # bytes per upload part (max 512, must be a multiple of 1)

# ---- history scanning ----
# When only some forum topics are selected, ask Telegram for exactly those topics instead of
# walking the entire chat history and throwing away every message that doesn't match.
TOPIC_TARGETED_SCAN = True
TOPIC_SCAN_MAX = 25            # above this many topics a plain full scan is cheaper
# ------------------------------------------------


class SkipMessage(Exception):
    """Raised for an intentional, non-retryable skip (filtered topic, too-large file, empty message)."""


# ------------------ look & feel ------------------
# One palette for every panel, table, prompt and progress bar, so the whole UI matches.
UI = {
    "accent": "#00e5c0",    # primary mint
    "accent2": "#7cf3ff",   # cyan highlight
    "violet": "#b18cff",    # borders / secondary
    "gold": "#ffd166",      # attention
    "dim": "#7a8b99",       # muted text
    "track": "#2b3b45",     # empty progress-bar track
}


RTL_CHARS = re.compile("[\u0590-\u07bf\u0860-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")
_BIDI_TIP = {"shown": False}


def display_text(text):
    """Make Persian/Arabic (RTL) names readable in the terminal.

    Telegram stores text in logical order, but terminals render characters strictly
    left-to-right, so RTL names show up reversed and garbled. This reshapes and
    reorders such text for DISPLAY ONLY -- nothing sent to Telegram is ever changed.
    """
    if text is None:
        return text
    text = str(text)
    if not RTL_CHARS.search(text):
        return text
    if not HAS_BIDI:
        if not _BIDI_TIP["shown"]:
            _BIDI_TIP["shown"] = True
            console.print(
                "[yellow]Persian/Arabic names need two small libraries to render "
                "correctly in a terminal:[/yellow]  pip install arabic-reshaper python-bidi"
            )
        return text
    try:
        return bidi_get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def chat_created_at(entity):
    """Creation datetime of a group/channel, or None when Telegram doesn't expose it."""
    return getattr(entity, "date", None)


def fmt_created(entity):
    """Creation date as YYYY-MM-DD, for telling old chats from new ones at a glance."""
    dt = chat_created_at(entity)
    if dt is None:
        return "\u2014"
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)[:10]


def chat_age_label(entity):
    """Compact age of a chat, e.g. '4y', '7mo', '12d'."""
    dt = chat_created_at(entity)
    if dt is None:
        return ""
    try:
        now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
        days = max((now - dt).days, 0)
    except Exception:
        return ""
    if days >= 365:
        return f"{days // 365}y"
    if days >= 30:
        return f"{days // 30}mo"
    return f"{days}d"


def chat_sort_key(dialog):
    """Oldest chats first; chats with an unknown creation date go last."""
    dt = chat_created_at(dialog.entity)
    try:
        return (0, dt.timestamp())
    except Exception:
        return (1, 0.0)


def header_panel(title, subtitle="", icon="", border=None):
    """The one header style used by every screen."""
    body = f"[bold {UI['accent']}]{icon}  {title}[/bold {UI['accent']}]"
    if subtitle:
        body += f"\n[{UI['dim']}]{subtitle}[/{UI['dim']}]"
    return Panel(
        Align.center(body), box=box.ROUNDED, expand=True, padding=(0, 4),
        border_style=border or UI["violet"],
    )


qstyle = QStyle([
    ("qmark", "fg:#00e5c0 bold"),
    ("question", "bold fg:#e8f6ff"),
    ("pointer", "fg:#b18cff bold"),
    ("highlighted", "fg:#00e5c0 bold"),
    ("selected", "fg:#7cf3ff bold"),
    ("separator", "fg:#5a6b78"),
    ("instruction", "fg:#7a8b99 italic"),
    ("text", "fg:#e8f6ff"),
    ("disabled", "fg:#5a6b78 italic"),
    ("answer", "fg:#00e5c0 bold"),
])


# ------------------ general helpers ------------------

def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9_\-]+", "_", name).strip("_") or "account"


# ---- progress file: cached in memory, written atomically and throttled ----
_PROGRESS_STATE = {"file": None, "data": None, "last_write": 0.0, "dirty": False}


def _quarantine_progress_file(path):
    """Move a damaged progress file aside instead of overwriting it, so nothing that
    might still be rescued by hand is ever thrown away."""
    try:
        broken = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, broken)
        console.print(f"[{UI['dim']}]The damaged file was kept as {os.path.basename(broken)}.[/{UI['dim']}]")
    except Exception:
        pass


def _read_progress_file(path):
    """Reads the progress file, falling back to the .bak copy when the main one is
    unreadable (power cut / killed process / full disk during a write)."""
    candidates = [(path, False)]
    if PROGRESS_BACKUPS:
        candidates.append((path + PROGRESS_BACKUP_SUFFIX, True))

    for candidate, is_backup in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("the progress file doesn't contain a JSON object")
            if is_backup:
                console.print(
                    f"[bold {UI['gold']}]\u267b Recovered the archive progress from the backup copy "
                    f"({os.path.basename(candidate)}).[/bold {UI['gold']}]\n"
                    f"[{UI['dim']}]At most the last few messages may be copied again; nothing is lost.[/{UI['dim']}]"
                )
            return data
        except Exception as e:
            console.print(
                f"[yellow]\u26a0 Couldn't read '{os.path.basename(candidate)}' ({e}).[/yellow]"
            )
            if not is_backup:
                _quarantine_progress_file(candidate)

    return {}


def _backup_progress_file(path):
    """Keep one previous-good copy of the progress file next to it."""
    if not PROGRESS_BACKUPS or not os.path.exists(path):
        return
    backup = path + PROGRESS_BACKUP_SUFFIX
    now = time.monotonic()
    if (now - _PROGRESS_STATE.get("last_backup", 0.0) < PROGRESS_BACKUP_SECONDS
            and os.path.exists(backup)):
        return
    try:
        shutil.copy2(path, backup)
        _PROGRESS_STATE["last_backup"] = now
    except Exception:
        pass  # a missing backup must never stop an archive run


def load_progress(progress_file):
    """Returns the whole progress dict for this account (cached, so the file isn't
    re-read/re-parsed for every single message)."""
    st = _PROGRESS_STATE
    if st["file"] != progress_file or st["data"] is None:
        st["file"] = progress_file
        st["data"] = _read_progress_file(progress_file)
        st["dirty"] = False
        st["last_write"] = 0.0
    return st["data"]


def flush_progress(force=False):
    """Writes the cached progress to disk (atomically). Throttled unless force=True."""
    st = _PROGRESS_STATE
    if not st["dirty"] or st["data"] is None or st["file"] is None:
        return
    if not force and (time.monotonic() - st["last_write"]) < PROGRESS_FLUSH_SECONDS:
        return
    # Written safely in three steps, so a crash can never leave a half-written file:
    #   1. write everything into a temporary file and force it onto the disk (fsync)
    #   2. keep the current file as .bak (the previous known-good version)
    #   3. swap the temporary file in atomically - readers see the old OR the new file
    target_path = st["file"]
    tmp_path = f"{target_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(st["data"], f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _backup_progress_file(target_path)
        os.replace(tmp_path, target_path)
    except Exception as e:
        console.print(f"[red]Couldn't save progress: {e}[/red]")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return
    st["last_write"] = time.monotonic()
    st["dirty"] = False


def save_progress(progress_file, key, data, force=True):
    all_data = load_progress(progress_file)
    all_data[key] = data
    _PROGRESS_STATE["dirty"] = True
    flush_progress(force=force)


def delete_progress(progress_file, key):
    all_data = load_progress(progress_file)
    if key in all_data:
        del all_data[key]
        _PROGRESS_STATE["dirty"] = True
        flush_progress(force=True)
        return True
    return False


def parse_progress_key(key):
    """Progress keys are either "<chat_id>" or "<chat_id>:author:<author_id>:<topic>".
    Returns (chat_id, author_filter_id) - author_filter_id is None for plain keys."""
    parts = str(key).split(":")
    chat_id = int(parts[0])
    author_id = None
    if len(parts) >= 3 and parts[1] == "author":
        try:
            author_id = int(parts[2])
        except ValueError:
            author_id = None
    return chat_id, author_id


# ---- disk space ----

def free_disk_bytes():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return shutil.disk_usage(DOWNLOAD_DIR).free


def has_enough_disk_space(min_gb, extra_bytes=0):
    """True if there's room for a file of `extra_bytes` PLUS a safety margin (and at
    least min_gb free overall). The old version only checked a flat 3GB, so a single
    5GB video could still fill the disk mid-download."""
    gb = 1024 ** 3
    needed = max(min_gb * gb, extra_bytes + DISK_SAFETY_MARGIN_GB * gb)
    return free_disk_bytes() >= needed


# ---- text / entity helpers ----

def utf16_len(text):
    """Telegram measures entity offsets in UTF-16 code units, not Python characters."""
    return len((text or "").encode("utf-16-le")) // 2


def build_message_text(message, sender_label=None, limit=None):
    """Returns (text, entities) for a copy of `message`, using raw_text + real formatting
    entities (so '*', '_' and backticks in the original text can't be re-parsed as
    Markdown and mangle the message). Optionally prefixes the sender's name in bold."""
    text = message.raw_text or ""
    entities = [copy.copy(e) for e in (message.entities or [])]

    if sender_label:
        prefix = f"{sender_label}:\n"
        shift = utf16_len(prefix)
        for e in entities:
            e.offset += shift
        entities.insert(0, types.MessageEntityBold(offset=0, length=utf16_len(sender_label)))
        text = prefix + text

    if limit and len(text) > limit:
        text = text[: limit - 1] + "\u2026"
        max_units = utf16_len(text)
        entities = [e for e in entities if e.offset + e.length <= max_units]

    return text, entities


def utf16_prefix_map(text):
    """prefix[i] = UTF-16 length of text[:i], so entity offsets can be re-based when text
    is split into chunks (astral chars like emoji count as 2 units, not 1)."""
    prefix = [0]
    total = 0
    for char in text:
        total += 2 if ord(char) > 0xFFFF else 1
        prefix.append(total)
    return prefix


def split_text_with_entities(text, entities, limit):
    """Splits text into <=limit chunks, preferring line/word boundaries, and rebuilds the
    formatting entities for every chunk (clipping the ones crossing a boundary) so bold,
    links and code blocks survive the split."""
    if not text or len(text) <= limit:
        return [(text, entities)]

    prefix = utf16_prefix_map(text)
    pieces = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n"), window.rfind(" "))
            if cut > limit // 2:  # only use a nice boundary if it isn't wastefully early
                end = start + cut + 1
        unit_start, unit_end = prefix[start], prefix[end]
        chunk_entities = []
        for entity in entities:
            new_start = max(entity.offset, unit_start)
            new_end = min(entity.offset + entity.length, unit_end)
            if new_end <= new_start:
                continue
            clipped = copy.copy(entity)
            clipped.offset = new_start - unit_start
            clipped.length = new_end - new_start
            chunk_entities.append(clipped)
        pieces.append((text[start:end], chunk_entities))
        start = end
    return pieces


def build_message_parts(message, sender_label=None, limit=None):
    """Like build_message_text, but returns a LIST of (text, entities) parts: the first one
    fits the limit and the rest are the overflow, to be sent as follow-up messages."""
    text, entities = build_message_text(message, sender_label)
    if not limit or len(text) <= limit:
        return [(text, entities)]
    if not SPLIT_LONG_TEXT:
        return [build_message_text(message, sender_label, limit=limit)]
    return split_text_with_entities(text, entities, limit)


async def send_text_continuations(client, target_entity, anchor_id, parts, fallback_reply_to):
    """Sends the overflow of a too-long message as replies chained to the message itself, so
    nothing is lost when Telegram's 1024-char caption / 4096-char text cap is hit. Failures
    here are reported but never fail the parent message (it was already sent)."""
    anchor = anchor_id or fallback_reply_to
    for index, (chunk, chunk_entities) in enumerate(parts, start=2):
        if not chunk.strip():
            continue
        try:
            follow_up = await client.send_message(
                target_entity, chunk, formatting_entities=chunk_entities or None,
                reply_to=anchor, link_preview=False,
            )
            anchor = getattr(follow_up, "id", anchor)
            console.print(f"  [dim]Long message: sent continuation part {index}.[/dim]")
            await asyncio.sleep(0.5)
        except FloodWaitError as e:
            rate_report_flood(getattr(e, "seconds", 0))
            console.print(f"[yellow]Slow-down while sending a continuation; waiting {e.seconds}s...[/yellow]")
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            console.print(f"[yellow]\u26a0 Couldn't send continuation part {index}: {e}[/yellow]")
            return


class SameChatError(Exception):
    """Raised when the destination resolves to the source chat itself."""
    pass


def entity_key(entity):
    """A stable identity for a chat: (id, access-less type marker). Ids alone are enough
    to tell two chats apart within one account."""
    if entity is None:
        return None
    return getattr(entity, "id", None)


def ensure_not_same_chat(source_entity, target_entity, where=""):
    """Hard safety guard: never write into the chat we are reading from. Protects against a
    mis-picked destination (or a stale/wrong target_id in progress.json) turning an archive
    run into a flood of duplicate messages inside the original group."""
    src = entity_key(source_entity)
    dst = entity_key(target_entity)
    if src is None or dst is None:
        return
    if src == dst:
        suffix = f" ({where})" if where else ""
        console.print(
            f"\n[bold red]STOPPED: the destination is the SAME chat as the source{suffix}.[/bold red]\n"
            f"[red]Nothing was sent. Copying a chat into itself would flood the original group.[/red]\n"
            f"[yellow]Fix it by choosing (or letting the script create) a different destination chat. "
            f"If a wrong destination was saved earlier, use the 'reset a chat's progress' option first.[/yellow]"
        )
        raise SameChatError(f"source and destination are the same chat (id={src})")


# ------------------ protected ("saving/forwarding restricted") source chats ------------------
# A chat with content protection enabled (noforwards) refuses EVERY attempt to move media by
# reference: direct re-send, album-by-reference and even parallel chunk downloads all die with
# FILE_REFERENCE_EXPIRED. Detecting that once, up front, saves three doomed attempts per file
# and lets us rebuild albums out of the downloaded files instead of losing their grouping.
PROTECTED_SOURCE = {"on": False}


def detect_protected_source(source_entity):
    """True when the source chat has 'restrict saving content' turned on."""
    if not PROTECTED_CHAT_AUTODETECT:
        return False
    return bool(getattr(source_entity, "noforwards", False))


def announce_protected_source(source_entity):
    """Sets the run-wide flag and explains it ONCE, instead of a warning per file."""
    PROTECTED_SOURCE["on"] = detect_protected_source(source_entity)
    if PROTECTED_SOURCE["on"]:
        console.print(
            "[bold yellow]\u26a0 This chat has content protection enabled "
            "(forwarding / saving restricted).[/bold yellow]\n"
            "[cyan]Telegram refuses to move protected media by reference, so every file is "
            "downloaded and re-uploaded from the start. Albums are rebuilt from the downloaded "
            "files so their grouping is kept, and file references are refreshed before each "
            "parallel download so multi-stream speed still works.[/cyan]"
        )
    else:
        console.print(
            "[dim]Source is not content-protected; media will be re-sent by reference when possible.[/dim]"
        )


def direct_media_allowed():
    """Reference-based re-send is pointless in a protected chat."""
    return PREFER_DIRECT_MEDIA and not PROTECTED_SOURCE["on"]


async def refresh_message(client, message):
    """Re-fetches a message so its file reference is fresh. Protected chats invalidate file
    references almost immediately, which is exactly what breaks the parallel download path.
    Returns the original message unchanged if anything goes wrong."""
    try:
        chat = getattr(message, "chat_id", None) or getattr(message, "peer_id", None)
        if chat is None:
            return message
        fresh = await client.get_messages(chat, ids=message.id)
        if fresh is not None and getattr(fresh, "media", None) is not None:
            return fresh
    except Exception:
        pass
    return message


def cleanup_paths(paths):
    """Best-effort removal of temporary files."""
    for p in paths or []:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


async def resolve_sender_label(client, message, cache):
    """Human-readable name of a message's sender (channel/group title for anonymous-admin
    posts, post_author for signed channel posts). Cached per sender id."""
    signed = getattr(message, "post_author", None)
    if signed:
        return signed

    sender_id = message.sender_id
    if sender_id is None:
        return None
    if sender_id in cache:
        return cache[sender_id]

    label = None
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None

    if sender is not None:
        title = getattr(sender, "title", None)
        if title:
            label = title
        else:
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            label = f"{first} {last}".strip()
            if not label and getattr(sender, "username", None):
                label = f"@{sender.username}"
    if not label:
        label = f"id {sender_id}"

    cache[sender_id] = label
    return label


# ---- Back navigation ----
# Every interactive screen offers a "\u2b05 Back" entry. Prompt helpers return BACK
# when it's picked, and the flows step ONE screen backwards instead of cancelling
# the whole job. Ctrl+C inside a list is treated the same as Back.
BACK = "__back__"


def back_choice(label="\u2b05  Back"):
    return questionary.Choice(title=label, value=BACK)


async def choose_sender_prefix_option():
    """Optional: put the sender's name at the top of every copied message/caption.
    Handy for topic archives where every message otherwise looks like it's from you."""
    answer = await questionary.select(
        "Prefix every copied message with the original sender's name (e.g. \u00abAli:\u00bb on the first line)?",
        choices=[
            questionary.Choice(title="\u274C  No", value="no"),
            questionary.Choice(title="\u2705  Yes", value="yes"),
            back_choice(),
        ],
        style=qstyle,
    ).ask_async()
    if answer is None or answer == BACK:
        return BACK
    return answer == "yes"


# ---- sender-name prefix: one answer for the whole chat, or Yes/No per topic ----
# The setting travels as either a bool (whole chat) or {topic_id: bool} (per topic).

def sender_names_used(setting):
    """True when at least one topic (or the whole chat) wants sender names."""
    if isinstance(setting, dict):
        return any(bool(v) for v in setting.values())
    return bool(setting)


def wants_sender_names(setting, topic_id=None):
    """Whether a message from this topic should be prefixed with the sender's name.
    Handles both int keys (this run) and string keys (reloaded from progress.json)."""
    if isinstance(setting, dict):
        if topic_id is None:
            return sender_names_used(setting)
        if topic_id in setting:
            return bool(setting[topic_id])
        return bool(setting.get(str(topic_id), False))
    return bool(setting)


async def build_sender_labels(client, group, setting, is_forum, cache):
    """{message_id: sender label} for a message group, honouring the per-topic Yes/No choice.
    Messages from topics answered 'No' get no label at all, so nothing is looked up for them."""
    labels = {}
    if not sender_names_used(setting):
        return labels
    for m in group:
        topic_id = (get_message_topic_id(m, is_forum) or 1) if is_forum else None
        if wants_sender_names(setting, topic_id):
            labels[m.id] = await resolve_sender_label(client, m, cache)
    return labels


async def fetch_topic_titles(client, source_entity):
    """{topic_id: title} for a forum source (General is always id 1)."""
    titles = {1: "General"}
    try:
        topics_result = await client(
            functions.messages.GetForumTopicsRequest(
                peer=source_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
            )
        )
        for t in topics_result.topics:
            tid = getattr(t, "id", None)
            if tid:
                titles[tid] = t.title
    except Exception:
        pass
    return titles


def print_sender_prefix_summary(setting, titles=None):
    """Yes/No overview, so the choice is visible before the copy starts."""
    if not isinstance(setting, dict):
        return
    titles = titles or {}
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 3),
                  border_style=UI["dim"], header_style=f"bold {UI['accent2']}",
                  title="\u270D  SENDER NAME PER TOPIC", title_style=f"bold {UI['accent']}")
    table.add_column("Topic", style="bold white")
    table.add_column("Sender name on top", justify="right")
    for tid, on in setting.items():
        table.add_row(
            display_text(titles.get(tid, f"topic {tid}")),
            f"[bold {UI['accent']}]Yes[/bold {UI['accent']}]" if on else f"[{UI['dim']}]No[/{UI['dim']}]",
        )
    console.print()
    console.print(table)
    console.print()


async def sender_prefix_for_topics(client, source_entity, topic_ids):
    """Per-topic Yes/No screen, driven entirely by the keyboard: space toggles a topic,
    arrows move, enter confirms. Checked topics get the sender's name on the first line of
    every message copied from them. Returns {topic_id: bool}."""
    ids = list(dict.fromkeys(topic_ids))
    titles = await fetch_topic_titles(client, source_entity)
    console.print(
        f"[{UI['dim']}]\u2191\u2193 move \u00b7 space toggles Yes/No \u00b7 enter confirms. "
        f"Checked = Yes (the sender's name is added at the top of messages copied from that "
        f"topic); unchecked = No.[/{UI['dim']}]"
    )
    picked = await questionary.checkbox(
        "Which topics should show the original sender's name?",
        choices=[
            questionary.Choice(title=display_text(titles.get(tid, f"topic {tid}")), value=tid, checked=False)
            for tid in ids
        ],
        style=qstyle,
    ).ask_async()
    if picked is None:
        return BACK  # Ctrl+C inside the list -> back to the previous question
    picked = set(picked)
    setting = {tid: (tid in picked) for tid in ids}
    print_sender_prefix_summary(setting, titles)
    return setting


async def choose_sender_prefix_setting(client, source_entity, is_forum, selected_topic_ids):
    """Asks whether copied messages should start with the original sender's name.
    On a chat with topics you can answer once for everything, or topic by topic.
    Returns a bool (whole chat) or {topic_id: bool} (per topic)."""
    if not is_forum:
        return await choose_sender_prefix_option()

    if selected_topic_ids:
        topic_ids = sorted(selected_topic_ids)
    else:
        topic_ids = sorted(await fetch_topic_titles(client, source_entity))

    if len(topic_ids) <= 1:
        return await choose_sender_prefix_option()

    while True:
        mode = await questionary.select(
            "Prefix copied messages with the original sender's name?",
            choices=[
                questionary.Choice(title="\u274C  No \u2014 for every topic", value="none"),
                questionary.Choice(title="\u2705  Yes \u2014 for every topic", value="all"),
                questionary.Choice(
                    title=f"\U0001F3AF  Decide topic by topic (Yes/No for each of the {len(topic_ids)} topics)",
                    value="per",
                ),
                back_choice(),
            ],
            style=qstyle,
        ).ask_async()

        if mode is None or mode == BACK:
            return BACK
        if mode == "all":
            return True
        if mode == "per":
            setting = await sender_prefix_for_topics(client, source_entity, topic_ids)
            if setting == BACK:
                continue  # back to this question
            return setting
        return False


# ---- media helpers ----

def is_link_preview_media(message):
    """True when the image shown in the message comes from a LINK PREVIEW card that
    Telegram generated itself (MessageMediaWebPage), not from a photo/file the sender
    actually attached. Those must never be downloaded/re-uploaded as real media - the
    destination regenerates the preview from the link on its own."""
    return isinstance(getattr(message, "media", None), types.MessageMediaWebPage)


def is_real_file_media(message):
    """True for media the sender genuinely attached (photo or document)."""
    return isinstance(
        getattr(message, "media", None),
        (types.MessageMediaPhoto, types.MessageMediaDocument),
    )


def was_sent_as_file(message):
    """True when the sender attached this as an uncompressed FILE (a document), so the copy
    has to go out as a file too. Sending an image document as a normal photo makes Telegram
    re-compress it and the original quality is lost.

    Videos, gifs, audio, voice/round notes and stickers are documents as well, but they must
    stay native/playable, so they deliberately do not count as plain files here."""
    doc = getattr(message, "document", None)
    if doc is None:
        return False  # MessageMediaPhoto: it genuinely was a compressed photo
    for attribute in (doc.attributes or []):
        if isinstance(attribute, (
            types.DocumentAttributeVideo,
            types.DocumentAttributeAudio,
            types.DocumentAttributeAnimated,
            types.DocumentAttributeSticker,
        )):
            return False
    return True


def media_upload_kwargs(message):
    """Keeps voice notes as voice notes, round videos round, gifs animated, etc. when we
    have to fall back to download + re-upload - and keeps files as files."""
    kwargs = {"force_document": was_sent_as_file(message)}
    doc = getattr(message, "document", None)
    if doc is not None:
        attributes = [
            a for a in (doc.attributes or [])
            if not isinstance(a, types.DocumentAttributeSticker)
        ]
        if attributes:
            kwargs["attributes"] = attributes
        if getattr(doc, "mime_type", None):
            kwargs["mime_type"] = doc.mime_type
    return kwargs


# ------------------ adaptive pacing ------------------
# One shared "how fast dare we go right now" value. Everything that sends a message
# sleeps for rate_delay() instead of a hard-coded constant, reports success with
# rate_report_ok() and reports a FloodWait with rate_report_flood().

_rate_state = {"delay": float(DELAY_BETWEEN_MESSAGES), "ok": 0, "floods": 0}


def rate_delay():
    return _rate_state["delay"] if ADAPTIVE_DELAY else DELAY_BETWEEN_MESSAGES


async def rate_sleep():
    await asyncio.sleep(rate_delay())


def rate_report_ok():
    """A message went through. After a clean streak, dare to go a bit faster."""
    if not ADAPTIVE_DELAY:
        return
    _rate_state["ok"] += 1
    if _rate_state["ok"] < SPEEDUP_AFTER:
        return
    _rate_state["ok"] = 0
    faster = max(MIN_MESSAGE_DELAY, round(_rate_state["delay"] * SPEEDUP_FACTOR, 2))
    if faster < _rate_state["delay"]:
        _rate_state["delay"] = faster
        console.print(
            f"[{UI['dim']}]\u26a1 no flood warnings for a while \u2014 speeding up to "
            f"{faster}s between messages[/{UI['dim']}]"
        )


def rate_report_flood(seconds=0):
    """Telegram pushed back. Back off immediately and forget the clean streak."""
    _rate_state["floods"] += 1
    if not ADAPTIVE_DELAY:
        return
    _rate_state["ok"] = 0
    slower = min(MAX_MESSAGE_DELAY, round(max(_rate_state["delay"] * SLOWDOWN_FACTOR,
                                              float(DELAY_BETWEEN_MESSAGES)), 2))
    if slower > _rate_state["delay"]:
        _rate_state["delay"] = slower
        console.print(
            f"[{UI['gold']}]\U0001F6A6 Telegram asked us to wait "
            f"{int(seconds)}s \u2014 slowing down to {slower}s between messages[/{UI['gold']}]"
        )


def rate_summary():
    return f"{_rate_state['delay']}s pause, {_rate_state['floods']} flood warning(s)"


# ------------------ overall progress / ETA ------------------

def fmt_duration(seconds):
    seconds = int(max(seconds or 0, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class RunProgress:
    """Whole-run counter printed as a single line every so often:
    "1,240 / 8,300  15.0%  elapsed 12m  ~47m left  3.1 msg/s".
    `total` may be None (unknown), in which case only the counter and rate show."""

    def __init__(self, total=None):
        self.total = total if isinstance(total, int) and total > 0 else None
        self.done = 0
        self.started = time.time()
        self._last_print = 0.0
        self._since_print = 0

    def tick(self, n=1):
        self.done += n
        self._since_print += n

    @property
    def elapsed(self):
        return max(time.time() - self.started, 0.001)

    def eta_seconds(self):
        if not self.total or self.done <= 0 or self.done >= self.total:
            return None
        return (self.total - self.done) / (self.done / self.elapsed)

    def maybe_print(self, force=False):
        if not SHOW_OVERALL_PROGRESS:
            return
        now = time.time()
        if not force and self._since_print < PROGRESS_LINE_EVERY and now - self._last_print < 30:
            return
        self._last_print = now
        self._since_print = 0
        rate = self.done / self.elapsed
        line = f"\u23f3 [bold {UI['accent2']}]{self.done:,}"
        if self.total:
            line += f" / {self.total:,}"
        line += f"[/bold {UI['accent2']}]"
        if self.total:
            line += f"  [{UI['accent']}]{self.done / self.total * 100:.1f}%[/{UI['accent']}]"
        line += f"  [{UI['dim']}]elapsed {fmt_duration(self.elapsed)}[/{UI['dim']}]"
        eta = self.eta_seconds()
        if eta is not None:
            line += f"  [{UI['gold']}]~{fmt_duration(eta)} left[/{UI['gold']}]"
        line += f"  [{UI['dim']}]{rate:.1f} msg/s \u00b7 {rate_delay()}s pause[/{UI['dim']}]"
        console.print(line)


async def last_n_start_id(client, entity, n, topic_id=None):
    """The offset_id to start walking from so that only the newest `n` messages are
    covered. Returns None when it can't be worked out (then the full history is used)."""
    if not n or n <= 0:
        return None
    try:
        if topic_id:
            messages = await client.get_messages(entity, limit=n, reply_to=topic_id)
        else:
            messages = await client.get_messages(entity, limit=n)
    except Exception:
        return None
    if not messages:
        return None
    return max(0, messages[-1].id - 1)


async def notify_finished(client, title, body):
    """Drop a short summary into your own Saved Messages, so long unattended runs on a
    server can be checked from the phone. Never allowed to break a finished run."""
    if not NOTIFY_ON_FINISH:
        return
    try:
        await client.send_message("me", f"\U0001F4E6 {title}\n\n{body}")
    except Exception:
        pass


async def detect_premium(client):
    """Checked once right after logging in: Premium accounts may upload 4 GB files
    instead of 2 GB. A normal account just keeps the standard limits."""
    global UPLOAD_LIMIT_GB
    try:
        me = await client.get_me()
    except Exception:
        return False
    premium = bool(getattr(me, "premium", False))
    UPLOAD_LIMIT_GB = PREMIUM_UPLOAD_LIMIT_GB if premium else FREE_UPLOAD_LIMIT_GB
    if premium:
        console.print(Panel(
            Align.center(
                f"[bold {UI['gold']}]\U0001F48E  Telegram Premium detected[/bold {UI['gold']}]\n"
                f"[white]upload limit raised to {UPLOAD_LIMIT_GB} GB per file[/white]"
            ),
            border_style=UI["gold"], box=box.ROUNDED, padding=(0, 4), expand=True,
        ))
    else:
        console.print(
            f"[{UI['dim']}]Standard account \u2014 {UPLOAD_LIMIT_GB} GB upload limit, "
            f"normal rules apply.[/{UI['dim']}]"
        )
    return premium


def make_transfer_progress():
    sep = TextColumn(f"[{UI['dim']}]\u2502[/{UI['dim']}]")
    return Progress(
        SpinnerColumn(spinner_name="dots", style=UI["accent"]),
        TextColumn("[bold]{task.fields[label]:<26}"),
        BarColumn(bar_width=40, style=UI["track"], complete_style=UI["accent"],
                  finished_style="bright_green", pulse_style=UI["violet"]),
        TextColumn(f"[bold {UI['accent2']}]" + "{task.percentage:>6.2f}%"),
        sep,
        DownloadColumn(binary_units=True),
        sep,
        TransferSpeedColumn(),
        sep,
        TimeRemainingColumn(compact=True),
        console=console,
        transient=True,
        refresh_per_second=10,
    )


# ------------------ account selection ------------------

async def choose_account():
    """Pick an existing account or create a new one. Returns (name, session_path, progress_file)."""
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    existing = sorted({
        f[: -len(".session")]
        for f in os.listdir(ACCOUNTS_DIR)
        if f.endswith(".session")
    })

    if existing:
        console.print()
        console.print(header_panel(
            "ACCOUNTS",
            f"{len(existing)} saved session(s) in {os.path.abspath(ACCOUNTS_DIR)}",
            icon="\U0001F511", border=UI["accent2"],
        ))
        console.print()

    choices = [questionary.Choice(title=f"\U0001F464  {name}", value=name) for name in existing]
    if existing:
        choices.append(questionary.Separator("\u2500" * 24))
    choices.append(questionary.Choice(title="\u2795  New account", value="__new__"))

    name = None
    while name is None:
        if existing:
            pick = await questionary.select(
                "Which account do you want to use?", choices=choices, style=qstyle,
            ).ask_async()
        else:
            pick = "__new__"

        if pick is None:
            console.print("[yellow]Cancelled.[/yellow]")
            sys.exit(0)

        if pick == "__new__":
            hint = " (leave empty to go back)" if existing else ""
            raw_name = await questionary.text(
                f"Pick a name for this account (just for you, e.g. 'personal' or 'work'){hint}:",
                style=qstyle,
            ).ask_async()
            if not raw_name:
                if existing:
                    continue  # \u2b05 back to the account list
                console.print("[yellow]Cancelled.[/yellow]")
                sys.exit(0)
            name = safe_filename(raw_name)
            if name in existing:
                console.print("[yellow]An account with this name already exists; using it.[/yellow]")
        else:
            name = pick

    session_path = os.path.join(ACCOUNTS_DIR, name)
    progress_file = os.path.join(ACCOUNTS_DIR, f"{name}_progress.json")
    return name, session_path, progress_file


# ------------------ chat selection menu ------------------

# ---- search / filter helper for the chat picker ----
# Persian text can be written with Arabic look-alike letters, so a plain substring
# search misses obvious matches. Fold the variants first, then match every typed
# word separately (order does not matter).
SEARCH_ACTION = "__search__"
CLEAR_SEARCH_ACTION = "__clear_search__"

_SEARCH_FOLD = {
    "\u064a": "\u06cc", "\u0649": "\u06cc", "\u0643": "\u06a9",
    "\u0629": "\u0647", "\u0623": "\u0627", "\u0625": "\u0627",
    "\u0622": "\u0627", "\u0624": "\u0648", "\u0626": "\u06cc",
    "\u200c": " ", "\u200f": "", "\u200e": "",
}
_SEARCH_STRIP = re.compile("[\u064b-\u0652\u0640]")


def norm_search(text):
    """Normalise text so Arabic/Persian spelling variants compare equal."""
    if not text:
        return ""
    text = str(text)
    for a, b in _SEARCH_FOLD.items():
        text = text.replace(a, b)
    text = _SEARCH_STRIP.sub("", text)
    return " ".join(text.lower().split())


def chat_matches(dialog, needle):
    """True when every word typed by the user appears in the chat name or its id."""
    if not needle:
        return True
    hay = norm_search(getattr(dialog, "name", "") or "")
    id_text = str(getattr(dialog, "id", "") or "")
    for term in norm_search(needle).split():
        if term not in hay and term not in id_text:
            return False
    return True


async def choose_dialog(client):
    with console.status("[cyan]Fetching chat list ..."):
        dialogs = await client.get_dialogs()

    channels, groups = [], []
    for d in dialogs:
        entity = d.entity
        if isinstance(entity, Channel) and entity.broadcast:
            channels.append(d)
        elif isinstance(entity, (Channel, Chat)):
            groups.append(d)

    # Oldest first, so it's obvious which chat has been around the longest.
    channels.sort(key=chat_sort_key)
    groups.sort(key=chat_sort_key)

    def render_table(title, icon, items):
        table = Table(
            title=f"{icon}  {title}   [{UI['dim']}]{len(items)} found \u00b7 oldest first[/{UI['dim']}]",
            box=box.SIMPLE_HEAD, show_lines=False, expand=True, padding=(0, 2),
            title_style=f"bold {UI['accent']}", header_style=f"bold {UI['accent2']}",
            border_style=UI["dim"],
        )
        table.add_column("#", style=UI["dim"], width=4, justify="right")
        table.add_column("Name", ratio=3, overflow="fold", style="bold white")
        table.add_column("Created", justify="center", width=12, style=UI["accent2"])
        table.add_column("Age", justify="right", width=6, style=UI["dim"])
        table.add_column("Topics", justify="center", width=8)
        table.add_column("Locked", justify="center", width=8)
        for i, d in enumerate(items, start=1):
            dash = f"[{UI['dim']}]\u2014[/{UI['dim']}]"
            table.add_row(
                str(i),
                display_text(d.name) or dash,
                fmt_created(d.entity),
                chat_age_label(d.entity),
                "\U0001F9F5" if getattr(d.entity, "forum", False) else dash,
                "\U0001F512" if getattr(d.entity, "noforwards", False) else dash,
            )
        return table

    console.print()
    console.print(header_panel(
        "SOURCE CHAT",
        "pick what you want to archive  \u00b7  \U0001F9F5 has topics  \u00b7  \U0001F512 content-protected",
        icon="\U0001F5C2",
    ))
    console.print()
    if channels:
        console.print(render_table("Channels", "\U0001F4E2", channels))
    if groups:
        console.print(render_table("Groups", "\U0001F465", groups))
    console.print()

    def choice_label(dialog, icon):
        # Fixed-width columns (date and age are ASCII) come FIRST so every row lines up,
        # and the -- possibly right-to-left -- name goes LAST so it can't shuffle the row.
        entity = dialog.entity
        forum_slot = "\U0001F9F5" if getattr(entity, "forum", False) else " \u00b7"
        lock_slot = "\U0001F512" if getattr(entity, "noforwards", False) else " \u00b7"
        age = chat_age_label(entity) or "?"
        name = display_text(dialog.name) or "\u2014"
        return f"{icon}  {fmt_created(entity)}  {age:>4}  {forum_slot} {lock_slot}  {name}"

    if not channels and not groups:
        console.print("[red]No groups or channels found.[/red]")
        return None

    total_all = len(channels) + len(groups)
    search = ""

    # The picker is a loop: searching, clearing the search and picking a chat all
    # come back here, so a wrong search is never a dead end.
    while True:
        shown_channels = [d for d in channels if chat_matches(d, search)]
        shown_groups = [d for d in groups if chat_matches(d, search)]
        found = len(shown_channels) + len(shown_groups)

        choices = []
        if search:
            choices.append(questionary.Choice(
                title="\u270e  Edit search  \u00ab" + search + "\u00bb",
                value=SEARCH_ACTION,
            ))
            choices.append(questionary.Choice(
                title="\u2715  Clear search  \u00b7  show all " + fmt_count(total_all),
                value=CLEAR_SEARCH_ACTION,
            ))
        else:
            choices.append(questionary.Choice(
                title="\U0001F50D  Search by name or id ...",
                value=SEARCH_ACTION,
            ))
        choices.append(questionary.Separator("\u2500" * 24))

        if found == 0:
            choices.append(questionary.Separator(
                "   nothing matches \u00ab" + search + "\u00bb \u2014 edit or clear the search"))
        if shown_channels:
            choices.append(questionary.Separator("\u2500\u2500 Channels \u2500\u2500"))
            for d in shown_channels:
                choices.append(questionary.Choice(title=choice_label(d, "\U0001F4E2"), value=d))
        if shown_groups:
            choices.append(questionary.Separator("\u2500\u2500 Groups \u2500\u2500"))
            for d in shown_groups:
                choices.append(questionary.Choice(title=choice_label(d, "\U0001F465"), value=d))

        choices.append(questionary.Separator("\u2500" * 24))
        choices.append(back_choice("\u2b05  Back to main menu"))

        question = "Which group or channel do you want to archive?"
        if search:
            question += "   (search: " + search + " \u2022 " + fmt_count(found) + " of " + fmt_count(total_all) + ")"

        selected = await questionary.select(
            question, choices=choices, style=qstyle, use_shortcuts=False,
        ).ask_async()

        if selected is None or selected == BACK:
            return None

        if selected == CLEAR_SEARCH_ACTION:
            search = ""
            continue

        if selected == SEARCH_ACTION:
            typed = await questionary.text(
                "Search (part of the name, several words allowed, empty = show all):",
                default=search, style=qstyle,
            ).ask_async()
            if typed is None:      # Ctrl+C inside the box: keep the previous search
                continue
            search = typed.strip()
            continue

        return selected


# ------------------ reset a chat's progress ------------------

async def reset_chat_progress(client, progress_file):
    all_data = load_progress(progress_file)
    if not all_data:
        console.print("[yellow]No saved progress for this account.[/yellow]")
        return

    choices = []
    with console.status("[cyan]Fetching chat names ..."):
        for chat_id, data in all_data.items():
            try:
                entity = await client.get_entity(int(chat_id))
                name = getattr(entity, "title", None) or getattr(entity, "first_name", chat_id)
            except Exception:
                name = f"(id {chat_id} - may no longer be accessible)"
            last_id = data.get("last_id", 0)
            choices.append(questionary.Choice(title=f"{name}  \u2014  up to message {last_id}", value=chat_id))

    choices.append(questionary.Choice(title="<- Back", value="__back__"))

    pick = await questionary.select(
        "Whose progress do you want to reset?", choices=choices, style=qstyle,
    ).ask_async()

    if not pick or pick == "__back__":
        return

    confirmed = await questionary.confirm(
        "Are you sure? This only clears the saved progress (next time it will start over "
        "and create a fresh destination chat). The destination chat already created won't "
        "be deleted automatically; delete it manually in Telegram if needed. Continue?",
        style=qstyle, default=False,
    ).ask_async()

    if confirmed:
        delete_progress(progress_file, pick)
        console.print("[green]\u2714 Progress cleared.[/green]")
    else:
        console.print("[yellow]Cancelled.[/yellow]")


# ------------------ why did this message fail? ------------------
# Every giving-up point records a short, plain-language reason for the message id.
# log_failed_links() then writes that reason next to the link, so the log answers
# "why" and not just "which".

_failure_reasons = {}    # message id -> reason it could not be sent
_oversized_skips = {}    # message id -> reason it was too big to even try


def describe_error(error):
    """Turns a raw Telegram/Python error into something a human can act on."""
    text = (str(error) or "").strip()
    upper = text.upper()
    name = error.__class__.__name__
    known = (
        ("NOT_ENOUGH_DISK_SPACE", "not enough free disk space"),
        ("MESSAGE_ID_INVALID", "the original message no longer exists (deleted)"),
        ("MSG_ID_INVALID", "the original message no longer exists (deleted)"),
        ("FILE_REFERENCE", "Telegram's link to the file expired while sending"),
        ("CHAT_WRITE_FORBIDDEN", "no permission to write in the destination chat"),
        ("CHAT_SEND_MEDIA_FORBIDDEN", "the destination doesn't allow this kind of media"),
        ("CHAT_SEND_", "the destination doesn't allow this kind of message"),
        ("USER_BANNED_IN_CHANNEL", "this account is banned in the destination chat"),
        ("CHANNEL_PRIVATE", "the destination chat is not reachable by this account"),
        ("TOPIC_CLOSED", "the destination topic is closed"),
        ("TOPIC_DELETED", "the destination topic was deleted"),
        ("SLOWMODE", "slow mode is enabled in the destination chat"),
        ("MEDIA_EMPTY", "Telegram rejected the media as empty"),
        ("MEDIA_INVALID", "Telegram rejected the media as invalid"),
        ("PHOTO_INVALID", "Telegram rejected the photo"),
        ("PHOTO_EXT_INVALID", "Telegram rejected the image format"),
        ("WEBPAGE_", "the link preview couldn't be rebuilt"),
        ("FILE_PART", "the upload broke midway (a file part was lost)"),
        ("TOO BIG", "the file is too big for this account to upload"),
        ("TOO_BIG", "the file is too big for this account to upload"),
        ("TIMEOUT", "timed out (connection too slow or dropped)"),
        ("TIMED OUT", "timed out (connection too slow or dropped)"),
        ("CONNECTION", "the connection to Telegram dropped"),
        ("DISCONNECT", "the connection to Telegram dropped"),
        ("AUTH_KEY", "Telegram invalidated this session"),
        ("FLOOD", "hit Telegram's rate limit"),
        ("VERIFICATION FAILED", "the message didn't show up in the destination after sending"),
    )
    for needle, friendly in known:
        if needle in upper:
            return friendly
    if name in ("TimeoutError", "CancelledError"):
        return "timed out (connection too slow or dropped)"
    short = " ".join(text.split())
    if len(short) > 120:
        short = short[:117] + "..."
    return f"{name}: {short}" if short else name


def note_failure_reason(msg_id, reason):
    _failure_reasons[msg_id] = reason


def clear_failure_reason(msg_id):
    _failure_reasons.pop(msg_id, None)


def failure_reason(msg_id):
    return _failure_reasons.get(msg_id) or "unknown reason (see the run output)"


def note_skip_reason(msg_id, reason):
    """Size-related skips never reach the failed list, but they are exactly the ones
    worth telling the user about, so they get their own section in the log."""
    text = (reason or "").strip()
    if text.startswith("size "):
        _oversized_skips[msg_id] = text.replace("size ", "", 1)


def reset_oversized_skips():
    _oversized_skips.clear()


def oversized_skips():
    return dict(_oversized_skips)


FAILED_LOG_FILE = "failed_messages.txt"


def message_link(entity, msg_id):
    """Best-effort t.me link to a message in the SOURCE chat.

    Public chats get t.me/<username>/<id>. Private channels/supergroups get the
    t.me/c/<internal_id>/<id> form, which opens for anyone who is a member of that
    chat. Small private groups have no web link at all, so the raw id is recorded.
    """
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(entity, Channel):
        return f"https://t.me/c/{entity.id}/{msg_id}"
    return f"(no public link) chat id {getattr(entity, 'id', '?')}, message id {msg_id}"


def log_failed_links(source_entity, source_name, failed_ids, context="copy run",
                     oversized_ids=None):
    """Append the source-chat links of failed messages to failed_messages.txt, each one
    with a plain-language reason, plus a short count-per-reason summary.

    The file lives next to this script. Every run ADDS a new numbered section
    (Run #1, Run #2, ...) separated by a dotted line; older sections are never
    touched, so the file is a growing history of what failed and why.
    Returns the file path, or None if nothing was written.
    """
    oversized_ids = dict(oversized_ids or {})
    if not failed_ids and not oversized_ids:
        return None
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:  # e.g. pasted into a notebook cell
        base_dir = os.path.abspath(".")
    path = os.path.join(base_dir, FAILED_LOG_FILE)
    run_no = 1
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                run_no = f.read().count("Run #") + 1
    except Exception:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            if run_no > 1:
                f.write("\n" + "." * 72 + "\n\n")
            f.write(f"Run #{run_no}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {context}\n")
            f.write(f"Source: {source_name}  (id {getattr(source_entity, 'id', '?')})\n\n")

            if failed_ids:
                tally = {}
                f.write(f"{len(failed_ids)} failed message(s):\n\n")
                for index, mid in enumerate(sorted(failed_ids), start=1):
                    reason = failure_reason(mid)
                    tally[reason] = tally.get(reason, 0) + 1
                    f.write(f"{index}) {message_link(source_entity, mid)}\n")
                    f.write(f"   reason: {reason}\n")
                if len(tally) > 0:
                    f.write("\nSummary by reason:\n")
                    for reason, how_many in sorted(tally.items(), key=lambda kv: -kv[1]):
                        f.write(f"   {how_many} x {reason}\n")

            if oversized_ids:
                if failed_ids:
                    f.write("\n")
                f.write(
                    f"{len(oversized_ids)} message(s) were SKIPPED because the attached file "
                    f"is too large to send:\n\n"
                )
                for index, mid in enumerate(sorted(oversized_ids), start=1):
                    f.write(f"{index}) {message_link(source_entity, mid)}\n")
                    f.write(f"   reason: {oversized_ids[mid]}\n")
                f.write(
                    "   (retrying will not help; a Premium account or a smaller file is needed)\n"
                )
        return path
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't write {FAILED_LOG_FILE}: {e}[/yellow]")
        return None


async def retry_failed_messages(client, progress_file):
    all_data = load_progress(progress_file)
    candidates = {k: v for k, v in all_data.items() if v.get("failed_ids")}

    if not candidates:
        console.print("[yellow]No chat currently has any failed messages recorded. \U0001F389[/yellow]")
        return

    choices = []
    with console.status("[cyan]Fetching chat names ..."):
        for progress_key, data in candidates.items():
            try:
                chat_id, author_id = parse_progress_key(progress_key)
                entity = await client.get_entity(chat_id)
                name = getattr(entity, "title", None) or getattr(entity, "first_name", str(chat_id))
                if author_id is not None:
                    name = f"{name}  [author-filtered]"
            except Exception:
                name = f"(key {progress_key} - may no longer be accessible)"
            choices.append(
                questionary.Choice(
                    title=f"{name}  \u2014  {len(data['failed_ids'])} failed message(s)",
                    value=progress_key,
                )
            )
    choices.append(questionary.Choice(title="<- Back", value="__back__"))

    pick = await questionary.select(
        "Retry failed messages for which chat?", choices=choices, style=qstyle,
    ).ask_async()

    if not pick or pick == "__back__":
        return

    saved = all_data[pick]
    failed_ids = list(saved.get("failed_ids", []))

    # NOTE: the progress key may be "<chat_id>:author:<author_id>:<topic>", so int(pick)
    # would blow up here - parse it properly and keep the author filter active.
    try:
        source_chat_id, author_filter_id = parse_progress_key(pick)
    except ValueError:
        console.print(f"[red]Couldn't understand the saved key \u00ab{pick}\u00bb.[/red]")
        return

    try:
        source_entity = await client.get_entity(source_chat_id)
        target_entity = await client.get_entity(saved["target_id"])
    except Exception as e:
        console.print(f"[red]Couldn't open the source/destination chat: {e}[/red]")
        return

    topic_map = {int(k): v for k, v in saved.get("topic_map", {}).items()}
    is_forum = saved.get("is_forum", False)
    selected_topic_ids = saved.get("selected_topic_ids")
    if selected_topic_ids is not None:
        selected_topic_ids = set(selected_topic_ids)
    show_sender_names = saved.get("show_sender_names", False)

    console.print(f"[cyan]Retrying {len(failed_ids)} message(s) ...[/cyan]")
    if author_filter_id is not None:
        console.print("[dim]This archive is author-filtered; the same filter is applied to the retry.[/dim]")

    id_map = saved.get("id_map", {})
    with console.status("[cyan]Fetching pinned messages ..."):
        pinned_ids = await get_pinned_ids(client, source_entity)

    still_failed = []
    fixed_count = 0
    label_cache = {}

    with make_transfer_progress() as bar:
        task_id = bar.add_task("idle", label="", total=None, visible=False)
        messages = await client.get_messages(source_entity, ids=failed_ids)
        for message in messages:
            if message is None:
                continue  # the original message no longer exists (deleted)

            sender_label = None
            retry_topic_id = (get_message_topic_id(message, is_forum) or 1) if is_forum else None
            if wants_sender_names(show_sender_names, retry_topic_id):
                sender_label = await resolve_sender_label(client, message, label_cache)

            status, _ = await process_single_message(
                client, message, target_entity, topic_map, is_forum, selected_topic_ids,
                id_map, pinned_ids, bar, task_id,
                author_filter_id=author_filter_id, sender_label=sender_label,
                duplicate_guard=True,
            )
            if status == "sent":
                fixed_count += 1
                console.print(f"[green]\u2714[/green] Message id={message.id} sent successfully.")
            elif status == "failed":
                still_failed.append(message.id)
            elif status == "disk_full":
                still_failed.append(message.id)
                console.print("[red]Stopping retry run due to low disk space.[/red]")
                break

            await rate_sleep()

    saved["failed_ids"] = still_failed
    saved["id_map"] = id_map
    save_progress(progress_file, pick, saved, force=True)

    retry_log_note = ""
    if still_failed:
        log_path = log_failed_links(
            source_entity,
            getattr(source_entity, "title", None) or str(source_chat_id),
            still_failed, context="retry run",
        )
        if log_path:
            retry_log_note = f"\n[dim]Their source links were appended to {os.path.basename(log_path)}.[/dim]"

    console.print(Panel(
        f"[green]{fixed_count}[/green] message(s) fixed."
        + (f"\n[red]{len(still_failed)}[/red] still failing: {', '.join(str(i) for i in still_failed)}" if still_failed else "\n[green]All failed messages resolved![/green]")
        + retry_log_note,
        title=f"[bold {UI['accent']}]\U0001F504  RETRY RESULT[/bold {UI['accent']}]",
        border_style=UI["accent2"], box=box.ROUNDED, padding=(1, 2), expand=True,
    ))

# ------------------ create destination chat ------------------

# ------------------ message counting ------------------

async def count_messages(client, entity, topic_id=None):
    """How many messages a chat (or one forum topic) holds. Uses limit=0, so Telegram only
    returns the counter and no message data at all - one cheap request. Returns None when
    the count isn't available, so callers can just show '?' instead of failing."""
    try:
        if topic_id is None:
            result = await client.get_messages(entity, limit=0)
        else:
            result = await client.get_messages(entity, limit=0, reply_to=topic_id)
        total = getattr(result, "total", None)
        return int(total) if total is not None else None
    except Exception:
        return None


def fmt_count(total):
    return f"{total:,}" if isinstance(total, int) else "?"


def topic_label(title, total):
    """Topic title with its message count, for the selection menus."""
    return f"{display_text(title)}   \u2022 {fmt_count(total)} msg"


async def topic_counts(client, source_entity, topic_ids):
    """{topic_id: total} for the given topics, counted concurrently."""
    ids = list(dict.fromkeys(topic_ids))
    with console.status(f"[{UI['accent']}]Counting messages per topic ...", spinner="dots"):
        results = await asyncio.gather(
            *(count_messages(client, source_entity, tid) for tid in ids),
            return_exceptions=True,
        )
    return {tid: (r if isinstance(r, int) else None) for tid, r in zip(ids, results)}


async def print_source_summary(client, dialog):
    """Confirmation card shown right after picking a chat: what it is, when it was created
    and how many messages are waiting to be copied."""
    entity = dialog.entity
    is_broadcast = isinstance(entity, Channel) and getattr(entity, "broadcast", False)
    is_forum = getattr(entity, "forum", False)
    with console.status(f"[{UI['accent']}]Counting messages ...", spinner="dots"):
        total = await count_messages(client, entity)

    info = Table.grid(padding=(0, 3))
    info.add_column(justify="right", style=UI["dim"])
    info.add_column(style="bold white")
    info.add_row("name", display_text(dialog.name) or "\u2014")
    info.add_row("type", ("\U0001F4E2 Channel" if is_broadcast else "\U0001F465 Group")
                 + ("  \u00b7  with topics \U0001F9F5" if is_forum else ""))
    info.add_row("created", f"{fmt_created(entity)}   [{UI['dim']}]({chat_age_label(entity) or '?'} old)[/{UI['dim']}]")
    info.add_row("messages", f"[bold {UI['accent']}]{fmt_count(total)}[/bold {UI['accent']}]"
                 + (f"   [{UI['dim']}](across all topics)[/{UI['dim']}]" if is_forum else ""))
    if getattr(entity, "noforwards", False):
        info.add_row("protected", f"\U0001F512 [{UI['gold']}]forwarding / saving restricted[/{UI['gold']}]")

    console.print()
    console.print(Panel(
        info, title=f"[bold {UI['accent']}]\U0001F4CB  SELECTED SOURCE[/bold {UI['accent']}]",
        border_style=UI["accent2"], box=box.ROUNDED, padding=(1, 4), expand=True,
    ))
    console.print()
    return total


async def choose_single_topic(client, source_entity):
    """If the source has topics, asks which single one to use. Returns None if the source
    isn't a forum (no topic concept applies), or a topic id."""
    if not getattr(source_entity, "forum", False):
        return None

    topics_result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=source_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        )
    )
    other_topics = [t for t in topics_result.topics if getattr(t, "id", None) != 1]
    counts = await topic_counts(client, source_entity, [1] + [t.id for t in other_topics])

    choices = [questionary.Choice(title=topic_label("General", counts.get(1)), value=1)]
    for t in other_topics:
        choices.append(questionary.Choice(title=topic_label(t.title, counts.get(t.id)), value=t.id))

    choices.append(back_choice())

    picked = await questionary.select(
        "Which topic?", choices=choices, style=qstyle,
    ).ask_async()
    if picked is None or picked == BACK:
        return BACK
    return picked


async def choose_author(client, source_entity):
    """Shows the chat's member list (plus a pseudo-entry for anonymous-admin posts) and
    returns the chosen sender id, or None if cancelled."""
    with console.status("[cyan]Fetching member list ..."):
        try:
            participants = await client.get_participants(source_entity, limit=500)
        except Exception as e:
            console.print(f"[red]Couldn't fetch the member list: {e}[/red]")
            return None

    choices = [
        questionary.Choice(
            title="\U0001F3AD Anonymous admin (messages posted as the group itself)",
            value=source_entity.id,
        )
    ]
    for p in sorted(participants, key=lambda u: (u.first_name or u.username or "").lower()):
        first = getattr(p, "first_name", "") or ""
        last = getattr(p, "last_name", "") or ""
        full = f"{first} {last}".strip()
        username = f"@{p.username}" if getattr(p, "username", None) else ""
        label = " ".join(x for x in [full, username] if x) or f"id {p.id}"
        choices.append(questionary.Choice(title=display_text(label), value=p.id))

    if len(participants) >= 500:
        console.print("[yellow]Note: only the first 500 members are listed.[/yellow]")

    choices.append(back_choice())

    picked = await questionary.select(
        "Whose messages do you want to copy?", choices=choices, style=qstyle,
    ).ask_async()
    if picked is None or picked == BACK:
        return BACK
    return picked


async def run_author_filtered_flow(client, progress_file):
    """Copies only one person's (or the anonymous-admin identity's) messages from one
    topic into the matching destination chat. Every question offers Back, which steps
    one screen backwards (topic <- author <- sender-name) or back to the chat list."""
    while True:
        selected = await choose_dialog(client)
        if selected is None:
            return
        await print_source_summary(client, selected)

        is_broadcast = isinstance(selected.entity, Channel) and selected.entity.broadcast
        is_forum = getattr(selected.entity, "forum", False)

        step = "topic"
        topic_id = None
        author_id = None
        show_sender_names = False
        limit_last_n = None
        start_from_id = None
        back_to_chats = False
        while True:
            if step == "topic":
                topic_id = await choose_single_topic(client, selected.entity)
                if topic_id == BACK:
                    back_to_chats = True
                    break
                step = "author"
            elif step == "author":
                author_id = await choose_author(client, selected.entity)
                if author_id == BACK or author_id is None:
                    if is_forum:
                        step = "topic"
                        continue
                    back_to_chats = True
                    break
                step = "amount"
            elif step == "amount":
                limit_last_n = await choose_message_limit(
                    "this person's messages" if not is_forum else "this topic"
                )
                if limit_last_n == BACK:
                    step = "author"
                    continue
                step = "start"
            elif step == "start":
                start_from_id = await choose_start_point(
                    client, selected.entity, "this topic" if is_forum else "this chat",
                )
                if start_from_id == BACK:
                    step = "amount"
                    continue
                step = "sender"
            elif step == "sender":
                show_sender_names = await choose_sender_prefix_setting(
                    client, selected.entity, is_forum, {topic_id} if is_forum else None
                )
                if show_sender_names == BACK:
                    step = "start"
                    continue
                break
        if back_to_chats:
            continue  # \u2b05 back to the chat list

        selected_topic_ids = {topic_id} if is_forum else None

        progress_key = f"{selected.id}:author:{author_id}:{topic_id if topic_id else 'all'}"
        saved = load_progress(progress_file).get(progress_key, {})

        if saved.get("target_id"):
            console.print("[cyan]A destination was already set up for this person/topic; continuing with it.[/cyan]")
            target_entity = await client.get_entity(saved["target_id"])
            topic_map = {int(k): v for k, v in saved.get("topic_map", {}).items()}
            saved["show_sender_names"] = show_sender_names
            save_progress(progress_file, progress_key, saved, force=True)
        else:
            target_entity, topic_map, reused_without_log = await resolve_or_create_target(
                client, selected, is_broadcast, is_forum, selected_topic_ids
            )
            if target_entity == BACK or target_entity is None:
                continue  # \u2b05 back to the chat list
            if reused_without_log:
                console.print(
                    "[yellow]No saved progress log found for this person/topic combination, so copying "
                    "will start from the beginning.[/yellow]"
                )
            saved = {
                "last_id": 0,
                "target_id": target_entity.id,
                "topic_map": topic_map,
                "is_forum": is_forum,
                "selected_topic_ids": list(selected_topic_ids) if selected_topic_ids is not None else None,
                "show_sender_names": show_sender_names,
            }
            save_progress(progress_file, progress_key, saved, force=True)

        await copy_all_messages(
            client, selected.entity, target_entity, topic_map, is_forum,
            progress_key, progress_file, selected.name,
            selected_topic_ids=selected_topic_ids, author_filter_id=author_id,
            show_sender_names=show_sender_names, limit_last_n=limit_last_n,
            start_from_id=start_from_id,
        )
        return

# ---- "start from this message" support ----
# Telegram message links come in a few shapes:
#   t.me/c/1234567890/1858        private chat, message 1858
#   t.me/c/1234567890/12/1858     forum topic 12, message 1858
#   t.me/somechannel/1858         public chat, message 1858
# The last number in the path is always the message id, so that is what we key on.
def parse_message_link(text):
    """Return (chat_ref, message_id) taken from a Telegram link, or (None, None).

    chat_ref is the numeric chat id as a string, or the public username, or None
    when the user simply typed a bare message id.
    """
    if text is None:
        return (None, None)
    text = str(text).strip()
    if not text:
        return (None, None)
    if text.isdigit():
        return (None, int(text))
    if "//" in text:                      # drop any scheme the user pasted
        text = text.split("//", 1)[1]
    text = text.split("?", 1)[0].strip().rstrip("/")
    m = re.match(r"^(?:www\.)?(?:t\.me|telegram\.me)/(.+)$", text, re.I)
    if not m:
        return (None, None)
    parts = [p for p in m.group(1).split("/") if p]
    if len(parts) < 2 or not parts[-1].isdigit():
        return (None, None)
    msg_id = int(parts[-1])
    head = parts[0]
    if head.lower() == "c":
        chat_ref = parts[1] if len(parts) >= 3 and parts[1].isdigit() else None
    else:
        chat_ref = head.lower()
    return (chat_ref, msg_id)


def link_matches_chat(chat_ref, entity):
    """Best-effort check that a pasted link really belongs to the chosen chat."""
    if not chat_ref:
        return True
    if chat_ref.isdigit():
        return str(getattr(entity, "id", "")) == chat_ref
    username = (getattr(entity, "username", None) or "").lower()
    return (not username) or username == chat_ref


async def preview_message(client, source_entity, msg_id):
    """Show the message the user pointed at, so a wrong link is obvious at once."""
    try:
        msg = await client.get_messages(source_entity, ids=msg_id)
    except Exception as e:
        console.print(f"[yellow]Could not load that message: {e}[/yellow]")
        return None
    if not msg:
        console.print(
            "[yellow]No message with that id was found in this chat. You can still use it "
            "as a starting point \u2014 copying will begin right after that id.[/yellow]"
        )
        return None
    body = (getattr(msg, "message", "") or "").strip().replace("\n", " ")
    if len(body) > 90:
        body = body[:90] + " ..."
    if not body:
        body = "[media / no text]"
    when = ""
    try:
        when = msg.date.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    console.print(
        f"[{UI['accent']}]Found:[/{UI['accent']}] id={msg_id}  "
        f"[{UI['dim']}]{when}[/{UI['dim']}]  {display_text(body)}"
    )
    return msg


async def choose_start_point(client, source_entity, what="this chat"):
    """Start at the very beginning, or at a message the user links to?

    Returns None (normal start / resume), a positive message id, or BACK.
    """
    mode = await questionary.select(
        f"Where should copying of {what} start?",
        choices=[
            questionary.Choice(
                title="\u23ee  From the beginning (or where it stopped last time)", value="begin"),
            questionary.Choice(
                title="\U0001F517  From a specific message \u2014 paste its link", value="link"),
            back_choice(),
        ],
        style=qstyle,
    ).ask_async()

    if mode is None or mode == BACK:
        return BACK
    if mode == "begin":
        return None

    console.print(
        f"[{UI['dim']}]In Telegram: right-click / long-press the message \u2192 Copy Message Link. "
        f"Works for private chats too (t.me/c/... links). A bare message id also works.[/{UI['dim']}]"
    )

    while True:
        raw = await questionary.text(
            "Message link or id (leave empty to go back):", style=qstyle,
        ).ask_async()
        if raw is None or not raw.strip():
            return BACK

        chat_ref, msg_id = parse_message_link(raw)
        if not msg_id:
            console.print(
                "[yellow]That does not look like a message link. Expected something like "
                "t.me/c/1234567890/1858 \u2014 or just the number 1858.[/yellow]"
            )
            continue

        if not link_matches_chat(chat_ref, source_entity):
            console.print(
                "[yellow]\u26a0 That link seems to belong to a DIFFERENT chat than the one you "
                "picked. Only its message number will be used.[/yellow]"
            )

        await preview_message(client, source_entity, msg_id)

        ok = await questionary.confirm(
            "Start from this message? (this message itself is copied too)",
            default=True, style=qstyle,
        ).ask_async()
        if ok:
            return msg_id
        # "no" simply loops back so another link can be pasted


async def choose_message_limit(what="this selection"):
    """Copy the whole history, or only the newest N messages?
    Returns None for everything, a positive int for "last N", or BACK."""
    mode = await questionary.select(
        f"How much of {what} should be copied?",
        choices=[
            questionary.Choice(title="\U0001F4DA  Everything (full history)", value="all"),
            questionary.Choice(title="\U0001F522  Only the most recent messages", value="last"),
            back_choice(),
        ],
        style=qstyle,
    ).ask_async()

    if mode is None or mode == BACK:
        return BACK
    if mode == "all":
        return None

    while True:
        raw = await questionary.text(
            "How many of the most recent messages? (e.g. 200 \u2014 leave empty to go back)",
            style=qstyle,
        ).ask_async()
        if raw is None or not raw.strip():
            return BACK
        raw = raw.strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        console.print("[yellow]Please type a positive whole number, for example 200.[/yellow]")


async def expected_run_total(client, source_entity, is_forum, selected_topic_ids,
                             limit_last_n, per_topic_limit):
    """Roughly how many messages this run will walk through - used for the ETA line."""
    try:
        if limit_last_n and not per_topic_limit:
            return int(limit_last_n)
        if is_forum and selected_topic_ids:
            counts = await topic_counts(client, source_entity, sorted(selected_topic_ids))
            values = [v for v in counts.values() if isinstance(v, int)]
            if not values:
                return None
            if limit_last_n:
                values = [min(v, limit_last_n) for v in values]
            return sum(values)
        total = await count_messages(client, source_entity)
        if total is None:
            return None
        return min(total, limit_last_n) if limit_last_n else total
    except Exception:
        return None


async def choose_topics(client, source_entity):
    """If the source has topics, asks whether to archive all or pick specific ones.
    Returns: None for all topics, or a set of selected topic ids (General is id 1,
    shown as a normal, pre-checked, toggleable choice - not forced)."""
    if not getattr(source_entity, "forum", False):
        return None

    topics_result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=source_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        )
    )
    topics = topics_result.topics
    other_topics = [t for t in topics if getattr(t, "id", None) != 1]

    if not other_topics:
        return None  # only General exists, no need to ask

    mode = await questionary.select(
        "This chat has topics. Which ones should be archived?",
        choices=[
            questionary.Choice(title="\u2705 All topics", value="all"),
            questionary.Choice(title="\U0001F3AF Pick specific topics", value="pick"),
            back_choice(),
        ],
        style=qstyle,
    ).ask_async()

    if mode is None or mode == BACK:
        return BACK
    if mode != "pick":
        return None

    counts = await topic_counts(client, source_entity, [1] + [t.id for t in other_topics])
    console.print(
        f"[{UI['dim']}]Use space to toggle, enter to confirm. "
        f"The number next to each topic is how many messages it has.[/{UI['dim']}]"
    )
    picked = await questionary.checkbox(
        "Which topics?",
        choices=[questionary.Choice(title=topic_label("General", counts.get(1)), value=1, checked=True)]
        + [questionary.Choice(title=topic_label(t.title, counts.get(t.id)), value=t.id)
           for t in other_topics],
        style=qstyle,
    ).ask_async()

    if picked is None:
        return BACK  # Ctrl+C inside the list -> go back

    return set(picked)


async def choose_additional_topics(client, source_entity, already_selected):
    """For a chat that's already set up (some topics already selected), offers to add more.
    Returns a set of newly-picked topic ids (subset of the ones not already selected), or
    an empty set if the user adds none / there's nothing left to add."""
    if not getattr(source_entity, "forum", False):
        return set()

    if already_selected is None:
        # None means "all topics" was already chosen - nothing more to add
        return set()

    topics_result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=source_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        )
    )
    remaining = [t for t in topics_result.topics if t.id not in already_selected]

    if not remaining:
        console.print("[dim]Every topic in this chat is already selected for archiving.[/dim]")
        return set()

    want_more = await questionary.confirm(
        "This chat already has a destination set up. Do you want to add more topics to copy this time?",
        style=qstyle, default=False,
    ).ask_async()
    if not want_more:
        return set()

    counts = await topic_counts(client, source_entity, [t.id for t in remaining])
    console.print(
        f"[{UI['dim']}]Use space to toggle, enter to confirm. "
        f"The number next to each topic is how many messages it has.[/{UI['dim']}]"
    )
    picked = await questionary.checkbox(
        "Which additional topics do you want to add?",
        choices=[questionary.Choice(title=topic_label(t.title, counts.get(t.id)), value=t.id)
                 for t in remaining],
        style=qstyle,
    ).ask_async()

    return set(picked) if picked else set()


async def get_target_topics_by_title(client, target_entity):
    """Returns {title: topic_id} for all existing topics in an already-created target chat."""
    title_map = {}
    try:
        topics_result = await client(
            functions.messages.GetForumTopicsRequest(
                peer=target_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
            )
        )
        for t in topics_result.topics:
            title_map[t.title] = t.id
    except Exception:
        pass
    return title_map


async def sync_topics(client, source_entity, target_entity, selected_topic_ids, existing_title_map=None):
    """Creates missing topics in target_entity to match source_entity's topics; reuses any
    target topic whose title already matches instead of creating a duplicate.
    Returns topic_map: {source_topic_id: target_topic_id}."""
    existing_title_map = existing_title_map or {}
    topic_map = {1: 1}

    topics_result = await client(
        functions.messages.GetForumTopicsRequest(
            peer=source_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        )
    )

    for topic in topics_result.topics:
        if getattr(topic, "id", None) == 1:
            continue
        if selected_topic_ids is not None and topic.id not in selected_topic_ids:
            continue

        if topic.title in existing_title_map:
            topic_map[topic.id] = existing_title_map[topic.title]
            console.print(f"  [cyan]=[/cyan] Topic already exists, reusing: {display_text(topic.title)}")
            continue

        try:
            create_result = await client(
                functions.messages.CreateForumTopicRequest(
                    peer=target_entity,
                    title=topic.title,
                    icon_color=getattr(topic, "icon_color", None),
                    icon_emoji_id=getattr(topic, "icon_emoji_id", None),
                    random_id=generate_random_long(),
                )
            )
            new_top_id = None
            for u in create_result.updates:
                msg = getattr(u, "message", None)
                if msg is not None and hasattr(msg, "id"):
                    new_top_id = msg.id
                    break
            if new_top_id:
                topic_map[topic.id] = new_top_id
                console.print(f"  [green]\u2714[/green] Topic created: {display_text(topic.title)}")
            else:
                console.print(f"  [yellow]\u26a0 Couldn't find the new topic id for \u00ab{display_text(topic.title)}\u00bb.[/yellow]")
        except FloodWaitError as e:
            rate_report_flood(getattr(e, "seconds", 0))
            console.print(f"  [yellow]Flood wait; waiting {e.seconds}s ...[/yellow]")
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            console.print(f"  [red]Error creating topic \u00ab{display_text(topic.title)}\u00bb: {e}[/red]")

        await asyncio.sleep(DELAY_BETWEEN_TOPICS)

    return topic_map


async def create_matching_chat(client, source_entity, source_name, selected_topic_ids=None):
    is_broadcast = isinstance(source_entity, Channel) and source_entity.broadcast
    is_forum = getattr(source_entity, "forum", False)
    kind = "channel" if is_broadcast else "group"

    console.print(f"\n[cyan]Creating new {kind} named \u00ab{source_name}\u00bb ...[/cyan]")

    result = await client(
        functions.channels.CreateChannelRequest(
            title=source_name,
            about="",
            megagroup=not is_broadcast,
            forum=(is_forum and not is_broadcast) or None,
        )
    )
    new_channel = result.chats[0]
    console.print(f"[green]\u2714 Created: {display_text(new_channel.title)}[/green]")

    # ---- make sure topics are really ON (Telegram often ignores forum=True at creation) ----
    if is_forum and not is_broadcast and not getattr(new_channel, "forum", False):
        try:
            await client(functions.channels.ToggleForumRequest(channel=new_channel, enabled=True))
            refreshed = await client.get_entity(new_channel)
            if getattr(refreshed, "forum", False):
                new_channel = refreshed
                console.print("[green]\u2714[/green] Topics enabled on the destination.")
            else:
                console.print("[yellow]\u26a0 Topics still look disabled; messages may all land in the main chat.[/yellow]")
        except Exception as e:
            console.print(
                f"[yellow]\u26a0 Couldn't enable topics ({e}). Telegram sometimes requires a minimum "
                f"member count first; everything will go into the main chat for now.[/yellow]"
            )

    # ---- make yourself anonymous in the group (channels are already "anonymous") ----
    if not is_broadcast:
        try:
            me = await client.get_me()
            await client(
                functions.channels.EditAdminRequest(
                    channel=new_channel,
                    user_id=me.id,
                    admin_rights=types.ChatAdminRights(
                        change_info=True, post_messages=True, edit_messages=True,
                        delete_messages=True, ban_users=True, invite_users=True,
                        pin_messages=True, add_admins=True, anonymous=True,
                        manage_call=True, manage_topics=True,
                    ),
                    rank="",
                )
            )
            console.print("[green]\u2714[/green] Anonymous admin mode enabled for you.")
        except Exception as e:
            console.print(
                f"[yellow]\u26a0 Couldn't enable anonymous mode: {e}[/yellow]\n"
                f"[dim]  (Telegram doesn't let the chat's creator be promoted; you can flip "
                f"\"Remain Anonymous\" by hand in the group's admin settings.)[/dim]"
            )

    # ---- copy the description (about/bio) + embed a hidden fingerprint marker ----
    # The marker lets us later find this exact destination again, even if some other,
    # unrelated chat happens to share the same title.
    marker = build_fingerprint_marker(source_entity.id)
    try:
        if isinstance(source_entity, Channel):
            full = await client(functions.channels.GetFullChannelRequest(channel=source_entity))
            about = full.full_chat.about
        else:
            full = await client(functions.messages.GetFullChatRequest(chat_id=source_entity.id))
            about = full.full_chat.about
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't fetch the source description: {e}[/yellow]")
        about = ""

    try:
        # Telegram caps the bio at 255 chars: trim the copied text (never the marker,
        # since finding this destination again depends on it).
        max_about = 255
        body = (about or "").strip()
        if body:
            room = max_about - len(marker) - 2
            if room < 0:
                body = ""
            elif len(body) > room:
                body = body[:room].rstrip()
        new_about = f"{body}\n\n{marker}" if body else marker
        await client(functions.messages.EditChatAboutRequest(peer=new_channel, about=new_about[:max_about]))
        console.print("[green]\u2714[/green] Description copied.")
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't set the description: {e}[/yellow]")

    # ---- copy the profile photo ----
    try:
        photo_path = await client.download_profile_photo(source_entity, file=DOWNLOAD_DIR)
        if photo_path:
            uploaded = await client.upload_file(photo_path)
            await client(
                functions.channels.EditPhotoRequest(
                    channel=new_channel,
                    photo=types.InputChatUploadedPhoto(file=uploaded),
                )
            )
            os.remove(photo_path)
            console.print("[green]\u2714[/green] Profile photo copied.")
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't copy the profile photo: {e}[/yellow]")

    topic_map = {}
    if is_forum and not is_broadcast:
        console.print("[cyan]Source has topics; creating them ...[/cyan]")
        topic_map = await sync_topics(client, source_entity, new_channel, selected_topic_ids)

    return new_channel, topic_map

def get_message_topic_id(message, is_forum):
    if not is_forum or not message.reply_to:
        return None
    if not getattr(message.reply_to, "forum_topic", False):
        return None
    return message.reply_to.reply_to_top_id or message.reply_to.reply_to_msg_id or 1


def get_reply_target_old_id(message, is_forum):
    """Returns the id of the specific message this one is a genuine reply to (not just
    'placed in a topic'), or None if not applicable. Telegram infers the correct topic
    thread automatically when replying to a message that's already inside one."""
    r = message.reply_to
    if not r:
        return None
    if is_forum and getattr(r, "forum_topic", False):
        top_id = r.reply_to_top_id
        msg_id = r.reply_to_msg_id
        if top_id and msg_id and top_id != msg_id:
            return msg_id
        return None
    return getattr(r, "reply_to_msg_id", None)


def build_fingerprint_marker(source_id):
    """A short, hidden-in-plain-sight tag placed in the destination chat's bio, used to
    identify it unambiguously later even if another chat happens to share its title."""
    return f"[archive-src:{source_id}]"


async def get_chat_about(client, entity):
    try:
        if isinstance(entity, Channel):
            full = await client(functions.channels.GetFullChannelRequest(channel=entity))
        else:
            full = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
        return full.full_chat.about or ""
    except Exception:
        return ""


async def find_existing_target(client, name, is_broadcast, source_id=None):
    """Looks through your dialogs for existing chats of the right type with the exact same
    name. If source_id is given, also checks each candidate's bio for our fingerprint marker
    and reports which one (if any) is an exact, unambiguous match.
    Returns (matches, exact_match) - exact_match is None if not found or not applicable."""
    with console.status("[cyan]Checking for an existing destination chat with the same name ..."):
        dialogs = await client.get_dialogs()

    matches = []
    for d in dialogs:
        entity = d.entity
        if not isinstance(entity, Channel):
            continue
        if entity.title != name:
            continue
        if is_broadcast and entity.broadcast:
            matches.append(entity)
        elif not is_broadcast and entity.megagroup:
            matches.append(entity)

    exact_match = None
    if source_id is not None and matches:
        marker = build_fingerprint_marker(source_id)
        for entity in matches:
            about = await get_chat_about(client, entity)
            if marker in about:
                exact_match = entity
                break

    return matches, exact_match



async def get_pinned_ids(client, source_entity):
    """Returns the ids of all pinned messages in the source chat."""
    pinned_ids = set()
    try:
        async for msg in client.iter_messages(source_entity, filter=InputMessagesFilterPinned):
            pinned_ids.add(msg.id)
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't fetch the pinned messages list: {e}[/yellow]")
    return pinned_ids


# ------------------ copy messages ------------------

# ------------------ parallel (fast) transfers ------------------
# Telegram limits how fast ONE stream can go. Both helpers below split a single file
# into many chunks that are requested/sent concurrently, which is typically several
# times faster. Everything is capability-checked at runtime and falls back to plain
# Telethon transfers if anything is unsupported or errors out.

def _accepts_kwargs(func, *names):
    """True if `func` really accepts these keyword arguments (Telethon versions differ)."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def download_target_path(message):
    file_obj = getattr(message, "file", None)
    name = getattr(file_obj, "name", None)
    if name:
        name = re.sub(r"[^\w.\-]+", "_", name).strip("_") or "file"
    else:
        name = f"file{getattr(file_obj, 'ext', '') or ''}"
    return os.path.join(DOWNLOAD_DIR, f"{message.id}_{name}")


async def fast_download_media(client, message, bar=None, task_id=None):
    """Downloads one file with several concurrent chunk streams. Returns the path, or
    None if the fast path isn't applicable (caller then uses the normal download)."""
    size = getattr(getattr(message, "file", None), "size", None)
    if not FAST_TRANSFER or not size or size < FAST_TRANSFER_MIN_MB * 1024 * 1024:
        return None
    if TRANSFER_CONNECTIONS < 2:
        return None
    if not _accepts_kwargs(client.iter_download, "offset", "request_size"):
        return None
    if PROTECTED_SOURCE["on"]:
        # one cheap request buys a valid file reference for all the parallel streams
        message = await refresh_message(client, message)

    request_size = max(4096, min(DOWNLOAD_REQUEST_SIZE_KB, 1024) * 1024)
    request_size -= request_size % 4096
    workers = max(2, min(TRANSFER_CONNECTIONS, 16))

    span = -(-size // workers)
    span = ((span + request_size - 1) // request_size) * request_size
    ranges = []
    offset = 0
    while offset < size:
        ranges.append((offset, min(offset + span, size)))
        offset += span
    if len(ranges) < 2:
        return None

    path = download_target_path(message)
    with open(path, "wb") as fh:
        fh.truncate(size)

    state = {"done": 0}
    if bar is not None:
        bar.reset(task_id, completed=0, total=size, visible=True,
                  label=f"\u2b07 Downloading #{message.id} \u00d7{len(ranges)}")

    async def worker(start, end):
        position = start
        with open(path, "r+b") as fh:
            fh.seek(start)
            async for chunk in client.iter_download(
                message, offset=start, request_size=request_size,
            ):
                if not chunk or position >= end:
                    break
                if position + len(chunk) > end:
                    chunk = chunk[: end - position]
                fh.write(chunk)
                position += len(chunk)
                state["done"] += len(chunk)
                if bar is not None:
                    bar.update(task_id, completed=min(state["done"], size), total=size)
                if position >= end:
                    break

    try:
        await asyncio.gather(*(worker(start, end) for start, end in ranges))
    except BaseException:
        if os.path.exists(path):
            os.remove(path)
        raise
    finally:
        if bar is not None:
            bar.update(task_id, visible=False)

    if os.path.getsize(path) != size:
        os.remove(path)
        return None
    return path


async def fast_upload_file(client, file_path, bar=None, task_id=None, label=""):
    """Uploads one file by sending its parts concurrently. Returns an InputFile(Big) ready
    to be passed to send_file, or None if the fast path isn't applicable."""
    size = os.path.getsize(file_path)
    if not FAST_TRANSFER or size < FAST_TRANSFER_MIN_MB * 1024 * 1024:
        return None
    if TRANSFER_CONNECTIONS < 2:
        return None

    part_size = max(1024, min(UPLOAD_PART_SIZE_KB, 512) * 1024)
    part_size -= part_size % 1024
    total_parts = -(-size // part_size)
    if total_parts > 8000:
        return None  # let Telethon deal with unusually huge files

    is_big = size > 10 * 1024 * 1024
    file_id = generate_random_long()
    md5 = None if is_big else hashlib.md5()
    semaphore = asyncio.Semaphore(max(2, min(TRANSFER_CONNECTIONS, 16)))
    state = {"done": 0}

    if bar is not None:
        bar.reset(task_id, completed=0, total=size, visible=True,
                  label=f"\u2b06 Uploading {label} \u00d7{semaphore._value}")

    async def send_part(index, data):
        async with semaphore:
            if is_big:
                request = functions.upload.SaveBigFilePartRequest(
                    file_id=file_id, file_part=index,
                    file_total_parts=total_parts, bytes=data,
                )
            else:
                request = functions.upload.SaveFilePartRequest(
                    file_id=file_id, file_part=index, bytes=data,
                )
            if not await client(request):
                raise RuntimeError(f"Telegram rejected upload part {index}")
        state["done"] += len(data)
        if bar is not None:
            bar.update(task_id, completed=min(state["done"], size), total=size)

    batch_limit = max(4, min(TRANSFER_CONNECTIONS, 16) * 3)
    tasks = []
    try:
        with open(file_path, "rb") as fh:
            index = 0
            while True:
                data = fh.read(part_size)
                if not data:
                    break
                if md5 is not None:
                    md5.update(data)
                tasks.append(asyncio.create_task(send_part(index, data)))
                index += 1
                if len(tasks) >= batch_limit:
                    await asyncio.gather(*tasks)
                    tasks = []
            if tasks:
                await asyncio.gather(*tasks)
                tasks = []
    except BaseException:
        for task in tasks:
            task.cancel()
        raise
    finally:
        if bar is not None:
            bar.update(task_id, visible=False)

    name = os.path.basename(file_path)
    if is_big:
        return types.InputFileBig(id=file_id, parts=total_parts, name=name)
    return types.InputFile(id=file_id, parts=total_parts, name=name,
                           md5_checksum=md5.hexdigest())


def uploaded_file_kwargs(message, file_path):
    """send_file() can't sniff a pre-uploaded InputFile, so spell out mime type and
    attributes (file name, duration, voice/round flags, ...) ourselves."""
    kwargs = media_upload_kwargs(message)
    attributes = list(kwargs.get("attributes") or [])
    if not any(isinstance(a, types.DocumentAttributeFilename) for a in attributes):
        attributes.append(
            types.DocumentAttributeFilename(file_name=os.path.basename(file_path))
        )
    kwargs["attributes"] = attributes
    if not kwargs.get("mime_type"):
        guessed, _ = mimetypes.guess_type(file_path)
        kwargs["mime_type"] = guessed or "application/octet-stream"
    return kwargs


def resolve_reply_to(message, is_forum, id_map, topic_map):
    """Reply to the copy of the same message it replied to in the source; otherwise just
    drop it into the matching topic."""
    old_top_id = get_message_topic_id(message, is_forum)
    reply_target_old_id = get_reply_target_old_id(message, is_forum)
    reply_to = None
    if reply_target_old_id is not None:
        reply_to = id_map.get(str(reply_target_old_id))
    if reply_to is None:
        reply_to = topic_map.get(old_top_id) if old_top_id else None
    return reply_to


def normalize_peer_id(value):
    """Telegram ids show up in two shapes: the raw id (4378482164) and the "marked"
    id Telethon returns for supergroups/channels (-1004378482164). Anonymous-admin
    posts are sent AS the group, so their sender_id is the marked form while the
    member list gives the raw form - comparing them directly never matches.
    This strips the marker so both shapes can be compared safely."""
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        text = str(-value)
        if text.startswith("100") and len(text) > 3:
            return int(text[3:])
        return -value
    return value


def message_sender_id(message):
    """sender_id, falling back to the chat itself for anonymous-admin / channel posts
    where Telethon reports no sender at all."""
    sender_id = getattr(message, "sender_id", None)
    if sender_id is None:
        sender_id = getattr(message, "chat_id", None)
    return sender_id


def check_filters(message, is_forum, selected_topic_ids, author_filter_id):
    if author_filter_id is not None:
        if normalize_peer_id(message_sender_id(message)) != normalize_peer_id(author_filter_id):
            raise SkipMessage("not from the selected author")
    if getattr(message, "action", None) is not None:
        raise SkipMessage("service message")
    if is_forum and selected_topic_ids is not None:
        effective_top_id = get_message_topic_id(message, is_forum) or 1
        if effective_top_id not in selected_topic_ids:
            raise SkipMessage("not in the selected topics")


async def find_duplicate_in_target(client, target_entity, message, text):
    """Before retrying a message, look at the tail of the destination: a send can time out
    AFTER Telegram already accepted it, and blindly retrying would create a duplicate."""
    src_size = getattr(getattr(message, "file", None), "size", None)
    needle = (text or "").strip()
    try:
        async for m in client.iter_messages(target_entity, limit=DUPLICATE_SCAN_LIMIT):
            m_size = getattr(getattr(m, "file", None), "size", None)
            m_text = (m.raw_text or "").strip()
            if src_size:
                if m_size == src_size and (not needle or m_text == needle):
                    return m
            elif needle and m_text == needle:
                return m
    except Exception:
        return None
    return None


async def verify_sent(client, target_entity, sent_message, has_media):
    """Double-check with Telegram that the message really landed (configurable, because it
    costs one extra API call per message)."""
    if VERIFY_SENT == "never":
        return
    if VERIFY_SENT == "media" and not has_media:
        return
    check = await client.get_messages(target_entity, ids=sent_message.id)
    if check is None:
        raise RuntimeError("verification failed: message not found in the destination after sending")


async def download_media_for_upload(client, message, bar, task_id):
    """Fallback path: download to disk first. Checks the file's REAL size against the free
    space and against Telegram's upload cap before wasting time on it."""
    size_bytes = getattr(getattr(message, "file", None), "size", None) or 0
    size_gb = size_bytes / (1024 ** 3)

    if size_bytes and MAX_FILE_SIZE_GB and size_gb > MAX_FILE_SIZE_GB:
        raise SkipMessage(f"size {size_gb:.2f}GB exceeds MAX_FILE_SIZE_GB")
    if size_bytes and UPLOAD_LIMIT_GB and size_gb > UPLOAD_LIMIT_GB:
        raise SkipMessage(f"size {size_gb:.2f}GB is above this account's {UPLOAD_LIMIT_GB}GB upload limit")
    if not has_enough_disk_space(MIN_FREE_DISK_GB, extra_bytes=size_bytes):
        raise RuntimeError("NOT_ENOUGH_DISK_SPACE")

    if FAST_TRANSFER:
        try:
            path = await fast_download_media(client, message, bar, task_id)
            if path:
                return path
        except FloodWaitError:
            raise
        except Exception as e:
            console.print(f"  [dim]Parallel download failed ({e}); using the single-stream one ...[/dim]")

    bar.reset(task_id, completed=0, total=size_bytes or None, visible=True,
              label=f"\u2b07 Downloading #{message.id}")

    def dl_cb(current, total, _t=task_id):
        bar.update(_t, completed=current, total=total)

    path = await client.download_media(message, file=DOWNLOAD_DIR, progress_callback=dl_cb)
    bar.update(task_id, visible=False)
    return path


async def send_file_media(client, message, target_entity, text, entities, reply_to, bar, task_id):
    """Sends a real attached photo/document. Tries to re-send it by reference first (no
    download, no disk usage, and stickers / voice notes / round videos / gifs keep their
    original type), and only falls back to download + re-upload if that isn't allowed."""
    if direct_media_allowed():
        try:
            bar.reset(task_id, completed=0, total=None, visible=True,
                      label=f"\u21bb Re-sending #{message.id}")
            sent = await client.send_file(
                target_entity, message.media,
                caption=text, formatting_entities=entities or None,
                reply_to=reply_to, force_document=was_sent_as_file(message),
            )
            bar.update(task_id, visible=False)
            return sent
        except (SkipMessage, FloodWaitError):
            raise
        except Exception as e:
            bar.update(task_id, visible=False)
            console.print(f"  [dim]Direct re-send not possible ({e}); downloading instead ...[/dim]")

    file_path = None
    try:
        file_path = await download_media_for_upload(client, message, bar, task_id)
        if not file_path:
            if text:
                return await client.send_message(
                    target_entity, text, formatting_entities=entities or None, reply_to=reply_to,
                )
            raise SkipMessage("media had nothing downloadable and no caption")

        if FAST_TRANSFER:
            try:
                uploaded = await fast_upload_file(
                    client, file_path, bar, task_id, label=f"#{message.id}",
                )
                if uploaded is not None:
                    sent = await client.send_file(
                        target_entity, uploaded,
                        caption=text, formatting_entities=entities or None,
                        reply_to=reply_to, **uploaded_file_kwargs(message, file_path),
                    )
                    return sent
            except FloodWaitError:
                raise
            except Exception as e:
                console.print(f"  [dim]Parallel upload failed ({e}); using the single-stream one ...[/dim]")

        bar.reset(task_id, completed=0, total=None, visible=True,
                  label=f"\u2b06 Uploading #{message.id}")

        def up_cb(current, total, _t=task_id):
            bar.update(_t, completed=current, total=total)

        sent = await client.send_file(
            target_entity, file_path,
            caption=text, formatting_entities=entities or None,
            progress_callback=up_cb, reply_to=reply_to,
            **media_upload_kwargs(message),
        )
        bar.update(task_id, visible=False)
        return sent
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


async def send_special_media(client, message, target_entity, text, entities, reply_to):
    """Copies media that isn't a file at all: polls, locations/venues, contacts and dice.
    These used to be silently dropped. Returns None if the type isn't supported."""
    m = message.media
    media = None

    if isinstance(m, types.MessageMediaPoll):
        poll = m.poll
        media = types.InputMediaPoll(
            poll=types.Poll(
                id=0,
                question=poll.question,
                answers=poll.answers,
                closed=getattr(poll, "closed", None),
                public_voters=getattr(poll, "public_voters", None),
                multiple_choice=getattr(poll, "multiple_choice", None),
                quiz=False,  # a quiz needs its correct answer, which we can't always read
            )
        )
    elif isinstance(m, types.MessageMediaVenue):
        media = types.InputMediaVenue(
            geo_point=types.InputGeoPoint(lat=m.geo.lat, long=m.geo.long),
            title=m.title, address=m.address, provider=m.provider,
            venue_id=m.venue_id, venue_type=m.venue_type,
        )
    elif isinstance(m, (types.MessageMediaGeo, types.MessageMediaGeoLive)):
        media = types.InputMediaGeoPoint(
            geo_point=types.InputGeoPoint(lat=m.geo.lat, long=m.geo.long)
        )
    elif isinstance(m, types.MessageMediaContact):
        media = types.InputMediaContact(
            phone_number=m.phone_number or "", first_name=m.first_name or "",
            last_name=m.last_name or "", vcard=m.vcard or "",
        )
    elif isinstance(m, types.MessageMediaDice):
        media = types.InputMediaDice(emoticon=m.emoticon)

    if media is None:
        return None

    return await client.send_file(
        target_entity, media, caption=text,
        formatting_entities=entities or None, reply_to=reply_to,
    )


async def send_one_message(client, message, target_entity, topic_map, is_forum,
                           selected_topic_ids, id_map, bar, task_id,
                           author_filter_id=None, sender_label=None, duplicate_guard=False):
    """Attempts to send a single message to target_entity. Returns the sent Message object.
    Raises SkipMessage for intentional, non-retryable skips. Any other exception (including
    FloodWaitError) is left for the caller to handle/retry."""
    check_filters(message, is_forum, selected_topic_ids, author_filter_id)

    reply_to = resolve_reply_to(message, is_forum, id_map, topic_map)
    has_file = is_real_file_media(message)
    # A link-preview card is NOT an attachment: those messages go out as plain text, where
    # Telegram allows 4096 chars. Only real captions are capped at 1024. Testing
    # `message.media` here used to truncate long link-preview messages at 1024 for no reason.
    parts = build_message_parts(
        message, sender_label, limit=CAPTION_LIMIT if has_file else TEXT_LIMIT,
    )
    text, entities = parts[0]
    extra_parts = parts[1:]

    if duplicate_guard:
        existing = await find_duplicate_in_target(client, target_entity, message, text)
        if existing is not None:
            console.print(f"  [dim]Message {message.id} was already in the destination; not sending it twice.[/dim]")
            return existing

    if has_file:
        sent_message = await send_file_media(
            client, message, target_entity, text, entities, reply_to, bar, task_id,
        )
    elif message.media is None or is_link_preview_media(message):
        # A link-preview card is NOT an attachment: send the text and let Telegram
        # rebuild the preview in the destination instead of uploading its thumbnail.
        if not text:
            raise SkipMessage("empty message")
        sent_message = await client.send_message(
            target_entity, text, formatting_entities=entities or None,
            reply_to=reply_to, link_preview=True,
        )
    else:
        sent_message = await send_special_media(
            client, message, target_entity, text, entities, reply_to,
        )
        if sent_message is None:
            if text:
                sent_message = await client.send_message(
                    target_entity, text, formatting_entities=entities or None, reply_to=reply_to,
                )
            else:
                raise SkipMessage(f"unsupported media type ({type(message.media).__name__})")

    if isinstance(sent_message, list):  # send_file can return a list
        sent_message = sent_message[0]

    if extra_parts:
        await send_text_continuations(
            client, target_entity, getattr(sent_message, "id", None), extra_parts, reply_to,
        )

    await verify_sent(client, target_entity, sent_message, has_file)
    return sent_message


async def maybe_pin(client, message, sent_message, target_entity, pinned_ids):
    if message.id in pinned_ids:
        try:
            await client.pin_message(target_entity, sent_message, notify=False)
            console.print(f"  [cyan]\U0001F4CC Message {message.id} pinned.[/cyan]")
            return True
        except Exception as e:
            console.print(f"  [yellow]\u26a0 Couldn't pin message {message.id}: {e}[/yellow]")
    return False


async def process_single_message(client, message, target_entity, topic_map, is_forum,
                                  selected_topic_ids, id_map, pinned_ids, bar, task_id,
                                  author_filter_id=None, sender_label=None, duplicate_guard=False):
    """Tries to send one message, retrying on transient errors. Returns one of:
    ('sent', sent_message), ('skipped', None), ('failed', None), ('disk_full', None).
    On success, records the id mapping (for reply simulation) and pins it if needed."""
    attempt = 0
    while True:
        try:
            sent_message = await send_one_message(
                client, message, target_entity, topic_map, is_forum, selected_topic_ids,
                id_map, bar, task_id, author_filter_id=author_filter_id,
                sender_label=sender_label,
                # after a failed attempt the message may already be there
                duplicate_guard=duplicate_guard or attempt > 0,
            )
            id_map[str(message.id)] = sent_message.id
            await maybe_pin(client, message, sent_message, target_entity, pinned_ids)
            clear_failure_reason(message.id)
            return "sent", sent_message

        except SkipMessage as e:
            note_skip_reason(message.id, str(e))
            return "skipped", None

        except FloodWaitError as e:
            rate_report_flood(getattr(e, "seconds", 0))
            console.print(f"[yellow]Telegram asked us to slow down; waiting {e.seconds}s...[/yellow]")
            await asyncio.sleep(e.seconds + 5)
            # doesn't count as a retry attempt; just try this message again

        except Exception as e:
            if str(e) == "NOT_ENOUGH_DISK_SPACE":
                console.print("[red]Not enough disk space; operation stopped. Free up space and run again.[/red]")
                note_failure_reason(message.id, "not enough free disk space")
                return "disk_full", None

            attempt += 1
            if attempt < MESSAGE_RETRIES:
                console.print(
                    f"[yellow]Attempt {attempt}/{MESSAGE_RETRIES} failed for message id={message.id}: "
                    f"{e} -> retrying in {MESSAGE_RETRY_BACKOFF}s ...[/yellow]"
                )
                await asyncio.sleep(MESSAGE_RETRY_BACKOFF)
            else:
                reason = describe_error(e)
                note_failure_reason(message.id, reason)
                console.print(
                    f"[red]Giving up on message id={message.id} after {MESSAGE_RETRIES} attempts: "
                    f"{reason}[/red]"
                )
                return "failed", None


async def process_album(client, messages, target_entity, topic_map, is_forum,
                        selected_topic_ids, id_map, pinned_ids, bar, task_id,
                        author_filter_id=None, sender_labels=None):
    """Sends a grouped media album (same grouped_id) as ONE album, so photo sets don't get
    split into separate messages with repeated captions. Falls back to one-by-one."""
    sender_labels = sender_labels or {}
    first = messages[0]
    try:
        check_filters(first, is_forum, selected_topic_ids, author_filter_id)
    except SkipMessage:
        return "skipped", []

    reply_to = resolve_reply_to(first, is_forum, id_map, topic_map)
    files, captions, leftovers = [], [], []
    for m in messages:
        files.append(m.media)
        parts = build_message_parts(m, sender_labels.get(m.id), limit=CAPTION_LIMIT)
        captions.append(parts[0][0])
        if len(parts) > 1:
            leftovers.append((m.id, parts[1:]))
    # an album goes out in a single call, so it is all-files or all-media: only force
    # documents when every item was attached as an uncompressed file
    album_force_document = all(was_sent_as_file(m) for m in messages)

    # A content-protected chat rejects an album built from media references, so download the
    # items first and send the album from real files. That keeps the album grouping instead of
    # silently degrading into separate one-by-one messages.
    album_paths = []
    album_prepare_error = None
    if PROTECTED_SOURCE["on"]:
        try:
            for m in messages:
                path = await download_media_for_upload(client, m, bar, task_id)
                if not path:
                    raise RuntimeError(f"nothing downloadable in message {m.id}")
                album_paths.append(path)
            files = album_paths
        except FloodWaitError:
            cleanup_paths(album_paths)
            raise
        except Exception as e:
            cleanup_paths(album_paths)
            album_paths = []
            if isinstance(e, RuntimeError) and str(e) == "NOT_ENOUGH_DISK_SPACE":
                bar.update(task_id, visible=False)
                console.print("[red]Not enough free disk space to prepare this album.[/red]")
                return "disk_full", []
            files = []
            album_prepare_error = e

    try:
        if not files:
            raise RuntimeError(f"couldn't prepare the album's files ({album_prepare_error})")
        bar.reset(task_id, completed=0, total=None, visible=True,
                  label=f"\U0001F5BC Album of {len(files)} (#{first.id})")
        sent = await client.send_file(
            target_entity, files, caption=captions,
            reply_to=reply_to, parse_mode=None, force_document=album_force_document,
        )
        bar.update(task_id, visible=False)
        if not isinstance(sent, list):
            sent = [sent]
        for src, dst in zip(messages, sent):
            id_map[str(src.id)] = dst.id
            await maybe_pin(client, src, dst, target_entity, pinned_ids)
        for src_id, parts in leftovers:
            await send_text_continuations(
                client, target_entity, id_map.get(str(src_id)), parts, reply_to,
            )
        return "sent", sent
    except FloodWaitError as e:
        rate_report_flood(getattr(e, "seconds", 0))
        bar.update(task_id, visible=False)
        console.print(f"[yellow]Telegram asked us to slow down; waiting {e.seconds}s...[/yellow]")
        await asyncio.sleep(e.seconds + 5)
    except Exception as e:
        bar.update(task_id, visible=False)
        console.print(f"[yellow]\u26a0 Couldn't send this album as one group ({e}); sending its items one by one.[/yellow]")
    finally:
        cleanup_paths(album_paths)

    sent_list, statuses = [], []
    for m in messages:
        status, sent_message = await process_single_message(
            client, m, target_entity, topic_map, is_forum, selected_topic_ids,
            id_map, pinned_ids, bar, task_id, author_filter_id=author_filter_id,
            sender_label=sender_labels.get(m.id),
        )
        statuses.append(status)
        if sent_message is not None:
            sent_list.append(sent_message)
        if status == "disk_full":
            return "disk_full", sent_list

    if "sent" in statuses:
        return "sent", sent_list
    if "failed" in statuses:
        return "failed", sent_list
    return "skipped", sent_list


async def process_message_group(client, messages, target_entity, topic_map, is_forum,
                               selected_topic_ids, id_map, pinned_ids, bar, task_id,
                               author_filter_id=None, sender_labels=None):
    sender_labels = sender_labels or {}
    if len(messages) == 1:
        message = messages[0]
        status, sent_message = await process_single_message(
            client, message, target_entity, topic_map, is_forum, selected_topic_ids,
            id_map, pinned_ids, bar, task_id, author_filter_id=author_filter_id,
            sender_label=sender_labels.get(message.id),
        )
        return status, ([sent_message] if sent_message is not None else [])

    return await process_album(
        client, messages, target_entity, topic_map, is_forum, selected_topic_ids,
        id_map, pinned_ids, bar, task_id, author_filter_id=author_filter_id,
        sender_labels=sender_labels,
    )


def topic_scan_ids(client, is_forum, selected_topic_ids):
    """Which topics can be pulled straight from Telegram (a huge speedup when only a few
    topics are selected), or None when we have no choice but to walk the whole history."""
    if not TOPIC_TARGETED_SCAN or not is_forum or not selected_topic_ids:
        return None
    ids = set(selected_topic_ids)
    if 1 in ids:
        return None  # the General topic has no reply_to id of its own
    if len(ids) > TOPIC_SCAN_MAX:
        return None
    if not _accepts_kwargs(client.iter_messages, "reply_to"):
        return None
    return sorted(ids)


async def topics_are_scannable(client, source_entity, topic_ids):
    """One cheap probe per topic: if Telegram refuses a targeted query, fall back safely."""
    for topic_id in topic_ids:
        try:
            await client.get_messages(source_entity, limit=1, reply_to=topic_id)
        except Exception:
            return False
    return True


async def _topic_message_stream(client, source_entity, topic_id, offset_id, stop_before):
    async for message in client.iter_messages(
        source_entity, reverse=True, offset_id=offset_id, reply_to=topic_id,
    ):
        if stop_before is not None and message.id >= stop_before:
            return
        yield message


async def iter_source_messages(client, source_entity, offset_id=0, stop_before=None, topic_ids=None,
                               topic_offsets=None):
    """Yields source messages oldest-first.

    With `topic_ids`, one stream per topic is opened and the streams are merged by message
    id, so the emitted order is identical to a full scan - which keeps `last_id`, reply
    mapping and resuming working exactly as before, while Telegram only sends the messages
    we actually want instead of the entire chat."""
    if topic_ids:
        streams, heads = {}, {}
        for topic_id in topic_ids:
            streams[topic_id] = _topic_message_stream(
                client, source_entity, topic_id,
                (topic_offsets or {}).get(topic_id, offset_id), stop_before,
            )
        for topic_id, stream in list(streams.items()):
            try:
                heads[topic_id] = await stream.__anext__()
            except StopAsyncIteration:
                streams.pop(topic_id, None)
        while heads:
            topic_id = min(heads, key=lambda key: heads[key].id)
            yield heads.pop(topic_id)
            stream = streams.get(topic_id)
            if stream is None:
                continue
            try:
                heads[topic_id] = await stream.__anext__()
            except StopAsyncIteration:
                streams.pop(topic_id, None)
        return

    async for message in client.iter_messages(source_entity, reverse=True, offset_id=offset_id):
        if stop_before is not None and message.id >= stop_before:
            return
        yield message


async def iter_message_groups(client, source_entity, offset_id=0, stop_before=None, topic_ids=None,
                              topic_offsets=None):
    """Like iter_messages(reverse=True), but yields LISTS: consecutive messages sharing a
    grouped_id (an album) come out together so they can be re-sent as one album."""
    buffer = []
    async for message in iter_source_messages(
        client, source_entity, offset_id=offset_id, stop_before=stop_before, topic_ids=topic_ids,
        topic_offsets=topic_offsets,
    ):
        gid = getattr(message, "grouped_id", None)
        if (buffer and gid is not None
                and getattr(buffer[-1], "grouped_id", None) == gid
                and len(buffer) < ALBUM_MAX_ITEMS):
            buffer.append(message)
            continue
        if buffer:
            yield buffer
        buffer = [message]
    if buffer:
        yield buffer


async def catch_up_new_topics(client, source_entity, target_entity, topic_map, is_forum,
                               progress_key, progress_file, new_topic_ids, until_id,
                               show_sender_names=False):
    """Used when new topics are added to a chat that's already partly archived: scans from
    the very beginning up to (but not including) until_id, copying only messages that
    belong to new_topic_ids, so earlier messages in the newly-added topics aren't missed."""
    ensure_not_same_chat(source_entity, target_entity, where="catch-up on new topics")
    announce_protected_source(source_entity)
    saved = load_progress(progress_file).get(progress_key, {})
    id_map = saved.get("id_map", {})
    label_cache = {}

    with console.status("[cyan]Fetching pinned messages ..."):
        pinned_ids = await get_pinned_ids(client, source_entity)

    count = 0
    scan_topic_ids = topic_scan_ids(client, is_forum, new_topic_ids)
    if scan_topic_ids and not await topics_are_scannable(client, source_entity, scan_topic_ids):
        scan_topic_ids = None
    console.rule(f"[bold {UI['accent']}]\u21bb  Catching up on newly added topics[/bold {UI['accent']}]",
                 style=UI["violet"])
    with make_transfer_progress() as bar:
        task_id = bar.add_task("idle", label="", total=None, visible=False)
        async for group in iter_message_groups(client, source_entity, offset_id=0,
                                               stop_before=until_id, topic_ids=scan_topic_ids):
            sender_labels = await build_sender_labels(
                client, group, show_sender_names, is_forum, label_cache
            )

            status, _ = await process_message_group(
                client, group, target_entity, topic_map, is_forum, new_topic_ids,
                id_map, pinned_ids, bar, task_id, sender_labels=sender_labels,
            )
            if status == "disk_full":
                break
            if status == "sent":
                count += len(group)
                console.print(f"[green]\u2714[/green] (catch-up) copied {len(group)} message(s) (id={group[-1].id})")

            saved["id_map"] = id_map
            save_progress(progress_file, progress_key, saved, force=False)
            # only pause after messages we really tried to send; skipped ones cost nothing
            if status in ("sent", "failed"):
                await rate_sleep()

    saved["id_map"] = id_map
    save_progress(progress_file, progress_key, saved, force=True)
    console.print(f"[cyan]Catch-up done: {count} older message(s) copied for the new topic(s).[/cyan]")
    return count


async def auto_retry_pass(client, source_entity, target_entity, topic_map, is_forum,
                          selected_topic_ids, id_map, pinned_ids, failed_ids,
                          author_filter_id=None, show_sender_names=False):
    """One automatic re-try of everything that failed during the run that just ended.
    Most failures are temporary (a dropped connection, a flood wait), so this single
    extra pass usually clears them without the user doing anything.
    Returns (fixed_count, still_failed_ids)."""
    ids = list(dict.fromkeys(failed_ids))
    if not ids:
        return 0, []

    console.rule(
        f"[bold {UI['accent']}]\u21bb  Automatic retry of {len(ids)} failed message(s)"
        f"[/bold {UI['accent']}]", style=UI["violet"],
    )
    try:
        messages = await client.get_messages(source_entity, ids=ids)
    except Exception as e:
        console.print(f"[yellow]Couldn't re-fetch the failed messages: {e}[/yellow]")
        return 0, ids

    live = [m for m in messages if m is not None]
    fixed = 0
    still_failed = []
    label_cache = {}

    with make_transfer_progress() as bar:
        task_id = bar.add_task("idle", label="", total=None, visible=False)
        for index, message in enumerate(live):
            sender_label = None
            retry_topic_id = (get_message_topic_id(message, is_forum) or 1) if is_forum else None
            if wants_sender_names(show_sender_names, retry_topic_id):
                sender_label = await resolve_sender_label(client, message, label_cache)

            status, _ = await process_single_message(
                client, message, target_entity, topic_map, is_forum, selected_topic_ids,
                id_map, pinned_ids, bar, task_id,
                author_filter_id=author_filter_id, sender_label=sender_label,
                duplicate_guard=True,
            )
            if status == "sent":
                fixed += 1
                rate_report_ok()
                console.print(f"[green]\u2714[/green] recovered message id={message.id}")
            elif status == "failed":
                still_failed.append(message.id)
            elif status == "disk_full":
                console.print("[red]Stopping the automatic retry: not enough disk space.[/red]")
                still_failed.extend(m.id for m in live[index:])
                break

            await rate_sleep()

    return fixed, list(dict.fromkeys(still_failed))


async def copy_all_messages(client, source_entity, target_entity, topic_map, is_forum, progress_key,
                            progress_file, source_name, selected_topic_ids=None,
                            author_filter_id=None, show_sender_names=False, limit_last_n=None,
                            start_from_id=None):
    ensure_not_same_chat(source_entity, target_entity, where="copy run")
    announce_protected_source(source_entity)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    reset_oversized_skips()

    saved = load_progress(progress_file).get(progress_key, {})
    last_id = saved.get("last_id", 0)
    if last_id:
        console.print(f"[cyan]Resuming after message id={last_id} ...[/cyan]")
    if isinstance(show_sender_names, dict):
        on_topics = [tid for tid, v in show_sender_names.items() if v]
        if on_topics:
            console.print(
                f"[cyan]Sender names will be added in {len(on_topics)} of "
                f"{len(show_sender_names)} selected topic(s).[/cyan]"
            )
    elif show_sender_names:
        console.print("[cyan]Sender names will be added at the top of each copied message.[/cyan]")

    with console.status("[cyan]Fetching pinned messages ..."):
        pinned_ids = await get_pinned_ids(client, source_entity)
    if pinned_ids:
        console.print(f"[cyan]{len(pinned_ids)} pinned message(s) found in the source; they'll be pinned in the destination too after copying.[/cyan]")

    scan_topic_ids = topic_scan_ids(client, is_forum, selected_topic_ids)
    if scan_topic_ids and not await topics_are_scannable(client, source_entity, scan_topic_ids):
        scan_topic_ids = None
    if scan_topic_ids:
        console.print(
            f"[cyan]Fetching only the {len(scan_topic_ids)} selected topic(s) directly from "
            f"Telegram instead of scanning the whole chat.[/cyan]"
        )
    else:
        console.print(
            "[dim]Walking the full chat history from the oldest message; on very large chats "
            "the first messages can take a minute to show up ...[/dim]"
        )

    # ---- "only the last N messages" window ----
    # Instead of walking everything, jump straight to the id where the last N begin.
    # With targeted topic scans that window is worked out per topic, so "last 100"
    # really means the last 100 OF EACH selected topic.
    start_id = last_id
    topic_offsets = None
    per_topic_limit = False
    if limit_last_n:
        if scan_topic_ids:
            per_topic_limit = True
            topic_offsets = {}
            with console.status(f"[{UI['accent']}]Locating the last {limit_last_n} message(s) per topic ..."):
                for topic_id in scan_topic_ids:
                    window_start = await last_n_start_id(client, source_entity, limit_last_n,
                                                         topic_id=topic_id)
                    topic_offsets[topic_id] = max(last_id, window_start or 0)
            console.print(
                f"[cyan]Only the last {limit_last_n} message(s) of each selected topic "
                f"will be copied.[/cyan]"
            )
        else:
            single_topic = None
            if is_forum and selected_topic_ids and len(selected_topic_ids) == 1:
                only = next(iter(selected_topic_ids))
                single_topic = only if only != 1 else None
            with console.status(f"[{UI['accent']}]Locating the last {limit_last_n} message(s) ..."):
                window_start = await last_n_start_id(client, source_entity, limit_last_n,
                                                     topic_id=single_topic)
            start_id = max(last_id, window_start or 0)
            console.print(f"[cyan]Only the last {limit_last_n} message(s) will be copied.[/cyan]")

    # ---- "start from this message" window ----
    # offset_id is exclusive when walking forwards, so subtract one to INCLUDE the
    # message the user linked to.
    if start_from_id:
        floor_id = max(0, int(start_from_id) - 1)
        if last_id and last_id > floor_id:
            console.print(
                "[yellow]\u26a0 Saved progress for this chat is already past that message, so "
                "some messages may be copied twice.[/yellow]"
            )
        start_id = max(start_id, floor_id) if limit_last_n else floor_id
        if topic_offsets:
            topic_offsets = {tid: max(v, floor_id) for tid, v in topic_offsets.items()}
        console.print(
            f"[cyan]Starting at message id={start_from_id} \u2014 everything from there on "
            f"will be copied.[/cyan]"
        )

    expected_total = await expected_run_total(
        client, source_entity, is_forum, selected_topic_ids, limit_last_n, per_topic_limit
    )
    tracker = RunProgress(expected_total)
    if expected_total:
        console.print(f"[{UI['dim']}]About {expected_total:,} message(s) to go through.[/{UI['dim']}]")

    count = 0
    skipped = 0
    pinned_done = 0
    albums = 0
    failed_ids = list(saved.get("failed_ids", []))
    id_map = saved.get("id_map", {})
    label_cache = {}

    try:
        with make_transfer_progress() as bar:
            task_id = bar.add_task("idle", label="", total=None, visible=False)
            async for group in iter_message_groups(client, source_entity, offset_id=start_id,
                                                   topic_ids=scan_topic_ids,
                                                   topic_offsets=topic_offsets):
                sender_labels = await build_sender_labels(
                    client, group, show_sender_names, is_forum, label_cache
                )

                status, sent_list = await process_message_group(
                    client, group, target_entity, topic_map, is_forum, selected_topic_ids,
                    id_map, pinned_ids, bar, task_id, author_filter_id=author_filter_id,
                    sender_labels=sender_labels,
                )

                if status == "disk_full":
                    break
                elif status == "sent":
                    count += len(sent_list) or len(group)
                    if len(group) > 1:
                        albums += 1
                    rate_report_ok()
                    console.print(
                        f"[green]\u2714[/green] {count} copied so far "
                        f"({'album of ' + str(len(group)) if len(group) > 1 else 'message'}, id={group[-1].id})"
                    )
                    for m in group:
                        if m.id in pinned_ids:
                            pinned_done += 1
                        if m.id in failed_ids:
                            failed_ids.remove(m.id)
                elif status == "failed":
                    skipped += len(group)
                    for m in group:
                        if m.id not in failed_ids:
                            failed_ids.append(m.id)
                # status == "skipped": nothing extra to record

                tracker.tick(len(group))
                tracker.maybe_print()

                saved["last_id"] = group[-1].id
                saved["failed_ids"] = failed_ids
                saved["id_map"] = id_map
                saved["show_sender_names"] = show_sender_names
                save_progress(progress_file, progress_key, saved, force=False)

                # only pause after messages we really tried to send. Sleeping on every
                # filtered-out message used to turn a 16-message topic into hours of waiting.
                if status in ("sent", "failed"):
                    await rate_sleep()
    finally:
        saved["failed_ids"] = failed_ids
        saved["id_map"] = id_map
        save_progress(progress_file, progress_key, saved, force=True)

    tracker.maybe_print(force=True)

    # ---- one automatic retry pass before reporting anything as failed ----
    auto_fixed = 0
    if AUTO_RETRY_AT_END and failed_ids:
        auto_fixed, failed_ids = await auto_retry_pass(
            client, source_entity, target_entity, topic_map, is_forum, selected_topic_ids,
            id_map, pinned_ids, failed_ids,
            author_filter_id=author_filter_id, show_sender_names=show_sender_names,
        )
        count += auto_fixed
        skipped = max(0, skipped - auto_fixed)
        saved["failed_ids"] = failed_ids
        saved["id_map"] = id_map
        save_progress(progress_file, progress_key, saved, force=True)

    elapsed_text = fmt_duration(tracker.elapsed)
    result_text = (
        f"Copied [bold green]{count}[/bold green] message(s) total."
        + (f"\n[cyan]{albums}[/cyan] album(s) kept grouped." if albums else "")
        + (f"\n[cyan]{pinned_done}[/cyan] message(s) pinned." if pinned_done else "")
        + (f"\n[green]{auto_fixed}[/green] message(s) recovered by the automatic retry." if auto_fixed else "")
        + (f"\n[yellow]{skipped}[/yellow] message(s) failed after {MESSAGE_RETRIES} attempts." if skipped else "")
        + f"\n[{UI['dim']}]took {elapsed_text} \u00b7 ended at {rate_summary()}[/{UI['dim']}]"
    )
    too_big = oversized_skips()
    if failed_ids:
        result_text += (
            f"\n[red]Failed message ids:[/red] {', '.join(str(i) for i in failed_ids)}"
            f"\n[dim]Use \"Retry failed messages\" from the main menu to try these again.[/dim]"
        )
    if too_big:
        result_text += (
            f"\n[{UI['gold']}]{len(too_big)}[/{UI['gold']}] message(s) were skipped because their "
            f"file is too large to send."
        )
    if failed_ids or too_big:
        log_path = log_failed_links(source_entity, source_name, failed_ids,
                                    context="copy run", oversized_ids=too_big)
        if log_path:
            result_text += (
                f"\n[dim]Their links \u2014 each with the reason \u2014 were appended to "
                f"{os.path.basename(log_path)}.[/dim]"
            )
    console.print(Panel(result_text, title=f"[bold {UI['accent']}]\u2714  RESULT[/bold {UI['accent']}]",
                        border_style=UI["accent"], box=box.DOUBLE_EDGE, padding=(1, 4),
                        expand=True))

    summary_lines = [
        f"Source: {source_name}",
        f"Copied: {count} message(s)",
        f"Duration: {elapsed_text}",
    ]
    if auto_fixed:
        summary_lines.append(f"Recovered by auto-retry: {auto_fixed}")
    if limit_last_n:
        summary_lines.append(f"Limit: last {limit_last_n} message(s)")
    if failed_ids:
        summary_lines.append(f"Still failing: {len(failed_ids)} (see failed_messages.txt)")
    else:
        summary_lines.append("No failed messages \u2705")
    await notify_finished(client, "Archive run finished", "\n".join(summary_lines))

# ------------------ main copy flow for one chat ------------------

async def resolve_or_create_target(client, selected, is_broadcast, is_forum, selected_topic_ids):
    """Finds an existing destination chat for `selected` (via the hidden bio marker, or by
    asking) or creates a new one. Returns (target_entity, topic_map, reused_without_log).
    Returns (None, None, False) if the user cancels. Does not touch progress.json."""
    kind = "channel" if is_broadcast else "group"

    candidates, exact_match = await find_existing_target(
        client, selected.name, is_broadcast, source_id=selected.entity.id
    )
    candidates = [c for c in candidates if c.id != selected.entity.id]
    if exact_match is not None and exact_match.id == selected.entity.id:
        exact_match = None

    target_entity = None
    if exact_match is not None:
        console.print(
            f"[green]\u2714[/green] Found an exact match via the hidden bio marker: "
            f"{display_text(exact_match.title)} (id {exact_match.id}). Reusing it automatically."
        )
        target_entity = exact_match
    elif candidates:
        pick_choices = [
            questionary.Choice(title=f"Reuse: {display_text(c.title)} (id {c.id})", value=c)
            for c in candidates
        ]
        pick_choices.append(questionary.Choice(title="Create a brand-new chat instead", value="__create__"))
        pick_choices.append(back_choice())
        picked = await questionary.select(
            f"Found {len(candidates)} existing {kind}(s) named \u00ab{display_text(selected.name)}\u00bb, but none "
            f"carry this source chat's marker (they may be unrelated chats that just share the name). "
            f"Reuse one anyway, or create new?",
            choices=pick_choices, style=qstyle,
        ).ask_async()
        if picked is None or picked == BACK:
            return BACK, None, False
        target_entity = None if picked == "__create__" else picked

    if target_entity is not None:
        console.print(f"[cyan]Reusing existing {kind}: {display_text(target_entity.title)}[/cyan]")
        topic_map = {1: 1}
        if is_forum and not is_broadcast:
            console.print("[cyan]Checking which topics already exist there ...[/cyan]")
            existing_titles = await get_target_topics_by_title(client, target_entity)
            topic_map = await sync_topics(
                client, selected.entity, target_entity, selected_topic_ids, existing_titles
            )
        return target_entity, topic_map, True  # True = reused, no progress log for it yet

    confirmed = await questionary.confirm(
        f"A new {kind} named \u00ab{display_text(selected.name)}\u00bb will be created and messages will be copied there. Confirm? (No = go back one step)",
        style=qstyle, default=True,
    ).ask_async()
    if not confirmed:
        return BACK, None, False

    target_entity, topic_map = await create_matching_chat(
        client, selected.entity, selected.name, selected_topic_ids=selected_topic_ids
    )
    return target_entity, topic_map, False


async def run_copy_flow(client, progress_file):
    """Main copy flow. Every question offers Back: it steps one screen backwards
    (topics <- sender-name <- destination) or back to the chat list / main menu."""
    while True:
        selected = await choose_dialog(client)
        if selected is None:
            return
        await print_source_summary(client, selected)

        progress_key = str(selected.id)
        saved = load_progress(progress_file).get(progress_key, {})

        if saved.get("target_id"):
            console.print("[cyan]A destination was already created for this chat; continuing with it.[/cyan]")
            target_entity = await client.get_entity(saved["target_id"])
            topic_map = {int(k): v for k, v in saved.get("topic_map", {}).items()}
            is_forum = saved.get("is_forum", False)
            selected_topic_ids = saved.get("selected_topic_ids")
            if selected_topic_ids is not None:
                selected_topic_ids = set(selected_topic_ids)

            show_sender_names = await choose_sender_prefix_setting(client, selected.entity, is_forum, selected_topic_ids)
            if show_sender_names == BACK:
                continue  # \u2b05 back to the chat list

            limit_last_n = await choose_message_limit("this chat")
            if limit_last_n == BACK:
                continue  # \u2b05 back to the chat list

            start_from_id = await choose_start_point(client, selected.entity, "this chat")
            if start_from_id == BACK:
                continue  # \u2b05 back to the chat list

            if is_forum:
                new_topic_ids = await choose_additional_topics(client, selected.entity, selected_topic_ids)
                if new_topic_ids == BACK:
                    continue  # \u2b05 back to the chat list
                if new_topic_ids:
                    console.print(f"[cyan]Adding {len(new_topic_ids)} new topic(s) ...[/cyan]")
                    existing_titles = await get_target_topics_by_title(client, target_entity)
                    new_topic_map = await sync_topics(
                        client, selected.entity, target_entity, new_topic_ids, existing_titles
                    )
                    topic_map.update(new_topic_map)
                    selected_topic_ids = selected_topic_ids | new_topic_ids
                    if isinstance(show_sender_names, dict):
                        # ask the same Yes/No question for the topics that were just added
                        extra = await sender_prefix_for_topics(
                            client, selected.entity, sorted(new_topic_ids)
                        )
                        if extra == BACK:
                            extra = {tid: False for tid in new_topic_ids}
                        show_sender_names.update(extra)

                    current_last_id = saved.get("last_id", 0)
                    if current_last_id:
                        await catch_up_new_topics(
                            client, selected.entity, target_entity, topic_map, is_forum,
                            progress_key, progress_file, new_topic_ids, current_last_id,
                            show_sender_names=show_sender_names,
                        )

                    saved["selected_topic_ids"] = list(selected_topic_ids)
                    saved["topic_map"] = topic_map

            saved["show_sender_names"] = show_sender_names
            save_progress(progress_file, progress_key, saved, force=True)
        else:
            is_broadcast = isinstance(selected.entity, Channel) and selected.entity.broadcast
            is_forum = getattr(selected.entity, "forum", False)

            step = "topics"
            selected_topic_ids = None
            show_sender_names = False
            limit_last_n = None
            start_from_id = None
            back_to_chats = False
            while True:
                if step == "topics":
                    selected_topic_ids = await choose_topics(client, selected.entity)
                    if selected_topic_ids == BACK:
                        back_to_chats = True
                        break
                    step = "amount"
                elif step == "amount":
                    limit_last_n = await choose_message_limit(
                        "the selected topic(s)" if selected_topic_ids else "this chat"
                    )
                    if limit_last_n == BACK:
                        if is_forum:
                            step = "topics"
                            continue
                        back_to_chats = True
                        break
                    step = "start"
                elif step == "start":
                    start_from_id = await choose_start_point(
                        client, selected.entity,
                        "the selected topic(s)" if selected_topic_ids else "this chat",
                    )
                    if start_from_id == BACK:
                        step = "amount"
                        continue
                    step = "sender"
                elif step == "sender":
                    show_sender_names = await choose_sender_prefix_setting(client, selected.entity, is_forum, selected_topic_ids)
                    if show_sender_names == BACK:
                        step = "start"
                        continue
                    step = "target"
                elif step == "target":
                    target_entity, topic_map, reused_without_log = await resolve_or_create_target(
                        client, selected, is_broadcast, is_forum, selected_topic_ids
                    )
                    if target_entity == BACK or target_entity is None:
                        step = "sender"  # \u2b05 one screen back
                        continue
                    break
            if back_to_chats:
                continue  # \u2b05 back to the chat list

            if reused_without_log:
                console.print(
                    "[yellow]No saved progress log found for this chat, so copying will start from the "
                    "beginning. If this chat/topic was already partly archived before, you may get some "
                    "duplicate messages.[/yellow]"
                )

            saved = {
                "last_id": 0,
                "target_id": target_entity.id,
                "topic_map": topic_map,
                "is_forum": is_forum,
                "selected_topic_ids": list(selected_topic_ids) if selected_topic_ids is not None else None,
                "show_sender_names": show_sender_names,
            }
            save_progress(progress_file, progress_key, saved, force=True)

        await copy_all_messages(
            client, selected.entity, target_entity, topic_map, is_forum,
            progress_key, progress_file, selected.name, selected_topic_ids=selected_topic_ids,
            show_sender_names=show_sender_names, limit_last_n=limit_last_n,
            start_from_id=start_from_id,
        )
        return

async def verify_archive(client, progress_file):
    """Health check for an archive that's already been copied: counts the messages in the
    source and in the destination (per topic when the chat has topics) and shows where
    something is missing. Destinations can legitimately hold a few MORE messages, because
    very long texts are split into follow-up messages."""
    all_data = load_progress(progress_file)
    entries = {k: v for k, v in all_data.items() if v.get("target_id")}
    if not entries:
        console.print("[yellow]No finished archive to check yet.[/yellow]")
        return

    choices = []
    with console.status("[cyan]Fetching chat names ..."):
        for progress_key, data in entries.items():
            try:
                chat_id, author_id = parse_progress_key(progress_key)
                entity = await client.get_entity(chat_id)
                name = getattr(entity, "title", None) or getattr(entity, "first_name", str(chat_id))
                if author_id is not None:
                    name = f"{name}  [author-filtered]"
            except Exception:
                name = f"(key {progress_key} - may no longer be accessible)"
            choices.append(questionary.Choice(title=display_text(name), value=progress_key))
    choices.append(back_choice())

    pick = await questionary.select(
        "Which archive do you want to check?", choices=choices, style=qstyle,
    ).ask_async()
    if not pick or pick == BACK:
        return

    data = entries[pick]
    try:
        source_chat_id, _author_id = parse_progress_key(pick)
        source_entity = await client.get_entity(source_chat_id)
        target_entity = await client.get_entity(data["target_id"])
    except Exception as e:
        console.print(f"[red]Couldn't open the source/destination chat: {e}[/red]")
        return

    is_forum = data.get("is_forum", False)
    topic_map = {int(k): v for k, v in data.get("topic_map", {}).items()}

    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 3),
                  border_style=UI["dim"], header_style=f"bold {UI['accent2']}",
                  title="\U0001FA7A  ARCHIVE HEALTH CHECK", title_style=f"bold {UI['accent']}")
    table.add_column("Topic", style="bold white")
    table.add_column("Source", justify="right")
    table.add_column("Destination", justify="right")
    table.add_column("Status", justify="right")

    missing_total = 0
    unknown = False

    def row_for(label, src_count, dst_count):
        nonlocal missing_total, unknown
        if not isinstance(src_count, int) or not isinstance(dst_count, int):
            unknown = True
            status = f"[{UI['dim']}]can't tell[/{UI['dim']}]"
        elif dst_count >= src_count:
            status = "[bright_green]complete[/bright_green]"
        else:
            gap = src_count - dst_count
            missing_total += gap
            status = f"[bold red]{gap} missing[/bold red]"
        table.add_row(display_text(label), fmt_count(src_count), fmt_count(dst_count), status)

    with console.status(f"[{UI['accent']}]Counting messages on both sides ...", spinner="dots"):
        if is_forum and topic_map:
            titles = await fetch_topic_titles(client, source_entity)
            for source_topic_id, target_topic_id in sorted(topic_map.items()):
                src_count = await count_messages(client, source_entity, source_topic_id)
                dst_count = await count_messages(client, target_entity, target_topic_id)
                row_for(titles.get(source_topic_id, f"topic {source_topic_id}"), src_count, dst_count)
        else:
            src_count = await count_messages(client, source_entity)
            dst_count = await count_messages(client, target_entity)
            row_for(getattr(source_entity, "title", "whole chat"), src_count, dst_count)

    console.print()
    console.print(table)

    notes = []
    if missing_total:
        notes.append(f"[bold red]{missing_total}[/bold red] message(s) look like they never arrived.")
        notes.append(f"[{UI['dim']}]Run this chat again from the main menu \u2014 already-copied "
                     f"messages are skipped, so only the gaps get filled.[/{UI['dim']}]")
    else:
        notes.append("[bright_green]Everything the counter can see has arrived.[/bright_green]")
    failed_here = data.get("failed_ids") or []
    if failed_here:
        notes.append(f"[{UI['gold']}]{len(failed_here)} message(s) are still on the failed list.[/{UI['gold']}]")
    if unknown:
        notes.append(f"[{UI['dim']}]Some counters weren't available; those rows are inconclusive.[/{UI['dim']}]")
    notes.append(f"[{UI['dim']}]A destination with MORE messages is normal: very long texts are "
                 f"split into follow-up messages.[/{UI['dim']}]")

    console.print(Panel("\n".join(notes),
                        title=f"[bold {UI['accent']}]\u2714  VERIFY RESULT[/bold {UI['accent']}]",
                        border_style=UI["accent2"], box=box.ROUNDED, padding=(1, 2), expand=True))


# ------------------ main ------------------

FALLBACK_BANNER = (
    "+==========================================================+\n"
    "|      A  R  C  H  I  V  E  R      \u2022      T E L E G R A M      |\n"
    "+==========================================================+"
)

BANNER_COLORS = ["#00ffd5", "#00e0ff", "#22b8ff", "#5a8bff", "#8a6bff", "#c15cff"]


def print_big_banner():
    """Big, gradient ASCII title that scales with the terminal width."""
    art = None
    if HAS_PYFIGLET:
        for font in ("block", "banner3-D", "colossal", "big", "slant", "standard"):
            try:
                art = pyfiglet.figlet_format("ARCHIVER", font=font, width=max(console.width, 100))
                if art and art.strip():
                    break
            except Exception:
                art = None
    if not art or not art.strip():
        art = FALLBACK_BANNER

    gradient = Text()
    for i, line in enumerate(art.rstrip("\n").splitlines()):
        gradient.append(line + "\n", style=f"bold {BANNER_COLORS[i % len(BANNER_COLORS)]}")

    console.print()
    console.print(Align.center(gradient))
    console.print(Align.center(
        Text("  T E L E G R A M   G R O U P   /   C H A N N E L   A R C H I V E R  ",
             style="bold #05221f on #00e5c0")
    ))
    console.print()
    console.print(Panel(
        Align.center(
            f"[bold {UI['accent2']}]\U0001F4E6  full-history archiver[/bold {UI['accent2']}]\n"
            f"[white]topics \u2022 albums \u2022 replies \u2022 pins \u2022 polls \u2022 resume-safe[/white]\n"
            f"[{UI['dim']}]runs on your own personal account (Telethon) \u2014 not a bot[/{UI['dim']}]"
        ),
        border_style=UI["violet"], box=box.DOUBLE_EDGE, padding=(1, 10), expand=True,
        subtitle=f"[{UI['dim']}]parallel transfers \u00b7 protected-chat aware \u00b7 topic-safe[/{UI['dim']}]",
    ))


def print_settings_summary(free_gb=None):
    table = Table(box=box.SIMPLE_HEAD, show_header=True,
                  header_style=f"bold {UI['accent2']}",
                  border_style=UI["dim"], padding=(0, 3), expand=True,
                  title="\u2699  ACTIVE SETTINGS", title_style=f"bold {UI['accent']}")
    table.add_column("Setting", style="bold white")
    table.add_column("Value", justify="right", style=UI["accent2"])
    table.add_row("Temp download folder", os.path.abspath(DOWNLOAD_DIR))
    if free_gb is not None:
        table.add_row("Free space there", f"{free_gb:.1f} GB")
    table.add_row(
        "Delay between messages",
        f"{DELAY_BETWEEN_MESSAGES}s start · auto {MIN_MESSAGE_DELAY}-{MAX_MESSAGE_DELAY}s"
        if ADAPTIVE_DELAY else f"{DELAY_BETWEEN_MESSAGES}s fixed",
    )
    table.add_row(
        "Direct media re-send (no download)",
        ("on (auto-off in protected chats)" if PROTECTED_CHAT_AUTODETECT else "on")
        if PREFER_DIRECT_MEDIA else "off",
    )
    table.add_row("Protected-chat detection", "auto" if PROTECTED_CHAT_AUTODETECT else "off")
    table.add_row(
        "Parallel transfer",
        f"{TRANSFER_CONNECTIONS} streams" if FAST_TRANSFER else "off",
    )
    table.add_row(
        "cryptg (fast AES)",
        "[bright_green]installed[/bright_green]" if HAS_CRYPTG
        else "[bold red]MISSING \u2192 pip install cryptg[/bold red]",
    )
    table.add_row("Send verification", str(VERIFY_SENT))
    table.add_row("Retries per message", str(MESSAGE_RETRIES))
    table.add_row("Automatic retry at the end", "on" if AUTO_RETRY_AT_END else "off")
    table.add_row(
        "Progress-file backup",
        f"on (every {PROGRESS_BACKUP_SECONDS}s)" if PROGRESS_BACKUPS else "off",
    )
    table.add_row("Summary to Saved Messages", "on" if NOTIFY_ON_FINISH else "off")
    table.add_row("Upload cap", f"{UPLOAD_LIMIT_GB} GB [{UI['dim']}](checked after login)[/{UI['dim']}]")
    console.print(table)


def _read_config_file():
    """Read API_ID / API_HASH from the config.py sitting next to this script.

    A missing config.py is fine (the script will offer to create it). The file is
    plain Python, so the user can edit it with any text editor.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        ns = {}
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            exec(compile(f.read(), CONFIG_FILE, "exec"), ns)
        return {k: ns[k] for k in ("API_ID", "API_HASH") if ns.get(k)}
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't read {CONFIG_FILE}: {e}[/yellow]")
        return {}


CONFIG_TEMPLATE = (
    "# ============ Telegram API credentials (telegram_archiver) ============\n"
    "# telegram_archiver.py reads API_ID and API_HASH from this file.\n"
    "# Get your own values at https://my.telegram.org -> API development tools\n"
    "# KEEP THIS FILE PRIVATE: never share it, upload it, or commit it to git.\n"
    "\n"
    "API_ID = {api_id}\n"
    'API_HASH = "{api_hash}"\n'
)


def _save_config_file(api_id, api_hash):
    """Write the credentials to config.py and make the file owner-only (600)."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(CONFIG_TEMPLATE.format(api_id=int(api_id), api_hash=str(api_hash)))
        try:
            os.chmod(CONFIG_FILE, 0o600)  # private: owner read/write only
        except Exception:
            pass  # e.g. Windows; the file is still written
        console.print(f"[green]\u2714 Saved to {CONFIG_FILE} (private, chmod 600).[/green]")
        return True
    except Exception as e:
        console.print(f"[red]Couldn't save {CONFIG_FILE}: {e}[/red]")
        return False


async def resolve_api_credentials():
    """Find API_ID/API_HASH without hard-coding them into this file.

    Order: environment variables -> config.py next to the script -> the values at
    the top of this file (Colab injects them there) -> ask interactively (with an
    option to save for next time).
    Returns (api_id, api_hash), or (None, None) if the user aborted.
    """
    # 1) environment variables
    env_id = os.environ.get("TG_API_ID") or os.environ.get("API_ID")
    env_hash = os.environ.get("TG_API_HASH") or os.environ.get("API_HASH")
    if env_id and env_hash:
        try:
            api_id = int(str(env_id).strip())
            console.print(f"[{UI['dim']}]API credentials: environment variables[/{UI['dim']}]")
            return api_id, str(env_hash).strip()
        except ValueError:
            console.print("[yellow]\u26a0 TG_API_ID in the environment isn't a number; ignoring it.[/yellow]")

    # 2) config.py next to the script (the normal way)
    cfg = _read_config_file()
    if cfg.get("API_ID") and cfg.get("API_HASH"):
        try:
            api_id = int(str(cfg["API_ID"]).strip())
            console.print(f"[{UI['dim']}]API credentials: {CONFIG_FILE}[/{UI['dim']}]")
            return api_id, str(cfg["API_HASH"]).strip()
        except ValueError:
            console.print(f"[yellow]\u26a0 API_ID in {CONFIG_FILE} isn't a number; ignoring it.[/yellow]")

    # 3) values hard-coded at the top of the file (old behaviour, still works)
    if API_ID and API_HASH:
        return API_ID, API_HASH

    # 4) first run: ask, and offer to remember
    console.print(Panel(
        Align.center("[bold]\U0001F511  API credentials needed[/bold]\n"
                     f"[{UI['dim']}]get them free at my.telegram.org \u2192 API development tools[/{UI['dim']}]"),
        border_style=UI["accent"], box=box.ROUNDED, padding=(1, 4), expand=True,
    ))
    api_id = await questionary.text("API_ID (numbers only):", style=qstyle).ask_async()
    if not api_id or not api_id.strip().isdigit():
        return None, None
    api_hash = await questionary.text("API_HASH:", style=qstyle).ask_async()
    if not api_hash or len(api_hash.strip()) < 8:
        return None, None
    api_id, api_hash = int(api_id.strip()), api_hash.strip()
    save = await questionary.confirm(
        f"Save them into {os.path.basename(CONFIG_FILE)} so you won't be asked again?",
        style=qstyle, default=True,
    ).ask_async()
    if save:
        _save_config_file(api_id, api_hash)
    return api_id, api_hash


async def main():
    global API_ID, API_HASH
    print_big_banner()

    api_id, api_hash = await resolve_api_credentials()
    if not api_id or not api_hash:
        console.print(
            Panel(Align.center("[bold red]\u26a0  Missing credentials[/bold red]\n"
                               "[white]Fill in config.py next to this script, or set the "
                               "TG_API_ID / TG_API_HASH environment variables[/white]\n"
                               f"[{UI['dim']}]get them from my.telegram.org[/{UI['dim']}]"),
                  border_style="red", box=box.DOUBLE_EDGE, padding=(1, 4), expand=True)
        )
        return
    API_ID, API_HASH = api_id, api_hash

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    free_gb = None
    try:
        free_gb = free_disk_bytes() / (1024 ** 3)
    except Exception as e:
        console.print(f"[yellow]\u26a0 Couldn't check free space for '{DOWNLOAD_DIR}': {e}[/yellow]")
    print_settings_summary(free_gb)
    if free_gb is not None and free_gb < MIN_FREE_DISK_GB:
        console.print(
            f"[yellow]\u26a0 Less than {MIN_FREE_DISK_GB}GB free here. If you meant to store this on "
            f"Google Drive, make sure it's actually mounted (e.g. via rclone) before pointing "
            f"DOWNLOAD_DIR at it.[/yellow]"
        )

    account_name, session_path, progress_file = await choose_account()
    console.print(f"[dim]Active account: {account_name}[/dim]")

    client = TelegramClient(
        session_path, API_ID, API_HASH,
        connection_retries=REQUEST_RETRIES, retry_delay=5, timeout=CONNECT_TIMEOUT,
    )
    await client.start()
    await detect_premium(client)

    try:
        while True:
            console.print()
            console.print(Panel(
                Align.center(
                    f"[bold {UI['accent']}]\u2756  M A I N   M E N U  \u2756[/bold {UI['accent']}]\n"
                    f"[{UI['dim']}]account[/{UI['dim']}] [bold white]{account_name}[/bold white]"
                ),
                box=box.DOUBLE_EDGE, border_style=UI["violet"], padding=(0, 6), expand=True,
            ))
            console.print()
            action = await questionary.select(
                "What do you want to do?",
                choices=[
                    questionary.Choice(title="\U0001F4E5  Pick a group/channel to copy", value="copy"),
                    questionary.Choice(title="\U0001F464  Copy only one person's messages from a topic", value="author"),
                    questionary.Choice(title="\U0001F504  Retry failed messages", value="retry"),
                    questionary.Choice(title="\U0001FA7A  Verify an archive (compare counts)", value="verify"),
                    questionary.Choice(title="\U0001F5D1\uFE0F   Reset a chat's progress", value="reset"),
                    questionary.Choice(title="\U0001F501  Switch account", value="switch"),
                    questionary.Choice(title="\u274C  Exit", value="exit"),
                ],
                style=qstyle,
            ).ask_async()
            console.print()

            if action is None or action == "exit":
                break
            elif action == "copy":
                try:
                    await run_copy_flow(client, progress_file)
                except SameChatError:
                    pass  # already explained on screen; back to the menu without sending anything
            elif action == "author":
                try:
                    await run_author_filtered_flow(client, progress_file)
                except SameChatError:
                    pass
            elif action == "retry":
                await retry_failed_messages(client, progress_file)
            elif action == "verify":
                await verify_archive(client, progress_file)
            elif action == "reset":
                await reset_chat_progress(client, progress_file)
            elif action == "switch":
                flush_progress(force=True)
                await client.disconnect()
                account_name, session_path, progress_file = await choose_account()
                console.print(f"[dim]Active account: {account_name}[/dim]")
                client = TelegramClient(
                    session_path, API_ID, API_HASH,
                    connection_retries=REQUEST_RETRIES, retry_delay=5, timeout=CONNECT_TIMEOUT,
                )
                await client.start()
                await detect_premium(client)
    finally:
        flush_progress(force=True)
        await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped. It will resume from here next time.[/yellow]")
