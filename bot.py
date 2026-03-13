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
RACING_API_USER   = os.getenv("RACING_API_USER", "fbt79UdYj5Hz1MDU2XwjVBXx")
RACING_API_PASS   = os.getenv("RACING_API_PASS", "ZR7xiE3NDqHBJG33PRhNPyYs")

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

# ── Fetch Horse Racing Data ────────────────────────────────────────────────────
def fetch_horse_racing():
    today = datetime.now().strftime("%Y-%m-%d")
    auth = (RACING_API_USER, RACING_API_PASS)
    races = []

    try:
        url = "https://api.theracingapi.com/v1/racecards/pro"
        params = {"date": today, "region": "gb"}
        r = requests.get(url, auth=auth, params=params, timeout=15)
        print(f"Racing API status: {r.status_code}")

        if r.status_code == 200:
            data = r.json()
            racecards = data.get("racecards", [])
            print(f"Found {len(racecards)} races today")

            for race in racecards[:12]:
                runners = []
                for horse in race.get("runners", []):
                    best_odds = None
                    best_bookie = ""
                    for bookie in horse.get("odds", []):
                        odds_val = bookie.get("decimal_odds") or bookie.get("odds_decimal")
                        if odds_val and (best_odds is None or float(odds_val) > float(best_odds)):
                            best_odds = float(odds_val)
                            best_bookie = bookie.get("bookmaker", "")

                    runners.append({
                        "horse": horse.get("horse", ""),
                        "jockey": horse.get("jockey", ""),
                        "trainer": horse.get("trainer", ""),
                        "form": horse.get("form", ""),
                        "age": horse.get("age", ""),
                        "best_odds": best_odds,
                        "best_bookie": best_bookie,
                        "official_rating": horse.get("official_rating", ""),
                    })

                runners.sort(key=lambda x: x["best_odds"] if x["best_odds"] else 999)

                races.append({
                    "race_name": race.get("race_name", ""),
                    "venue": race.get("course", ""),
                    "time": race.get("off_time", ""),
                    "distance": race.get("distance", ""),
                    "going": race.get("going", ""),
                    "race_class": race.get("race_class", ""),
                    "runners": runners[:8],
                })

        elif r.status_code == 401:
            print("Racing API auth failed")
        else:
            print(f"Racing API error: {r.text[:200]}")

    except Exception as e:
        print(f"Horse racing fetch error: {e}")

    return races

# ── Ask Claude to Analyse ──────────────────────────────────────────────────────
def analyse_bets(football_odds, horse_races):
    today = datetime.now().strftime("%A %d %B %Y")
    football_summary = json.dumps(football_odds, indent=2)
    racing_summary = json.dumps(horse_races, indent=2)

    prompt = f"""
You are an expert UK sports betting analyst. Today is {today}.

FOOTBALL ODDS (live from UK bookmakers):
{football_summary}

TODAY'S UK HORSE RACING CARDS (live data with best odds per bookmaker):
{racing_summary}

Pick the TOP 6 bets: 4 football + 2 horse racing.
For horses: pick based on form (1s and 2s = good), official rating, going conditions, and best_odds value.
Always use the best_odds value from the data for horses and name the best_bookie.
Rank all 6 from most confident to most risky across 3 tiers: BEST (2), MEDIUM (2), RISKY (2).
At least 1 horse pick must appear in BEST or MEDIUM.

Respond ONLY in this exact JSON, no extra text:
{{
  "date": "{today}",
  "best": [
    {{"event": "name", "bet": "bet", "odds": 2.10, "best_bookie": "Bet365", "confidence": 58, "value": "High", "analysis": "reason"}},
    {{"event": "name", "bet": "bet", "odds": 2.10, "best_bookie": "Betfair", "confidence": 58, "value": "High", "analysis": "reason"}}
  ],
  "medium": [
    {{"event": "name", "bet": "bet", "odds": 2.10, "best_bookie": "Paddy Power", "confidence": 48, "value": "Medium", "analysis": "reason"}},
    {{"event": "name", "bet": "bet", "odds": 2.10, "best_bookie": "William Hill", "confidence": 48, "value": "Medium", "analysis": "reason"}}
  ],
  "risky": [
    {{"event": "name", "bet": "bet", "odds": 4.50, "best_bookie": "Coral", "confidence": 35, "value": "High", "analysis": "reason"}},
    {{"event": "name", "bet": "bet", "odds": 2.20, "best_bookie": "Ladbrokes", "confidence": 40, "value": "Medium", "analysis": "reason"}}
  ]
}}
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

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
            is_horse = " - " in p["event"] or any(x in p["event"] for x in ["Cheltenham","Ascot","Newmarket","Sandown","Kempton","York","Doncaster","Haydock","Lingfield","Windsor"])
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
            is_horse = " - " in p["event"] or any(x in p["event"] for x in ["Cheltenham","Ascot","Newmarket","Sandown","Kempton"])
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
    print(f"Football: {len(football_odds)} events")
    horse_races = fetch_horse_racing()
    print(f"Racing: {len(horse_races)} races")
    picks = analyse_bets(football_odds, horse_races)
    telegram_msg = format_telegram(picks)
    email_html = format_email_html(picks)
    send_telegram(telegram_msg)
    send_email(email_html, picks["date"])
    print("Done! Picks sent.")

if __name__ == "__main__":
    main()
