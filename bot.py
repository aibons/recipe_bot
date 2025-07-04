#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recipe_bot — Telegram-бот, который скачивает короткие ролики
(Instagram Reels / TikTok / YouTube Shorts) и присылает их вместе
с рецептом.  Работает на python-telegram-bot v22.

• long-polling и health-check на :8080 запускаются параллельно
  через asyncio — без ошибки «RuntimeError: This event loop is already running».
• Все тексты экранированы под Markdown V2 → Telegram не ругается.
• cookies берутся из файлов (укажите пути в .env):
    IG_COOKIES_FILE, TT_COOKIES_FILE, YT_COOKIES_FILE
"""

# ── stdlib ─────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio
import logging
import os
import sqlite3
import textwrap
from pathlib import Path
from urllib.parse import urlparse

# ── 3-rd party ─────────────────────────────────────────────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from telegram import Update, constants
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ── ENV & config ───────────────────────────────────────────────────────
load_dotenv()
TOKEN           = os.getenv("TELEGRAM_TOKEN", "")
OWNER_ID        = int(os.getenv("OWNER_ID", "0"))            # кому неограниченно
FREE_LIMIT      = int(os.getenv("FREE_LIMIT", "6"))            # бесплатных роликов
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")
TT_COOKIES_FILE = os.getenv("TT_COOKIES_FILE", "")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
)
log = logging.getLogger("recipe_bot")

# ── tiny SQLite для счётчика бесплатных скачиваний ────────────────────
DB_PATH = Path("quota.db")

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS quota (
                    uid   INTEGER PRIMARY KEY,
                    used  INTEGER NOT NULL DEFAULT 0
                )"""
        )


def quota_use(uid: int, inc: int = 1) -> int:
    """Увеличивает счётчик и возвращает новое значение."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO quota(uid, used) VALUES(?, 0)", (uid,))
        cur.execute("UPDATE quota SET used = used + ? WHERE uid = ?", (inc, uid))
        cur.execute("SELECT used FROM quota WHERE uid = ?", (uid,))
        (used,) = cur.fetchone()
    return used


# ── yt-dlp ─────────────────────────────────────────────────────────────
DL_OPTS = {
    "quiet": True,
    "cookiefile": None,  # будет подставляться динамически
    "paths": {"home": str(Path.cwd())},
    "outtmpl": "%(.id)s.%(ext)s",
    "merge_output_format": "mp4",
}

async def download(url: str) -> tuple[Path, dict]:
    """Скачиваем ролик в отдельном потоке, чтобы не блокировать loop."""
    # подставляем cookies по домену
    host = urlparse(url).hostname or ""
    if "instagram" in host and IG_COOKIES_FILE:
        DL_OPTS["cookiefile"] = IG_COOKIES_FILE
    elif "tiktok" in host and TT_COOKIES_FILE:
        DL_OPTS["cookiefile"] = TT_COOKIES_FILE
    elif YT_COOKIES_FILE:
        DL_OPTS["cookiefile"] = YT_COOKIES_FILE

    def _dl() -> tuple[Path, dict]:
        with YoutubeDL(DL_OPTS) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info)), info

    try:
        return await asyncio.to_thread(_dl)
    except DownloadError as e:
        raise RuntimeError("yt-dlp: " + str(e)) from e


# ── Telegram UI ────────────────────────────────────────────────────────
WELCOME = escape_markdown(
    textwrap.dedent(
        """
        🔥 *Recipe Bot* — сохраняю рецепт из короткого видео!

        Бесплатно доступно *6* роликов\.
        Тарифы \(скоро\):

        • 100 роликов — 299 ₽
        • 200 роликов + 30 дн\. — 199 ₽

        Пришлите ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам\!
        """
    ).strip(),
    version=2,
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: D401
    await update.message.reply_text(WELCOME, parse_mode=constants.ParseMode.MARKDOWN_V2)


# ── обработчик входящих ссылок ─────────────────────────────────────────
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    uid = update.effective_user.id

    # бесплатный лимит
    if uid != OWNER_ID and quota_use(uid) > FREE_LIMIT:
        await update.message.reply_text("⚠️ Лимит бесплатных роликов исчерпан.")
        return

    # «печатает»
    await update.message.chat.send_action(constants.ChatAction.TYPING)
    try:
        video_path, _info = await download(url)
    except Exception as e:  # noqa: BLE001
        log.warning("download failure: %s", e)
        await update.message.reply_text("❌ Не смог скачать это видео.")
        return

    # «загружает»
    await update.message.chat.send_action(constants.ChatAction.UPLOAD_VIDEO)
    await update.message.reply_video(video=video_path.read_bytes(), caption="✅ Готово!")
    video_path.unlink(missing_ok=True)


# ── aiohttp health-check ───────────────────────────────────────────────
async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", health)])
    return app


# ── main: long-poll + aiohttp в одном loop ────────────────────────────
async def main() -> None:
    init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # health-check runner (aiohttp) ------------------------------------------------
    runner = web.AppRunner(aio_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)
    await site.start()

    # Telegram polling ------------------------------------------------------------
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=[constants.UpdateType.MESSAGE],
        drop_pending_updates=True,
    )

    # работаем, пока не прервут
    await application.updater.idle()

    # graceful shutdown ------------------------------------------------------------
    await application.stop()
    await application.shutdown()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())