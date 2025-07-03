#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
recipe_bot  –  Telegram-бот, который скачивает короткие ролики
(Instagram Reels / TikTok / YouTube Shorts) и присылает их вместе
с рецептом.  Работает на python-telegram-bot v22.

• long-polling и health-check на :8080 запускаются параллельно
  через asyncio.gather – поэтому НЕТ ошибки
  “RuntimeError: This event loop is already running”.
• все тексты экранируются Markdown V2 → Telegram не ругается.
• cookies берутся из файлов (укажите пути в .env):
      IG_COOKIES_FILE, TT_COOKIES_FILE, YT_COOKIES_FILE
"""

# ─────────────────────── stdlib ───────────────────────
from __future__ import annotations
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import shutil          # для which(ffmpeg)
# ─────────────────────── 3-rd party ───────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import Update, constants
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
# ──────────────────────────────────────────────────────

# ─── ENV ───────────────────────────────────────────────
load_dotenv()

TOKEN              = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
IG_COOKIES_FILE    = os.getenv("IG_COOKIES_FILE", "")   # cookies_instagram.txt
TT_COOKIES_FILE    = os.getenv("TT_COOKIES_FILE", "")   # cookies_tiktok.txt
YT_COOKIES_FILE    = os.getenv("YT_COOKIES_FILE", "")   # cookies_youtube.txt

OWNER_ID           = int(os.getenv("OWNER_ID", "248610561"))
FREE_LIMIT         = 6

openai.api_key = OPENAI_API_KEY
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("recipe_bot")

# ─── SQLite: счётчик бесплатных запросов ───────────────
DB = Path("bot.db")
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS quota"
            "(uid INTEGER PRIMARY KEY, used INTEGER DEFAULT 0)"
        )

def quota_use(uid: int) -> int:
    with sqlite3.connect(DB) as con:
        con.execute("INSERT OR IGNORE INTO quota(uid,used) VALUES(?,0)", (uid,))
        con.execute("UPDATE quota SET used = used+1 WHERE uid=?", (uid,))
        cur = con.execute("SELECT used FROM quota WHERE uid=?", (uid,))
        (used,) = cur.fetchone()
    return used
# ───────────────────────────────────────────────────────

# ─── yt-dlp базовые опции ─────────────────────────────
YDL_OPTS: dict = {
    "quiet": True,
    "noprogress": True,
    "outtmpl": "%(title).100s.%(ext)s",
    "merge_output_format": "mp4",
    "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
}

def add_cookiefile(url: str, opts: dict) -> None:
    host = urlparse(url).netloc
    if host.endswith("instagram.com") and IG_COOKIES_FILE:
        opts["cookiefile"] = IG_COOKIES_FILE
    elif "tiktok" in host and TT_COOKIES_FILE:
        opts["cookiefile"] = TT_COOKIES_FILE
    elif "youtube" in host and YT_COOKIES_FILE:
        opts["cookiefile"] = YT_COOKIES_FILE
# ───────────────────────────────────────────────────────

def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None

async def download(url: str) -> Path:
    opts = YDL_OPTS.copy()
    add_cookiefile(url, opts)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    except DownloadError as e:
        raise RuntimeError("yt-dlp: " + str(e)) from e

# ─── Telegram handlers ────────────────────────────────
WELCOME = escape_markdown(
    textwrap.dedent("""
    🔥 *Recipe Bot* — сохраняю рецепт из короткого видео\!

    Бесплатно доступно *6* роликов\.  
    Тарифы \(скоро\):

    • 100 роликов — 299 ₽  
    • 200 роликов + 30 дн\. — 199 ₽  

    Пришлите ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам\!
    """).strip(),
    version=2,
)

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=constants.ParseMode.MARKDOWN_V2)

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    uid = update.effective_user.id

    # лимит
    if uid != OWNER_ID and quota_use(uid) > FREE_LIMIT:
        await update.message.reply_text("🚧 Лимит бесплатных роликов исчерпан.")
        return

    status = await update.message.reply_text("🏃‍♂️ Скачиваю…")
    try:
        video_path = await asyncio.to_thread(download, url)
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action=constants.ChatAction.UPLOAD_VIDEO)
        await ctx.bot.send_video(chat_id=update.effective_chat.id,
                                 video=video_path.read_bytes())
        await status.delete()
    except Exception as e:
        log.warning("download failure: %s", e)
        await status.edit_text("❌ Не смог скачать это видео.")

# ─── AIOHTTP health на / ──────────────────────────────
async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")

def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", health)])
    return app

# ─── main: long-poll + aiohttp в одном loop ───────────
async def main() -> None:
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    await asyncio.gather(
        app.initialize(),
        return_exceptions=False,  # если init упадёт — сразу прекратим
    )
    # запуск
    await asyncio.gather(
        app.start(),
        app.updater.start_polling(allowed_updates=[constants.UpdateType.MESSAGE]),
        web._run_app(aio_app(), host="0.0.0.0", port=8080),
    )

if __name__ == "__main__":
    asyncio.run(main())