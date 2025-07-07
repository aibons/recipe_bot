#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram bot that extracts recipes from short cooking videos."""

# Requires ``ffmpeg`` to be installed and available in ``PATH``.

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
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

BASE_DIR = Path(__file__).resolve().parent

# Per-chat locks to prevent concurrent processing of multiple videos in the
# same chat.  This avoids duplicate downloads when a user sends several links
# quickly or resends the same link.
chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def escape_markdown_v2(text: str) -> str:
    """Escape Telegram Markdown V2 special characters."""
    chars = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in chars else c for c in text)


def parse_recipe_blocks(text: str) -> dict:
    """Parse a plain text recipe into blocks used by the formatter.

    The parser is tolerant to various formatting styles and preserves
    indentation so that nested lists can be reconstructed later.
    """

    blocks = {"title": "", "ingredients": [], "steps": [], "extra": ""}
    current = None

    for raw_line in text.splitlines():
        # Preserve leading spaces so nested lists remain intact
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        l = stripped.lower()
        if l.startswith("рецепт") or l.startswith("название"):
            parts = stripped.split(":", 1)
            if len(parts) > 1:
                blocks["title"] = parts[1].strip()
            else:
                blocks["title"] = stripped.partition(" ")[2].strip()
            continue
        if l.startswith("ингредиенты"):
            current = "ingredients"
            continue
        if l.startswith("приготов") or l.startswith("шаг"):
            current = "steps"
            continue
        if l.startswith("дополнительно") or l.startswith("совет") or l.startswith("примеч"):
            current = "extra"
            continue

        if current == "ingredients":
            item = stripped.lstrip("-• ").rstrip(".")
            indent = len(line) - len(line.lstrip())
            if indent:
                item = " " * indent + item
            blocks["ingredients"].append(item)
        elif current == "steps":
            step = stripped.lstrip("0123456789.- ")
            if step.endswith(".") and len(step.split()) > 1:
                step = step[:-1]
            indent = len(line) - len(line.lstrip())
            if indent:
                step = " " * indent + step
            blocks["steps"].append(step)
        elif current == "extra":
            if blocks["extra"]:
                blocks["extra"] += "\n"
            blocks["extra"] += stripped

    return blocks


def format_recipe_markdown(recipe: dict, original_url: str = "", duration: str = "") -> str:
    """Return recipe formatted with Telegram Markdown V2.

    Example output::

        🍽️ Цезарь с жареной курицей

        🛒 Ингредиенты
        🔸 Для курицы:
        • Куриная грудка — 2 шт.
        • Мука — ½ стакана
        • Яйцо — 5 шт.
        • Панировочные сухари — 1 стакан
        • Масло для жарки — по вкусу

        🔸 Для салата:
        • Листья салата — 8 шт.
        • Перец халапеньо — 1 шт.
        • Сельдерей — 1 стебель

        🔸 Для заправки:
        • Каперсы — 2 ст. л.
        • Дижонская горчица — 2 ст. л.
        • Чеснок — 1 зубчик
        • Яичный желток — 1 шт.
        • Анчоусы — 3 филе
        • Лимонный сок — 1 шт.
        • Масло виноградных косточек — 1½ стакана

        ⸻

        👩‍🍳 Шаги приготовления
        1. Баттерфляйд и разбей куриные грудки.
        2. Обваляй каждую грудку в муке, яйце и сухарях.
        3. Жарь до золотистой корочки с двух сторон.
        4. Подготовь салат и заправку, смешай ингредиенты.
        5. Сервируй салат с курицей, посыпь пармезаном и укрась.

        ⸻

        💡 Дополнительно
        Можно добавить зелёный лук и оливковое масло для вкуса.

        ⸻

        🔗 Оригинал (59 сек.)
    """

    parts: list[str] = []
    sep = "⸻"

    title = (recipe.get("title") or "").strip()
    if title:
        parts.append(f"🍽️ {escape_markdown_v2(title)}")
        parts.append("")

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        parts.append("🛒 *Ингредиенты*")
        for item in ingredients:
            raw = item.rstrip()
            if not raw:
                continue
            if raw.endswith(":"):
                head = raw[:-1].strip()
                parts.append(f"🔸 *{escape_markdown_v2(head)}:*")
                continue
            indent = len(raw) - len(raw.lstrip())
            text = raw.lstrip()
            if text.startswith(("-", "•")):
                text = text[1:].lstrip()
            if "—" in text:
                name, qty = text.split("—", 1)
            elif "-" in text:
                name, qty = text.split("-", 1)
            else:
                name, qty = text, "по вкусу"
            name = name.strip() or "?"
            qty = qty.strip() or "по вкусу"
            parts.append(f"{' ' * indent}• {escape_markdown_v2(name)} — {escape_markdown_v2(qty)}")
        parts.append(sep)
        parts.append("")

    steps = recipe.get("steps") or []
    if steps:
        parts.append("👩‍🍳 *Шаги приготовления*")
        idx = 1
        for step in steps:
            raw = step.rstrip()
            if not raw:
                continue
            indent = len(raw) - len(raw.lstrip())
            text = raw.lstrip()
            if text.startswith(("-", "•")) or indent > 0:
                text = text.lstrip("-• ")
                parts.append(f"{' ' * indent}• {escape_markdown_v2(text)}")
                continue
            text = text.lstrip("0123456789.- ")
            parts.append(f"{idx}. {escape_markdown_v2(text)}")
            idx += 1
        parts.append(sep)
        parts.append("")

    extra = (recipe.get("extra") or "").strip()
    if extra:
        parts.append("💡 *Дополнительно*")
        for line in extra.splitlines():
            parts.append(escape_markdown_v2(line.strip()))
        parts.append(sep)
        parts.append("")

    if original_url:
        line = f"🔗 [Оригинал]({escape_markdown_v2(original_url)})"
        if duration:
            line += f" {escape_markdown_v2(f'({duration})')}"
        parts.append(line)

    while parts and parts[-1] == "":
        parts.pop()

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
    except (ValueError, TypeError, AttributeError) as e:
        log.warning(f"Invalid URL format: {url}, error: {e}")
        return False


# ---------------------------------------------------------------------------
# Environment and database
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "6"))
LOCK_TIMEOUT = int(os.getenv("LOCK_TIMEOUT", "300"))

IG_COOKIES_CONTENT = os.getenv("IG_COOKIES_CONTENT", "")
TT_COOKIES_CONTENT = os.getenv("TT_COOKIES_CONTENT", "")
YT_COOKIES_CONTENT = os.getenv("YT_COOKIES_CONTENT", "")

IG_COOKIES_FILE = Path(
    os.getenv("IG_COOKIES_FILE", str(BASE_DIR / "cookies_instagram.txt"))
).resolve()
TT_COOKIES_FILE = Path(
    os.getenv("TT_COOKIES_FILE", str(BASE_DIR / "cookies_tiktok.txt"))
).resolve()
YT_COOKIES_FILE = Path(
    os.getenv("YT_COOKIES_FILE", str(BASE_DIR / "cookies_youtube.txt"))
).resolve()

IG_COOKIES_PATH = str(Path(IG_COOKIES_FILE).expanduser().resolve())
TT_COOKIES_PATH = str(Path(TT_COOKIES_FILE).expanduser().resolve())
YT_COOKIES_PATH = str(Path(YT_COOKIES_FILE).expanduser().resolve())


def is_cookie_file_readable(path: str, name: str) -> bool:
    """Return True if the cookie file exists and can be read."""
    try:
        with open(path, "rb"):
            pass
        return True
    except Exception as exc:
        log.error(f"{name} cookies not found or invalid: {exc}")
        return False

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
        elif Path(IG_COOKIES_PATH).exists():
            opts["cookiefile"] = IG_COOKIES_PATH
    elif "tiktok.com" in url:
        if TT_COOKIES_CONTENT:
            temp_cookie = create_temp_cookies_file(TT_COOKIES_CONTENT)
        elif Path(TT_COOKIES_PATH).exists():
            opts["cookiefile"] = TT_COOKIES_PATH
    elif "youtube.com" in url or "youtu.be" in url:
        if YT_COOKIES_CONTENT:
            temp_cookie = create_temp_cookies_file(YT_COOKIES_CONTENT)
        elif Path(YT_COOKIES_PATH).exists():
            opts["cookiefile"] = YT_COOKIES_PATH
    if temp_cookie:
        opts["cookiefile"] = temp_cookie
    return opts, temp_cookie


def _sync_download(url: str) -> Tuple[Optional[Path], Optional[dict], Optional[str]]:
    temp_dir = Path(tempfile.mkdtemp())
    temp_cookie = None
    path: Optional[Path] = None
    info: Optional[dict] = None
    error: Optional[str] = None
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
        return path, info, None
    except DownloadError as e:
        error = str(e)
        log.error(f"Download error: {error}")
        return None, None, error
    finally:
        if temp_cookie:
            Path(temp_cookie).unlink(missing_ok=True)
        if path is None or not path.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


async def download_video(url: str) -> Tuple[Optional[Path], Optional[dict], Optional[str]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_download, url)


def compress_video_to_720p(path: Path) -> Optional[str]:
    """Compress and scale video to maximum 720p using ffmpeg.

    Returns ``None`` on success or an error message on failure."""
    out_path = path.with_name(path.stem + "_720p" + path.suffix)
    scale_expr = "scale='if(gte(iw,ih),720,-2)':'if(gte(ih,iw),720,-2)'"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-vf",
        scale_expr,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "28",
        "-c:a",
        "copy",
        "-loglevel",
        "error",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        path.unlink(missing_ok=True)
        out_path.rename(path)
        return None
    except subprocess.CalledProcessError as exc:  # pragma: no cover - ffmpeg not invoked in tests
        log.error(f"ffmpeg error: {exc.stderr}")
        out_path.unlink(missing_ok=True)
        return exc.stderr.strip() if exc.stderr else "ffmpeg execution failed"
    except Exception as exc:  # pragma: no cover - ffmpeg not invoked in tests
        log.error(f"ffmpeg error: {exc}")
        out_path.unlink(missing_ok=True)
        return str(exc)


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

async def transcribe_video(path: Path) -> tuple[str, Optional[str]]:
    """Return speech transcription for given video file and possible error."""
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        with open(path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        return resp.strip(), None
    except Exception as exc:
        log.error(f"Transcription error: {exc}")
        return "", str(exc)


async def extract_recipe_from_video_text(text: str) -> str:
    """Extract a recipe from provided text using OpenAI."""
    prompt = (
        "Извлеки подробный кулинарный рецепт из описания видео. "
        "Верни заголовок, ингредиенты и шаги приготовления."
    )
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
            max_tokens=700,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        log.error(f"OpenAI error: {exc}", exc_info=True)
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
        text = "👑 Вы владелец бота - лимитов нет"
    else:
        text = f"Использовано: {used}/{FREE_LIMIT}"
    await update.message.reply_text(text)


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    uid = update.effective_user.id

    lock = chat_locks[uid]
    now = asyncio.get_event_loop().time()
    if lock.locked():
        start_time = ctx.user_data.get("processing_started")
        if start_time and now - start_time > LOCK_TIMEOUT:
            log.warning(f"Lock timeout for {uid}, releasing")
            try:
                lock.release()
            except RuntimeError:
                pass
            ctx.user_data.pop("processing_started", None)
        else:
            last = ctx.user_data.get("last_url")
            if last == url:
                await update.message.reply_text(
                    "⏳ Предыдущее видео еще обрабатывается, пожалуйста подождите"
                )
            else:
                await update.message.reply_text(
                    "⏳ Подождите, идет обработка предыдущего видео"
                )
            return

    ctx.user_data["last_url"] = url
    log.info(f"Start processing for {uid}: {url}")
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=5)
        acquired = True
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⚠️ Слишком много запросов. Попробуйте ещё раз позднее."
        )
        return

    ctx.user_data["processing_started"] = now
    video_path: Optional[Path] = None
    try:
        if not is_supported_url(url):
            await update.message.reply_text(
                "Неподдерживаемая ссылка. Пришлите Instagram Reels, TikTok или YouTube Shorts"
            )
            return

        if uid != OWNER_ID and get_quota_usage(uid) >= FREE_LIMIT:
            await update.message.reply_text("Бесплатный лимит исчерпан")
            return

        if "instagram.com" in url:
            if not IG_COOKIES_CONTENT and not Path(IG_COOKIES_PATH).exists():
                msg = "❌ Не удалось скачать видео. Не найден файл cookies для платформы Instagram."
                log.error(msg)
                await update.message.reply_text(msg)
                return
            if not IG_COOKIES_CONTENT and not is_cookie_file_readable(IG_COOKIES_PATH, "Instagram"):
                await update.message.reply_text(
                    "❌ Не удалось скачать видео. Проверьте файл cookies для Instagram."
                )
                return
        elif "tiktok.com" in url:
            if not TT_COOKIES_CONTENT and not Path(TT_COOKIES_PATH).exists():
                msg = "❌ Не удалось скачать видео. Не найден файл cookies для платформы TikTok."
                log.error(msg)
                await update.message.reply_text(msg)
                return
        elif "youtube.com" in url or "youtu.be" in url:
            if not YT_COOKIES_CONTENT and not Path(YT_COOKIES_PATH).exists():
                msg = "❌ Не удалось скачать видео. Не найден файл cookies для платформы YouTube."
                log.error(msg)
                await update.message.reply_text(msg)
                return

            await update.message.reply_text("🏃 Скачиваю...")

            try:
                video_path, info, err = await download_video(url)
            except Exception as exc:
                log.error(f"Download exception: {exc}", exc_info=True)
                video_path, info, err = None, None, str(exc)

            if err:
                emsg = err.lower()
                if "private" in emsg:
                    reason = "Видео приватное или требует входа в аккаунт."
                elif "instagram.com" in url and (
                    "login" in emsg or "sign in" in emsg or "forbidden" in emsg or "403" in emsg or "401" in emsg
                ):
                    reason = (
                        "Проверьте, пожалуйста, актуальность cookies для Instagram и загрузите новый файл."
                    )
                    log.error(f"Instagram auth error: {err}")
                elif "cookie" in emsg or "cookies" in emsg:
                    reason = "Куки устарели или недействительны."
                elif "403" in emsg or "forbidden" in emsg or "login" in emsg or "sign in" in emsg:
                    reason = "Требуется вход в аккаунт."
                else:
                    reason = err
                log.error(f"Processing error for {uid}: {reason}")
                await update.message.reply_text(f"❌ Не удалось скачать видео. {reason}")
                return

            if not video_path or not info or not video_path.exists():
                log.error(f"Processing error for {uid}: download failed")
                await update.message.reply_text(
                    "❌ Не удалось скачать видео. Возможные причины: приватное видео, требуется вход в аккаунт, видео было удалено или временные проблемы с платформой."
                )
                return

            ffmpeg_error = compress_video_to_720p(video_path)
            if ffmpeg_error:
                log.error(f"Processing error for {uid}: {ffmpeg_error}")
                await update.message.reply_text(
                    f"Не удалось обработать видео: {ffmpeg_error}"
                )
                shutil.rmtree(video_path.parent, ignore_errors=True)
                return

            with open(video_path, "rb") as f:
                await update.message.reply_video(video=f)

            title = (info.get("title") or "").strip()
            desc = (info.get("description") or "").strip()
            need_transcript = not title and len(desc) < 20
            transcript = ""
            t_err = None
            if need_transcript:
                await update.message.reply_text("🤖 Распознаю речь...")
                transcript, t_err = await transcribe_video(video_path)
                if t_err and not transcript:
                    await update.message.reply_text(
                        f"❌ Ошибка транскрипции: {t_err}"
                    )
                    log.error(f"Transcription failed for {uid}: {t_err}")
                    return

            text_for_ai = transcript if transcript else f"{title}\n{desc}"
            recipe_text = await extract_recipe_from_video_text(text_for_ai)
            blocks = parse_recipe_blocks(recipe_text)
            if not (blocks.get("title") or blocks.get("ingredients") or blocks.get("steps")):
                if need_transcript and not transcript:
                    await update.message.reply_text("❌ Не удалось распознать речь и извлечь рецепт из видео")
                else:
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

            log.info(f"Finished processing for {uid}: {url}")

            if uid != OWNER_ID:
                increment_quota(uid)

    finally:
        if video_path:
            tmpdir = video_path.parent
            video_path.unlink(missing_ok=True)
            shutil.rmtree(tmpdir, ignore_errors=True)
        if acquired:
            lock.release()
        ctx.user_data.pop("last_url", None)
        ctx.user_data.pop("processing_started", None)
        log.info(f"State reset for {uid}")
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
    if shutil.which("ffmpeg") is None:
        log.error("ffmpeg is required but was not found in PATH")
        return
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
