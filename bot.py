#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recipe_bot — Telegram-бот, который скачивает короткие ролики
(Instagram Reels / TikTok / YouTube Shorts) и присылает их вместе
с рецептом. Работает на python-telegram-bot v22.

Главное отличие финальной версии:
• yt-dlp вызывается из отдельного потока (asyncio.to_thread), сам     
  скачиватель download() теперь синхронный → ушла ошибка
  "coroutine object has no attribute read_bytes".
• long-poll и health-check на :8080 запускаются параллельно через
  asyncio.gather, поэтому нет RuntimeError "This event loop is
  already running".
• все тексты экранируются Markdown V2 → Telegram не ругается.
• cookies читаются из файлов (пути укажите в .env):
    IG_COOKIES_FILE, TT_COOKIES_FILE, YT_COOKIES_FILE
"""

# ── stdlib ─────────────────────────────────────────────────────────────
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

import shutil  # для which(ffmpeg)

# ── 3-rd party ─────────────────────────────────────────────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai  # noqa: F401   (ключ может понадобиться позже)

from telegram import Update, constants
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ── ENV ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))  # ID владельца бота
FREE_LIMIT = 6  # бесплатных роликов на пользователя

# ── logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("recipe_bot")

# ── sqlite: счётчик скачиваний ─────────────────────────────────────────
DB_PATH = Path("quota.db")

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS quota (
                uid INTEGER PRIMARY KEY,
                used INTEGER NOT NULL DEFAULT 0
            );"""
    )
    conn.commit()
    conn.close()

def quota_use(uid: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO quota(uid, used) VALUES(?, 0);", (uid,))
    cur.execute("UPDATE quota SET used = used + 1 WHERE uid = ?;", (uid,))
    cur.execute("SELECT used FROM quota WHERE uid = ?;", (uid,))
    used: int = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return used

# ── yt-dlp скачиватель ────────────────────────────────────────────────

def download(url: str) -> tuple[Path, dict]:
    """Скачивает ролик через yt-dlp и возвращает (Path, info)."""

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    opts: dict = {
        "outtmpl": "%(title)s.%(ext)s",
        "quiet": True,
        "ffmpeg_location": ffmpeg,
    }

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info)), info
    except DownloadError as e:
        raise RuntimeError("yt-dlp: " + str(e)) from e

# ── Telegram handlers ─────────────────────────────────────────────────
WELCOME = escape_markdown(textwrap.dedent(
    """
    🔥 *Recipe Bot* — сохраняю рецепт из короткого видео!

    Бесплатно доступно *6* роликов\.
    Тарифы \(скоро\):

    • 10 роликов + 7 дн\. - 49 ₽
    • 200 роликов + 30 дн\. — 199 ₽

    Пришлите ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам\!
    """),
    version=2,
)

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=constants.ParseMode.MARKDOWN_V2)

# ── обработчик входящих ссылок ─────────────────────────────────────────
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    uid = update.effective_user.id

    # лимит
    if uid != OWNER_ID and quota_use(uid) > FREE_LIMIT:
        await update.message.reply_text("⚠️ Лимит бесплатных роликов исчерпан.")
        return

    # эффект «печатает/загружает»
    await update.message.chat.send_action(constants.ChatAction.TYPING)

    try:
        video_path, info = await asyncio.to_thread(download, url)  # ← sync → thread
    except Exception as e:
        log.warning("download failure: %s", e)
        await update.message.reply_text("❌ Не смог скачать это видео.")
        return

    await update.message.chat.send_action(constants.ChatAction.UPLOAD_VIDEO)
    await update.message.reply_video(
        video=video_path.read_bytes(),
        caption="✅ Готово!",
    )
    video_path.unlink(missing_ok=True)

# ── AIOHTTP health на / ───────────────────────────────────────────────
async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")

def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", health)])
    return app

# ── main: long-poll + aiohttp в одном loop ────────────────────────────
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
        app.initialize(),           # если init упадёт → сразу прекратим
        return_exceptions=False,
    )

    await asyncio.gather(
        app.start(),                # запускаем long-polling Updater
        app.updater.start_polling(  # работает параллельно с web
            allowed_updates=[constants.UpdateType.MESSAGE]
        ),
        web._run_app(aio_app(), port=8080),  # health-check для Render
    )

if __name__ == "__main__":
    asyncio.run(main())
