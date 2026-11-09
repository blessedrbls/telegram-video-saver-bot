"""Video Saver — a Telegram bot that sends TikTok / Instagram Reels / YouTube Shorts
videos straight into the chat, so friends don't have to open the app."""

import asyncio
import logging
import os
import re
import tempfile

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video-saver")

TOKEN = os.getenv("BOT_TOKEN")
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")  # optional, see README
MAX_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit (50 MB)

LINK_RE = re.compile(
    r"https?://\S*(tiktok\.com|instagram\.com|youtube\.com/shorts|youtu\.be)\S*"
)

dp = Dispatcher()


def download(url: str, folder: str) -> str:
    """Download a video with yt-dlp and return the path to the file."""
    opts = {
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        # best video up to 1080p + audio merged into mp4 (needs ffmpeg),
        # fall back to any single ready-made file
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer(
        "Send me a TikTok, Instagram Reels or YouTube Shorts link and I'll reply with the video 🎬\n"
        "Works in groups too — just add me to the chat."
    )


@dp.message(F.text.regexp(LINK_RE))
async def handle_link(msg: Message):
    url = LINK_RE.search(msg.text).group(0)
    wait = await msg.reply("⏳ Downloading...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # yt-dlp is blocking, so run it in a thread to keep the bot responsive
            path = await asyncio.to_thread(download, url, tmp)
            if os.path.getsize(path) > MAX_SIZE:
                await msg.reply("😕 The video is larger than 50 MB — Telegram won't let a bot send it.")
            else:
                await msg.reply_video(FSInputFile(path))
    except Exception:
        log.exception("Failed to download %s", url)
        await msg.reply("❌ Couldn't download this video. It may be private or the link is broken.")
    finally:
        await wait.delete()


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set")
    await dp.start_polling(Bot(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
