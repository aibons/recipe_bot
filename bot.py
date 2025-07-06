#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram bot that extracts recipes from short cooking videos."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def escape_markdown_v2(text: str) -> str:
    """Escape Telegram Markdown V2 special characters."""
    chars = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in chars else c for c in text)


def parse_recipe_blocks(text: str) -> dict:
    """Parse a plain text recipe into blocks used by the formatter."""
    blocks = {"title": "", "ingredients": [], "steps": [], "extra": ""}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        l = line.lower()
        if l.startswith("рецепт"):
            parts = line.split(":", 1)
            blocks["title"] = parts[1].strip() if len(parts) > 1 else ""
            continue
        if l.startswith("ингредиенты"):
            current = "ingredients"
            continue
        if l.startswith("приготов") or l.startswith("шаги"):
            current = "steps"
            continue
        if l.startswith("дополнительно"):
            current = "extra"
            continue

        if current == "ingredients":
            item = line.lstrip("-• ").strip()
            if item.endswith('.'):
                item = item[:-1]
            blocks["ingredients"].append(item)
        elif current == "steps":
            blocks["steps"].append(line.lstrip("0123456789. "))
        elif current == "extra":
            if blocks["extra"]:
                blocks["extra"] += "\n"
            blocks["extra"] += line
    return blocks


def format_recipe_markdown(recipe: dict, original_url: str = "", duration: str = "") -> str:
    """Return recipe formatted with Telegram Markdown V2."""
    parts = []
    sep = "⸻"

    title = recipe.get("title")
    if title:
        parts.append(f"🍽️ *{escape_markdown_v2(title)}*")

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        if parts:
            parts.append(sep)
        parts.append("🛒 *Ингредиенты*")
        for item in ingredients:
            item = item.strip()
            if item.endswith(":"):
                parts.append(escape_markdown_v2(item))
                continue
            if "—" in item:
                name, qty = item.split("—", 1)
            elif "-" in item:
                name, qty = item.split("-", 1)
            else:
                name, qty = item, "по вкусу"
            name = name.strip() or "?"
            qty = qty.strip() or "по вкусу"
            parts.append(f"• {escape_markdown_v2(name)} — {escape_markdown_v2(qty)}")

    steps = recipe.get("steps") or []
    if steps:
        if parts:
            parts.append(sep)
        parts.append("👩‍🍳 *Шаги приготовления*")
        for i, step in enumerate(steps, 1):
            parts.append(f"{i}. {escape_markdown_v2(step.strip())}")

    extra = recipe.get("extra")
    if extra:
        if parts:
            parts.append(sep)
        parts.append("💡 *Дополнительно*")
        parts.append(escape_markdown_v2(extra))

    if original_url:
        if parts:
            parts.append(sep)
        line = f"🔗 [Оригинал]({escape_markdown_v2(original_url)})"
        if duration:
            line += f" {escape_markdown_v2(f'({duration})')}"
        parts.append(line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def is_supported_url(url: str) -> bool:
    """Return True if the url is from Instagram, TikTok or YouTube."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if "instagram.com" in host:
            return "/reel/" in path or "/p/" in path or "/tv/" in path
        if "tiktok.com" in host or host in {"vm.tiktok.com", "vt.tiktok.com"}:
            return True
        if "youtube.com" in host or "youtu.be" in host:
            return "/shorts/" in path or "v=" in parsed.query or "youtu.be" in host
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Environment and database
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "6"))

IG_COOKIES_CONTENT = os.getenv("IG_COOKIES_CONTENT", "")
TT_COOKIES_CONTENT = os.getenv("TT_COOKIES_CONTENT", "")
YT_COOKIES_CONTENT = os.getenv("YT_COOKIES_CONTENT", "")

IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "cookies_instagram.txt")
TT_COOKIES_FILE = os.getenv("TT_COOKIES_FILE", "cookies_tiktok.txt")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "cookies_youtube.txt")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def init_db() -> None:
    with sqlite3.connect("bot.db") as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS quota (uid INTEGER PRIMARY KEY, n INTEGER DEFAULT 0)"""
        )
        db.commit()


def get_quota_usage(uid: int) -> int:
    with sqlite3.connect("bot.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        return row[0] if row else 0


def increment_quota(uid: int) -> int:
    with sqlite3.connect("bot.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        n = (row[0] if row else 0) + 1
        db.execute("INSERT OR REPLACE INTO quota(uid,n) VALUES(?,?)", (uid, n))
        db.commit()
        return n


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def create_temp_cookies_file(content: str) -> Optional[str]:
    if not content:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def get_ydl_opts(url: str) -> Tuple[dict, Optional[str]]:
    headers = {"User-Agent": "Mozilla/5.0 (RecipeBot)"}
    opts = {
        "format": "best[height<=720]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": headers,
    }
    temp_cookie = None
    if "instagram.com" in url:
        if IG_COOKIES_CONTENT:
            temp_cookie = create_temp_cookies_file(IG_COOKIES_CONTENT)
        elif Path(IG_COOKIES_FILE).exists():
            opts["cookiefile"] = IG_COOKIES_FILE
    elif "tiktok.com" in url:
        if TT_COOKIES_CONTENT:
            temp_cookie = create_temp_cookies_file(TT_COOKIES_CONTENT)
        elif Path(TT_COOKIES_FILE).exists():
            opts["cookiefile"] = TT_COOKIES_FILE
    elif "youtube.com" in url or "youtu.be" in url:
        if YT_COOKIES_CONTENT:
            temp_cookie = create_temp_cookies_file(YT_COOKIES_CONTENT)
        elif Path(YT_COOKIES_FILE).exists():
            opts["cookiefile"] = YT_COOKIES_FILE
    if temp_cookie:
        opts["cookiefile"] = temp_cookie
    return opts, temp_cookie


def _sync_download(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    temp_dir = Path(tempfile.mkdtemp())
    temp_cookie = None
    path: Optional[Path] = None
    info: Optional[dict] = None
    try:
        opts, temp_cookie = get_ydl_opts(url)
        opts["outtmpl"] = str(temp_dir / "%(id)s.%(ext)s")
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            if not path.exists():
                for f in temp_dir.iterdir():
                    if f.is_file():
                        path = f
                        break
        return path, info
    except DownloadError as e:
        log.error(f"Download error: {e}")
        return None, None
    finally:
        if temp_cookie:
            Path(temp_cookie).unlink(missing_ok=True)
        if path is None or not path.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


async def download_video(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_download, url)


# ---------------------------------------------------------------------------
# OpenAI helper
# ---------------------------------------------------------------------------

async def extract_recipe_from_video(info: dict) -> str:
    prompt = (
        "Извлеки подробный кулинарный рецепт из описания видео. "
        "Верни заголовок, ингредиенты и шаги приготовления."
    )
    text = (info.get("title", "") or "") + "\n" + (info.get("description", "") or "")
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
            max_tokens=700,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        log.error(f"OpenAI error: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Bot messages
# ---------------------------------------------------------------------------

WELCOME = (
    "🔥 Recipe Bot — извлекаю рецепт из короткого видео!\n\n"
    "Бесплатно доступно 6 роликов.\n\n"
    "Пришлите ссылку на видео с рецептом, а я скачаю его и извлеку рецепт!"
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    used = get_quota_usage(uid)
    if uid == OWNER_ID:
        text = "👑 Вы владелец бота \- лимитов нет"
    else:
        text = f"Использовано: {used}/{FREE_LIMIT}"
    await update.message.reply_text(text)


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    uid = update.effective_user.id

    if not is_supported_url(url):
        await update.message.reply_text(
            "Неподдерживаемая ссылка. Пришлите Instagram Reels, TikTok или YouTube Shorts"
        )
        return

    if uid != OWNER_ID and get_quota_usage(uid) >= FREE_LIMIT:
        await update.message.reply_text("Бесплатный лимит исчерпан")
        return

    await update.message.reply_text("🏃 Скачиваю...")

    try:
        video_path, info = await download_video(url)
    except Exception as exc:
        log.error(f"Download exception: {exc}")
        video_path, info = None, None

    if not video_path or not info or not video_path.exists():
        await update.message.reply_text(
            "❌ Не удалось скачать видео. Возможные причины: приватное видео, требуется вход в аккаунт, видео было удалено или временные проблемы с платформой."
        )
        return

    with open(video_path, "rb") as f:
        await update.message.reply_video(video=f)

    recipe_text = await extract_recipe_from_video(info)
    blocks = parse_recipe_blocks(recipe_text)
    if not (blocks.get("title") or blocks.get("ingredients") or blocks.get("steps")):
        await update.message.reply_text("Не удалось извлечь рецепт из видео")
    else:
        md = format_recipe_markdown(
            blocks,
            original_url=info.get("webpage_url", url),
            duration=str(int(info.get("duration", 0))) + " сек." if info.get("duration") else "",
        )
        await update.message.reply_text(
            md,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    if uid != OWNER_ID:
        increment_quota(uid)

    tmpdir = video_path.parent
    video_path.unlink(missing_ok=True)
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Web server helpers
# ---------------------------------------------------------------------------

async def health_check(_: web.Request) -> web.Response:
    return web.Response(text="OK")


def create_web_app(app: Application) -> web.Application:
    web_app = web.Application()
    web_app.router.add_get("/", health_check)

    async def webhook_handler(request: web.Request) -> web.Response:
        data = await request.json()
        await app.process_update(Update.de_json(data, app.bot))
        return web.Response(text="OK")

    web_app.router.add_post("/", webhook_handler)
    return web_app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    if not TOKEN or not OPENAI_API_KEY:
        log.error("Missing TELEGRAM_TOKEN or OPENAI_API_KEY")
        return

    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    web_app = create_web_app(app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await app.initialize()
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await app.bot.set_webhook(url=webhook_url)
        await app.start()
    else:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
