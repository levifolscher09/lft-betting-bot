# LFT Betting Bot

Sends daily betting picks (horse racing + football) via Telegram and Email at 7:00 AM.

## Files
- `bot.py` — main bot logic (fetch odds → analyse → send)
- `scheduler.py` — runs bot daily at 07:00 AM
- `requirements.txt` — Python dependencies
- `Procfile` — Railway deployment config

## Environment Variables (set in Railway)
| Variable | Value |
|---|---|
| `TELEGRAM_TOKEN` | Your BotFather token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `ODDS_API_KEY` | Your OddsAPI key |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASS` | Your Gmail app password |
| `RECIPIENT_EMAIL` | Email to receive picks |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

## Deploy to Railway
1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Add all environment variables above
4. Railway auto-detects Procfile and runs `python scheduler.py`

## Test locally
```bash
pip install -r requirements.txt
pip install schedule
export ANTHROPIC_API_KEY=your_key_here
python bot.py  # runs once immediately
```
