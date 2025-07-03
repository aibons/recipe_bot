#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# --------------- внешние зависимости ---------------
from __future__ import annotations
import asyncio, datetime as dt, json, os, re, sqlite3, subprocess
import tempfile, textwrap, logging, shutil
from pathlib import Path
from urllib.parse import urlparse

import aiohttp.web as web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai                              # если нужен GPT-рецепт

from telegram import (
    Update,
    LabeledPrice,
    SuccessfulPayment,
    constants as tg_const,
)
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.helpers import escape_markdown
# ----------------------------------------------------

# --------------- загрузка .env ----------------------
load_dotenv()

TOKEN              = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
IG_COOKIES_FILE    = os.getenv("IG_COOKIES_FILE", "")
TT_COOKIES_FILE    = os.getenv("TT_COOKIES_FILE", "")
YT_COOKIES_FILE    = os.getenv("YT_COOKIES_FILE", "")
OWNER_ID           = int(os.getenv("OWNER_ID", "248610561"))  # безлимит
FREE_LIMIT         = 6
# ----------------------------------------------------

# --------------- БД (счётчик) ------------------------
DB = Path("bot.db")
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS quota"
            "(user_id INTEGER PRIMARY KEY, used INTEGER, ts TEXT)"
        )

def inc_counter(uid: int) -> int:
    now = dt.datetime.utcnow().isoformat(" ")
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO quota VALUES (?, 0, ?)", (uid, now))
        cur.execute("UPDATE quota SET used = used + 1, ts=? WHERE user_id=?",
                    (now, uid))
        cur.execute("SELECT used FROM quota WHERE user_id=?", (uid,))
        return cur.fetchone()[0]
# ----------------------------------------------------

# --------------- yt-dlp базовые опции ----------------
YDL_BASE = {
    "outtmpl": "%(title)s.%(ext)s",
    "quiet":   True,
    "noprogress": True,
    "format": "best[height<=720]+bestaudio/best",
}
def _add_cookiefile(url: str, ydl_opts: dict[str, str]) -> None:
    host = urlparse(url).netloc
    if host.endswith("instagram.com") and IG_COOKIES_FILE:
        ydl_opts["cookiefile"] = IG_COOKIES_FILE
    elif "tiktok" in host and TT_COOKIES_FILE:
        ydl_opts["cookiefile"] = TT_COOKIES_FILE
    elif "youtube" in host and YT_COOKIES_FILE:
        ydl_opts["cookiefile"] = YT_COOKIES_FILE
# ----------------------------------------------------

async def download(url: str) -> Path:
    """Скачивает видео и возвращает путь к файлу"""
    opts = YDL_BASE.copy()
    _add_cookiefile(url, opts)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))

# --------------- Telegram-хэндлеры -------------------
WELCOME = escape_markdown(
    textwrap.dedent("""
    🔥 *Recipe Bot* — помогаю сохранить рецепт из короткого видео\!

    🆓 Доступно *6* бесплатных роликов\.  
    Платные тарифы \(скоро\):

    • 100 роликов — 299 ₽  
    • 200 роликов + 30 дней — 199 ₽  

    Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам\!
    """),
    version=2,
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME,
                                    parse_mode=tg_const.ParseMode.MARKDOWN_V2)

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    url = update.message.text.strip()

    # лимит
    used = inc_counter(uid)
    if uid != OWNER_ID and used > FREE_LIMIT:
        await update.message.reply_text("🚧 Бесплатный лимит исчерпан.")
        return

    msg = await update.message.reply_text("🏃‍♂️ Скачиваю…")
    try:
        video_path = await asyncio.to_thread(download, url)
        await update.message.reply_video(video_path.read_bytes())
    except Exception as e:
        logging.warning("download error: %s", e)
        await msg.edit_text("❌ Не смог скачать это видео.")
    else:
        await msg.delete()

# --------------- AIOHTTP health-check ----------------
async def hello(_: web.Request) -> web.Response:
    return web.Response(text="OK")

def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", hello)])
    return app
# ----------------------------------------------------

# --------------- глобальный обработчик ошибок --------
async def error_handler(update: object,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("update %r caused %s", update, context.error)
# ----------------------------------------------------

# --------------- main --------------------------------
async def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                        level=logging.INFO)

    init_db()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
    )
    application.add_error_handler(error_handler)

    await asyncio.gather(
        application.run_polling(allowed_updates=[tg_const.UpdateType.MESSAGE]),
        web._run_app(aio_app(), host="0.0.0.0", port=8080),
    )
# -----------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())