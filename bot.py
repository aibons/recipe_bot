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
from telegram.ext import (
    Application, ContextTypes,
    CommandHandler, MessageHandler, filters,
)
from telegram.ext.webhookhandler import WebhookRequestHandler
# ENV
load_dotenv()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is required")

# Cookie файлы (опционально)
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")
TT_COOKIES_FILE = os.getenv("TT_COOKIES_FILE", "")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "")

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
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect("data/usage.db") as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS quota(
                uid INTEGER PRIMARY KEY, 
                n INTEGER DEFAULT 0
            )
        """)
        db.commit()

def get_quota_usage(uid: int) -> int:
    """Получить текущее использование квоты"""
    with sqlite3.connect("data/usage.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        return row[0] if row else 0

def increment_quota(uid: int) -> int:
    """Увеличить счетчик использования и вернуть новое значение"""
    with sqlite3.connect("data/usage.db") as db:
        cur = db.execute("SELECT n FROM quota WHERE uid=?", (uid,))
        row = cur.fetchone()
        current = row[0] if row else 0
        new_count = current + 1
        db.execute("INSERT OR REPLACE INTO quota(uid,n) VALUES(?,?)", (uid, new_count))
        db.commit()
        return new_count

def get_ydl_opts(url: str) -> dict:
    """Получить опции yt-dlp для конкретного URL"""
    opts = {
        "format": "best[height<=720]/best",
        "quiet": True,
        "no_warnings": True,
        "extractaudio": False,
        "audioformat": "mp3",
        "outtmpl": "%(id)s.%(ext)s",
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    
    # Добавляем cookies если они есть
    if "instagram.com" in url and IG_COOKIES_FILE and Path(IG_COOKIES_FILE).exists():
        opts["cookiefile"] = IG_COOKIES_FILE
    elif "tiktok.com" in url and TT_COOKIES_FILE and Path(TT_COOKIES_FILE).exists():
        opts["cookiefile"] = TT_COOKIES_FILE
    elif ("youtube.com" in url or "youtu.be" in url) and YT_COOKIES_FILE and Path(YT_COOKIES_FILE).exists():
        # Дополнительно проверим для youtube.com/shorts
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc or "/shorts" in parsed.path:
            opts["cookiefile"] = YT_COOKIES_FILE
        elif "youtube.com" in parsed.netloc:
            opts["cookiefile"] = YT_COOKIES_FILE
    
    return opts

async def download_video(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    """Скачать видео асинхронно"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_download, url)

def _sync_download(url: str) -> Tuple[Optional[Path], Optional[dict]]:
    """Синхронная функция для скачивания видео"""
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        opts = get_ydl_opts(url)
        opts["outtmpl"] = str(temp_dir / "%(id)s.%(ext)s")
        
        with YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                if info:
                    # Найти скачанный файл
                    video_path = Path(ydl.prepare_filename(info))
                    if video_path.exists():
                        return video_path, info
                    else:
                        # Попробовать найти файл в temp_dir
                        for file in temp_dir.glob("*"):
                            if file.is_file() and file.suffix in ['.mp4', '.mkv', '.webm', '.mov']:
                                return file, info
                return None, None
            except DownloadError as e:
                log.error(f"Download error: {e}")
                return None, None
    except Exception as e:
        log.error(f"Unexpected error during download: {e}")
        return None, None

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
    supported_domains = [
        'instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be',
        'vm.tiktok.com', 'vt.tiktok.com'
    ]
    
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return any(supported in domain for supported in supported_domains)
    except:
        return False

# Welcome текст
WELCOME = """
🔥 **Recipe Bot** — сохраняю рецепт из короткого видео!

Бесплатно доступно **6** роликов.
Тарифы (скоро):

- 10 роликов — 49 ₽
- 200 роликов + 30 дн. — 199 ₽

Пришлите ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!

**Поддерживаемые платформы:**
- Instagram Reels
- TikTok
- YouTube Shorts
""".strip()

# Handlers
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        WELCOME,
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
    
    await update.message.reply_text(status_text)

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик URL-ов"""
    url = update.message.text.strip()
    uid = update.effective_user.id

    await update.message.reply_text("🏃 Скачиваю...")

    # Проверка поддерживаемых URL
    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ Поддерживаются только ссылки на Instagram Reels, TikTok и YouTube Shorts"
        )
        return
    
    # Проверка лимита
    if uid != OWNER_ID:
        current_usage = get_quota_usage(uid)
        if current_usage >= FREE_LIMIT:
            await update.message.reply_text("ℹ️ Лимит бесплатных роликов исчерпан.")
            return
    
    # Показать "печатает..."
    await update.message.chat.send_action(constants.ChatAction.TYPING)
    
    try:
        # Скачиваем видео
        video_path, video_info = await download_video(url)
        
        if not video_path or not video_path.exists():
            await update.message.reply_text("❌ Не удалось скачать видео. Возможно, оно приватное или требует аутентификацию.")
            # Отправляем рецепт с сообщением об ошибке, чтобы был фидбек
            recipe = "🤖 Не удалось скачать видео. Попробуйте другую ссылку."
            await update.message.reply_text(recipe, parse_mode=constants.ParseMode.MARKDOWN)
            return
        
        # Проверяем размер файла (Telegram лимит 50MB)
        file_size = video_path.stat().st_size
        if file_size > 50 * 1024 * 1024:  # 50MB
            await update.message.reply_text("❌ Видео слишком большое для отправки (максимум 50MB).")
            video_path.unlink(missing_ok=True)
            # Отправляем рецепт с сообщением об ошибке, чтобы был фидбек
            recipe = "🤖 Видео слишком большое для отправки. Попробуйте другое видео."
            await update.message.reply_text(recipe, parse_mode=constants.ParseMode.MARKDOWN)
            return
        
        # Извлекаем рецепт
        recipe = await extract_recipe_from_video(video_info)
        
        # Отправляем видео без длинного caption
        with open(video_path, 'rb') as video_file:
            await update.message.reply_video(
                video=video_file,
                caption="",  # Caption пустой или максимум короткое название
                parse_mode=None
            )
        
        # Разбить рецепт на секции
        blocks = parse_recipe_blocks(recipe)
        if not (blocks["title"] or blocks["ingredients"] or blocks["steps"] or blocks["extra"]):
            # fallback — просто экранируем оригинальный текст
            md = escape_markdown_v2(recipe)
        else:
            md = format_recipe_markdown(blocks)

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
        
        # После отправки видео, отправить рецепт отдельно (дублируется выше, но если нужно еще раз, можно удалить этот комментарий)
        # await update.message.reply_text(md, parse_mode=constants.ParseMode.MARKDOWN)
        
    except Exception as e:
        log.error(f"Error processing URL {url}: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке видео. Попробуйте позже.")

# Health check для Render
async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint"""
    return web.Response(text="OK", status=200)

def create_web_app(application: Application) -> web.Application:
    """Создать веб-приложение для health check и webhook"""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_post("/", WebhookRequestHandler(application))
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


def format_recipe_markdown(recipe: dict) -> str:
    lines = []
    if recipe.get("title"):
        lines.append(f"*{escape_markdown_v2('Рецепт: ' + recipe['title'])}*")
    if recipe.get("ingredients"):
        lines.append(f"\n*{escape_markdown_v2('Ингредиенты:')}*")
        for i in recipe['ingredients']:
            lines.append(f"- {escape_markdown_v2(i)}")
    if recipe.get("steps"):
        lines.append(f"\n*{escape_markdown_v2('Приготовление:')}*")
        for idx, s in enumerate(recipe['steps'], 1):
            lines.append(f"{idx}. {escape_markdown_v2(s)}")
    if recipe.get("extra"):
        lines.append(f"\n*{escape_markdown_v2('Дополнительно:')}*\n{escape_markdown_v2(recipe['extra'])}")
    return "\n".join(lines)

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
    m = re.search(r"[Рр]ецепт:?\s*([^\n*]*)", text)
    if m:
        blocks["title"] = m.group(1).strip(" *")
    # Ингредиенты
    ingr = re.search(r"[Ии]нгредиенты:?\**\n(.+?)(\n[Пп]риг|[\n\d]+\.)", text, re.DOTALL)
    if ingr:
        ingr_lines = [i.strip('•-').strip() for i in ingr.group(1).strip().split('\n') if i.strip()]
        blocks["ingredients"] = ingr_lines
    # Шаги
    steps = re.search(r"[Пп]риготовление:?\**\n(.+?)(\n[Дд]ополнительно|$)", text, re.DOTALL)
    if steps:
        steps_lines = [s.strip("0123456789. ").strip() for s in steps.group(1).split('\n') if s.strip()]
        blocks["steps"] = steps_lines
    # Дополнительно
    extra = re.search(r"[Дд]ополнительно:?\**\n(.+)", text, re.DOTALL)
    if extra:
        blocks["extra"] = extra.group(1).strip()
    return blocks

# Экранирование Markdown V2
def escape_markdown_v2(text: str) -> str:
    """Экранирует спецсимволы для Markdown V2 Telegram"""
    chars = r"\_*[]()~`>#+-=|{}.!"
    return ''.join(f"\\{c}" if c in chars else c for c in text)