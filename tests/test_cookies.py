import sys
import os
import types
import tempfile
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stub external dependencies used in bot.py so that it can be imported
for name in [
    "aiohttp", "aiohttp.web",
    "dotenv", "dotenv.main",
    "yt_dlp", "yt_dlp.utils",
    "openai",
    "telegram", "telegram.ext",
]:
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

# Provide minimal stubs for submodules/classes used during import
sys.modules["aiohttp.web"].Application = object
sys.modules["yt_dlp"].YoutubeDL = object
sys.modules["yt_dlp.utils"].DownloadError = Exception
sys.modules["openai"].OpenAI = object
sys.modules["telegram"].Update = object
sys.modules["telegram"].constants = types.SimpleNamespace(ParseMode=None)

dotenv_mod = sys.modules.get("dotenv")
setattr(dotenv_mod, "load_dotenv", lambda *args, **kwargs: None)

telegram_ext = sys.modules.get("telegram.ext")
setattr(telegram_ext, "Application", object)
setattr(telegram_ext, "ContextTypes", object)
setattr(telegram_ext, "CommandHandler", object)
setattr(telegram_ext, "MessageHandler", object)
setattr(telegram_ext, "filters", object)

# Set required environment variables for importing bot
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

import bot
import pytest


@pytest.mark.parametrize(
    "var,url",
    [
        ("IG_COOKIES_CONTENT", "https://www.instagram.com/p/abc/"),
        ("TT_COOKIES_CONTENT", "https://www.tiktok.com/@user/video/123"),
        ("YT_COOKIES_CONTENT", "https://www.youtube.com/watch?v=xyz"),
    ],
)
def test_sync_download_uses_temp_cookies(monkeypatch, tmp_path, var, url):
    cookie_paths = []

    def fake_create_temp_cookies_file(content: str) -> str:
        fd, path = tempfile.mkstemp(dir=tmp_path, suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        cookie_paths.append(path)
        return path

    monkeypatch.setattr(bot, "create_temp_cookies_file", fake_create_temp_cookies_file)

    def fake_mkdtemp():
        d = tmp_path / "dl"
        d.mkdir(exist_ok=True)
        return str(d)

    monkeypatch.setattr(bot.tempfile, "mkdtemp", fake_mkdtemp)

    last_opts = {}

    class DummyDL:
        def __init__(self, opts):
            last_opts.clear()
            last_opts.update(opts)
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {"id": "vid", "ext": "mp4"}

        def prepare_filename(self, info):
            outtmpl = self.opts["outtmpl"]
            path = outtmpl.replace("%(id)s", info["id"]).replace("%(ext)s", info["ext"])
            Path(path).write_text("video")
            return path

    monkeypatch.setattr(bot, "YoutubeDL", DummyDL)

    # Reset cookie settings
    monkeypatch.setattr(bot, "IG_COOKIES_CONTENT", "" if var != "IG_COOKIES_CONTENT" else "cookie")
    monkeypatch.setattr(bot, "TT_COOKIES_CONTENT", "" if var != "TT_COOKIES_CONTENT" else "cookie")
    monkeypatch.setattr(bot, "YT_COOKIES_CONTENT", "" if var != "YT_COOKIES_CONTENT" else "cookie")

    path, info, err = bot._sync_download(url)

    assert err is None
    assert path is not None and info is not None
    assert len(cookie_paths) == 1

    cookie_path = Path(cookie_paths[0])
    assert last_opts.get("cookiefile") == cookie_paths[0]
    # cookie file should be deleted in finally block
    assert not cookie_path.exists()

    # cleanup output file and directory
    if path.exists():
        path.unlink()
    if path.parent.exists():
        path.parent.rmdir()
