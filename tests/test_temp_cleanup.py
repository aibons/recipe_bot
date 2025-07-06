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


def test_sync_download_cleans_temp_dir_on_failure(monkeypatch, tmp_path):
    created = []

    def fake_mkdtemp():
        d = tmp_path / f"temp_{len(created)}"
        d.mkdir()
        created.append(d)
        return str(d)

    monkeypatch.setattr(bot.tempfile, "mkdtemp", fake_mkdtemp)

    class DummyDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def extract_info(self, url, download=False):
            raise bot.DownloadError("fail")

    monkeypatch.setattr(bot, "YoutubeDL", DummyDL)

    path, info, err = bot._sync_download("http://example.com")
    assert path is None and info is None
    assert err is not None

    for d in created:
        assert not Path(d).exists()
