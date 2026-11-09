"""Video Saver — a Telegram bot that sends TikTok / Instagram Reels / YouTube Shorts
videos straight into the chat, with a one-tap audio extract and owner-only stats."""

import asyncio
import glob
import os
import re
import sqlite3
import tempfile
import time

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # owner's Telegram ID — only they can use /stats
MAX_SIZE = 50 * 1024 * 1024                 # Telegram Bot API upload limit (50 MB)

# Supported links: TikTok, Instagram, YouTube Shorts
LINK_RE = re.compile(
    r"https?://\S*(tiktok\.com|instagram\.com|youtube\.com/shorts|youtu\.be)\S*"
)

dp = Dispatcher()

# ---------- SQLite storage for stats and button links (single file: stats.db) ----------
db = sqlite3.connect("stats.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen INTEGER);
CREATE TABLE IF NOT EXISTS links (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT);
CREATE TABLE IF NOT EXISTS downloads (
    user_id INTEGER, chat_id INTEGER, kind TEXT, platform TEXT, ok INTEGER, ts INTEGER
);
""")


def platform_of(url: str) -> str:
    if "tiktok" in url:
        return "TikTok"
    if "instagram" in url:
        return "Instagram"
    return "YouTube"


def log_download(msg_user_id: int, chat_id: int, kind: str, url: str, ok: bool):
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (msg_user_id, now))
    db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?)",
               (msg_user_id, chat_id, kind, platform_of(url), int(ok), now))
    db.commit()


def save_link(url: str) -> int:
    # callback_data is limited to 64 bytes, so the URL is stored in the DB and the button carries its id
    cur = db.execute("INSERT INTO links (url) VALUES (?)", (url,))
    db.commit()
    return cur.lastrowid


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


def download_audio(url: str, folder: str) -> tuple[str, str, str]:
    opts = base_opts(folder) | {
        "format": "ba/b",
        # ffmpeg extracts the soundtrack to mp3
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = glob.glob(f"{folder}/*.mp3")[0]
    title = (info.get("title") or "audio")[:60]
    author = info.get("uploader") or info.get("channel") or ""
    return path, title, author


# ---------- Commands ----------
@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer("Send me a TikTok, Instagram Reels or YouTube Shorts link and I'll reply with the video 🎬\n"
                     "Tap 🎵 Audio under the video if you only need the sound. Works in groups too.")


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
    errors = q("SELECT COUNT(*) FROM downloads WHERE ok = 0")
    groups = q("SELECT COUNT(DISTINCT chat_id) FROM downloads WHERE chat_id < 0")
    by_platform = db.execute(
        "SELECT platform, COUNT(*) FROM downloads WHERE ok = 1 AND kind = 'video' "
        "GROUP BY platform ORDER BY 2 DESC").fetchall()
    platforms = "\n".join(f"   {p}: {n}" for p, n in by_platform) or "   nothing yet"
    await msg.answer(
        "📊 <b>Stats</b>\n\n"
        f"👤 Users: {users} (+{new_users} in 24h)\n"
        f"👥 Groups: {groups}\n"
        f"🎬 Downloads: {total} (24h: {today})\n"
        f"🎵 Audio extracts: {audio}\n"
        f"❌ Errors: {errors}\n\n"
        f"By platform:\n{platforms}",
        parse_mode="HTML",
    )


# ---------- Link -> video ----------
@dp.message(F.text.regexp(LINK_RE))
async def handle_link(msg: Message):
    url = LINK_RE.search(msg.text).group(0)
    wait = await msg.reply("⏳ Downloading...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # yt-dlp is blocking, so run it in a thread to keep the bot responsive
            path = await asyncio.to_thread(download_video, url, tmp)
            if os.path.getsize(path) > MAX_SIZE:
                await msg.reply("😕 The video is larger than 50 MB — Telegram won't let a bot send it.")
                log_download(msg.from_user.id, msg.chat.id, "video", url, False)
                return
            button = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎵 Audio", callback_data=f"audio:{save_link(url)}")
            ]])
            await msg.reply_video(FSInputFile(path), reply_markup=button)
            log_download(msg.from_user.id, msg.chat.id, "video", url, True)
    except Exception as e:
        print("Download error:", e)
        log_download(msg.from_user.id, msg.chat.id, "video", url, False)
        await msg.reply("❌ Couldn't download this video. It may be private or the link is broken.")
    finally:
        await wait.delete()


# ---------- "🎵 Audio" button ----------
@dp.callback_query(F.data.startswith("audio:"))
async def handle_audio(call: CallbackQuery):
    row = db.execute("SELECT url FROM links WHERE id = ?",
                     (int(call.data.split(":")[1]),)).fetchone()
    if not row:
        await call.answer("This link has expired, please send it again 🙏", show_alert=True)
        return
    url = row[0]
    await call.answer("🎵 Extracting audio...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, title, author = await asyncio.to_thread(download_audio, url, tmp)
            await call.message.reply_audio(FSInputFile(path), title=title, performer=author)
            log_download(call.from_user.id, call.message.chat.id, "audio", url, True)
    except Exception as e:
        print("Audio error:", e)
        log_download(call.from_user.id, call.message.chat.id, "audio", url, False)
        await call.message.reply("❌ Couldn't extract audio from this video.")


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set")
    await dp.start_polling(Bot(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
