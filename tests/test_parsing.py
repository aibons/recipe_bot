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
sys.modules["telegram"].Update = object
sys.modules["telegram"].constants = types.SimpleNamespace(ParseMode=None)
# Provide minimal attribute for load_dotenv in dotenv
dotenv_mod = sys.modules.get("dotenv")
setattr(dotenv_mod, "load_dotenv", lambda *args, **kwargs: None)

# Minimal attributes required from telegram.ext
telegram_ext = sys.modules.get("telegram.ext")
setattr(telegram_ext, "Application", object)
setattr(telegram_ext, "ContextTypes", object)
setattr(telegram_ext, "CommandHandler", object)
setattr(telegram_ext, "MessageHandler", object)
setattr(telegram_ext, "filters", object)

import pytest

# Set required environment variables for importing bot
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from bot import parse_recipe_blocks, is_supported_url


def test_parse_recipe_blocks_typical():
    text = (
        "\n".join([
            "Рецепт: Оладьи из кабачков",
            "",
            "Ингредиенты:",
            "- кабачок — 1 шт.",
            "- яйцо — 1 шт.",
            "- мука — 100 г",
            "",
            "Приготовление:",
            "1. Натереть кабачок",
            "2. Смешать с яйцом и мукой",
            "3. Обжарить на сковороде",
            "",
            "Дополнительно:",
            "Подавайте со сметаной.",
        ])
    )
    expected = {
        "title": "Оладьи из кабачков",
        "ingredients": [
            "кабачок — 1 шт",
            "яйцо — 1 шт",
            "мука — 100 г",
        ],
        "steps": [
            "Натереть кабачок",
            "Смешать с яйцом и мукой",
            "Обжарить на сковороде",
        ],
        "extra": "Подавайте со сметаной.",
    }
    assert parse_recipe_blocks(text) == expected


def test_is_supported_url_valid():
    valid_urls = [
        "https://www.instagram.com/reel/abc123/",
        "https://www.instagram.com/p/xyz/",
        "https://vm.tiktok.com/ZGJ/",
        "https://www.tiktok.com/@user/video/123",
        "https://youtu.be/abc",
        "https://www.youtube.com/watch?v=def",
        "https://youtube.com/shorts/ghi",
    ]
    for url in valid_urls:
        assert is_supported_url(url), f"URL should be supported: {url}"


def test_is_supported_url_invalid():
    invalid_urls = [
        "https://example.com/video/1",
        "https://www.instagram.com/user/",  # missing reel/p/tv
        "https://youtube.com/channel/123",   # unsupported youtube path
        "instagram.com/test",                # missing scheme
    ]
    for url in invalid_urls:
        assert not is_supported_url(url), f"URL should not be supported: {url}"


