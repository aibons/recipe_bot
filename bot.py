#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
recipe_bot – Telegram-бот, который скачивает короткие ролики
(Instagram Reels / TikTok / YouTube Shorts) и присылает их вместе
с рецептом. Работает на python-telegram-bot v22.
"""

# Если ты деплоишь на Render/сервер:
# 1. В настройках Render укажи переменную окружения WEBHOOK_URL вида:
#    https://recipe-bot-q839.onrender.com/
#    (или актуальный URL твоего Render-сервиса)

from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import textwrap
import tempfile
import os
from pathlib import Path

print("=== FILES IN CURRENT DIR ===")
for file in Path(".").glob("*"):
    print(file)
print("============================")
from urllib.parse import urlparse
from typing import Optional, Tuple

# third-party
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import Update, constants
# third-party
from telegram.ext import (
    Application, ContextTypes,
    CommandHandler, MessageHandler, filters,
)

# === Markdown utils and recipe block parsing ===
def escape_markdown_v2(text: str) -> str:
    """Экранирует спецсимволы для Markdown V2 Telegram"""
    chars = r"\_*[]()~`>#+-=|{}.!"
    return ''.join(f"\\{c}" if c in chars else c for c in text)

# Парсер блоков из ответа OpenAI (markdown -> dict)
def parse_recipe_blocks(text: str) -> dict:
    import re
    blocks = {
        "title": "",
        "ingredients": [],
        "steps": [],
        "extra": ""
    }
    # Title (гибко)
    m = re.search(r"[Рр]ецепт\:?\s*([^\n\*\_\-\:\[\]\(\)\~\`\>\#\+\=\|\{\}\.\!]*)", text)
    if m:
        blocks["title"] = m.group(1).strip(" *_-:[]()~`>#+=|{}.!")
    # Ингредиенты
    ingr = re.search(r"[Ии]нгредиенты\:?\**\n(.+?)(\n[Пп]риг|[\n\d]+\.)", text, re.DOTALL)
    if ingr:
        ingr_lines = [i.strip('•-*_[]()~`>#+=|{}.!').strip() for i in ingr.group(1).strip().split('\n') if i.strip()]
        blocks["ingredients"] = ingr_lines
    # Шаги
    steps = re.search(r"[Пп]риготовление\:?\**\n(.+?)(\n[Дд]ополнительно|$)", text, re.DOTALL)
    if steps:
        steps_lines = [s.strip("0123456789. *_-:[]()~`>#+=|{}.!").strip() for s in steps.group(1).split('\n') if s.strip()]
        blocks["steps"] = steps_lines
    # Дополнительно
    extra = re.search(r"[Дд]ополнительно\:?\**\n(.+)", text, re.DOTALL)
    if extra:
        blocks["extra"] = extra.group(1).strip()
    return blocks

def format_recipe_markdown(recipe: dict, original_url: str = "", duration: str = "") -> str:
    lines = []
    # Заголовок
    if recipe.get("title"):
        lines.append(f"🍲 *{escape_markdown_v2(recipe['title'].upper())}*")
    # Ингредиенты
    if recipe.get("ingredients"):
        lines.append("\n🛒 *Ингредиенты*")
        for i in recipe['ingredients']:
            lines.append(f"• {escape_markdown_v2(i)}")
    if recipe.get("ingredients"):
        lines.append("\n_____")
    # Шаги приготовления
    if recipe.get("steps"):
        lines.append("👨‍🍳 *Шаги приготовления*")
        for idx, s in enumerate(recipe['steps'], 1):
            lines.append(f"{idx}. {escape_markdown_v2(s)}")
        lines.append("\n_____")
    # Дополнительно
    if recipe.get("extra"):
        lines.append(f"💡 *Дополнительно*\n{escape_markdown_v2(recipe['extra'])}\n\n_____")
    # Оригинал и длительность
    if original_url:
        orig = f"[Оригинал]({original_url})"
        if duration:
            orig += f" {escape_markdown_v2(f'({duration})')}"
        lines.append(orig)
    return "\n".join(lines)

# ENV
load_dotenv()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is required")

# Cookie файлы (опционально)
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "cookies_instagram.txt")
TT_COOKIES_FILE = os.getenv("TT_COOKIES_FILE", "cookies_tiktok.txt")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "cookies_youtube.txt")

# Cookie содержимое из переменных окружения (альтернатива файлам)
IG_COOKIES_CONTENT = os.getenv("IG_COOKIES_CONTENT", "")
TT_COOKIES_CONTENT = os.getenv("TT_COOKIES_CONTENT", "")
YT_COOKIES_CONTENT = os.getenv("YT_COOKIES_CONTENT", "")

OWNER_ID = int(os.getenv("OWNER_ID", "248610561"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "6"))

# Логирование
log = logging.getLogger("recipe_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s"
)

# Глушим httpx и telegram.ext._utils.networkloop до WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.WARNING)

def init_db() -> None:
    """Инициализация базы данных для отслеживания использования"""
    with sqlite3.connect("bot.db") as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS quota(
                uid INTEGER PRIMARY KEY, 
                n INTEGER DEFAULT 0
            )
        """)
        db.commit()

def get_quota_usage(uid: int) -> int:
    """Получить текущее использование квоты"""
    with sqlite3.connect("bot.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        return row[0] if row else 0

def increment_quota(uid: int) -> int:
    """Увеличить счетчик использования и вернуть новое значение"""
    with sqlite3.connect("bot.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        current = row[0] if row else 0
        new_count = current + 1
        db.execute("INSERT OR REPLACE INTO quota(uid,n) VALUES(?,?)", (uid, new_count))
        db.commit()
        return new_count

def create_temp_cookies_file(content: str) -> Optional[str]:
    """Создать временный файл cookies из содержимого переменной окружения"""
    if not content:
        return None
    
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(content)
            return f.name
    except Exception as e:
        log.error(f"Failed to create temp cookies file: {e}")
        return None

def get_ydl_opts(url: str) -> dict:
    """Получить опции yt-dlp для конкретного URL"""
    
    # Базовые настройки
    opts = {
        "format": "best[height<=720]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "extractaudio": False,
        "audioformat": "mp3",
        "outtmpl": "%(id)s.%(ext)s",
        "writesubtitles": False,
        "writeautomaticsub": False,
        "no_check_certificate": True,
        "prefer_insecure": True,
        # Добавляем User-Agent для обхода блокировок
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Accept-Encoding": "gzip,deflate",
            "Accept-Charset": "ISO-8859-1,utf-8;q=0.7,*;q=0.7",
            "Keep-Alive": "115",
            "Connection": "keep-alive",
        },
        # Дополнительные настройки для обхода ограничений
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "retry_sleep_functions": {
            "http": lambda n: min(4**n, 60),
            "fragment": lambda n: min(4**n, 60),
        }
    }
    
    # Специфичные настройки для разных платформ
    if "instagram.com" in url:
        opts.update({
            "format": "best[height<=720]/best",
            "extractor_args": {
                "instagram": {
                    "api_version": "v1",
                }
            }
        })
        # Используем cookies из переменной окружения или файла
        if IG_COOKIES_CONTENT:
            temp_cookies = create_temp_cookies_file(IG_COOKIES_CONTENT)
            if temp_cookies:
                opts["cookiefile"] = temp_cookies
        elif IG_COOKIES_FILE and Path(IG_COOKIES_FILE).exists():
            opts["cookiefile"] = IG_COOKIES_FILE
            
    elif "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        opts.update({
            "format": "best[height<=720]/best",
            "extractor_args": {
                "tiktok": {
                    "api_hostname": "api.tiktokv.com",
                }
            }
        })
        # Используем cookies из переменной окружения или файла
        if TT_COOKIES_CONTENT:
            temp_cookies = create_temp_cookies_file(TT_COOKIES_CONTENT)
            if temp_cookies:
                opts["cookiefile"] = temp_cookies
        elif TT_COOKIES_FILE and Path(TT_COOKIES_FILE).exists():
            opts["cookiefile"] = TT_COOKIES_FILE
            
    elif "youtube.com" in url or "youtu.be" in url:
        # Проверяем, что это Shorts
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc or "/shorts" in parsed.path:
            opts.update({
                "format": "best[height<=720]/best[ext=mp4]/best",
            })
            # Используем cookies из переменной окружения или файла
            if YT_COOKIES_CONTENT:
                temp_cookies = create_temp_cookies_file(YT_COOKIES_CONTENT)
                if temp_cookies:
                    opts["cookiefile"] = temp_cookies
            elif YT_COOKIES_FILE and Path(YT_COOKIES_FILE).exists():
                opts["cookiefile"] = YT_COOKIES_FILE
        elif "youtube.com" in parsed.netloc:
            opts.update({
                "format": "best[height<=720]/best[ext=mp4]/best",
            })
            # Используем cookies из переменной окружения или файла
            if YT_COOKIES_CONTENT:
                temp_cookies = create_temp_cookies_file(YT_COOKIES_CONTENT)
                if temp_cookies:
                    opts["cookiefile"] = temp_cookies
            elif YT_COOKIES_FILE and Path(YT_COOKIES_FILE).exists():
                opts["cookiefile"] = YT_COOKIES_FILE
    
    return opts

async def download_video(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    """Скачать видео асинхронно"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_download, url)

def _sync_download(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    """Синхронная функция для скачивания видео"""
    import shutil
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        opts = get_ydl_opts(url)
        opts["outtmpl"] = str(temp_dir / "%(id)s.%(ext)s")
        
        log.info(f"Starting download for URL: {url}")
        log.info(f"Using yt-dlp options: {opts}")
        
        with YoutubeDL(opts) as ydl:
            try:
                # Сначала попытаемся получить информацию без загрузки
                log.info("Extracting video info...")
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    log.error("Failed to extract video info")
                    return None, None
                    
                log.info(f"Video info extracted: title='{info.get('title', 'N/A')}', duration={info.get('duration', 'N/A')}")
                
                # Теперь загружаем
                log.info("Starting video download...")
                info = ydl.extract_info(url, download=True)
                
                if info:
                    # Найти скачанный файл
                    video_path = Path(ydl.prepare_filename(info))
                    log.info(f"Expected file path: {video_path}")
                    
                    if video_path.exists():
                        log.info(f"Video downloaded successfully: {video_path} (size: {video_path.stat().st_size} bytes)")
                        return video_path, info
                    else:
                        # Попробовать найти файл в temp_dir
                        log.warning("Expected file not found, searching in temp directory...")
                        video_files = list(temp_dir.glob("*"))
                        log.info(f"Files in temp directory: {video_files}")
                        
                        for file in temp_dir.glob("*"):
                            if file.is_file() and file.suffix.lower() in ['.mp4', '.mkv', '.webm', '.mov', '.avi', '.flv']:
                                log.info(f"Found video file: {file} (size: {file.stat().st_size} bytes)")
                                return file, info
                        
                        log.error("No video files found in temp directory")
                        
                return None, None
                
            except DownloadError as e:
                error_msg = str(e).lower()
                log.error(f"yt-dlp Download error: {e}")
                
                # Обработка специфичных ошибок
                if "private" in error_msg or "login" in error_msg:
                    log.error("Video is private or requires authentication")
                elif "not available" in error_msg or "removed" in error_msg:
                    log.error("Video is not available or has been removed")
                elif "geo" in error_msg or "country" in error_msg:
                    log.error("Video is geo-blocked")
                elif "copyright" in error_msg:
                    log.error("Video is blocked due to copyright")
                else:
                    log.error(f"Unknown download error: {error_msg}")
                    
                return None, None
                
            except Exception as e:
                log.error(f"Unexpected error during yt-dlp extraction: {e}")
                return None, None
                
    except Exception as e:
        log.error(f"Unexpected error during download setup: {e}")
        return None, None
    finally:
        # Cleanup temporary directory and all its contents
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as cleanup_error:
            log.warning(f"Failed to cleanup temp directory {temp_dir}: {cleanup_error}")

async def extract_recipe_from_video(video_info: dict) -> str:
    """Извлечь рецепт из информации о видео используя OpenAI"""
    try:
        # Используем title и description для анализа
        title = video_info.get('title', '')
        description = video_info.get('description', '')
        
        # Если нет полезной информации, возвращаем базовое сообщение
        if not title and not description:
            return "🤖 Не удалось извлечь рецепт из видео. Попробуйте другое видео с более подробным описанием."
        
        prompt = f"""
        Проанализируй следующую информацию о видео и извлеки рецепт, если он есть:

        Заголовок: {title}
        Описание: {description}

        Если в видео есть рецепт, выведи его в следующем формате:
        📝 **Рецепт: [название блюда]**
        
        🥘 **Ингредиенты:**
        • [список ингредиентов]
        
        👨‍🍳 **Приготовление:**
        1. [пошаговые инструкции]

        💡 **Дополнительно:**
        Если есть дополнительная информация или описание, добавь её сюда.

        Если рецепта нет, просто напиши "В этом видео нет рецепта." и при возможности добавь дополнительную информацию из описания.
        """
        
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Ты помощник, который извлекает рецепты из описаний видео. Отвечай на русском языке."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7
            )
        )
        
        recipe = response.choices[0].message.content.strip()
        return recipe if recipe else "🤖 Не удалось извлечь рецепт из видео."
        
    except Exception as e:
        log.error(f"Error extracting recipe: {e}")
        return "🤖 Ошибка при извлечении рецепта. Попробуйте позже."

def is_supported_url(url: str) -> bool:
    """Проверить, поддерживается ли URL"""
    supported_patterns = [
        # Instagram
        'instagram.com/reel', 'instagram.com/p/', 'instagram.com/tv/',
        # TikTok
        'tiktok.com/@', 'tiktok.com/t/', 'vm.tiktok.com', 'vt.tiktok.com',
        # YouTube
        'youtube.com/shorts/', 'youtu.be/', 'youtube.com/watch?v=',
    ]
    
    try:
        parsed = urlparse(url.lower())
        domain = parsed.netloc.lower()
        path = parsed.path.lower()
        full_url = f"{domain}{path}"
        
        # Проверяем основные домены
        if any(domain in ['instagram.com', 'www.instagram.com'] for domain in [domain]):
            return '/reel' in path or '/p/' in path or '/tv/' in path
        
        if any(domain in ['tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'] for domain in [domain]):
            return True
            
        if any(domain in ['youtube.com', 'www.youtube.com', 'youtu.be', 'www.youtu.be'] for domain in [domain]):
            return '/shorts/' in path or 'youtu.be' in domain or 'v=' in parsed.query
        
        # Дополнительная проверка по паттернам
        return any(pattern in full_url for pattern in supported_patterns)
        
    except (ValueError, TypeError, AttributeError) as e:
        log.warning(f"Invalid URL format: {url}, error: {e}")
        return False

# Welcome текст
WELCOME = """
🔥 **Recipe Bot** — извлекаю рецепт из короткого видео!

Бесплатно доступно **6** роликов.
Тарифы (скоро):

- 10 роликов — 49 ₽
- 200 роликов + 30 дн. — 199 ₽

Пришлите ссылку на видео с рецептом, а я скачаю его и извлеку рецепт!

**Поддерживаемые форматы:**
📱 Instagram: Reels (/reel/, /p/, /tv/)
🎵 TikTok: @username/video/, vm.tiktok.com, vt.tiktok.com
📺 YouTube: Shorts (/shorts/), обычные видео

**Пример ссылок:**
• instagram.com/reel/xyz...
• tiktok.com/@user/video/123...
• youtube.com/shorts/abc...
""".strip()

# Handlers
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        escape_markdown_v2(WELCOME),
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )

async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать статус использования"""
    uid = update.effective_user.id
    used = get_quota_usage(uid)
    
    if uid == OWNER_ID:
        status_text = "👑 Вы владелец бота - неограниченное использование"
    else:
        remaining = max(0, FREE_LIMIT - used)
        status_text = f"📊 Использовано: {used}/{FREE_LIMIT}\n🆓 Осталось: {remaining}"
    
    await update.message.reply_text(status_text, parse_mode=None)

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик URL-ов"""
    url = update.message.text.strip()
    uid = update.effective_user.id

    await update.message.reply_text("🏃 Скачиваю...", parse_mode=None)

    # Проверка поддерживаемых URL
    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ Неподдерживаемый формат ссылки!\n\n"
            "✅ Поддерживаются:\n"
            "📱 Instagram: /reel/, /p/, /tv/\n"
            "🎵 TikTok: @username/video/, vm.tiktok.com, vt.tiktok.com\n"
            "📺 YouTube: /shorts/, обычные видео\n\n"
            "Примеры правильных ссылок:\n"
            "• instagram.com/reel/CXXxXxX...\n"
            "• tiktok.com/@username/video/123...\n"
            "• youtube.com/shorts/abc123...",
            parse_mode=None
        )
        return

    # Проверка лимита
    if uid != OWNER_ID:
        current_usage = get_quota_usage(uid)
        if current_usage >= FREE_LIMIT:
            await update.message.reply_text(
                "ℹ️ Лимит бесплатных роликов исчерпан.",
                parse_mode=None
            )
            return

    # Показать "печатает..."
    await update.message.chat.send_action(constants.ChatAction.TYPING)

    # --- fallback_md должен быть определён всегда ---
    fallback_blocks = {
        "title": "Не удалось извлечь рецепт",
        "ingredients": [],
        "steps": [],
        "extra": "🤖 Не удалось извлечь рецепт из видео. Попробуйте самостоятельно посмотреть описание ролика или текст под видео."
    }
    fallback_md = format_recipe_markdown(
        fallback_blocks,
        original_url=url,
        duration=""
    )
    video_info = None
    video_path = None
    try:
        # Скачиваем видео
        video_path, video_info = await download_video(url)

        # Если video_info уже есть — обновляем fallback_md с его данными
        if video_info:
            fallback_blocks["title"] = video_info.get("title", "Не удалось извлечь рецепт").strip()
            fallback_md = format_recipe_markdown(
                fallback_blocks,
                original_url=video_info.get("webpage_url", url),
                duration=str(int(video_info.get("duration", 0))) + " сек." if "duration" in video_info else ""
            )

        # Явный лог, если video_path или video_info отсутствуют
        if not video_path or not video_path.exists():
            log.error(f"Download failed or file does not exist for url: {url}")
            
            # Определяем тип ошибки на основе платформы
            if "instagram.com" in url:
                error_msg = "❌ Не удалось скачать Instagram Reels. Возможные причины:\n• Видео приватное или требует входа в аккаунт\n• Видео было удалено\n• Временные проблемы с Instagram API"
            elif "tiktok.com" in url:
                error_msg = "❌ Не удалось скачать TikTok видео. Возможные причины:\n• Видео приватное\n• Видео недоступно в вашем регионе\n• Видео было удалено автором"
            elif "youtube.com" in url or "youtu.be" in url:
                error_msg = "❌ Не удалось скачать YouTube видео. Возможные причины:\n• Видео приватное или ограничено по возрасту\n• Видео недоступно в вашем регионе\n• Проблемы с авторскими правами"
            else:
                error_msg = "❌ Не удалось скачать видео. Попробуйте другую ссылку."
                
            await update.message.reply_text(error_msg, parse_mode=None)
            
            # Отправляем fallback_md (текстовый блок)
            await update.message.reply_text(
                fallback_md,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return
            
        if not video_info:
            log.error(f"Download returned no video_info for url: {url}")
            await update.message.reply_text(
                "❌ Не удалось получить информацию о видео. Попробуйте другую ссылку.",
                parse_mode=None
            )
            await update.message.reply_text(
                fallback_md,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        # Проверяем размер файла (Telegram лимит 50MB)
        file_size = video_path.stat().st_size
        if file_size > 50 * 1024 * 1024:  # 50MB
            await update.message.reply_text(
                "❌ Видео слишком большое для отправки (максимум 50MB).",
                parse_mode=None
            )
            video_path.unlink(missing_ok=True)
            await update.message.reply_text(
                fallback_md,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        # Отправляем видео СРАЗУ (caption пустой, текст отдельным сообщением)
        with open(video_path, 'rb') as video_file:
            await update.message.reply_video(
                video=video_file,
                caption="",  # Caption пустой или максимум короткое название
                parse_mode=None
            )

        # fallback_md уже актуален (с web_url и title)
        try:
            recipe = await extract_recipe_from_video(video_info)
            log.info(f"Extracted recipe raw:\n{recipe}")
            blocks = parse_recipe_blocks(recipe)
            # Если текст пустой, или только с ошибкой — fallback шаблон
            invalid = (
                not recipe or recipe.strip().startswith("🤖")
                or not (blocks["title"] or blocks["ingredients"] or blocks["steps"])
            )
            if invalid:
                md = fallback_md
            else:
                md = format_recipe_markdown(
                    blocks,
                    original_url=video_info.get("webpage_url", url),
                    duration=str(int(video_info.get("duration", 0))) + " сек." if "duration" in video_info else ""
                )
        except Exception as err:
            log.error(f"Ошибка извлечения рецепта: {err}")
            md = fallback_md  # fallback по шаблону!

        await update.message.reply_text(
            md,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

        # Увеличиваем счетчик использования
        if uid != OWNER_ID:
            increment_quota(uid)

        # Удаляем временный файл
        video_path.unlink(missing_ok=True)

    except Exception as e:
        log.error(f"Error processing URL {url}: {e}")
        
        # Определяем тип ошибки и даем соответствующее сообщение
        error_type = type(e).__name__
        if "timeout" in str(e).lower():
            error_msg = "⏱️ Превышено время ожидания. Попробуйте позже или другую ссылку."
        elif "network" in str(e).lower() or "connection" in str(e).lower():
            error_msg = "🌐 Проблемы с сетью. Проверьте соединение и повторите попытку."
        elif "permission" in str(e).lower() or "access" in str(e).lower():
            error_msg = "🔒 Ошибка доступа к видео. Возможно, оно приватное."
        else:
            error_msg = f"❌ Произошла ошибка при обработке видео.\nТип ошибки: {error_type}\n\nПопробуйте другую ссылку или повторите попытку позже."
        
        await update.message.reply_text(error_msg, parse_mode=None)
        
        # fallback_md уже определён (на случай если видео_info нет — минимальный шаблон)
        await update.message.reply_text(
            fallback_md,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

# Health check для Render
async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint"""
    return web.Response(text="OK", status=200)

def create_web_app(application: Application) -> web.Application:
    """Создать веб-приложение для health check и webhook"""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    async def telegram_webhook(request: web.Request) -> web.Response:
        data = await request.json()
        await application.process_update(Update.de_json(data, application.bot))
        return web.Response(text="OK")

    app.router.add_post("/", telegram_webhook)
    return app

async def main() -> None:
    """Основная функция"""
    init_db()
    
    # Создаем Telegram приложение
    application = Application.builder().token(TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    # Создаем веб-сервер для health check + webhook
    web_app = create_web_app(application)
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    
    log.info(f"Health check server started on port {port}")
    
    # Запускаем Telegram бота
    await application.initialize()
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
    if WEBHOOK_URL:
        await application.bot.set_webhook(url=WEBHOOK_URL)
        log.info(f"Webhook set to: {WEBHOOK_URL}")
        await application.start()  # Запускать только если webhook!
    else:
        # Для локальной отладки можно fallback на polling
        log.warning("WEBHOOK_URL не задан. Запуск через polling (локально).")
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
    
    log.info("Recipe bot started successfully!")
    
    try:
        # Держим приложение запущенным
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        # Корректное завершение
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
