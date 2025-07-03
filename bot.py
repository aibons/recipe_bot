##############################################################################
# bot.py – актуальная версия с микро-HTTP сервером, paywall (пока выключен), #
# правильным порядком initialize / start и исключением ID 248610561 из лимитов
##############################################################################

from __future__ import annotations
import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import textwrap
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from telegram import (
    Update,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
)
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
import openai
# ─────────────── ЛОГГЕР (добавь эти две строки) ────────────────
import logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# _______________________________________________________________

# ──────────────────────────── Константы и env ──────────────────────────────
load_dotenv()
TOKEN          = os.environ["TELEGRAM_TOKEN"]
OPENAI_KEY     = os.environ["OPENAI_API_KEY"]
PROVIDER_TOKEN = os.getenv("YOOKASSA_TOKEN", "")      # будет нужен, когда ENABLE_PAYMENTS = True
openai.api_key = OPENAI_KEY

ADMIN_IDS       = {248610561}
FREE_LIMIT      = 6

PKG100_PRICE    = 299_00        # 100 роликов
SUB_PRICE       = 199_00        # подписка 199 ₽
SUB_VOLUME      = 200           # 200 роликов
SUB_DAYS        = 30            # срок подписки

ENABLE_PAYMENTS = False         # включить True, когда настроишь YooKassa

TMP_WAV   = ".wav"
LONG_SIDE = 720                 # итоговая длинная сторона

YDL_BASE = {
    "quiet": True,
    "outtmpl": "%(id)s.%(ext)s",
    "merge_output_format": "mp4",
}

# ──────────────────────────────── БД (SQLite) ──────────────────────────────
DB = sqlite3.connect("bot.db")
DB.execute(
    """CREATE TABLE IF NOT EXISTS users(
         uid        INTEGER PRIMARY KEY,
         used       INTEGER DEFAULT 0,
         balance    INTEGER DEFAULT 0,
         paid_until DATE DEFAULT NULL
       )"""
)
DB.commit()


# ──────────────────────────────── Утилиты учёта ────────────────────────────
def quota(uid: int) -> tuple[int, int]:
    """Вернём (used, balance) с учётом истёкшей подписки."""
    used, balance, until = 0, 0, None
    row = DB.execute(
        "SELECT used, balance, paid_until FROM users WHERE uid=?", (uid,)
    ).fetchone()
    if row:
        used, balance, until = row
    if until and dt.date.today() > dt.date.fromisoformat(until):
        balance = 0
    return used, balance


def add_usage(uid: int, delta: int = 1) -> None:
    DB.execute(
        "INSERT INTO users(uid, used) VALUES(?, 0) "
        "ON CONFLICT(uid) DO UPDATE SET used = used + ?",
        (uid, delta),
    )
    DB.commit()


def add_balance(uid: int, add: int = 0, days: int = 0) -> None:
    """add — сколько роликов добавить, days — продлить подписку."""
    used, balance = quota(uid)
    balance += add
    new_until = (
        (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else None
    )
    DB.execute(
        "INSERT INTO users(uid, balance, paid_until) VALUES(?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET balance=?, paid_until=?",
        (uid, balance, new_until, balance, new_until),
    )
    DB.commit()


# ────────────────────────────── FFmpeg helpers ─────────────────────────────
def ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", *map(str, args)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def extract_audio(src: Path, dst: Path) -> bool:
    try:
        ffmpeg("-y", "-i", src, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", dst)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False


def normalize(src: Path) -> Path:
    """Любой вход → MP4 H264, длинная сторона 720, square-pixels."""
    dst = src.with_name(src.stem + "_720.mp4")
    vf = f"scale='if(gt(iw,ih),{LONG_SIDE},-2)':'if(gt(iw,ih),-2,{LONG_SIDE})',setsar=1"
    ffmpeg(
        "-y", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", dst
    )
    return dst


# ───────────────────────────── Загрузка ролика ─────────────────────────────
def download(url: str) -> tuple[Path, dict]:
    formats = [
        "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "best[height<=720]",
        "best",
    ]
    for fmt in formats:
        try:
            with YoutubeDL({**YDL_BASE, "format": fmt}) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info)), info
        except DownloadError:
            continue
    raise RuntimeError("Не смог скачать ролик :(")


# ────────────────────────────── Форматирование текста ──────────────────────
EMOJI = {
    "лимон": "🍋", "кекс": "🧁", "крылышк": "🍗", "пицц": "🍕", "салат": "🥗",
    "бургер": "🍔", "шокол": "🍫", "суп": "🥣", "паста": "🍝", "рыб": "🐟",
    "куриц": "🐔", "фрикадел": "🍽️"
}
LABEL = {
    "servingsuggestion": "Совет по подаче",
    "preparationtime": "Время подготовки",
    "cookingtime": "Время готовки",
    "totaltime": "Общее время",
    "garnish": "Гарнир"
}


def icon(title: str) -> str:
    return next((e for k, e in EMOJI.items() if k in title.lower()), "🍽️")


fmt_ing = lambda i: f"• {i.get('name')} — {i.get('quantity')}".rstrip(" —") if isinstance(i, dict) else f"• {i}"
fmt_step = lambda n, s: f"{n}. {(s.get('step') if isinstance(s, dict) else s)}"
fmt_extra = lambda e: "\n".join(
    f"• {LABEL.get(k, k)}: {v}" for k, v in e.items()
) if isinstance(e, dict) else str(e)


# ─────────────────────────────── Приветствие ───────────────────────────────
WELCOME = textwrap.dedent(f"""
🔥 *Recipe Bot* — превращаю короткие кулинарные видео в пошаговый рецепт!

🆓 У тебя *{FREE_LIMIT}* бесплатных видео.
Платные тарифы (скоро):

•  100 роликов — *299 ₽*  
•  200 роликов + 30 дней — *199 ₽*

Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!
""").strip()


# ─────────────────────────────── Telegram Handlers ─────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(WELCOME)


async def buy100(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAYMENTS:
        await update.message.reply_text("Оплата пока отключена 🙈")
        return
    await ctx.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Пакет 100 роликов",
        description="Единоразово +100 роликов без срока действия.",
        payload="pkg100",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice("100 роликов", PKG100_PRICE)],
    )


async def subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAYMENTS:
        await update.message.reply_text("Оплата пока отключена 🙈")
        return
    await ctx.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Подписка (200 роликов, 30 дней)",
        description="Каждый месяц 200 роликов, действует 30 дней.",
        payload="sub",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice("Подписка 30 дней", SUB_PRICE)],
    )


async def precheckout(pre: PreCheckoutQuery, ctx: ContextTypes.DEFAULT_TYPE):
    await pre.answer(ok=True)


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload == "pkg100":
        add_balance(uid, add=100)
        msg = "✅ +100 роликов успешно начислено!"
    elif payload == "sub":
        add_balance(uid, add=SUB_VOLUME, days=SUB_DAYS)
        msg = f"✅ Подписка активна до {(dt.date.today()+dt.timedelta(days=SUB_DAYS)).isoformat()}."
    else:
        msg = "Получен неизвестный платёж 🤔"
    await update.message.reply_text(msg)


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if not re.search(r"(instagram|tiktok|youtu)", text):
        await update.message.reply_text("Пришли ссылку на Instagram / TikTok / YouTube ролик 😉")
        return

    # ────────── лимиты ──────────
    if uid not in ADMIN_IDS:
        used, balance = quota(uid)
        if balance > 0:
            add_balance(uid, add=-1)     # списать один
        elif used >= FREE_LIMIT:
            await update.message.reply_text(
                f"🔒 Бесплатный лимит {FREE_LIMIT} роликов исчерпан.\n"
                "Скоро появится оплата.\nПока можешь попробовать с другого аккаунта 😉"
            )
            return
        else:
            add_usage(uid)

    await update.message.reply_text("🏃 Скачиваю…")
    raw, meta = download(text)
    if (meta.get("duration") or 0) > 120:
        await update.message.reply_text("❌ Видео длиннее 2 минут")
        return

    vid = normalize(raw)
    wav = Path(tempfile.mktemp(suffix=TMP_WAV))
    whisper = ""
    if extract_audio(vid, wav):
        with wav.open("rb") as f:
            whisper = openai.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru", response_format="text"
            )

    system_prompt = (
        "Ты кулинарный помощник. Верни JSON "
        "{title, ingredients[], steps[], extra?}. "
        "ingredients — массив объектов name+quantity."
    )
    answer = openai.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": textwrap.dedent(
                    f"""
                    Подпись:
                    {meta.get('description', '')}
                    ---
                    Транскрипт:
                    {whisper or '[аудио недоступно]'}
                    """
                ),
            },
        ],
    )
    recipe = json.loads(answer.choices[0].message.content)

    # ────────── отправляем видео
    vmsg = await update.message.reply_video(
        vid.read_bytes(), supports_streaming=True
    )

    # ────────── формируем текст
    lines = [
        f"*{icon(recipe.get('title', 'Рецепт'))} {recipe.get('title', 'Рецепт')}*\n",
        "🛒 *Ингредиенты*",
        *[fmt_ing(i) for i in recipe.get("ingredients", [])],
        "\n⸻\n",
        "👩‍🍳 *Шаги приготовления*",
        *[
            fmt_step(n + 1, s)
            for n, s in enumerate(recipe.get("steps", []))
        ],
    ]
    if recipe.get("extra"):
        lines += ["\n⸻\n", "💡 *Дополнительно*", fmt_extra(recipe["extra"])]
    lines += ["\n⸻\n", f"🔗 [Оригинал]({text})"]

    await ctx.bot.send_message(
        update.effective_chat.id,
        "\n".join(lines)[:4000],
        parse_mode="Markdown",
        reply_to_message_id=vmsg.message_id,
        disable_web_page_preview=True,
    )


# ────────────────────────────── Aiohttp healthcheck ────────────────────────
async def health(_):  # noqa: D401
    return web.Response(text="ok")


def aio_app():
    app = web.Application()
    app.router.add_get("/health", health)
    return app


# ─────────────────────────────── main (asyncio) ────────────────────────────
async def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # ----- handlers -----
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    if ENABLE_PAYMENTS:
        app.add_handler(CommandHandler("buy100", buy100))
        app.add_handler(CommandHandler("subscribe", subscribe))
        app.add_handler(PreCheckoutQueryHandler(precheckout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

   # ----- START -----
await app.initialize()          # 1. подготовка
await app.start()               # 2. запускаем Application
await app.updater.start_polling()   # 3. начинаем long-polling

# aiohttp healthcheck идёт параллельно
await web._run_app(aio_app(), host="0.0.0.0", port=8080)

if __name__ == "__main__":
    asyncio.run(main())
