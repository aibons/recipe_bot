"""
Recipe Bot — Telegram
Ссылка ► ролик ≤ 120 с ► MP4 720 px + рецепт (ингредиенты, шаги, extra, ссылка)
Площадки: TikTok / Instagram Reels / YouTube Shorts
"""
from __future__ import annotations
import asyncio, json, os, re, subprocess, tempfile, textwrap
from pathlib import Path

from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
import openai

# ──────────────── константы ────────────────
MAX_DUR   = 120            # сек
LONG_SIDE = 720            # целевая длинная сторона
TMP_WAV   = ".wav"

load_dotenv()
TOKEN = os.environ["TELEGRAM_TOKEN"]
openai.api_key = os.environ["OPENAI_API_KEY"]
SESSION = os.getenv("IG_SESSIONID", "")

YDL_BASE: dict[str, object] = {
    "quiet": True,
    "outtmpl": "%(id)s.%(ext)s",
    "merge_output_format": "mp4",
}
if SESSION:
    YDL_BASE["cookiesfromstring"] = (
        ".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\t" + SESSION + "\n"
    )

# ──────────────── FFmpeg helpers ────────────────
def ensure_ffmpeg() -> None:
    subprocess.run(["ffmpeg", "-version"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def extract_audio(src: Path, dst: Path) -> bool:
    ensure_ffmpeg()
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(dst)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False

def normalize_video(src: Path) -> Path:
    """
    Любой вход ► MP4 H.264 со square-pixels, длинная сторона = 720 px.
    """
    ensure_ffmpeg()
    dst = src.with_name(src.stem + "_norm.mp4")

    vf = (f"scale='if(gt(iw,ih),{LONG_SIDE},-2)':'if(gt(iw,ih),-2,{LONG_SIDE})',"
          "setsar=1")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src),
         "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", str(dst)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return dst

# ──────────────── форматирование ────────────────
LABEL = {"servingsuggestion":"Совет по подаче",
         "preparationtime":"Время подготовки",
         "cookingtime":"Время готовки",
         "totaltime":"Общее время",
         "garnish":"Гарнир"}
EMOJI = {"лимон":"🍋","кекс":"🧁","крылышк":"🍗","пицц":"🍕","салат":"🥗",
         "бургер":"🍔","шокол":"🍫","суп":"🥣","паста":"🍝","рыб":"🐟",
         "куриц":"🐔","фрикадель":"🍽️"}
def icon(title:str)->str: return next((e for k,e in EMOJI.items() if k in title.lower()),"🍽️")
ing   = lambda i: f"• {i.get('name')} — {i.get('quantity')}".rstrip(" —") if isinstance(i,dict) else f"• {i}"
step  = lambda n,s: f"{n}. {(s.get('step') if isinstance(s,dict) else s)}"
extra = lambda e: "\n".join(f"• {LABEL.get(k,k)}: {v}" for k,v in e.items()) if isinstance(e,dict) else str(e)

# ──────────────── загрузка с fallback ────────────────
def download(url: str) -> tuple[Path, dict]:
    is_tt = "tiktok" in url
    formats = ["best[ext=mp4]/best"] if is_tt else [
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
    raise RuntimeError("Не удалось скачать ролик ни в одном формате")

# ──────────────── Telegram handler ────────────────
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    vid = aud = None
    try:
        url = (update.message.text or "").strip()
        if not re.search(r"(instagram|tiktok|youtu)", url):
            await update.message.reply_text("Пришли ссылку на Instagram / TikTok / YouTube"); return

        await update.message.reply_text("🏃 Скачиваю…")
        raw_vid, meta = download(url)
        if (meta.get("duration") or 0) > MAX_DUR:
            await update.message.reply_text("❌ Видео длиннее 2 минут"); return

        vid = normalize_video(raw_vid)

        # ─── аудио → Whisper
        aud = Path(tempfile.mktemp(suffix=TMP_WAV))
        whisper = ""
        if extract_audio(vid, aud):
            with aud.open("rb") as f:
                whisper = openai.audio.transcriptions.create(
                    model="whisper-1", file=f, language="ru", response_format="text")

        # ─── GPT-4o → рецепт
        caption = meta.get("description","")
        system = ("Ты кулинарный помощник. Верни JSON "
                  "{title, ingredients[], steps[], extra?}. "
                  "ingredients — массив объектов name+quantity.")
        chat = openai.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type":"json_object"},
            messages=[
                {"role":"system","content": system},
                {"role":"user","content": textwrap.dedent(f"""
                    Подпись:
                    {caption}
                    ---
                    Транскрипт:
                    {whisper or '[аудио отсутствует]'}
                """)}
            ])
        rec = json.loads(chat.choices[0].message.content)

        # ─── отправка
        title = rec.get("title","Рецепт")
        vmsg  = await update.message.reply_video(vid.read_bytes(), supports_streaming=True)

        lines = [f"*{icon(title)} {title}*\n","🛒 *Ингредиенты*",
                 *[ing(i) for i in rec.get("ingredients",[])],
                 "\n⸻\n","👩‍🍳 *Шаги приготовления*",
                 *[step(n+1,s) for n,s in enumerate(rec.get("steps",[]))]]
        if rec.get("extra"): lines += ["\n⸻\n","💡 *Дополнительно*", extra(rec["extra"])]
        lines += ["\n⸻\n", f"🔗 [Оригинал]({url})"]

        await ctx.bot.send_message(update.effective_chat.id,
                                   "\n".join(lines)[:4000],
                                   parse_mode="Markdown",
                                   reply_to_message_id=vmsg.message_id,
                                   disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

    finally:
        for p in (vid, aud):
            if p and p.exists():
                p.unlink(missing_ok=True)

# ──────────────── main ────────────────
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("Recipe Bot запущен — ролики ≤ 2 мин"); app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
