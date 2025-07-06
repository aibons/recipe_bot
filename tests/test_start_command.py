import sys
import os
import types

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

class DummyParseMode:
    MARKDOWN_V2 = "MarkdownV2"

sys.modules["telegram"].Update = object
sys.modules["telegram"].constants = types.SimpleNamespace(ParseMode=DummyParseMode)

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
import asyncio

# Replace ParseMode in bot.constants with our dummy so cmd_start works even if
# bot was imported in another test with a different stub.
bot.constants.ParseMode = DummyParseMode


@pytest.mark.asyncio
def test_cmd_start_sends_welcome():
    recorded = {}

    async def reply_text(text, **kwargs):
        recorded['text'] = text
        recorded.update(kwargs)

    update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=reply_text))

    asyncio.run(bot.cmd_start(update, None))

    assert recorded.get('text') == bot.WELCOME
    assert recorded.get('parse_mode') == bot.constants.ParseMode.MARKDOWN_V2
