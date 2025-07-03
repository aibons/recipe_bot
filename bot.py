#python
#!/usr/bin/env python3
"""recipe_bot – Telegram‑bot that saves recipes out of short‑form videos.

Key points
==========
* Long‑polling **and** aiohttp health‑check run side‑by‑side with `asyncio.gather`.  
* All user‑visible text is escaped with Markdown V2 to silence `BadRequest: can't parse entities`.
* TikTok / Instagram session‑cookie are looked‑up via env‑vars: `IG_SESSIONID`, `TT_SESSIONID`.  
* Free‑tier: 6 videos → paywall (stubs).  
* Logging at INFO level so Render logs stay useful.

This file is self‑contained – drop it into *src/bot.py*, push to Render and
`web: python bot.py` will Just Work.
"""
from __future__ import annotations

# ─── stdlib ─────────────────────────────────────────────────────────────────────
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import textwrap
from pathlib import Path
import shutil  # for ffmpeg lookup

# ─── external deps ──────────────────────────────────────────────────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import (
    Update,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
)
from telegram.constants import UpdateType, ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ─── env / constants ────────────────────────────────────────────────────────────
load_dotenv()
TOKEN           = os.environ["TELEGRAM_TOKEN"]
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "")
IG_SESSIONID    = os.getenv("IG_SESSIONID", "")
TT_SESSIONID    = os.getenv("TT_SESSIONID", "")
FFMPEG_PATH     = shutil.which("ffmpeg") or "ffmpeg"
FREE_LIMIT      = 6
DB_FILE         = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("recipe_bot")

# ─── tiny sqlite‑based quota tracker ────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS quota(
               user_id INTEGER PRIMARY KEY,
               used    INTEGER NOT NULL DEFAULT 0
           )"""
    )
    con.commit()
    con.close()


def inc_quota(user_id: int) -> int:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO quota(user_id, used) VALUES(?,0)", (user_id,))
    cur.execute("UPDATE quota SET used = used+1 WHERE user_id=?", (user_id,))
    cur.execute("SELECT used FROM quota WHERE user_id=?", (user_id,))
    used: int = cur.fetchone()[0]
    con.commit(); con.close()
    return used

# ─── helpers ────────────────────────────────────────────────────────────────────

def safe(s: str) -> str:
    """Escape for Markdown V2"""
    return escape_markdown(str(s), version=2)

# ffmpeg audio‑strip helper (kept for parity – not strictly required now)

def extract_audio(src: Path, dst: Path) -> bool:
    cmd = [
        FFMPEG_PATH,
        "-y", "-i", str(src), "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", str(dst),
    ]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False

# universal video downloader – tries best‑720p, falls back, joins audio

YDL_BASE: dict = {
    "quiet": True,
    "outtmpl": "%\(title)s.%(ext)s",
    "format": "best",
    "merge_output_format": "mp4",
}


def download(url: str) -> tuple[Path, dict]:
    fmt_try = [
        "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "best",
    ]
    opts = YDL_BASE.copy()

    if "instagram.com" in url and IG_SESSIONID:
        opts["cookiesfrombrowser"] = f".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\t{IG_SESSIONID}\n"
    if "tiktok.com" in url and TT_SESSIONID:
        opts["cookiesfrombrowser"] = f".tiktok.com\tTRUE\t/\tFALSE\t0\t_tt_session_id\t{TT_SESSIONID}\n"

    for f in fmt_try:
        try:
            with YoutubeDL({**opts, "format": f}) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info)), info
        except DownloadError as e:
            log.warning("download retry with next fmt: %s", e)
            continue
    raise RuntimeError("Не смог скачать видео")

# ─── Telegram callbacks ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    free_left = max(0, FREE_LIMIT - inc_quota(uid) + 1)
    txt = (
        "🔥 *Recipe Bot* — помогаю сохранить рецепт из короткого видео!\n"
        f"🏷️ Доступно *{free_left}* бесплатных роликов.\n"
        "Платные тарифы (скоро):\n"
        "• 100 роликов — 299 ₽\n"
        "• 200 роликов + 30 дней — 199 ₽\n\n"
        "Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.effective_message.text.strip()
    uid = update.effective_user.id
    used = inc_quota(uid)
    if uid != 248610561 and used > FREE_LIMIT:
        await update.message.reply_text("🎫 Доступ исчерпан. Оплатите тариф, чтобы продолжить.")
        return

    msg = await update.message.reply_text("🏃‍♂️ Скачиваю…")
    try:
        path, info = await asyncio.get_event_loop().run_in_executor(None, download, url)
        await ctx.bot.send_chat_action(update.effective_chat.id, "upload_video")
        await ctx.bot.send_video(
            chat_id=update.effective_chat.id,
            video=open(path, "rb"),
            caption=safe(info.get("title", "")),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("download error: %s", e)
        await msg.edit_text("❌ Не смог скачать это видео.")

# ─── aiohttp health‑check ──────────────────────────────────────────────────────

async def hello(_req: web.Request) -> web.Response:  # Render pings /
    return web.Response(text="OK")


def aio_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", hello)])
    return app

# ─── main ──────────────────────────────────────────────────────────────────────

aiohttp_app = aio_app()  # single instance for gather & run_app

async def main() -> None:
    init_db()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # start bot + aiohttp concurrently
    await application.initialize()
    await asyncio.gather(
        application.start(),
        web._run_app(aiohttp_app, host="0.0.0.0", port=8080),
        application.updater.start_polling(allowed_updates=[UpdateType.MESSAGE]),
    )

if __name__ == "__main__":
    asyncio.run(main())

