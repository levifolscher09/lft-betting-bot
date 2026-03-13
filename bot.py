import os
import requests
import smtplib
import json
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "8580976907:AAGR4jmA0qrn6vw8RlSoszpP7uIxnYUuc98")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7711190329")
ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "08c2d60df2bf862873f6f3a32e93ad1c")
GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS", "LFT.Trades05@gmail.com")
GMAIL_APP_PASS  = os.getenv("GMAIL_APP_PASS", "xutl ivtg whbf rree")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "LFT.Trades05@gmail.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Fetch Odds ─────────────────────────────────────────────────────────────────
def fetch_odds():
    sports = [
        "soccer_epl",
        "soccer_uefa_champs_league",
        "soccer_efl_champ",
        "soccer_england_league1",
        "horse_racing_uk",
    ]
    all_odds = []

    for sport in sports:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            }
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for event in data[:5]:  # top 5 per sport
                    all_odds.append({
                        "sport": sport,
                        "home_team": event.get("home_team", ""),
                        "away_team": event.get("away_team", ""),
                        "commence_time": event.get("commence_time", ""),
                        "bookmakers": event.get("bookmakers", [])[:2],
                    })
        except Exception as e:
            print(f"Error fetching {sport}: {e}")

    return all_odds

# ── Ask Claude to Analyse ──────────────────────────────────────────────────────
def analyse_bets(odds_data):
    today = datetime.now().strftime("%A %d %B %Y")

    odds_summary = json.dumps(odds_data, indent=2)

    prompt = f"""
You are an expert sports betting analyst. Today is {today}.

Here is today's live odds data from UK bookmakers across football and horse racing:

{odds_summary}

Your job:
1. Analyse each event and its odds
2. Use your knowledge of current form, team news, historical performance, and value betting principles
3. Select the TOP 6 best bets of the day
4. Rank them from most confident to most risky
5. Split into 3 tiers: BEST (2 picks), MEDIUM (2 picks), RISKY (2 picks)

For each pick provide:
- Event name
- Your recommended bet (which team/outcome to back)
- The best decimal odds available
- Confidence % (your estimated real probability)
- Value rating (odds vs real probability — is this good value?)
- 2-3 sentence analysis explaining WHY this is a good bet

Respond ONLY in this exact JSON format, no extra text:
{{
  "date": "{today}",
  "best": [
    {{
      "event": "Team A vs Team B",
      "bet": "Team A to Win",
      "odds": 2.10,
      "confidence": 58,
      "value": "High",
      "analysis": "Your reasoning here."
    }},
    {{
      "event": "Team C vs Team D",
      "bet": "Under 2.5 Goals",
      "odds": 1.85,
      "confidence": 62,
      "value": "High",
      "analysis": "Your reasoning here."
    }}
  ],
  "medium": [
    {{
      "event": "Team E vs Team F",
      "bet": "Team E to Win",
      "odds": 2.50,
      "confidence": 48,
      "value": "Medium",
      "analysis": "Your reasoning here."
    }},
    {{
      "event": "Team G vs Team H",
      "bet": "Both Teams to Score",
      "odds": 1.72,
      "confidence": 55,
      "value": "Medium",
      "analysis": "Your reasoning here."
    }}
  ],
  "risky": [
    {{
      "event": "Team I vs Team J",
      "bet": "Team J to Win",
      "odds": 4.50,
      "confidence": 35,
      "value": "High",
      "analysis": "Your reasoning here."
    }},
    {{
      "event": "Team K vs Team L",
      "bet": "Team K -1 Asian Handicap",
      "odds": 2.20,
      "confidence": 40,
      "value": "Medium",
      "analysis": "Your reasoning here."
    }}
  ]
}}
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ── Format Telegram Message ────────────────────────────────────────────────────
def format_telegram(picks):
    date = picks["date"]
    lines = []
    lines.append(f"🏆 *LFT DAILY BETTING PICKS*")
    lines.append(f"📅 {date}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    tiers = [
        ("🟢 BEST BETS", picks["best"]),
        ("🟡 MEDIUM BETS", picks["medium"]),
        ("🔴 RISKY BETS", picks["risky"]),
    ]

    for tier_name, tier_picks in tiers:
        lines.append(f"\n*{tier_name}*")
        for p in tier_picks:
            lines.append(f"\n⚽ *{p['event']}*")
            lines.append(f"📌 Bet: {p['bet']}")
            lines.append(f"💰 Odds: {p['odds']} | Confidence: {p['confidence']}% | Value: {p['value']}")
            lines.append(f"📊 _{p['analysis']}_")
            lines.append("──────────────────")

    lines.append("\n// lf-trades · daily picks")
    lines.append("⚠️ _Gamble responsibly. 18+ only._")
    return "\n".join(lines)

# ── Format Email ───────────────────────────────────────────────────────────────
def format_email_html(picks):
    date = picks["date"]

    def tier_html(tier_picks, colour, label, emoji):
        rows = ""
        for p in tier_picks:
            rows += f"""
            <div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:16px;margin-bottom:12px;">
                <div style="font-size:13px;color:#888;margin-bottom:6px;">{emoji} {p['event']}</div>
                <div style="font-size:16px;font-weight:bold;color:#f0b429;margin-bottom:8px;">📌 {p['bet']}</div>
                <div style="display:flex;gap:12px;margin-bottom:10px;flex-wrap:wrap;">
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">💰 Odds: <b style="color:#fff">{p['odds']}</b></span>
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">🎯 Confidence: <b style="color:#fff">{p['confidence']}%</b></span>
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">⚡ Value: <b style="color:#fff">{p['value']}</b></span>
                </div>
                <div style="font-size:13px;color:#aaa;line-height:1.6;">{p['analysis']}</div>
            </div>"""

        return f"""
        <div style="margin-bottom:28px;">
            <div style="font-size:11px;font-weight:bold;letter-spacing:0.1em;color:{colour};margin-bottom:12px;">{label}</div>
            {rows}
        </div>"""

    best_html   = tier_html(picks["best"],   "#22c55e", "🟢 BEST BETS",   "⚽")
    medium_html = tier_html(picks["medium"], "#f59e0b", "🟡 MEDIUM BETS", "⚽")
    risky_html  = tier_html(picks["risky"],  "#ef4444", "🔴 RISKY BETS",  "⚽")

    return f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">

    <div style="text-align:center;margin-bottom:28px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:6px;">LOOKING FOR TRADES</div>
        <div style="font-size:26px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div>
        <div style="font-size:13px;color:#666;margin-top:6px;">{date}</div>
    </div>

    {best_html}
    {medium_html}
    {risky_html}

    <div style="border-top:1px solid #222;padding-top:16px;text-align:center;font-size:11px;color:#444;margin-top:8px;">
        // lf-trades · daily picks &nbsp;|&nbsp; Gamble responsibly. 18+ only.
    </div>
</div>
</body>
</html>"""

# ── Send Telegram ──────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    r = requests.post(url, json=payload, timeout=10)
    print(f"Telegram: {r.status_code} - {r.text[:100]}")

# ── Send Email ─────────────────────────────────────────────────────────────────
def send_email(html_body, date):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏆 LFT Daily Betting Picks — {date}"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    print("Email sent successfully")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching odds...")
    odds = fetch_odds()
    print(f"Fetched {len(odds)} events")

    print("Analysing with Claude...")
    picks = analyse_bets(odds)

    print("Formatting and sending...")
    telegram_msg = format_telegram(picks)
    email_html   = format_email_html(picks)

    send_telegram(telegram_msg)
    send_email(email_html, picks["date"])

    print("Done! Picks sent.")

if __name__ == "__main__":
    main()
