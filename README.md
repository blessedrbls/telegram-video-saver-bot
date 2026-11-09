# 🎬 Video Saver — Telegram bot

A Telegram bot that turns **TikTok, Instagram Reels and YouTube Shorts** links into videos right in the chat —
and photo slideshows / carousels into albums.
Send a link to the bot (or drop it in a group where the bot is a member) and it replies with the video file,
so your friends can watch it without opening the app.

## Features

- ✅ TikTok, Instagram Reels, YouTube Shorts
- ✅ Works in private chats and **groups**
- 🖼 **Photo slideshows & carousels** (TikTok photo posts, Instagram multi-photo posts) sent as Telegram albums
- ⚡ **Cache**: a link that was already sent is re-sent instantly by Telegram `file_id` — no re-download
- ✅ Up to 1080p with audio (video and audio are merged via ffmpeg)
- 🎵 **Audio button** under every video (and under TikTok slideshows) — get the soundtrack as an mp3
- 📊 **Owner-only `/stats`**: users, groups, downloads (total / last 24h), audio extracts, errors, breakdown by platform
- ✅ Handles several users at once (downloads run in a background thread)
- 🛡 **Anti-spam & load control**: max 5 requests per minute per user, max 2 downloads at once (the rest wait in a queue)
- ✅ Friendly errors: private videos, broken links, files over Telegram's 50 MB limit
- ✅ Runs 24/7 on a Linux server as a systemd service with auto-restart

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Short instructions |
| `/id` | everyone | Shows your Telegram user ID |
| `/stats` | owner only | Usage statistics (ignored for everyone else) |

Stats, cache and button links are stored in a local SQLite file (`stats.db`), no external database needed.

## Tech stack

| Part | Tool |
|---|---|
| Language | Python 3.10+ |
| Telegram framework | [aiogram 3](https://github.com/aiogram/aiogram) |
| Downloader | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (+ curl-cffi) for video, [gallery-dl](https://github.com/mikf/gallery-dl) for photo posts |
| Media processing | ffmpeg |
| YouTube JS challenges | Deno |
| Storage | SQLite |
| Hosting | Ubuntu 24.04 VPS (Oracle Cloud Free Tier), systemd |

## Quick start (local)

```bash
git clone https://github.com/blessedrbls/telegram-video-saver-bot.git
cd telegram-video-saver-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="your-token-from-BotFather"
export ADMIN_ID="your-telegram-id"   # optional, enables /stats
python bot.py
```

System requirements: **ffmpeg** and **Deno** must be installed
(macOS: `brew install ffmpeg deno`, Ubuntu: `sudo apt install ffmpeg` + the Deno install script).

## Using it in groups

1. In [@BotFather](https://t.me/BotFather): `/setjoingroups` → **Enable**, `/setprivacy` → **Disable**
2. Add the bot to your group. Anyone who posts a supported link gets the video as a reply.

## Deploy to a server (24/7)

```bash
# on an Ubuntu server
sudo apt update && sudo apt install -y python3-venv ffmpeg
curl -fsSL https://deno.land/install.sh | sh -s -- -y
sudo ln -s ~/.deno/bin/deno /usr/local/bin/deno

git clone https://github.com/blessedrbls/telegram-video-saver-bot.git ~/video-bot
python3 -m venv ~/video-bot/venv
~/video-bot/venv/bin/pip install -r ~/video-bot/requirements.txt

cp ~/video-bot/.env.example ~/video-bot/.env   # then put your token and ADMIN_ID inside
sudo cp ~/video-bot/deploy/video-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now video-bot
```

Logs: `journalctl -u video-bot -n 50`

## Cookies (optional)

Some platforms block anonymous downloads from datacenter IPs
(YouTube: *"Sign in to confirm you're not a bot"*, Instagram: *"empty media response"*).
Export cookies from a browser where you're logged in (e.g. with the *Get cookies.txt LOCALLY* extension),
save them as `cookies.txt` next to `bot.py` and restart the bot.
Use a secondary account. **Never commit cookies.txt** (or `stats.db`) — both are already in `.gitignore`.

## Maintenance

Platforms change often — if downloads start failing, update yt-dlp:

```bash
pip install -U yt-dlp
```

## Disclaimer

For personal use only. Respect the platforms' terms of service and the content creators' rights —
don't re-upload other people's videos as your own.
