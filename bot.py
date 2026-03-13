import os
import requests
import smtplib
import json
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "8580976907:AAGR4jmA0qrn6vw8RlSoszpP7uIxnYUuc98")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "7711190329")
ODDS_API_KEY      = os.getenv("ODDS_API_KEY", "08c2d60df2bf862873f6f3a32e93ad1c")
GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS", "LFT.Trades05@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS", "xutl ivtg whbf rree")
RECIPIENT_EMAIL   = os.getenv("RECIPIENT_EMAIL", "LFT.Trades05@gmail.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Fetch Football Odds ────────────────────────────────────────────────────────
def fetch_football_odds():
    sports = [
        "soccer_epl",
        "soccer_uefa_champs_league",
        "soccer_efl_champ",
        "soccer_england_league1",
        "soccer_england_league2",
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
                for event in data[:5]:
                    all_odds.append({
                        "sport": sport,
                        "home_team": event.get("home_team", ""),
                        "away_team": event.get("away_team", ""),
                        "commence_time": event.get("commence_time", ""),
                        "bookmakers": event.get("bookmakers", [])[:3],
                    })
        except Exception as e:
            print(f"Error fetching {sport}: {e}")
    return all_odds

# ── Ask Claude to Analyse with Web Search ─────────────────────────────────────
def analyse_bets(football_odds):
    today = datetime.now().strftime("%A %d %B %Y")
    football_summary = json.dumps(football_odds, indent=2)

    prompt = f"""You are an expert UK sports betting analyst. Today is {today}.

STEP 1 - Search the web for today's best UK horse racing tips and odds.
Search for: "best horse racing tips today {today} UK oddschecker"
Also search for: "horse racing tips {today} racing post"
Find real horses running TODAY with their current best odds from UK bookmakers.

STEP 2 - Here is today's live football odds data:
{football_summary}

STEP 3 - Pick the TOP 6 bets total: 4 football + 2 horse racing.
For horse racing use the real horses, races and best odds you found in your search.
For each horse include which bookmaker has the best odds.
Rank all 6 from most confident to most risky across 3 tiers: BEST (2), MEDIUM (2), RISKY (2).
At least 1 horse pick must be in BEST or MEDIUM tier.

Respond ONLY in this exact JSON, no extra text, no markdown:
{{
  "date": "{today}",
  "best": [
    {{"event": "Team A vs Team B", "bet": "Team A to Win", "odds": 2.10, "best_bookie": "Bet365", "confidence": 58, "value": "High", "analysis": "Reasoning here."}},
    {{"event": "Horse Name - Venue HH:MM", "bet": "Horse Name to Win", "odds": 3.50, "best_bookie": "Paddy Power", "confidence": 45, "value": "High", "analysis": "Reasoning here."}}
  ],
  "medium": [
    {{"event": "Team E vs Team F", "bet": "Team E to Win", "odds": 2.50, "best_bookie": "William Hill", "confidence": 48, "value": "Medium", "analysis": "Reasoning here."}},
    {{"event": "Team G vs Team H", "bet": "BTTS", "odds": 1.72, "best_bookie": "Betfair", "confidence": 55, "value": "Medium", "analysis": "Reasoning here."}}
  ],
  "risky": [
    {{"event": "Team I vs Team J", "bet": "Team J to Win", "odds": 4.50, "best_bookie": "Coral", "confidence": 35, "value": "High", "analysis": "Reasoning here."}},
    {{"event": "Team K vs Team L", "bet": "Team K -1 Handicap", "odds": 2.20, "best_bookie": "Ladbrokes", "confidence": 40, "value": "Medium", "analysis": "Reasoning here."}}
  ]
}}"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )

    # Extract final text block (comes after tool use)
    raw = ""
    for block in message.content:
        if hasattr(block, "text"):
            raw = block.text  # keep overwriting — we want the last text block

    raw = raw.strip()
    # Strip markdown fences if present
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    # Find JSON object in raw text
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    return json.loads(raw)

# ── Format Telegram Message ────────────────────────────────────────────────────
def format_telegram(picks):
    date = picks["date"]
    lines = []
    lines.append("🏆 *LFT DAILY BETTING PICKS*")
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
            is_horse = " - " in p["event"] or any(x in p["event"] for x in [
                "Cheltenham","Ascot","Newmarket","Sandown","Kempton","York",
                "Doncaster","Haydock","Lingfield","Windsor","Goodwood","Epsom",
                "Leicester","Nottingham","Wolverhampton","Chester"
            ])
            emoji = "🐴" if is_horse else "⚽"
            lines.append(f"\n{emoji} *{p['event']}*")
            lines.append(f"📌 Bet: {p['bet']}")
            bookie = f" @ {p.get('best_bookie','')}" if p.get('best_bookie') else ""
            lines.append(f"💰 Odds: {p['odds']}{bookie} | Confidence: {p['confidence']}% | Value: {p['value']}")
            lines.append(f"📊 _{p['analysis']}_")
            lines.append("──────────────────")

    lines.append("\n// lf-trades · daily picks")
    lines.append("⚠️ _Gamble responsibly. 18+ only._")
    return "\n".join(lines)

# ── Format Email ───────────────────────────────────────────────────────────────
def format_email_html(picks):
    date = picks["date"]

    def tier_html(tier_picks, colour, label):
        rows = ""
        for p in tier_picks:
            is_horse = " - " in p["event"] or any(x in p["event"] for x in [
                "Cheltenham","Ascot","Newmarket","Sandown","Kempton","York","Doncaster"
            ])
            emoji = "🐴" if is_horse else "⚽"
            bookie_badge = f'<span style="background:#2a2000;padding:3px 8px;border-radius:4px;font-size:11px;color:#f0b429;border:1px solid #f0b42940;">📍 Best odds: {p.get("best_bookie","")}</span>' if p.get("best_bookie") else ""
            rows += f"""
            <div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:16px;margin-bottom:12px;">
                <div style="font-size:13px;color:#888;margin-bottom:6px;">{emoji} {p['event']}</div>
                <div style="font-size:16px;font-weight:bold;color:#f0b429;margin-bottom:8px;">📌 {p['bet']}</div>
                <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center;">
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">💰 Odds: <b style="color:#fff">{p['odds']}</b></span>
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">🎯 Confidence: <b style="color:#fff">{p['confidence']}%</b></span>
                    <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">⚡ Value: <b style="color:#fff">{p['value']}</b></span>
                    {bookie_badge}
                </div>
                <div style="font-size:13px;color:#aaa;line-height:1.6;">{p['analysis']}</div>
            </div>"""
        return f"""
        <div style="margin-bottom:28px;">
            <div style="font-size:11px;font-weight:bold;letter-spacing:0.1em;color:{colour};margin-bottom:12px;">{label}</div>
            {rows}
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
    <div style="text-align:center;margin-bottom:28px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:6px;">LOOKING FOR TRADES</div>
        <div style="font-size:26px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div>
        <div style="font-size:13px;color:#666;margin-top:6px;">{date}</div>
    </div>
    {tier_html(picks["best"],   "#22c55e", "🟢 BEST BETS")}
    {tier_html(picks["medium"], "#f59e0b", "🟡 MEDIUM BETS")}
    {tier_html(picks["risky"],  "#ef4444", "🔴 RISKY BETS")}
    <div style="border-top:1px solid #222;padding-top:16px;text-align:center;font-size:11px;color:#444;margin-top:8px;">
        // lf-trades · daily picks &nbsp;|&nbsp; Gamble responsibly. 18+ only.
    </div>
</div>
</body>
</html>"""

# ── Send Telegram ──────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting LFT Betting Bot...")
    football_odds = fetch_football_odds()
    print(f"Football: {len(football_odds)} events fetched")
    print("Analysing with Claude + web search for horse racing...")
    picks = analyse_bets(football_odds)
    telegram_msg = format_telegram(picks)
    email_html = format_email_html(picks)
    send_telegram(telegram_msg)
    send_email(email_html, picks["date"])
    print("Done! Picks sent.")

if __name__ == "__main__":
    main()
