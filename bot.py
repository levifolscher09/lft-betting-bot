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

# ── Ask Claude to Analyse ──────────────────────────────────────────────────────
def analyse_bets(football_odds):
    today = datetime.now().strftime("%A %d %B %Y")
    football_summary = json.dumps(football_odds, indent=2)

    prompt = f"""You are an expert UK sports betting analyst. Today is {today}.

TASK: Produce a daily betting report with TWO separate sections — Horse Racing and Football.

━━━ SECTION 1: HORSE RACING (4 picks) ━━━
Use web search to find today's best UK horse racing tips and odds.
Search: "best horse racing tips today {today} UK"
Search: "horse racing tips {today} racing post oddschecker"
Search: "top horse tips {today} cheltenham newmarket ascot"

Find 4 real horses running TODAY in UK races. For each horse find:
- The horse name, race venue and race time
- Best available decimal odds from UK bookmakers
- Which bookmaker has the best odds
- Recent form and why it's a good bet

Rank the 4 horses into:
- BEST (1 pick) — highest confidence
- MEDIUM (2 picks) — solid value
- RISKY (1 pick) — longer odds but good value

━━━ SECTION 2: FOOTBALL (2 picks) ━━━
Here is today's live football odds from UK bookmakers:
{football_summary}

Pick the 2 best football bets from the data above.
Rank them:
- BEST (1 pick)
- RISKY (1 pick)

━━━ OUTPUT FORMAT ━━━
Respond ONLY in this exact JSON, no extra text, no markdown fences:
{{
  "date": "{today}",
  "horse_racing": {{
    "best": [
      {{"event": "Horse Name - Venue HH:MM", "bet": "Horse Name to Win", "odds": 3.50, "best_bookie": "Paddy Power", "confidence": 50, "value": "High", "analysis": "Reasoning."}}
    ],
    "medium": [
      {{"event": "Horse Name - Venue HH:MM", "bet": "Horse Name to Win", "odds": 4.00, "best_bookie": "Bet365", "confidence": 42, "value": "High", "analysis": "Reasoning."}},
      {{"event": "Horse Name - Venue HH:MM", "bet": "Horse Name Each Way", "odds": 6.00, "best_bookie": "William Hill", "confidence": 35, "value": "Medium", "analysis": "Reasoning."}}
    ],
    "risky": [
      {{"event": "Horse Name - Venue HH:MM", "bet": "Horse Name to Win", "odds": 9.00, "best_bookie": "Betfair", "confidence": 25, "value": "High", "analysis": "Reasoning."}}
    ]
  }},
  "football": {{
    "best": [
      {{"event": "Team A vs Team B", "bet": "Team A to Win", "odds": 1.90, "best_bookie": "Bet365", "confidence": 62, "value": "High", "analysis": "Reasoning."}}
    ],
    "risky": [
      {{"event": "Team C vs Team D", "bet": "Team D to Win", "odds": 4.20, "best_bookie": "Coral", "confidence": 32, "value": "High", "analysis": "Reasoning."}}
    ]
  }}
}}"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )

    # Get the last text block (after web search tool use)
    raw = ""
    for block in message.content:
        if hasattr(block, "text"):
            raw = block.text

    raw = raw.strip()

    # Strip markdown fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    # Extract JSON object
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    return json.loads(raw)

# ── Format Telegram ────────────────────────────────────────────────────────────
def format_telegram(picks):
    date = picks["date"]
    lines = []
    lines.append("🏆 *LFT DAILY BETTING PICKS*")
    lines.append(f"📅 {date}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # ── Horse Racing Section
    lines.append("\n🐴 *HORSE RACING*")
    lines.append("──────────────────")

    hr = picks["horse_racing"]
    horse_tiers = [
        ("🟢 BEST", hr.get("best", [])),
        ("🟡 MEDIUM", hr.get("medium", [])),
        ("🔴 RISKY", hr.get("risky", [])),
    ]
    for tier_name, tier_picks in horse_tiers:
        if tier_picks:
            lines.append(f"\n*{tier_name}*")
            for p in tier_picks:
                lines.append(f"\n🐴 *{p['event']}*")
                lines.append(f"📌 Bet: {p['bet']}")
                bookie = f" @ {p.get('best_bookie','')}" if p.get('best_bookie') else ""
                lines.append(f"💰 Odds: {p['odds']}{bookie} | Confidence: {p['confidence']}% | Value: {p['value']}")
                lines.append(f"📊 _{p['analysis']}_")
                lines.append("──────────────────")

    # ── Football Section
    lines.append("\n⚽ *FOOTBALL*")
    lines.append("──────────────────")

    fb = picks["football"]
    football_tiers = [
        ("🟢 BEST", fb.get("best", [])),
        ("🟡 MEDIUM", fb.get("medium", [])),
        ("🔴 RISKY", fb.get("risky", [])),
    ]
    for tier_name, tier_picks in football_tiers:
        if tier_picks:
            lines.append(f"\n*{tier_name}*")
            for p in tier_picks:
                lines.append(f"\n⚽ *{p['event']}*")
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

    def pick_card(p, colour, emoji):
        bookie_badge = f'<span style="background:#2a2000;padding:3px 8px;border-radius:4px;font-size:11px;color:#f0b429;border:1px solid #f0b42940;">📍 {p.get("best_bookie","")}</span>' if p.get("best_bookie") else ""
        return f"""
        <div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:16px;margin-bottom:12px;">
            <div style="font-size:13px;color:#888;margin-bottom:6px;">{emoji} {p['event']}</div>
            <div style="font-size:16px;font-weight:bold;color:#f0b429;margin-bottom:8px;">📌 {p['bet']}</div>
            <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center;">
                <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">💰 Odds: <b style="color:#fff">{p['odds']}</b></span>
                <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">🎯 {p['confidence']}% confidence</span>
                <span style="background:#222;padding:4px 10px;border-radius:4px;font-size:12px;color:#ccc;">⚡ {p['value']} value</span>
                {bookie_badge}
            </div>
            <div style="font-size:13px;color:#aaa;line-height:1.6;">{p['analysis']}</div>
        </div>"""

    def section_html(title, emoji, tiers_data, pick_emoji):
        content = f'<div style="font-size:18px;font-weight:bold;color:#fff;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #333;">{emoji} {title}</div>'
        tier_colours = {"best": "#22c55e", "medium": "#f59e0b", "risky": "#ef4444"}
        tier_labels  = {"best": "🟢 BEST", "medium": "🟡 MEDIUM", "risky": "🔴 RISKY"}
        for tier_key in ["best", "medium", "risky"]:
            tier_picks = tiers_data.get(tier_key, [])
            if tier_picks:
                colour = tier_colours[tier_key]
                label  = tier_labels[tier_key]
                content += f'<div style="font-size:11px;font-weight:bold;letter-spacing:0.1em;color:{colour};margin:16px 0 8px;">{label}</div>'
                for p in tier_picks:
                    content += pick_card(p, colour, pick_emoji)
        return f'<div style="margin-bottom:32px;">{content}</div>'

    horse_html    = section_html("Horse Racing", "🐴", picks["horse_racing"], "🐴")
    football_html = section_html("Football", "⚽", picks["football"], "⚽")

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
    <div style="text-align:center;margin-bottom:32px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:6px;">LOOKING FOR TRADES</div>
        <div style="font-size:26px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div>
        <div style="font-size:13px;color:#666;margin-top:6px;">{date}</div>
    </div>
    {horse_html}
    {football_html}
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
    email_html   = format_email_html(picks)
    send_telegram(telegram_msg)
    send_email(email_html, picks["date"])
    print("Done! Picks sent.")

if __name__ == "__main__":
    main()
