"""Video Saver — a Telegram bot that sends TikTok / Instagram Reels / YouTube Shorts
videos (and photo slideshows / carousels as albums) straight into the chat.
Extras: one-tap audio extract, instant re-sends from cache, anti-spam, owner-only stats."""

import asyncio
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo,
                           Message)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # owner's Telegram ID — only they can use /stats
MAX_SIZE = 50 * 1024 * 1024                 # Telegram Bot API upload limit (50 MB)
MAX_ITEMS = 20                              # max items sent from one post

VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic")

# Supported links: TikTok, Instagram, YouTube Shorts
LINK_RE = re.compile(
    r"https?://\S*(tiktok\.com|instagram\.com|youtube\.com/shorts|youtu\.be)\S*"
)

dp = Dispatcher()

# ---------- Anti-spam ----------
LIMIT = 5        # max 5 requests...
PERIOD = 60      # ...per 60 seconds per user
recent = defaultdict(deque)            # user_id -> timestamps of recent requests
download_slots = asyncio.Semaphore(2)  # at most 2 downloads at once so a small VPS doesn't run out of memory


def wait_seconds(user_id: int) -> int:
    """0 = allowed, otherwise how many seconds the user has to wait."""
    now = time.time()
    q = recent[user_id]
    while q and now - q[0] > PERIOD:   # drop requests older than PERIOD
        q.popleft()
    if len(q) >= LIMIT:
        return int(PERIOD - (now - q[0])) + 1
    q.append(now)
    return 0


# ---------- SQLite storage: stats, button links, cache (single file: stats.db) ----------
db = sqlite3.connect("stats.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen INTEGER);
CREATE TABLE IF NOT EXISTS links (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT);
CREATE TABLE IF NOT EXISTS downloads (
    user_id INTEGER, chat_id INTEGER, kind TEXT, platform TEXT, ok INTEGER, ts INTEGER
);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, items TEXT);
""")


def platform_of(url: str) -> str:
    if "tiktok" in url:
        return "TikTok"
    if "instagram" in url:
        return "Instagram"
    return "YouTube"


def log_download(user_id: int, chat_id: int, kind: str, url: str, ok: bool):
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (user_id, now))
    db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?)",
               (user_id, chat_id, kind, platform_of(url), int(ok), now))
    db.commit()


def save_link(url: str) -> int:
    # callback_data is limited to 64 bytes, so the URL is stored in the DB and the button carries its id
    cur = db.execute("INSERT INTO links (url) VALUES (?)", (url,))
    db.commit()
    return cur.lastrowid


# ---------- Cache ----------
# Key = URL without query string (?igsh=..., ?si=...), so the same post shared differently still matches
def cache_key(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def cache_get(url: str):
    row = db.execute("SELECT items FROM cache WHERE key = ?", (cache_key(url),)).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(url: str, items: list):
    # items: [["video", file_id], ["photo", file_id], ...] — file_id is the file's id on Telegram's servers
    db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (cache_key(url), json.dumps(items)))
    db.commit()


def cache_drop(url: str):
    db.execute("DELETE FROM cache WHERE key = ?", (cache_key(url),))
    db.commit()


# ---------- Downloading ----------
def base_opts(folder: str) -> dict:
    opts = {"outtmpl": f"{folder}/%(id)s.%(ext)s", "noplaylist": True, "quiet": True}
    # optional cookies.txt for sites that require login (see README)
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    return opts


def download_video(url: str, folder: str) -> str:
    opts = base_opts(folder) | {
        # best video up to 1080p + audio merged into mp4 (needs ffmpeg)
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def to_jpg(path: str) -> str:
    """Telegram may reject webp/heic as photos — convert them to jpg with ffmpeg."""
    if path.lower().endswith((".jpg", ".jpeg", ".png")):
        return path
    out = os.path.splitext(path)[0] + ".jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, out], timeout=60)
    return out if os.path.exists(out) else path


def download_gallery(url: str, folder: str) -> list[str]:
    """Photo posts and carousels are fetched with gallery-dl (yt-dlp handles video only)."""
    out = os.path.join(folder, "gallery")
    cmd = [sys.executable, "-m", "gallery_dl", "-D", out, "--range", f"1-{MAX_ITEMS}"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    res = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        print("gallery-dl:", res.stderr.strip()[-500:])
    files = sorted(f for f in glob.glob(f"{out}/*") if f.lower().endswith(VIDEO_EXT + PHOTO_EXT))
    return [f if f.lower().endswith(VIDEO_EXT) else to_jpg(f) for f in files]


def fetch(url: str, folder: str) -> list[str]:
    """Returns a list of files: a single video, or several photos/videos (carousel)."""
    error = None
    if "/photo/" not in url:              # /photo/ in a TikTok URL = slideshow, no video to fetch
        try:
            path = download_video(url, folder)
            if path.lower().endswith(VIDEO_EXT):
                return [path]
            # for TikTok slideshows yt-dlp may return audio only — fall back to the photos
        except Exception as e:
            error = e
    files = download_gallery(url, folder)
    if not files:
        raise error or RuntimeError("no video or photos found in this post")
    return files


# ---------- Sending ----------
def audio_button(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎵 Audio", callback_data=f"audio:{save_link(url)}")
    ]])


async def send_items(msg: Message, url: str, items: list) -> list:
    """items: [("video"|"photo", file or file_id)]. A single video gets the Audio button, otherwise an album."""
    if len(items) == 1 and items[0][0] == "video":
        sent = [await msg.reply_video(items[0][1], reply_markup=audio_button(url))]
    else:
        sent = []
        for i in range(0, len(items), 10):          # Telegram albums hold max 10 items
            chunk = items[i:i + 10]
            if len(chunk) == 1:
                kind, media = chunk[0]
                sent.append(await (msg.reply_photo(media) if kind == "photo" else msg.reply_video(media)))
            else:
                sent += await msg.reply_media_group([
                    InputMediaPhoto(media=m) if k == "photo" else InputMediaVideo(media=m) for k, m in chunk
                ])
        # albums can't carry buttons, and TikTok slideshows have music —
        # so the Audio button goes in a separate message
        if "tiktok" in url:
            await msg.reply("🎵 Slideshow music:", reply_markup=audio_button(url))
    return [["photo", m.photo[-1].file_id] if m.photo else ["video", m.video.file_id] for m in sent]


# ---------- Commands ----------
@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer("Send me a TikTok, Instagram Reels or YouTube Shorts link and I'll reply with the video 🎬\n"
                     "Photo slideshows and carousels come as an album 🖼\n"
                     "Tap 🎵 Audio if you only need the sound. Works in groups too.")


@dp.message(Command("id"))
async def my_id(msg: Message):
    await msg.answer(f"Your Telegram ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("stats"))
async def stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return  # silently ignored for everyone except the owner
    day_ago = int(time.time()) - 86400
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    users = q("SELECT COUNT(*) FROM users")
    new_users = q("SELECT COUNT(*) FROM users WHERE first_seen > ?", day_ago)
    total = q("SELECT COUNT(*) FROM downloads WHERE ok = 1")
    today = q("SELECT COUNT(*) FROM downloads WHERE ok = 1 AND ts > ?", day_ago)
    audio = q("SELECT COUNT(*) FROM downloads WHERE ok = 1 AND kind = 'audio'")
    albums = q("SELECT COUNT(*) FROM downloads WHERE ok = 1 AND kind = 'album'")
    cached = q("SELECT COUNT(*) FROM downloads WHERE ok = 1 AND kind = 'cached'")
    errors = q("SELECT COUNT(*) FROM downloads WHERE ok = 0")
    groups = q("SELECT COUNT(DISTINCT chat_id) FROM downloads WHERE chat_id < 0")
    by_platform = db.execute(
        "SELECT platform, COUNT(*) FROM downloads WHERE ok = 1 AND kind != 'audio' "
        "GROUP BY platform ORDER BY 2 DESC").fetchall()
    platforms = "\n".join(f"   {p}: {n}" for p, n in by_platform) or "   nothing yet"
    await msg.answer(
        "📊 <b>Stats</b>\n\n"
        f"👤 Users: {users} (+{new_users} in 24h)\n"
        f"👥 Groups: {groups}\n"
        f"🎬 Sent: {total} (24h: {today})\n"
        f"⚡ From cache (instant): {cached}\n"
        f"🖼 Albums: {albums}\n"
        f"🎵 Audio: {audio}\n"
        f"❌ Errors: {errors}\n\n"
        f"By platform:\n{platforms}",
        parse_mode="HTML",
    )


# ---------- Link -> video / album ----------
@dp.message(F.text.regexp(LINK_RE))
async def handle_link(msg: Message):
    url = LINK_RE.search(msg.text).group(0)

    # 1. Sent before? Re-send instantly by file_id, no download
    if (items := cache_get(url)):
        try:
            await send_items(msg, url, items)
            log_download(msg.from_user.id, msg.chat.id, "cached", url, True)
            return
        except Exception as e:
            print("Cache miss, downloading again:", e)
            cache_drop(url)

    # 2. Anti-spam (cache hits don't count — they cost nothing)
    if (sec := wait_seconds(msg.from_user.id)):
        await msg.reply(f"🐢 Too many links in a row. Please wait {sec} s.")
        return

    # 3. Download
    wait = await msg.reply("⏳ Downloading...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # yt-dlp and gallery-dl are blocking, so run them in a thread;
            # download_slots queues this job if 2 downloads are already running
            async with download_slots:
                files = await asyncio.to_thread(fetch, url, tmp)
            files = [f for f in files if os.path.getsize(f) <= MAX_SIZE]
            if not files:
                await msg.reply("😕 The file is larger than 50 MB — Telegram won't let a bot send it.")
                log_download(msg.from_user.id, msg.chat.id, "video", url, False)
                return
            items = [("video" if f.lower().endswith(VIDEO_EXT) else "photo", FSInputFile(f)) for f in files]
            sent = await send_items(msg, url, items)
            cache_put(url, sent)
            kind = "video" if len(sent) == 1 and sent[0][0] == "video" else "album"
            log_download(msg.from_user.id, msg.chat.id, kind, url, True)
    except Exception as e:
        print("Download error:", e)
        log_download(msg.from_user.id, msg.chat.id, "video", url, False)
        await msg.reply("❌ Couldn't download this post. It may be private or the link is broken.")
    finally:
        await wait.delete()


# ---------- "🎵 Audio" button ----------
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus")


def download_audio(url: str, folder: str) -> tuple[str, str, str]:
    opts = base_opts(folder) | {
        "format": "ba/b",
        # ffmpeg extracts the soundtrack to mp3
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = glob.glob(f"{folder}/*.mp3")[0]
        title = (info.get("title") or "audio")[:60]
        author = info.get("uploader") or info.get("channel") or ""
        return path, title, author
    except Exception as e:
        # fallback for slideshows: gallery-dl fetches the music along with the photos
        print("yt-dlp audio failed, trying gallery-dl:", e)
        out = os.path.join(folder, "gallery-audio")
        cmd = [sys.executable, "-m", "gallery_dl", "-D", out]
        if os.path.exists("cookies.txt"):
            cmd += ["--cookies", "cookies.txt"]
        subprocess.run(cmd + [url], capture_output=True, text=True, timeout=180)
        found = [f for f in glob.glob(f"{out}/*") if f.lower().endswith(AUDIO_EXT)]
        if not found:
            raise
        return found[0], "Slideshow music", ""


@dp.callback_query(F.data.startswith("audio:"))
async def handle_audio(call: CallbackQuery):
    row = db.execute("SELECT url FROM links WHERE id = ?",
                     (int(call.data.split(":")[1]),)).fetchone()
    if not row:
        await call.answer("This link has expired, please send it again 🙏", show_alert=True)
        return
    url = row[0]
    if (sec := wait_seconds(call.from_user.id)):
        await call.answer(f"🐢 Too fast. Please wait {sec} s.", show_alert=True)
        return
    await call.answer("🎵 Extracting audio...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            async with download_slots:
                path, title, author = await asyncio.to_thread(download_audio, url, tmp)
            await call.message.reply_audio(FSInputFile(path), title=title, performer=author)
            log_download(call.from_user.id, call.message.chat.id, "audio", url, True)
    except Exception as e:
        print("Audio error:", e)
        log_download(call.from_user.id, call.message.chat.id, "audio", url, False)
        await call.message.reply("❌ Couldn't extract audio from this post.")


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set")
    await dp.start_polling(Bot(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
