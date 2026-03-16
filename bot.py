import os
import requests
import smtplib
import json
import sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "8580976907:AAGR4jmA0qrn6vw8RlSoszpP7uIxnYUuc98")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "7711190329")
ODDS_API_KEY      = os.getenv("ODDS_API_KEY", "08c2d60df2bf862873f6f3a32e93ad1c")
GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS", "LFT.Trades05@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS", "xutl ivtg whbf rree")
RECIPIENT_EMAIL   = os.getenv("RECIPIENT_EMAIL", "LFT.Trades05@gmail.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BANKROLL          = float(os.getenv("BANKROLL", "1000"))
DB_PATH           = os.getenv("DB_PATH", "/data/lft_bot.db")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SPORTS = {
    "horse_racing": {"emoji": "🐴", "label": "Horse Racing"},
    "nfl":          {"emoji": "🏈", "label": "NFL"},
    "nba":          {"emoji": "🏀", "label": "NBA"},
    "golf":         {"emoji": "⛳", "label": "Golf"},
}

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, sport TEXT, event TEXT, bet TEXT,
            odds REAL, confidence INTEGER, value TEXT, tier TEXT,
            stake REAL, best_bookie TEXT, analysis TEXT,
            result TEXT DEFAULT 'pending', profit_loss REAL DEFAULT 0,
            odds_band TEXT, day_of_week TEXT, bet_type TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategy_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_key TEXT UNIQUE, rule_value TEXT,
            reason TEXT, updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE, total_picks INTEGER,
            winners INTEGER, losers INTEGER,
            profit_loss REAL, roi REAL, bankroll REAL
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ── Kelly Criterion ────────────────────────────────────────────────────────────
def get_odds_band(odds):
    if odds < 2.0:  return "odds-on (<2.0)"
    if odds < 3.0:  return "short (2.0-3.0)"
    if odds < 5.0:  return "medium (3.0-5.0)"
    if odds < 10.0: return "big (5.0-10.0)"
    return "outsider (10.0+)"

def kelly_stake(confidence_pct, odds, bankroll, tier="medium"):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT rule_value FROM strategy_rules WHERE rule_key='kelly_fraction'")
    row = c.fetchone()
    conn.close()
    fraction = float(row[0]) if row else 0.25
    if tier == "risky": fraction *= 0.6

    p = confidence_pct / 100
    q = 1 - p
    b = odds - 1
    kelly = (b * p - q) / b
    if kelly <= 0: return 0
    return round(min(kelly * fraction * bankroll, bankroll * 0.05), 2)

def extract_bet_type(bet):
    bet_l = bet.lower()
    if "each way" in bet_l or "e/w" in bet_l: return "each_way"
    if "top 10" in bet_l: return "top_10"
    if "top 5" in bet_l: return "top_5"
    if "h2h" in bet_l or "head" in bet_l: return "h2h"
    if "spread" in bet_l: return "spread"
    if "over" in bet_l: return "over"
    if "under" in bet_l: return "under"
    if "prop" in bet_l: return "prop"
    if "future" in bet_l or "draft" in bet_l or "season" in bet_l: return "future"
    if "win" in bet_l: return "win"
    return "other"

# ── Strategy Evolution ─────────────────────────────────────────────────────────
def evolve_strategy():
    conn = get_db()
    c = conn.cursor()
    changes = []

    # Check losing streaks by sport + bet type
    c.execute("SELECT DISTINCT sport, bet_type FROM picks WHERE bet_type IS NOT NULL AND result != 'pending'")
    for sport, bet_type in c.fetchall():
        c.execute("""
            SELECT result FROM picks
            WHERE sport=? AND bet_type=? AND result != 'pending'
            ORDER BY date DESC LIMIT 8
        """, (sport, bet_type))
        recent = [r[0] for r in c.fetchall()]
        consecutive_losses = sum(1 for _ in iter(lambda: recent.pop(0) if recent and recent[0]=='lost' else None, None)) if recent else 0
        losses = recent.count('lost') if recent else 0
        total = len(recent)
        if total >= 5 and losses/total >= 0.7:
            key = f"avoid_{sport}_{bet_type}".replace(" ","_").lower()
            c.execute("INSERT OR REPLACE INTO strategy_rules (rule_key,rule_value,reason,updated_at) VALUES (?,?,?,?)",
                     (key,"deprioritise",f"{bet_type} ({sport}) losing {losses}/{total}",datetime.now().isoformat()))
            changes.append(f"⚠️ Deprioritising {bet_type} ({sport}) — {losses}/{total} losses")

    # Consecutive losing days
    c.execute("""
        SELECT date, SUM(profit_loss) FROM picks
        WHERE result != 'pending' GROUP BY date ORDER BY date DESC LIMIT 7
    """)
    days = c.fetchall()
    losing_run = 0
    for d in days:
        if (d[1] or 0) < 0: losing_run += 1
        else: break

    if losing_run >= 3:
        c.execute("INSERT OR REPLACE INTO strategy_rules (rule_key,rule_value,reason,updated_at) VALUES (?,?,?,?)",
                 ("kelly_fraction","0.15",f"{losing_run} consecutive losing days",datetime.now().isoformat()))
        changes.append(f"💰 Stakes reduced — {losing_run} losing days in a row")
    elif losing_run == 0 and len(days) >= 3:
        c.execute("INSERT OR REPLACE INTO strategy_rules (rule_key,rule_value,reason,updated_at) VALUES (?,?,?,?)",
                 ("kelly_fraction","0.25","Good form",datetime.now().isoformat()))

    conn.commit()
    conn.close()
    return changes

# ── Results Check ──────────────────────────────────────────────────────────────
def check_pending_results():
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""
        SELECT id, date, event, bet, odds, stake, sport
        FROM picks WHERE result='pending' AND date < ?
        ORDER BY date DESC LIMIT 20
    """, (today,))
    pending = c.fetchall()
    conn.close()

    if not pending:
        print("No pending picks")
        return

    picks_list = [{"id":p[0],"date":p[1],"event":p[2],"bet":p[3],"odds":p[4],"sport":p[6]} for p in pending]
    prompt = f"""Check results for these bets. Search for each outcome.

{json.dumps(picks_list, indent=2)}

Return ONLY JSON:
{{"results":[{{"id":1,"result":"won","notes":"brief"}},{{"id":2,"result":"lost","notes":"brief"}},{{"id":3,"result":"unknown","notes":"could not find"}}]}}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            messages=[{"role":"user","content":prompt}]
        )
        raw = ""
        for block in message.content:
            if hasattr(block,"text"): raw = block.text
        start = raw.find("{"); end = raw.rfind("}") + 1
        if start == -1 or end <= start: return
        data = json.loads(raw[start:end])

        conn = get_db()
        c = conn.cursor()
        for r in data.get("results",[]):
            if r["result"] == "unknown": continue
            c.execute("SELECT stake, odds FROM picks WHERE id=?", (r["id"],))
            row = c.fetchone()
            if not row: continue
            stake, odds = row
            pnl = round(stake*(odds-1),2) if r["result"]=="won" else -stake
            c.execute("UPDATE picks SET result=?, profit_loss=? WHERE id=?", (r["result"], pnl, r["id"]))
        conn.commit()
        conn.close()
        print(f"Results updated for {len(data.get('results',[]))} picks")
    except Exception as e:
        print(f"Result check error: {e}")

# ── Historical Intelligence ────────────────────────────────────────────────────
def build_intelligence():
    conn = get_db()
    c = conn.cursor()

    def q(sql, p=()):
        c.execute(sql, p)
        return c.fetchall()

    s = "result != 'pending'"

    # Overall
    c.execute(f"SELECT COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE {s}")
    tot, wins, pnl, staked = c.fetchone()
    tot=tot or 0; wins=wins or 0; pnl=round(pnl or 0,2); staked=round(staked or 0,2)

    # By sport
    sport_stats = {}
    for row in q(f"SELECT sport,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE {s} GROUP BY sport"):
        sp,t,w,pl,st = row
        sport_stats[sp] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2),"roi":round((pl or 0)/(st or 1)*100,1)}

    # By bet type per sport
    type_stats = {}
    for row in q(f"SELECT sport,bet_type,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss) FROM picks WHERE {s} AND bet_type IS NOT NULL GROUP BY sport,bet_type HAVING COUNT(*)>=3"):
        sp,bt,t,w,pl = row
        key = f"{sp}_{bt}"
        type_stats[key] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2)}

    # By tier
    tier_stats = {}
    for row in q(f"SELECT tier,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE {s} GROUP BY tier"):
        ti,t,w,pl,st = row
        tier_stats[ti] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"roi":round((pl or 0)/(st or 1)*100,1)}

    # By odds band
    band_stats = {}
    for row in q(f"SELECT odds_band,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE {s} AND odds_band IS NOT NULL GROUP BY odds_band"):
        bd,t,w,pl,st = row
        band_stats[bd] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"roi":round((pl or 0)/(st or 1)*100,1)}

    # Strategy rules
    rules = {}
    for row in q("SELECT rule_key,rule_value,reason FROM strategy_rules"):
        rules[row[0]] = {"value":row[1],"reason":row[2]}

    # 30 day trend
    m30 = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")
    c.execute("SELECT SUM(profit_loss) FROM picks WHERE date>=? AND result!='pending'", (m30,))
    r30 = c.fetchone()

    conn.close()

    # Build compact summary
    best_sport = max(sport_stats.items(), key=lambda x: x[1].get("roi",0), default=("none",{}))
    best_band = max(band_stats.items(), key=lambda x: x[1].get("roi",0), default=("none",{}))
    avoid = [k for k,v in rules.items() if "deprioritise" in str(v.get("value",""))]
    prioritise = [k for k,v in rules.items() if "prioritise" in str(v.get("value",""))]

    summary = f"""Total settled: {tot} bets | Win rate: {round(wins/tot*100,1) if tot else 0}% | P&L: £{pnl} | ROI: {round(pnl/staked*100,1) if staked else 0}%
Best sport: {best_sport[0]} (ROI: {best_sport[1].get('roi','N/A')}%)
Best odds band: {best_band[0]} (ROI: {best_band[1].get('roi','N/A')}%)
30-day P&L: £{round(r30[0] or 0,2) if r30 else 0}
AVOID: {', '.join(avoid) if avoid else 'none'}
PRIORITISE: {', '.join(prioritise) if prioritise else 'best judgment'}
By sport: {json.dumps({k: f"{v['win_rate']}% WR / {v['roi']}% ROI" for k,v in sport_stats.items()})}
By bet type: {json.dumps({k: f"{v['win_rate']}% WR" for k,v in list(type_stats.items())[:8]})}"""

    return summary

# ── Fetch Live Odds ────────────────────────────────────────────────────────────
def fetch_live_odds():
    sports_map = {
        "nba": "basketball_nba",
        "nfl": "americanfootball_nfl",
        "golf_pga": "golf_pga_championship_winner",
    }
    odds_data = {}
    for label, sport_key in sports_map.items():
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={"apiKey":ODDS_API_KEY,"regions":"uk","markets":"h2h,spreads",
                        "oddsFormat":"decimal","dateFormat":"iso"},
                timeout=10
            )
            if r.status_code == 200:
                events = r.json()[:6]
                compact = []
                for e in events:
                    best = {}
                    for bm in e.get("bookmakers",[])[:2]:
                        for mkt in bm.get("markets",[]):
                            for o in mkt.get("outcomes",[]):
                                name = o["name"]
                                price = float(o.get("price",0))
                                if name not in best or price > best[name]["odds"]:
                                    best[name] = {"odds":price,"bookie":bm["title"]}
                    compact.append({"match":f"{e.get('home_team','')} vs {e.get('away_team','')}","time":e.get("commence_time","")[:16],"odds":best})
                odds_data[label] = compact
                print(f"{label}: {len(compact)} events")
        except Exception as e:
            print(f"Odds fetch error {label}: {e}")
            odds_data[label] = []
    return odds_data

# ── Main Analysis ──────────────────────────────────────────────────────────────
def analyse_all_sports(odds_data, intelligence_summary):
    today = datetime.now().strftime("%A %d %B %Y")

    nba_str  = json.dumps(odds_data.get("nba",[]), indent=1)
    nfl_str  = json.dumps(odds_data.get("nfl",[]), indent=1)
    golf_str = json.dumps(odds_data.get("golf_pga",[]), indent=1)

    prompt = f"""Expert UK sports betting AI. Today is {today}.

HISTORY: {intelligence_summary}

NBA ODDS: {nba_str}

TASK - 16 picks total, 4 per sport:
1. HORSE RACING: Search "horse racing tips {today} UK nap value" - find 4 real horses today
2. NFL: Search "NFL 2026 futures draft odds" - pick 4 futures/props with real UK odds
3. NBA: Use odds above + search "NBA picks {today}" - pick 4 best value bets
4. GOLF: Search "PGA Tour tips {today} top 10 h2h" - pick 4 (2x top10 + 2x h2h)

Each pick: event, bet, decimal odds, bookmaker, confidence%, value, analysis.
Tiers per sport: BEST(1) MEDIUM(2) RISKY(1).

Output ONLY this JSON, no markdown, no extra text:
{{
  "date": "{today}",
  "horse_racing": {{
    "best": [{{"event":"Horse - Venue HH:MM","bet":"Win","odds":3.5,"best_bookie":"Paddy Power","confidence":52,"value":"High","bet_type":"win","analysis":"reason"}}],
    "medium": [
      {{"event":"Horse - Venue HH:MM","bet":"Win","odds":5.0,"best_bookie":"Bet365","confidence":40,"value":"High","bet_type":"win","analysis":"reason"}},
      {{"event":"Horse - Venue HH:MM","bet":"Each Way","odds":8.0,"best_bookie":"William Hill","confidence":33,"value":"Medium","bet_type":"each_way","analysis":"reason"}}
    ],
    "risky": [{{"event":"Horse - Venue HH:MM","bet":"Win","odds":12.0,"best_bookie":"Betfair","confidence":20,"value":"High","bet_type":"win","analysis":"reason"}}]
  }},
  "nfl": {{
    "best": [{{"event":"NFL Future","bet":"Team X to win Super Bowl","odds":6.0,"best_bookie":"Bet365","confidence":25,"value":"High","bet_type":"future","analysis":"reason"}}],
    "medium": [
      {{"event":"NFL Draft","bet":"Player X top 5 pick","odds":2.5,"best_bookie":"Coral","confidence":45,"value":"High","bet_type":"future","analysis":"reason"}},
      {{"event":"NFL Season","bet":"Team X over 9.5 wins","odds":1.9,"best_bookie":"William Hill","confidence":55,"value":"Medium","bet_type":"future","analysis":"reason"}}
    ],
    "risky": [{{"event":"NFL Future","bet":"Team X division winner","odds":4.0,"best_bookie":"Ladbrokes","confidence":30,"value":"High","bet_type":"future","analysis":"reason"}}]
  }},
  "nba": {{
    "best": [{{"event":"Team A vs Team B","bet":"Team A to Win","odds":1.85,"best_bookie":"Bet365","confidence":58,"value":"High","bet_type":"win","analysis":"reason"}}],
    "medium": [
      {{"event":"Team C vs Team D","bet":"Team C -4.5 spread","odds":1.9,"best_bookie":"William Hill","confidence":52,"value":"Medium","bet_type":"spread","analysis":"reason"}},
      {{"event":"Team E vs Team F","bet":"Over 224.5 points","odds":1.88,"best_bookie":"Betfair","confidence":54,"value":"Medium","bet_type":"over","analysis":"reason"}}
    ],
    "risky": [{{"event":"Team G vs Team H","bet":"Team H upset win","odds":3.5,"best_bookie":"Coral","confidence":32,"value":"High","bet_type":"win","analysis":"reason"}}]
  }},
  "golf": {{
    "best": [{{"event":"Tournament - Player Name","bet":"Top 10 Finish","odds":2.5,"best_bookie":"Bet365","confidence":45,"value":"High","bet_type":"top_10","analysis":"reason"}}],
    "medium": [
      {{"event":"Tournament - Player A vs Player B","bet":"Player A H2H","odds":1.8,"best_bookie":"Paddy Power","confidence":55,"value":"Medium","bet_type":"h2h","analysis":"reason"}},
      {{"event":"Tournament - Player Name","bet":"Top 10 Finish","odds":3.0,"best_bookie":"William Hill","confidence":38,"value":"High","bet_type":"top_10","analysis":"reason"}}
    ],
    "risky": [{{"event":"Tournament - Player Name","bet":"Top 5 Finish","odds":5.0,"best_bookie":"Coral","confidence":25,"value":"High","bet_type":"top_5","analysis":"reason"}}]
  }}
}}"""

    import time

    for attempt in range(3):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2500,
                tools=[{"type":"web_search_20250305","name":"web_search"}],
                messages=[{"role":"user","content":prompt}]
            )
            break
        except Exception as e:
            if "rate_limit" in str(e).lower() and attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"Rate limit hit — waiting {wait}s before retry {attempt+2}/3...")
                time.sleep(wait)
            else:
                raise

    raw = ""
    for block in message.content:
        if hasattr(block,"text"): raw = block.text

    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"): part = part[4:].strip()
            if part.startswith("{"): raw = part; break

    start = raw.find("{"); end = raw.rfind("}") + 1
    if start != -1 and end > start: raw = raw[start:end]
    return json.loads(raw)

# ── Save Picks ─────────────────────────────────────────────────────────────────
def save_and_stake_picks(picks):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT SUM(profit_loss) FROM picks WHERE result!='pending'")
    row = c.fetchone()
    conn.close()
    current_bankroll = BANKROLL + (row[0] or 0)

    today    = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")
    now      = datetime.now().isoformat()

    for sport in SPORTS.keys():
        sport_picks = picks.get(sport, {})
        for tier in ["best","medium","risky"]:
            for p in sport_picks.get(tier, []):
                stake = kelly_stake(p["confidence"], p["odds"], current_bankroll, tier)
                p["stake"] = stake
                p["potential_return"] = round(stake * p["odds"], 2)
                bet_type = p.get("bet_type") or extract_bet_type(p["bet"])

                conn2 = get_db()
                c2 = conn2.cursor()
                c2.execute("""
                    INSERT INTO picks (date,sport,event,bet,odds,confidence,value,tier,
                                       stake,best_bookie,analysis,odds_band,day_of_week,bet_type,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (today, sport, p["event"], p["bet"], p["odds"], p["confidence"],
                      p["value"], tier, stake, p.get("best_bookie",""), p["analysis"],
                      get_odds_band(p["odds"]), day_name, bet_type, now))
                conn2.commit()
                conn2.close()

# ── Stats ──────────────────────────────────────────────────────────────────────
def get_stats():
    conn = get_db()
    c = conn.cursor()
    week_ago  = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")

    c.execute("SELECT COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE date>=? AND result!='pending'",(week_ago,))
    w = c.fetchone()
    c.execute("SELECT COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE date>=? AND result!='pending'",(month_ago,))
    m = c.fetchone()

    # Per sport this month
    c.execute("SELECT sport,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss) FROM picks WHERE date>=? AND result!='pending' GROUP BY sport",(month_ago,))
    sport_month = {row[0]:{"bets":row[1],"wins":row[2] or 0,"pnl":round(row[3] or 0,2)} for row in c.fetchall()}

    conn.close()
    week  = {"bets":w[0] or 0,"wins":w[1] or 0,"pnl":round(w[2] or 0,2)} if w else None
    month = {"bets":m[0] or 0,"wins":m[1] or 0,"pnl":round(m[2] or 0,2),
             "roi":round((m[2] or 0)/(m[3] or 1)*100,1) if m and m[3] else 0,
             "win_rate":round((m[1] or 0)/(m[0] or 1)*100,1) if m and m[0] else 0} if m else None
    return week, month, sport_month

# ── Format Telegram ────────────────────────────────────────────────────────────
def format_telegram(picks, week, month, sport_month, strategy_changes):
    date = picks["date"]
    lines = ["🏆 *LFT DAILY BETTING PICKS*", f"📅 {date}", "━━━━━━━━━━━━━━━━━━━━"]

    # Stats header
    if month and month["bets"] >= 5:
        pc = "📈" if month["pnl"] >= 0 else "📉"
        lines.append(f"\n{pc} *30-Day:* {month['wins']}W/{month['bets']-month['wins']}L | ROI: {month['roi']}% | P&L: £{month['pnl']}")
    if week and week["bets"] >= 2:
        lines.append(f"📊 *Week:* {week['wins']}W/{week['bets']-week['wins']}L | P&L: £{week['pnl']}")

    # Strategy alerts
    if strategy_changes:
        lines.append("\n🔄 *AUTO-ADJUSTMENTS*")
        for ch in strategy_changes: lines.append(ch)

    lines.append("━━━━━━━━━━━━━━━━━━━━")

    tier_labels = {"best":"🟢 BEST","medium":"🟡 MEDIUM","risky":"🔴 RISKY"}

    for sport_key, sport_info in SPORTS.items():
        sport_picks = picks.get(sport_key, {})
        if not any(sport_picks.get(t) for t in ["best","medium","risky"]): continue

        lines.append(f"\n{sport_info['emoji']} *{sport_info['label'].upper()}*")

        # Sport month stats
        sm = sport_month.get(sport_key)
        if sm and sm["bets"] >= 3:
            wr = round(sm["wins"]/sm["bets"]*100,1)
            lines.append(f"_{wr}% win rate this month | P&L: £{sm['pnl']}_")
        lines.append("──────────────────")

        for tier in ["best","medium","risky"]:
            tier_picks = sport_picks.get(tier, [])
            if not tier_picks: continue
            lines.append(f"\n*{tier_labels[tier]}*")
            for p in tier_picks:
                lines.append(f"\n{sport_info['emoji']} *{p['event']}*")
                lines.append(f"📌 {p['bet']}")
                bookie = f" @ {p.get('best_bookie','')}" if p.get('best_bookie') else ""
                lines.append(f"💰 {p['odds']}{bookie} | 🎯 {p['confidence']}% | ⚡ {p['value']}")
                if p.get("stake",0) > 0:
                    lines.append(f"💵 £{p.get('stake',0)} → £{p.get('potential_return',0)}")
                lines.append(f"📊 _{p['analysis']}_")
                lines.append("──────────────────")

    lines.append("\n// lf-trades · daily picks")
    lines.append("⚠️ _Gamble responsibly. 18+ only._")
    return "\n".join(lines)

# ── Format Email ───────────────────────────────────────────────────────────────
def format_email_html(picks, week, month, sport_month, strategy_changes):
    date = picks["date"]

    stats_html = ""
    if month and month["bets"] >= 5:
        pc = "#22c55e" if month["pnl"] >= 0 else "#ef4444"
        wc = "#22c55e" if (week and week["pnl"] >= 0) else "#ef4444"
        stats_html = f"""
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px;">
            <div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;">
                <div style="font-size:10px;color:#555;margin-bottom:4px;">30-DAY WIN RATE</div>
                <div style="font-size:20px;font-weight:bold;color:#fff;">{month['win_rate']}%</div>
            </div>
            <div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;">
                <div style="font-size:10px;color:#555;margin-bottom:4px;">30-DAY ROI</div>
                <div style="font-size:20px;font-weight:bold;color:{pc};">{month['roi']}%</div>
            </div>
            <div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;">
                <div style="font-size:10px;color:#555;margin-bottom:4px;">MONTHLY P&L</div>
                <div style="font-size:20px;font-weight:bold;color:{pc};">£{month['pnl']}</div>
            </div>
            <div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;">
                <div style="font-size:10px;color:#555;margin-bottom:4px;">WEEK P&L</div>
                <div style="font-size:20px;font-weight:bold;color:{wc};">£{week['pnl'] if week else 'N/A'}</div>
            </div>
        </div>"""

    alerts_html = ""
    if strategy_changes:
        items = "".join([f'<div style="padding:5px 0;border-bottom:1px solid #222;font-size:12px;color:#ccc;">{a}</div>' for a in strategy_changes])
        alerts_html = f'<div style="background:#1a1a1a;border-radius:8px;padding:14px;margin-bottom:18px;"><div style="font-size:10px;color:#f0b429;font-weight:bold;margin-bottom:8px;">🔄 AUTO-ADJUSTMENTS</div>{items}</div>'

    def pick_card(p, colour, emoji):
        bookie = f'<span style="background:#2a2000;padding:2px 8px;border-radius:4px;font-size:11px;color:#f0b429;">📍 {p.get("best_bookie","")}</span>' if p.get("best_bookie") else ""
        stake_row = f'<div style="margin-top:6px;background:#111;border-radius:5px;padding:6px 10px;font-size:12px;color:#aaa;">💵 £{p.get("stake",0)} → <b style="color:#22c55e">£{p.get("potential_return",0)}</b></div>' if p.get("stake",0) > 0 else ""
        return f"""
        <div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:14px;margin-bottom:10px;">
            <div style="font-size:12px;color:#666;margin-bottom:3px;">{emoji} {p['event']}</div>
            <div style="font-size:14px;font-weight:bold;color:#f0b429;margin-bottom:7px;">📌 {p['bet']}</div>
            <div style="display:flex;gap:6px;margin-bottom:5px;flex-wrap:wrap;align-items:center;">
                <span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">💰 {p['odds']}</span>
                <span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">🎯 {p['confidence']}%</span>
                <span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">⚡ {p['value']}</span>
                {bookie}
            </div>
            {stake_row}
            <div style="font-size:12px;color:#aaa;line-height:1.6;margin-top:8px;">{p['analysis']}</div>
        </div>"""

    def sport_section(sport_key, sport_info, sport_picks, sm):
        if not any(sport_picks.get(t) for t in ["best","medium","risky"]): return ""
        tc = {"best":"#22c55e","medium":"#f59e0b","risky":"#ef4444"}
        tl = {"best":"🟢 BEST","medium":"🟡 MEDIUM","risky":"🔴 RISKY"}

        stats_row = ""
        if sm and sm["bets"] >= 3:
            wr = round(sm["wins"]/sm["bets"]*100,1)
            pc = "#22c55e" if sm["pnl"] >= 0 else "#ef4444"
            stats_row = f'<div style="font-size:11px;color:#666;margin-bottom:12px;">{wr}% win rate this month | P&L: <span style="color:{pc}">£{sm["pnl"]}</span></div>'

        html = f'<div style="font-size:16px;font-weight:bold;color:#fff;margin-bottom:6px;padding-bottom:8px;border-bottom:1px solid #222;">{sport_info["emoji"]} {sport_info["label"]}</div>{stats_row}'
        for tk in ["best","medium","risky"]:
            tp = sport_picks.get(tk,[])
            if tp:
                html += f'<div style="font-size:10px;font-weight:bold;letter-spacing:0.1em;color:{tc[tk]};margin:12px 0 7px;">{tl[tk]}</div>'
                for p in tp:
                    html += pick_card(p, tc[tk], sport_info["emoji"])
        return f'<div style="margin-bottom:28px;">{html}</div>'

    sports_html = ""
    _, _, sm_data = get_stats()
    for sport_key, sport_info in SPORTS.items():
        sports_html += sport_section(sport_key, sport_info, picks.get(sport_key,{}), sm_data.get(sport_key))

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:620px;margin:0 auto;padding:24px;">
    <div style="text-align:center;margin-bottom:20px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:5px;">LOOKING FOR TRADES</div>
        <div style="font-size:24px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div>
        <div style="font-size:12px;color:#555;margin-top:4px;">{date}</div>
    </div>
    {stats_html}
    {alerts_html}
    {sports_html}
    <div style="border-top:1px solid #1a1a1a;padding-top:12px;text-align:center;font-size:11px;color:#333;margin-top:6px;">
        // lf-trades · daily picks &nbsp;|&nbsp; Gamble responsibly. 18+ only.
    </div>
</div>
</body></html>"""

# ── Weekly Report ──────────────────────────────────────────────────────────────
def build_weekly_report():
    if datetime.now().strftime("%A") != "Sunday": return None
    conn = get_db()
    c = conn.cursor()
    week_ago = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss),SUM(stake) FROM picks WHERE date>=? AND result!='pending'",(week_ago,))
    row = c.fetchone()
    if not row or not row[0]: conn.close(); return None
    total,wins,pnl,staked = row
    wins=wins or 0; pnl=round(pnl or 0,2); staked=round(staked or 0,2)
    c.execute("SELECT sport,COUNT(*),SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),SUM(profit_loss) FROM picks WHERE date>=? AND result!='pending' GROUP BY sport",(week_ago,))
    sport_rows = c.fetchall()
    c.execute("SELECT event,bet,odds,profit_loss FROM picks WHERE date>=? AND result='won' ORDER BY profit_loss DESC LIMIT 1",(week_ago,))
    best = c.fetchone()
    conn.close()

    roi = round(pnl/staked*100,1) if staked else 0
    wr = round(wins/total*100,1) if total else 0
    report = f"""📊 *LFT WEEKLY REPORT*
Week ending {datetime.now().strftime('%d %B %Y')}
━━━━━━━━━━━━━━━━━━━━

*Summary:* {wins}W/{total-wins}L | {wr}% WR | ROI: {roi}% | P&L: £{pnl}

*By Sport:*"""
    for sp,t,w,pl in sport_rows:
        emoji = SPORTS.get(sp,{}).get("emoji","🎯")
        report += f"\n{emoji} {sp}: {w or 0}W/{t-(w or 0)}L | £{round(pl or 0,2)}"
    if best:
        report += f"\n\n🏆 *Best Pick:* {best[0]} — {best[1]} @ {best[2]} | +£{round(best[3],2)}"
    report += "\n\n// lf-trades · weekly analysis"
    return report

# ── Send ───────────────────────────────────────────────────────────────────────
def send_telegram(text):
    chunks = [text[i:i+4000] for i in range(0,len(text),4000)]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":chunk,"parse_mode":"Markdown"},
            timeout=10
        )
        print(f"Telegram: {r.status_code}")

def send_email(html_body, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as server:
        server.login(GMAIL_ADDRESS,GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS,RECIPIENT_EMAIL,msg.as_string())
    print("Email sent")

# ── Main ───────────────────────────────────────────────────────────────────────
def auto_seed_if_empty():
    """Automatically seeds historical data if database is empty"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM picks WHERE result != 'pending'")
    existing = c.fetchone()[0]
    conn.close()

    if existing >= 20:
        print(f"Database has {existing} settled picks — skipping seed")
        return

    print("Database empty — auto-seeding 3 months of historical data...")
    sports_queries = {
        "horse_racing": [
            f"best UK horse racing results winners December 2024 January 2025",
            f"Cheltenham Newmarket horse racing results winners odds February 2025"
        ],
        "nba": [
            f"NBA game results scores December 2024 January 2025 winners",
            f"NBA betting results picks February 2025"
        ],
        "nfl": [
            f"NFL game results scores playoffs January 2025",
            f"NFL futures odds Super Bowl 2025 predictions results"
        ],
        "golf": [
            f"PGA Tour tournament results winners top 10 December 2024 January 2025",
            f"golf tournament results odds February 2025"
        ]
    }

    total = 0
    for sport, queries in sports_queries.items():
        try:
            query = queries[0]
            prompt = f"""Search for: "{query}"

Based on real results you find, generate 15 realistic historical betting picks for {sport}.
Use REAL events, teams, horses, players from the search results.
Apply realistic win rates: horse win 30%, horse EW 40%, NBA 53%, NFL game 52%, NFL future 25%, golf top10 35%, golf H2H 50%.

Date range: 2024-12-01 to 2025-02-28
Return ONLY a JSON array, no markdown:
[
  {{
    "date": "2024-12-15",
    "sport": "{sport}",
    "event": "Real event name",
    "bet": "Specific bet",
    "odds": 2.5,
    "confidence": 45,
    "value": "High",
    "tier": "medium",
    "stake": 20.0,
    "best_bookie": "Bet365",
    "analysis": "Brief reason",
    "result": "won",
    "profit_loss": 30.0,
    "bet_type": "win"
  }}
]"""

            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=3000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )

            raw = ""
            for block in message.content:
                if hasattr(block, "text"): raw = block.text

            raw = raw.strip()
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip()
                    if part.startswith("json"): part = part[4:].strip()
                    if part.startswith("["): raw = part; break

            start = raw.find("["); end = raw.rfind("]") + 1
            if start == -1 or end <= start:
                print(f"  No data for {sport}")
                continue

            picks = json.loads(raw[start:end])
            conn2 = get_db()
            c2 = conn2.cursor()
            inserted = 0
            for p in picks:
                try:
                    date = p.get("date","2025-01-15")
                    try: day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
                    except: day_name = "Monday"
                    odds = float(p.get("odds", 2.0))
                    stake = float(p.get("stake", 20.0))
                    result = p.get("result","lost")
                    pnl = round(stake*(odds-1),2) if result=="won" else -stake

                    c2.execute("""
                        INSERT INTO picks (date,sport,event,bet,odds,confidence,value,tier,
                                          stake,best_bookie,analysis,result,profit_loss,
                                          odds_band,day_of_week,bet_type,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (date, sport, p.get("event",""), p.get("bet",""),
                          odds, int(p.get("confidence",40)), p.get("value","Medium"),
                          p.get("tier","medium"), stake, p.get("best_bookie","Bet365"),
                          p.get("analysis",""), result, pnl,
                          get_odds_band(odds), day_name, p.get("bet_type","win"),
                          datetime.now().isoformat()))
                    inserted += 1
                except Exception as e:
                    pass
            conn2.commit()
            conn2.close()
            total += inserted
            print(f"  {sport}: {inserted} picks seeded")
        except Exception as e:
            print(f"  {sport} seed error: {e}")

    print(f"Auto-seed complete: {total} historical picks loaded")

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] LFT Bot v4 starting...")
    init_db()

    # Check if seeding needed — if so, seed and exit, let next run do picks
    conn_check = get_db()
    c_check = conn_check.cursor()
    c_check.execute("SELECT COUNT(*) FROM picks WHERE result != 'pending'")
    existing_count = c_check.fetchone()[0]
    conn_check.close()

    if existing_count < 20:
        print("Database empty — seeding history now. Picks will run at next scheduled time.")
        auto_seed_if_empty()
        print("Seeding done. Bot will send picks at 7AM tomorrow.")
        return

    print("Checking pending results...")
    check_pending_results()

    print("Evolving strategy...")
    strategy_changes = evolve_strategy()

    print("Building intelligence...")
    intelligence = build_intelligence()

    print("Fetching live odds...")
    odds_data = fetch_live_odds()

    print("Analysing all sports with Claude...")
    picks = analyse_all_sports(odds_data, intelligence)

    print("Saving picks and calculating stakes...")
    save_and_stake_picks(picks)

    week, month, sport_month = get_stats()

    print("Sending picks...")
    tg_msg    = format_telegram(picks, week, month, sport_month, strategy_changes)
    email_html = format_email_html(picks, week, month, sport_month, strategy_changes)
    send_telegram(tg_msg)
    send_email(email_html, f"🏆 LFT Daily Picks — {picks['date']}")

    weekly = build_weekly_report()
    if weekly:
        send_telegram(weekly)
        print("Weekly report sent")

    print("Done! LFT Bot v4 complete.")

if __name__ == "__main__":
    main()
