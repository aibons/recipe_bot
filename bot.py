#############################################################################
#  bot.py • Recipe-Bot  (Telegram)                                           #
#                                                                           #
#  1) Одна-единственная long-poll-сессия (run_polling)                       #
# 2) Любой вывод пользователю → Markdown V2 + экранирование                 #
# 3) Куки Instagram / TikTok берутся из .env / vars Render                  #
# 4) yt-dlp получает cookies через opts["cookies"]                          #
# 5) Логирование INFO                                                      #
#############################################################################

from __future__ import annotations

# ─── стандартные ───────────────────────────────────────────────────────────
import asyncio, datetime as dt, json, os, re, sqlite3, subprocess, tempfile, \
       textwrap, logging, shutil
from pathlib import Path

# ─── сторонние ─────────────────────────────────────────────────────────────
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import openai

from telegram import (
    Update, LabeledPrice, PreCheckoutQuery, SuccessfulPayment
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application, ContextTypes, CommandHandler, MessageHandler,
    PreCheckoutQueryHandler, filters
)
from telegram.constants import UpdateType
# ────────────────────────────────────────────────────────────────────────────

# ── env ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN          = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
IG_SESSIONID   = os.getenv("IG_SESSIONID")        # из cookie Instagram
TT_SESSIONID   = os.getenv("TT_SESSIONID")        # из cookie TikTok
OWNER_ID       = 248610561                        # ваш Telegram id
FREE_LIMIT     = 6                                # пробные ролики

# ── yt-dlp базовый конфиг ──────────────────────────────────────────────────
YDL_BASE = dict(
    quiet=True, outtmpl={"default": "%(id)s.%(ext)s"},
    retries=3, format="bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    merge_output_format="mp4"
)

# ── база счётчика роликов ──────────────────────────────────────────────────
DB = Path("bot.db")
def init_db() -> None:
    conn = sqlite3.connect(DB); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS quota
                   (uid INTEGER PRIMARY KEY, used INTEGER)""")
    conn.commit(); conn.close()

def inc(uid: int) -> int:
    conn = sqlite3.connect(DB); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO quota VALUES (?,0)", (uid,))
    cur.execute("UPDATE quota SET used = used+1 WHERE uid=?", (uid,))
    cur.execute("SELECT used FROM quota WHERE uid=?", (uid,))
    used, = cur.fetchone(); conn.commit(); conn.close(); return used

# ── утилиты ────────────────────────────────────────────────────────────────
log = logging.getLogger("recipe_bot")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

EMOJI = {"лимон": "🍋", "кекс": "🧁", "крыл": "🍗", "бургер": "🍔",
         "паста": "🍝", "салат": "🥗", "суп": "🍜", "фрика": "🍲"}

def safe(txt: str) -> str:              # Markdown V2 escape
    return escape_markdown(txt, version=2)

def choose_emoji(title: str) -> str:
    for k, e in EMOJI.items():
        if k in title.lower():
            return e
    return "🍽️"

# ── скачивание видео ───────────────────────────────────────────────────────
def ffmpeg(*args) -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg not installed")
    cmd = ["ffmpeg"] + list(args)
    subprocess.run(cmd, check=True, text=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

def extract_audio(src: Path, dst: Path) -> bool:
    try:
        ffmpeg("-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
               "-ar", "16000", "-ac", "1", dst)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False

def download(url: str) -> tuple[Path, dict]:
    opts          = YDL_BASE.copy()
    opts["cookies"] = {}                # <─ заполняем по необходимости
    if IG_SESSIONID and "instagram" in url:
        opts["cookies"]["sessionid"]    = IG_SESSIONID
    if TT_SESSIONID and "tiktok" in url:
        opts["cookies"]["tt_sessionid"] = TT_SESSIONID

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info)), info
    except DownloadError as e:
        raise RuntimeError("Не смог скачать видео") from e

# ── Telegram-хендлеры ──────────────────────────────────────────────────────
WELCOME = (
    "🔥 *Recipe Bot* — помогаю сохранить рецепт из короткого видео!\n\n"
    "🆓 Доступно *{left}* бесплатных роликов.\n"
    "Платные тарифы (скоро):\n"
    " • 100 роликов — 299 ₽\n"
    " • 200 роликов + 30 дней — 199 ₽\n\n"
    "Пришли ссылку на Reels / Shorts / TikTok, а остальное я сделаю сам!"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cur = inc(uid) if uid != OWNER_ID else 0
    left = max(FREE_LIMIT - cur, 0)
    await update.message.reply_text(safe(WELCOME.format(left=left)),
                                    parse_mode="MarkdownV2")

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    uid = update.effective_user.id

    # квота
    if uid != OWNER_ID and inc(uid) > FREE_LIMIT:
        await update.message.reply_text("⚠️ Лимит исчерпан, ожидайте тарифы.")
        return

    msg = await update.message.reply_text("🏃 Скачиваю…")
    try:
        video_path, info = download(url)
    except Exception as e:
        log.warning("download error: %s", e)
        await msg.edit_text("❌ Не смог скачать это видео.")
        return

    # отправляем видео
    await update.message.reply_video(video_path.open("rb"))

    # формируем текст-рецепт (place-holder)
    title  = info.get("title", "Рецепт")
    emoji  = choose_emoji(title)
    recipe = f"*{safe(title)}* {emoji}\n\n(здесь будет AI-рецепт)\n\n🔗 [Оригинал]({url})"
    await update.message.reply_text(recipe, parse_mode="MarkdownV2")

# ── AIOHTTP «живой» роут (проверка для Render) ─────────────────────────────
async def hello(_: web.Request) -> web.Response:
    return web.Response(text="OK")

def aio_app() -> web.Application:
    app = web.Application(); app.add_routes([web.get("/", hello)]); return app

# ── main ───────────────────────────────────────────────────────────────────
async def main() -> None:
    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    await app.initialize()          # 1. подготовить Application
    await asyncio.gather(           # 2. запустить две корутины параллельно
        app.start(),               #    – long-poll loop
        web._run_app(aio_app(),    #    – aiohttp server на :8080
                     host="0.0.0.0",
                     port=8080),
    )

# run_polling  ➜ запускает long-poll-петлю + aiohttp-сервер

if __name__ == "__main__":
    asyncio.run(main())