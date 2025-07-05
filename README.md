# Recipe Bot

Recipe Bot is a Telegram bot that downloads short cooking videos (Instagram Reels, TikTok, YouTube Shorts) and sends them back along with the extracted recipe.

## Features
- Supports links from Instagram, TikTok and YouTube
- Extracts the recipe text with OpenAI and formats it for Telegram
- Sends the original video and recipe in chat

## Setup
1. Clone this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in the variables. At minimum set
   `TELEGRAM_TOKEN` and `OPENAI_API_KEY`. Cookie variables like
   `IG_COOKIES_CONTENT`, `TT_COOKIES_CONTENT` and `YT_COOKIES_CONTENT` can be
   added if needed. See [DEPLOYMENT.md](DEPLOYMENT.md) for details on all
   variables.
4. Run the bot:
   ```bash
   python bot.py
   ```

## Running tests
The project uses `pytest` for tests:
```bash
pytest
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment instructions and environment variable descriptions.
