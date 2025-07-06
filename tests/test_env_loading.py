import sys
import os
import types
import importlib

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

import bot


def test_env_variables_loaded(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "token123")
    monkeypatch.setenv("OPENAI_API_KEY", "key123")
    importlib.reload(bot)
    assert bot.TOKEN == "token123"
    assert bot.OPENAI_API_KEY == "key123"
