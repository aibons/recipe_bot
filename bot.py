"""
Recipe Bot — Telegram
Ссылка → ролик ≤ 2 мин → нормализованное MP4 + рецепт
"""

from __future__ import annotations
import asyncio, datetime as dt, json, os, re, sqlite3, subprocess, tempfile, textwrap
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from telegram import (Update, LabeledPrice, PreCheckoutQuery,
                      SuccessfulPayment)
from telegram.ext import (Application, ContextTypes, MessageHandler,
                          CommandHandler, filters, PreCheckoutQueryHandler)
import openai

# ────────── конфиг ──────────
load_dotenv()
TOKEN          = os.environ["TELEGRAM_TOKEN"]
OPENAI_KEY     = os.environ["OPENAI_API_KEY"]
PROVIDER_TOKEN = os.getenv("YOOKASSA_TOKEN", "")
openai.api_key = OPENAI_KEY

ADMIN_IDS       = {248610561}
FREE_LIMIT      = 6
PKG100_PRICE    = 299_00   # копейки
SUB_PRICE       = 199_00   # копейки
SUB_DAYS        = 30
SUB_VOLUME      = 200
ENABLE_PAYMENTS = False    # ← включи True, когда решишь открыть оплату

DB = sqlite3.connect("bot.db")
DB.execute("""CREATE TABLE IF NOT EXISTS users(
  uid        INTEGER PRIMARY KEY,
  used       INTEGER DEFAULT 0,
  balance    INTEGER DEFAULT 0,
  paid_until DATE DEFAULT NULL
);""")
DB.commit()

YDL_BASE = {"quiet": True, "outtmpl": "%(id)s.%(ext)s",
            "merge_output_format": "mp4"}

# ────────── утилиты учёта ──────────
def quota(uid: int) -> tuple[int, int]:
    """возвращает (used, balance) с учётом истёкшей подписки"""
    used, bal, until = (0, 0, None)
    row = DB.execute("SELECT used,balance,paid_until FROM users WHERE uid=?", (uid,)).fetchone()
    if row:
        used, bal, until = row
    if until and dt.date.today() > dt.date.fromisoformat(until):
        bal = 0
    return used, bal

def add_usage(uid: int, delta=1):
    DB.execute("INSERT INTO users(uid,used) VALUES(?,0)"
               "ON CONFLICT(uid) DO UPDATE SET used=used+?",
               (uid, delta))
    DB.commit()

def add_balance(uid: int, add:int=0, days:int=0):
    used, bal = quota(uid)
    bal += add
    until = (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else None
    DB.execute("INSERT INTO users(uid,balance,paid_until) VALUES(?,?,?) "
               "ON CONFLICT(uid) DO UPDATE SET balance=?, paid_until=?",
               (uid, bal, until, bal, until))
    DB.commit()

# ────────── ffmpeg helpers ──────────
def ffmpeg(*args):
    subprocess.run(["ffmpeg", *args], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)

def extract_audio(src: Path, dst: Path) -> bool:
    try:
        ffmpeg("-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", dst)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False

def normalize(src: Path) -> Path:
    dst = src.with_name(src.stem + "_720.mp4")
    vf = "scale='if(gt(iw,ih),720,-2)':'if(gt(iw,ih),-2,720)',setsar=1"
    ffmpeg("-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", dst)
    return dst

# ────────── скачивание ──────────
def download(url: str) -> tuple[Path, dict]:
    fmts = ["bestvideo[height<=720]+bestaudio/best[height<=720]", "best[height<=720]", "best"]
    for f in fmts:
        try:
            with YoutubeDL({**YDL_BASE, "format": f}) as y:
                info = y.extract_info(url, download=True)
                return Path(y.prepare_filename(info)), info
        except DownloadError:
            continue
    raise RuntimeError("Не смог скачать ролик")

# ────────── приветствие ──────────
WELCOME = textwrap.dedent("""
🔥 *Recipe Bot* — превращаю короткие видео в понятный рецепт!

🆓 Тебе доступно *6* бесплатных видео.
Хочешь больше? Будут такие тарифы:
•  100 роликов — *299 ₽*  
•  Подписка *199 ₽ / 30 дн* — включает 200 роликов  
(оплата ЮMoney, появится совсем скоро)

Просто пришли ссылку на Reels / TikTok / Shorts — я всё сделаю.
""").strip()

# ────────── Telegram handlers ──────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(WELCOME)

async def buy100(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAYMENTS:
        await update.message.reply_text("Оплата пока отключена 🙈"); return
    await ctx.bot.send_invoice(update.effective_chat.id,
        "Пакет 100 роликов", "Единоразово +100 роликов.", "pkg100",
        PROVIDER_TOKEN, "RUB", [LabeledPrice("100 роликов", PKG100_PRICE)])

async def subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAYMENTS:
        await update.message.reply_text("Оплата пока отключена 🙈"); return
    await ctx.bot.send_invoice(update.effective_chat.id,
        "Подписка", "200 роликов, 30 дней.", "sub",
        PROVIDER_TOKEN, "RUB", [LabeledPrice("Подписка 30 дней", SUB_PRICE)])

async def precheckout(pre: PreCheckoutQuery, ctx: ContextTypes.DEFAULT_TYPE):
    await pre.answer(ok=True)

async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload == "pkg100":
        add_balance(uid, add=100)
        msg = "✅ +100 роликов! Спасибо за покупку."
    elif payload == "sub":
        add_balance(uid, add=SUB_VOLUME, days=SUB_DAYS)
        msg = f"✅ Подписка активна: +{SUB_VOLUME} роликов до {(dt.date.today()+dt.timedelta(days=SUB_DAYS)).isoformat()}."
    else:
        msg = "Платёж получен, но не распознан."
    await update.message.reply_text(msg)

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    url  = (update.message.text or "").strip()

    if not re.search(r"(instagram|tiktok|youtu)", url):
        await update.message.reply_text("Пришли ссылку на Instagram / TikTok / YouTube"); return

    # --- лимиты ---
    if uid not in ADMIN_IDS:
        used, bal = quota(uid)
        if bal:
            add_balance(uid, add=-1)
        elif used >= FREE_LIMIT:
            await update.message.reply_text(
                "🔒 Бесплатный лимит 6 роликов исчерпан.\n"
                "Скоро появится оплата (100 роликов / подписка).")
            return
        else:
            add_usage(uid)

    await update.message.reply_text("🏃 Скачиваю…")
    raw, meta = download(url)
    if (meta.get("duration") or 0) > 120:
        await update.message.reply_text("❌ Видео длиннее 2 минут"); return

    vid = normalize(raw)
    wav = Path(tempfile.mktemp(suffix=TMP_WAV))
    whisper = ""
    if extract_audio(vid, wav):
        with wav.open("rb") as f:
            whisper = openai.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru", response_format="text")

    sys = ("Ты кулинарный помощник. Верни JSON "
           "{title, ingredients[], steps[], extra?}. "
           "ingredients — массив объектов name+quantity.")
    chat = openai.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type":"json_object"},
        messages=[
            {"role":"system","content":sys},
            {"role":"user","content": textwrap.dedent(f"""
                Подпись:
                {meta.get('description','')}
                ---
                Транскрипт:
                {whisper or '[аудио нет]'}
            """)}
        ])
    rec = json.loads(chat.choices[0].message.content)

    vmsg = await update.message.reply_video(vid.read_bytes(), supports_streaming=True)
    txt = [f"*{icon(rec.get('title','Рецепт'))} {rec.get('title','Рецепт')}*\n",
           "🛒 *Ингредиенты*", *[ing(i) for i in rec.get("ingredients",[])],
           "\n⸻\n","👩‍🍳 *Шаги приготовления*",
           *[f"{n+1}. {step}" for n, step in enumerate(rec.get("steps",[]))]]
    if rec.get("extra"):
        txt += ["\n⸻\n","💡 *Дополнительно*", extra(rec["extra"])]
    txt += ["\n⸻\n", f"🔗 [Оригинал]({url})"]

    await ctx.bot.send_message(update.effective_chat.id,
                               "\n".join(txt)[:4000],
                               parse_mode="Markdown",
                               reply_to_message_id=vmsg.message_id,
                               disable_web_page_preview=True)

# ────────── микро-HTTP server ──────────
async def health(_): return web.Response(text="ok")

def aio_app():
    app = web.Application()
    app.router.add_get("/health", health)
    return app

# ────────── main ──────────
async def main():
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    if ENABLE_PAYMENTS:
        bot.add_handler(CommandHandler("buy100", buy100))
        bot.add_handler(CommandHandler("subscribe", subscribe))
        bot.add_handler(PreCheckoutQueryHandler(precheckout))
        bot.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

    await asyncio.gather(
        bot.initialize(), bot.start(),
        web._run_app(aio_app(), host="0.0.0.0", port=8080)
    )

if __name__ == "__main__":
    asyncio.run(main())
