#!/usr/bin/env python
#  bot.py • Recipe-Bot (Telegram)
# ────────────────────────────────────────────────────────────────
#  • long-polling:  initialize → start → updater.start_polling()
#  • ответы экранируем  escape_markdown(V2)
#  • приветствие + рецепт → Markdown V2
#  • IG / TikTok / YT-Shorts — via cookies
#  • ускоренная загрузка aria2c (если есть)
#  • health-check на :8080  (Render Free)
#  • Paywall (ЮMoney)   ENABLE_PAY = True
# ────────────────────────────────────────────────────────────────

from __future__ import annotations

# ===== стандартные =====
import asyncio, datetime as dt, json, os, re, sqlite3, subprocess, tempfile, textwrap
from pathlib import Path
import logging, shutil

# ===== сторонние =====
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
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ─────────── ENV ───────────
load_dotenv()

TOKEN           = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
IG_SESSIONID    = os.getenv("IG_SESSIONID")          # Instagram cookie
TT_SESSIONID    = os.getenv("TT_SESSIONID")          # TikTok cookie (sid_tt)
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE")       # cookies_youtube.txt
ENABLE_PAY      = bool(int(os.getenv("ENABLE_PAY", "0")))  # 0/1
OWNER_ID        = int(os.getenv("OWNER_ID", "248610561"))  # безлимит

openai.api_key = OPENAI_API_KEY
logging.basicConfig(level="INFO", format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("recipe_bot")

# ─────────── yt-dlp BASE OPTIONS ───────────
YDL_BASE = {
    "quiet": True,
    "outtmpl": "%(id)s.%(ext)s",
    "merge_output_format": "mp4",
}


# ───────────────────────────────────────────────────── helpers ──
def escape(s: str) -> str:
    return escape_markdown(str(s), version=2)


def extract_audio(src: Path, dst: Path) -> bool:
    """ffmpeg -> wav (Whisper)"""
    try:
        subprocess.check_call(
            ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False


# ───────────— DOWNLOAD —──────────
def download(url: str) -> tuple[Path, dict]:
    """
    Скачивает ролик (IG / TikTok / YT-Shorts) с учётом куков.
    Возвращает (файл, meta).
    """
    opts = YDL_BASE.copy()

    # Instagram cookie
    if IG_SESSIONID and "instagram.com" in url:
        ck = Path("ig_cookie.txt")
        if not ck.exists():
            ck.write_text(f".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\t{IG_SESSIONID}\n")
        opts["cookiefile"] = str(ck)

    # TikTok cookie (sid_tt)
    if TT_SESSIONID and "tiktok.com" in url:
        ck = Path("tt_cookie.txt")
        if not ck.exists():
            ck.write_text(f".tiktok.com\tTRUE\t/\tFALSE\t0\tsid_tt\t{TT_SESSIONID}\n")
        opts["cookiefile"] = str(ck)

    # YouTube cookies.txt
    if YT_COOKIES_FILE and ("youtu.be" in url or "youtube.com" in url):
        opts["cookiefile"] = YT_COOKIES_FILE

    # ускоряем скачивание
    if shutil.which("aria2c"):
        opts |= {
            "external_downloader": "aria2c",
            "external_downloader_args": ["-x", "8", "-k", "1M"],
        }

    fmts = [
        "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "best[height<=720]",
        "best",
    ]
    last_err = None
    for fmt in fmts:
        try:
            with YoutubeDL({**opts, "format": fmt}) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info)), info
        except DownloadError as e:
            last_err = e
            continue
    raise RuntimeError(f"Не смог скачать видео: {last_err}")


# ───────────— форматирование Markdown —──────────
EMOJI = {
    "лимон": "🍋", "кекс": "🧁", "крыл": "🍗", "бургер": "🍔",
    "паста": "🍝", "салат": "🥗", "суп": "🍲", "фрика": "🥘",
}


def fmt_ing(i: str) -> str:
    return f"• {escape(i)}"


def fmt_steps(lst: list[str]) -> str:
    return "\n".join(f"{n+1}. {escape(s)}" for n, s in enumerate(lst))


# ─────────────────────────────── handlers ──────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔥 *Recipe Bot* — помогаю сохранить рецепт из короткого видео!\n\n"
        "🆓 Доступно *6* бесплатных роликов.\n"
        "*Платные тарифы (скоро):*\n"
        "• 100 роликов — 299 ₽\n"
        "• 200 роликов + 30 дней — 199 ₽\n\n"
        "Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!"
    )
    await upd.message.reply_text(text, parse_mode="MarkdownV2")


async def handle(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = upd.message.text.strip()

    if "instagram.com" not in url and "youtube" not in url and "tiktok" not in url:
        return await upd.message.reply_text("Дай ссылку на Instagram, YouTube Shorts или TikTok 😉")

    await upd.message.reply_text("🏃 Скачиваю…")

    # скачиваем
    try:
        vid_fn, meta = download(url)
    except Exception as e:
        log.warning("download error: %s", e)
        return await upd.message.reply_text("❌ Не смог скачать это видео.")

    # ---- аудио → Whisper
    wav = Path(tempfile.mktemp(suffix=".wav"))
    if not extract_audio(vid_fn, wav):
        return await upd.message.reply_text("❌ Не смог извлечь звук.")

    whisper_txt = ""
    try:
        with open(wav, "rb") as f:
            wresp = openai.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru", response_format="text"
            )
            whisper_txt = wresp.text
    except Exception as e:
        log.warning("whisper error: %s", e)

    # ---- GPT: строим рецепт
    try:
        chat = openai.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Ты кулинарный помощник. Верни JSON {title, ingredients[], steps[], extra}"},
                {"role": "user", "content": (meta.get("description") or "") + "\n" + whisper_txt},
            ],
        )
        rec = json.loads(chat.choices[0].message.content)
    except Exception as e:
        log.warning("gpt error: %s", e)
        return await upd.message.reply_text("❌ Не смог разобрать рецепт.")

    # ---- формируем текст
    title = rec.get("title", "Рецепт")
    emoji = next((e for k, e in EMOJI.items() if k.lower() in title.lower()), "🍽️")

    lines = [
        f"*{escape(title)}* {emoji}",
        "",
        "*🛒 Ингредиенты*",
        *(fmt_ing(i) for i in rec.get("ingredients", [])),
        "",
        "*👩‍🍳 Шаги приготовления*",
        fmt_steps(rec.get("steps", [])),
    ]
    extra = rec.get("extra")
    if extra:
        lines += ["", "*💡 Дополнительно*", escape(extra)]
    lines += ["", "⸻", f"🔗 [Оригинал]({escape(url)})"]

    text_block = "\n".join(lines)[:4000]

    # ---- отправляем
    with open(vid_fn, "rb") as vf:
        await upd.message.reply_video(vf)
    await upd.message.reply_text(text_block, parse_mode="MarkdownV2")

    # ---- cleanup
    for p in (vid_fn, wav):
        try:
            p.unlink()
        except Exception:
            pass


# ─────────────────────────────── paywall (опционально) ─────────
async def buy100(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_invoice(
        title="Пакет 100 роликов",
        description="100 скачиваний рецептов",
        payload="buy100",
        provider_token=os.getenv("YUMONEY_TOKEN", ""),
        currency="RUB",
        prices=[LabeledPrice("100 роликов", 29900)],
    )


async def subscribe(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_invoice(
        title="Подписка 30 дней + 200 роликов",
        description="30 дней безлимита (до 200 шт)",
        payload="sub200",
        provider_token=os.getenv("YUMONEY_TOKEN", ""),
        currency="RUB",
        prices=[LabeledPrice("Подписка", 19900)],
    )


async def prechk(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.bot.answer_pre_checkout_query(upd.pre_checkout_query.id, ok=True)


async def paid(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text("✅ Оплата получена! Скоро тарифы заработают 😉")


# ─────────────────────────────── AioHTTP health ────────────────
async def aio_app():
    app = web.Application()

    async def health(req):
        return web.json_response({"status": "ok"})

    app.router.add_get("/health", health)
    return app


# ─────────────────────────────── MAIN ───────────────────────────
async def main():
    app = Application.builder().token(TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    if ENABLE_PAY:
        app.add_handler(CommandHandler("buy100", buy100))
        app.add_handler(CommandHandler("subscribe", subscribe))
        app.add_handler(PreCheckoutQueryHandler(prechk))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

    await app.initialize()  # подготовка

    # запускаем параллельно: Telegram-бот + health-сервер
    await asyncio.gather(
        app.start(),
        app.updater.start_polling(),
        web._run_app(aio_app(), host="0.0.0.0", port=8080),
    )


if __name__ == "__main__":
    asyncio.run(main())