##############################################################################
#  bot.py  •  Recipe-Bot (Telegram)                                          #
#  Правки:                                                                   #
#    • long-polling запускается (initialize → start → updater.start_polling) #
#    • всё, что приходит от пользователя, экранируем   escape_markdown(V2)  #
#    • WELCOME и рецепт отправляются в Markdown V2 (без BadRequest)          #
#    • логирование INFO добавлено                                            #
##############################################################################

from __future__ import annotations

# ───── стандартные ─────
import asyncio, datetime as dt, json, os, re, sqlite3, subprocess, tempfile, textwrap
from pathlib import Path
import logging

# ───── сторонние ─────
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

# ─────────── логгер ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("recipe_bot")

# ─────────── Константы и ENV ───────────
load_dotenv()
TOKEN          = os.environ["TELEGRAM_TOKEN"]
OPENAI_KEY     = os.environ["OPENAI_API_KEY"]
PROVIDER_TOKEN = os.getenv("YOOKASSA_TOKEN", "")
IG_SESSIONID   = os.getenv("IG_SESSIONID", "")

openai.api_key = OPENAI_KEY

ADMIN_IDS    = {248610561}
FREE_LIMIT   = 6

PKG100_PRICE = 299_00
SUB_PRICE    = 199_00
SUB_VOLUME   = 200
SUB_DAYS     = 30
ENABLE_PAY   = False                 # включи True, когда добавишь токен YooKassa

YDL_BASE = {"quiet": True, "outtmpl": "%(id)s.%(ext)s", "merge_output_format": "mp4"}

# ─────────── SQLite учёт ───────────
DB = sqlite3.connect("bot.db")
DB.execute("""CREATE TABLE IF NOT EXISTS users(
  uid INTEGER PRIMARY KEY,
  used INTEGER    DEFAULT 0,
  balance INTEGER DEFAULT 0,
  paid_until DATE DEFAULT NULL);""")
DB.commit()

def quota(uid: int) -> tuple[int, int]:
    used, bal, until = DB.execute(
        "SELECT used,balance,paid_until FROM users WHERE uid=?", (uid,)
    ).fetchone() or (0, 0, None)
    if until and dt.date.today() > dt.date.fromisoformat(until):
        bal = 0
    return used, bal

def add_usage(uid: int, d: int = 1):
    DB.execute("INSERT INTO users(uid,used) VALUES(?,0) "
               "ON CONFLICT(uid) DO UPDATE SET used=used+?", (uid, d))
    DB.commit()

def add_balance(uid: int, add: int = 0, days: int = 0):
    used, bal = quota(uid)
    bal += add
    until = (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else None
    DB.execute("""INSERT INTO users(uid,balance,paid_until) VALUES(?,?,?)
                  ON CONFLICT(uid) DO UPDATE SET balance=?,paid_until=?""",
               (uid, bal, until, bal, until))
    DB.commit()

# ─────────── ffmpeg helpers ───────────
def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", *map(str, args)],
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL,
                   check=True)

def normalize(src: Path) -> Path:
    out = src.with_name(src.stem + "_720.mp4")
    vf = "scale='if(gt(iw,ih),720,-2)':'if(gt(iw,ih),-2,720)',setsar=1"
    ffmpeg("-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", out)
    return out

def extract_audio(src: Path, dst: Path) -> bool:
    try:
        ffmpeg("-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", dst)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False

# ─────────── загрузка видео ───────────
def download(url: str) -> tuple[Path, dict]:
    """
    Скачивает ролик (Instagram / TikTok / YouTube-Shorts) и возвращает
    (Path к файлу, meta-info).  Использует cookie-файлы, если нужны.
    """
    opts = YDL_BASE.copy()

    # ── подхватываем cookie-файлы ──────────────────────────────────
    if IG_SESSIONID and "instagram.com" in url:
        ck = Path("ig_cookie.txt")
        if not ck.exists():
            ck.write_text(
                f".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\t{IG_SESSIONID}\n"
            )
        opts["cookiefile"] = str(ck)

    if TT_SESSIONID and "tiktok.com" in url:
        ck = Path("tt_cookie.txt")
        if not ck.exists():
            ck.write_text(
                f".tiktok.com\tTRUE\t/\tFALSE\t0\ttt_session_id\t{TT_SESSIONID}\n"
            )
        opts["cookiefile"] = str(ck)

    if YT_COOKIES and ("youtu.be" in url or "youtube.com" in url):
        opts["cookiefile"] = YT_COOKIES

    # ── ускоряем скачивание, если есть aria2c ─────────────────────
    if shutil.which("aria2c"):
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "8", "-k", "1M"]

    fmts = [
        "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "best[height<=720]",
        "best",
    ]
    last_err = None
    for f in fmts:
        try:
            with YoutubeDL({**opts, "format": f}) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info)), info
        except DownloadError as e:
            last_err = e
            continue
    raise RuntimeError(f"Не смог скачать видео: {last_err}")

# ─────────── форматирование MarkdownV2 ───────────
EMOJI = {"лимон": "🍋", "кекс": "🧁", "крыл": "🍗", "бургер": "🍔",
         "паста": "🍝", "салат": "🥗", "суп": "🥣", "фрика": "🥘"}

def safe(s: str) -> str:
    return escape_markdown(str(s), version=2)

def fmt_ing(i):
    if isinstance(i, dict):
        return f"• {safe(i.get('name'))}{' — '+safe(i['quantity']) if i.get('quantity') else ''}"
    return f"• {safe(i)}"

def fmt_step(n, s):
    text = s.get('step') if isinstance(s, dict) else s
    return f"{n}. {safe(text)}"

WELCOME = """
🔥 Recipe Bot — помогаю сохранить рецепт из короткого видео!

🆓 Доступно *6* бесплатных роликов.
Платные тарифы (скоро):

• 100 роликов — 299 ₽  
• 200 роликов + 30 дней — 199 ₽  

Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!
""".strip()

# ─────────── Telegram handlers ───────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(
        escape_markdown(WELCOME, version=2),
        parse_mode="MarkdownV2"
    )

async def buy100(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAY: return await upd.message.reply_text("Оплата пока отключена 🙈")
    await ctx.bot.send_invoice(
        upd.effective_chat.id, "Пакет 100 роликов",
        "Единоразово +100 роликов.", "pkg100", PROVIDER_TOKEN,
        currency="RUB", prices=[LabeledPrice("100 роликов", PKG100_PRICE)]
    )

async def subscribe(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_PAY: return await upd.message.reply_text("Оплата пока отключена 🙈")
    await ctx.bot.send_invoice(
        upd.effective_chat.id, "Подписка (200 роликов/30 дн)",
        "Каждый месяц 200 роликов.", "sub", PROVIDER_TOKEN,
        currency="RUB", prices=[LabeledPrice("Подписка 30 дн", SUB_PRICE)]
    )

async def prechk(q: PreCheckoutQuery, ctx: ContextTypes.DEFAULT_TYPE):
    await q.answer(ok=True)

async def paid(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    if upd.message.successful_payment.invoice_payload == "pkg100":
        add_balance(uid, add=100)
        msg = "✅ +100 роликов начислено!"
    else:
        add_balance(uid, add=SUB_VOLUME, days=SUB_DAYS)
        until = (dt.date.today()+dt.timedelta(days=SUB_DAYS)).isoformat()
        msg = f"✅ Подписка активна до {until}"
    await upd.message.reply_text(msg)

async def handle(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = upd.effective_user.id
    url = (upd.message.text or "").strip()

    if not re.search(r"(instagram|tiktok|youtu)", url, re.I):
        return await upd.message.reply_text("Дай ссылку на Instagram / TikTok / YouTube 😉")

    # лимиты
    if uid not in ADMIN_IDS:
        used, bal = quota(uid)
        if bal > 0:
            add_balance(uid, add=-1)
        elif used >= FREE_LIMIT:
            return await upd.message.reply_text(
                f"🔒 Бесплатный лимит {FREE_LIMIT} роликов исчерпан."
            )
        else:
            add_usage(uid)

    await upd.message.reply_text("🏃 Скачиваю…")
    try:
        raw, meta = download(url)
    except Exception as e:
        log.warning("download error: %s", e)
        return await upd.message.reply_text("❌ Не смог скачать это видео.")

    if (meta.get("duration") or 0) > 120:
        return await upd.message.reply_text("❌ Видео дольше 2 минут.")

    vid = normalize(raw)
    wav = Path(tempfile.mktemp(suffix=".wav"))
    whisper = ""
    if extract_audio(vid, wav):
        with wav.open("rb") as f:
            whisper = openai.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru", response_format="text"
            )

    sys_prompt = "Ты кулинарный помощник. Верни JSON {title,ingredients[],steps[],extra?}"
    chat = openai.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"{meta.get('description','')}\n---\n{whisper}"},
        ],
    )
    rec = json.loads(chat.choices[0].message.content)

    vmsg = await upd.message.reply_video(vid.read_bytes(), supports_streaming=True)

    title = rec.get("title", "Рецепт")
    caption_lines = [
        f"*{EMOJI.get(title.lower()[:4], '🍽️')} {safe(title)}*",
        "",
        "🛒 *Ингредиенты*",
        *[fmt_ing(i) for i in rec.get("ingredients", [])],
        "",
        "👩‍🍳 *Шаги приготовления*",
        *[fmt_step(n+1, s) for n, s in enumerate(rec.get("steps", []))]
    ]
    if rec.get("extra"):
        extra = "\n".join(f"• {safe(k)}: {safe(v)}" for k, v in rec["extra"].items())
        caption_lines += ["", "💡 *Дополнительно*", extra]
    caption_lines += ["", f"🔗 [Оригинал]({url})"]

    text_block = "\n".join(caption_lines)[:4000]

    await ctx.bot.send_message(
        chat_id=upd.effective_chat.id,
        text=text_block,
        parse_mode="MarkdownV2",
        reply_to_message_id=vmsg.message_id,
        disable_web_page_preview=True,
    )

# ─────────── healthcheck ───────────
async def health(_): return web.Response(text="ok")
def aio_app():
    a = web.Application()
    a.router.add_get("/health", health)
    return a

# ─────────── main ───────────
async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    if ENABLE_PAY:
        app.add_handler(CommandHandler("buy100", buy100))
        app.add_handler(CommandHandler("subscribe", subscribe))
        app.add_handler(PreCheckoutQueryHandler(prechk))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

    # ───── START (правильный порядок + poll) ─────
    await app.initialize()                           # 1) подготовка
    await asyncio.gather(                           # 2) параллельно:
        app.start(),                                #   • запускаем Application
        app.updater.start_polling(),                #   • запускаем long-polling  ✅
        web._run_app(aio_app(), host="0.0.0.0", port=8080),  #   • health-сервер
    )

if __name__ == "__main__":
    asyncio.run(main())