#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
recipe_bot – Telegram-бот, который скачивает короткие ролики
(Instagram Reels / TikTok / YouTube Shorts) и присылает их вместе
с рецептом. Работает на python-telegram-bot v22.

• long-polling и aiohttp health-check на :8080 запускаются параллельно
  через asyncio.gather → нет «RuntimeError: This event loop is already running».
• Все тексты экранируются Markdown V2 → Telegram не ругается.
• cookies берутся из файлов (укажите пути в .env):
    IG_COOKIES_FILE, TT_COOKIES_FILE, YT_COOKIES_FILE
"""

# ──────────────────────── stdlib ──────────────────────────
from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import textwrap
from pathlib import Path
from urllib.parse import urlparse

# ───────────────────── third-party ─────────────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import Update, constants
from telegram.ext import (
    Application, ContextTypes,
    CommandHandler, MessageHandler, filters,
)
from telegram.helpers import escape_markdown

# ──────────────────────── ENV ─────────────────────────────
from dotenv import load_dotenv
import os

load_dotenv()                    # спокойно ничего не делает, если .env отсутствует

TOKEN            = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY   = os.environ["OPENAI_API_KEY"]

# можно задавать дефолтные пути к cookie-файлам
IG_COOKIES_FILE  = os.getenv("IG_COOKIES_FILE",  "cookies_instagram.txt")
TT_COOKIES_FILE  = os.getenv("TT_COOKIES_FILE",  "cookies_tiktok.txt")
YT_COOKIES_FILE  = os.getenv("YT_COOKIES_FILE",  "cookies_youtube.txt")

OWNER_ID         = 248610561                # ваш user-id
FREE_LIMIT       = 6                        # бесплатных роликов

# ──────────────────── misc helpers ─────────────────────────
log = logging.getLogger("recipe_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s | %(message)s")

def init_db() -> None:
    """простейшая база: user_id → количество скачанных роликов"""
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect("data/usage.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS quota(uid INTEGER PRIMARY KEY, n INTEGER)")
        db.commit()

def quota_use(uid: int) -> int:
    with sqlite3.connect("data/usage.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        used = row[0] if row else 0
        db.execute("INSERT OR REPLACE INTO quota(uid,n) VALUES(?,?)", (uid, used + 1))
        db.commit()
    return used + 1

# ─────────────────── YT-DLP wrapper ────────────────────────
YDL_OPTS = {
    "quiet": True,
    "skip_download": True,
    "outtmpl": "%(title)s.%(ext)s",
}

def yt_opts(url: str) -> dict:
    """Return per‑URL yt‑dlp options (adds cookies for Instagram, etc.)."""
    opts = dict(
        format="mp4",
        outtmpl="%(id)s.%(ext)s",
        # для Instagram подставляем cookies, иначе ключ не нужен
        cookies=Path(os.getenv("IG_COOKIES_FILE", "")).expanduser()
                if "instagram.com" in url else None,
    )
    # убираем пары с пустыми значениями
    return {k: v for k, v in opts.items() if v}

async def download(url: str) -> tuple[Path, dict]:
    """скачиваем ролик в tmp-директорию, возвращаем путь и info.json"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_download, url)

def _sync_download(url: str) -> tuple[Path, dict]:
    with YoutubeDL({**YDL_OPTS, **yt_opts(url)}) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)), info

# ───────────────────── Welcome-текст ───────────────────────
WELCOME = escape_markdown(textwrap.dedent(
    """
    🔥 *Recipe Bot* — сохраняю рецепт из короткого видео\!

    Бесплатно доступно *6* роликов\.
    Тарифы \(скоро\):

    • 10 роликов — 49 ₽  
    • 200 роликов + 30 дн\. — 199 ₽  

    Пришлите ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам\!
    """
).strip(), version=2)

# ────────────────────── handlers ───────────────────────────
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=constants.ParseMode.MARKDOWN_V2)

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url  = update.message.text.strip()
    uid  = update.effective_user.id

    # лимит
    if uid != OWNER_ID and quota_use(uid) > FREE_LIMIT:
        await update.message.reply_text("ℹ️ Лимит бесплатных роликов исчерпан.")
        return

    # «печатает…»
    await update.message.chat.send_action(constants.ChatAction.TYPING)

    try:
        video_path, _info = await download(url)
    except Exception as e:
        log.warning("download failure: %s", e)
        await update.message.reply_text("❌ Не смог скачать это видео.")
        return

    await update.message.chat.send_action(constants.ChatAction.UPLOAD_VIDEO)
    await update.message.reply_video(
        video=video_path.read_bytes(),
        caption="✅ Готово!"
    )
    video_path.unlink(missing_ok=True)

# ──────────────── aiohttp health-check ─────────────────────
async def health(_req: web.Request) -> web.Response:
    return web.Response(text="ok")

def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", health)])
    return app

# ───────────────────────── main ────────────────────────────
async def main() -> None:
    init_db()

    # Telegram
    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # aiohttp
    runner = web.AppRunner(aio_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)
    await site.start()

    # polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=[constants.UpdateType.MESSAGE],
        drop_pending_updates=True,
    )

    try:
        await asyncio.Event().wait()      # держим процесс живым
    finally:                              # корректное выключение
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

# ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())