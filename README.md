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
3. Install [ffmpeg](https://ffmpeg.org/) and ensure it is available in your `PATH`.
4. Set environment variables `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, and `WEBHOOK_URL` (required for deployment, see [DEPLOYMENT.md](DEPLOYMENT.md) for details). Optional variables for cookies and other settings are also described there.
   If one of these variables is missing, the bot logs an error and exits.
5. Run the bot:
   ```bash
   python bot.py
   ```

## Running tests
The project uses `pytest` for tests:
```bash
pytest
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment instructions and environment variable descriptions.
