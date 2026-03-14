import os
import requests
import smtplib
import json
import sqlite3
import math
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "8580976907:AAGR4jmA0qrn6vw8RlSoszpP7uIxnYUuc98")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "7711190329")
ODDS_API_KEY       = os.getenv("ODDS_API_KEY", "08c2d60df2bf862873f6f3a32e93ad1c")
FOOTBALL_API_KEY   = os.getenv("FOOTBALL_API_KEY", "")       # api-football.com free key
GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS", "LFT.Trades05@gmail.com")
GMAIL_APP_PASS     = os.getenv("GMAIL_APP_PASS", "xutl ivtg whbf rree")
RECIPIENT_EMAIL    = os.getenv("RECIPIENT_EMAIL", "LFT.Trades05@gmail.com")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
BANKROLL           = float(os.getenv("BANKROLL", "1000"))
DB_PATH            = os.getenv("DB_PATH", "/data/lft_bot.db")
WEATHER_API_KEY    = os.getenv("WEATHER_API_KEY", "")        # openweathermap.org free key

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
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
            result TEXT DEFAULT 'pending',
            profit_loss REAL DEFAULT 0,
            odds_band TEXT, day_of_week TEXT,
            bet_type TEXT, venue TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategy_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_key TEXT UNIQUE,
            rule_value TEXT,
            reason TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id INTEGER,
            alert_type TEXT,
            sent_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            total_picks INTEGER, winners INTEGER,
            losers INTEGER, profit_loss REAL,
            roi REAL, bankroll REAL
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY RULES — self-evolving
# ─────────────────────────────────────────────────────────────────────────────
def get_strategy_rules():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT rule_key, rule_value, reason FROM strategy_rules")
    rules = {row[0]: {"value": row[1], "reason": row[2]} for row in c.fetchall()}
    conn.close()
    return rules

def set_strategy_rule(key, value, reason):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO strategy_rules (rule_key, rule_value, reason, updated_at)
        VALUES (?, ?, ?, ?)
    """, (key, str(value), reason, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def evolve_strategy():
    """
    Analyses losing streaks and patterns across ALL history.
    Rewrites strategy rules automatically.
    Returns a summary of changes made.
    """
    conn = get_db()
    c = conn.cursor()
    changes = []

    # ── Check losing streaks by bet type (last 10 bets per type)
    c.execute("""
        SELECT bet_type, sport,
               COUNT(*) as total,
               SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses,
               SUM(profit_loss) as pnl
        FROM picks
        WHERE result != 'pending' AND bet_type IS NOT NULL
        GROUP BY bet_type, sport
        HAVING total >= 5
    """)
    for row in c.fetchall():
        bet_type, sport, total, losses, pnl = row
        loss_rate = (losses / total) if total > 0 else 0
        key = f"avoid_{sport}_{bet_type}".replace(" ", "_").lower()

        if loss_rate >= 0.7 and total >= 5:
            reason = f"{bet_type} ({sport}) losing {losses}/{total} bets, ROI: £{round(pnl,2)}"
            set_strategy_rule(key, "deprioritise", reason)
            changes.append(f"⚠️ Deprioritising {bet_type} ({sport}) — {losses}/{total} losses")
        elif loss_rate <= 0.35 and total >= 5:
            set_strategy_rule(key, "prioritise", f"{bet_type} ({sport}) winning {total-losses}/{total}")
            changes.append(f"✅ Prioritising {bet_type} ({sport}) — strong performer")

    # ── Check consecutive losing days
    c.execute("""
        SELECT date, SUM(profit_loss) as daily_pnl
        FROM picks
        WHERE result != 'pending'
        GROUP BY date
        ORDER BY date DESC
        LIMIT 7
    """)
    daily = c.fetchall()
    losing_days = 0
    for day in daily:
        if day[1] < 0:
            losing_days += 1
        else:
            break

    if losing_days >= 3:
        set_strategy_rule("kelly_fraction", "0.15",
            f"{losing_days} consecutive losing days — reducing stake size")
        changes.append(f"💰 Reduced Kelly fraction to 15% — {losing_days} losing days in a row")
    elif losing_days == 0 and len(daily) >= 3:
        set_strategy_rule("kelly_fraction", "0.25", "Good recent form — normal stakes")

    # ── Check if risky bets are destroying bankroll
    c.execute("""
        SELECT COUNT(*), SUM(profit_loss)
        FROM picks
        WHERE tier='risky' AND result != 'pending'
    """)
    risky_row = c.fetchone()
    if risky_row and risky_row[0] and risky_row[0] >= 5:
        risky_roi = (risky_row[1] or 0) / risky_row[0]
        if risky_roi < -2:
            set_strategy_rule("risky_stake_multiplier", "0.5",
                f"Risky bets losing consistently — halving risky stakes")
            changes.append("🔻 Halving stakes on risky picks — poor historical performance")

    # ── Check best performing odds band and lock it in
    c.execute("""
        SELECT odds_band,
               COUNT(*) as total,
               SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
               SUM(profit_loss) as pnl,
               SUM(stake) as staked
        FROM picks
        WHERE result != 'pending' AND odds_band IS NOT NULL
        GROUP BY odds_band
        HAVING total >= 5
        ORDER BY (SUM(profit_loss)/MAX(SUM(stake),1)) DESC
        LIMIT 1
    """)
    best_band = c.fetchone()
    if best_band:
        set_strategy_rule("best_odds_band", best_band[0],
            f"Best ROI band: {best_band[0]} — {best_band[2]}/{best_band[1]} wins, £{round(best_band[3],2)} profit")
        changes.append(f"🎯 Targeting {best_band[0]} odds range — best historical ROI")

    conn.close()
    return changes

# ─────────────────────────────────────────────────────────────────────────────
# KELLY STAKING
# ─────────────────────────────────────────────────────────────────────────────
def kelly_stake(confidence_pct, odds, bankroll, tier="medium"):
    rules = get_strategy_rules()
    fraction = float(rules.get("kelly_fraction", {}).get("value", 0.25))
    risky_mult = float(rules.get("risky_stake_multiplier", {}).get("value", 1.0))

    p = confidence_pct / 100
    q = 1 - p
    b = odds - 1
    kelly = (b * p - q) / b
    if kelly <= 0:
        return 0

    stake = kelly * fraction * bankroll
    if tier == "risky":
        stake *= risky_mult

    return round(min(stake, bankroll * 0.05), 2)

def get_odds_band(odds):
    if odds < 2.0:  return "odds-on (<2.0)"
    if odds < 3.0:  return "short (2.0-3.0)"
    if odds < 5.0:  return "medium (3.0-5.0)"
    if odds < 10.0: return "big (5.0-10.0)"
    return "outsider (10.0+)"

def extract_bet_type(bet):
    bet_l = bet.lower()
    if "each way" in bet_l or "e/w" in bet_l: return "each_way"
    if "btts" in bet_l or "both teams" in bet_l: return "btts"
    if "over" in bet_l: return "over_goals"
    if "under" in bet_l: return "under_goals"
    if "handicap" in bet_l: return "handicap"
    if "draw" in bet_l: return "draw"
    if "win" in bet_l: return "win"
    return "other"

# ─────────────────────────────────────────────────────────────────────────────
# RESULTS — Football API + web search fallback
# ─────────────────────────────────────────────────────────────────────────────
def check_football_result_api(event, date):
    """Try API-Football first for accurate football results"""
    if not FOOTBALL_API_KEY:
        return None
    try:
        url = "https://v3.football.api-sports.io/fixtures"
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        params = {"date": date, "status": "FT"}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            fixtures = r.json().get("response", [])
            for f in fixtures:
                home = f["teams"]["home"]["name"]
                away = f["teams"]["away"]["name"]
                if home.lower() in event.lower() or away.lower() in event.lower():
                    goals_h = f["goals"]["home"]
                    goals_a = f["goals"]["away"]
                    return {"home": home, "away": away,
                            "score": f"{goals_h}-{goals_a}",
                            "home_win": goals_h > goals_a,
                            "away_win": goals_a > goals_h,
                            "draw": goals_h == goals_a}
    except Exception as e:
        print(f"Football API error: {e}")
    return None

def check_all_pending_results():
    """Check all pending picks using API where possible, web search as fallback"""
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""
        SELECT id, date, event, bet, odds, stake, sport, bet_type
        FROM picks
        WHERE result = 'pending' AND date < ?
        ORDER BY date DESC
    """, (today,))
    pending = c.fetchall()
    conn.close()

    if not pending:
        print("No pending picks to check")
        return 0, 0, 0

    # Try football API first for football picks
    api_resolved = {}
    for p in pending:
        pick_id, date, event, bet, odds, stake, sport, bet_type = p
        if sport == "football" and FOOTBALL_API_KEY:
            result_data = check_football_result_api(event, date)
            if result_data:
                bet_lower = bet.lower()
                won = False
                if "home" in bet_lower or result_data["home"].lower() in bet_lower:
                    won = result_data["home_win"]
                elif "away" in bet_lower or result_data["away"].lower() in bet_lower:
                    won = result_data["away_win"]
                elif "draw" in bet_lower:
                    won = result_data["draw"]
                elif "btts" in bet_lower or "both teams" in bet_lower:
                    score = result_data["score"].split("-")
                    won = int(score[0]) > 0 and int(score[1]) > 0
                api_resolved[pick_id] = "won" if won else "lost"

    # Web search for everything else
    unresolved = [p for p in pending if p[0] not in api_resolved]
    web_resolved = {}

    if unresolved:
        picks_list = [{"id": p[0], "date": p[1], "event": p[2],
                       "bet": p[3], "odds": p[4], "sport": p[6]} for p in unresolved]
        prompt = f"""Check the results of these bets. Search for each one.

{json.dumps(picks_list, indent=2)}

Return ONLY this JSON:
{{
  "results": [
    {{"id": 1, "result": "won", "notes": "brief note"}},
    {{"id": 2, "result": "lost", "notes": "brief note"}},
    {{"id": 3, "result": "unknown", "notes": "could not find"}}
  ]
}}"""
        try:
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1500,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            raw = ""
            for block in message.content:
                if hasattr(block, "text"):
                    raw = block.text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start != -1 and end > start:
                data = json.loads(raw[start:end])
                for r in data.get("results", []):
                    if r["result"] != "unknown":
                        web_resolved[r["id"]] = r["result"]
        except Exception as e:
            print(f"Web result check error: {e}")

    # Apply all results
    all_resolved = {**api_resolved, **web_resolved}
    conn = get_db()
    c = conn.cursor()
    winners = losers = 0
    total_pnl = 0.0

    for pick_id, result in all_resolved.items():
        c.execute("SELECT stake, odds FROM picks WHERE id=?", (pick_id,))
        row = c.fetchone()
        if not row:
            continue
        stake, odds = row
        pnl = round(stake * (odds - 1), 2) if result == "won" else -stake
        c.execute("UPDATE picks SET result=?, profit_loss=? WHERE id=?",
                  (result, pnl, pick_id))
        if result == "won":
            winners += 1
        else:
            losers += 1
        total_pnl += pnl

    conn.commit()
    conn.close()
    print(f"Results: {winners}W {losers}L £{round(total_pnl,2)}")
    return winners, losers, total_pnl

# ─────────────────────────────────────────────────────────────────────────────
# LOSING STREAK DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
def detect_losing_streaks():
    """
    Finds any bet type / sport combination on a losing streak >= 3.
    Returns alert messages to include in morning report.
    """
    conn = get_db()
    c = conn.cursor()
    alerts = []

    # Per bet type streaks
    c.execute("SELECT DISTINCT sport, bet_type FROM picks WHERE bet_type IS NOT NULL AND result != 'pending'")
    combos = c.fetchall()

    for sport, bet_type in combos:
        c.execute("""
            SELECT result FROM picks
            WHERE sport=? AND bet_type=? AND result != 'pending'
            ORDER BY date DESC LIMIT 5
        """, (sport, bet_type))
        recent = [r[0] for r in c.fetchall()]
        consecutive_losses = 0
        for r in recent:
            if r == "lost":
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= 3:
            alerts.append(f"🔴 {bet_type.replace('_',' ').title()} ({sport}) — {consecutive_losses} losses in a row. Strategy auto-adjusted.")

    # Overall losing days
    c.execute("""
        SELECT date, SUM(profit_loss)
        FROM picks WHERE result != 'pending'
        GROUP BY date ORDER BY date DESC LIMIT 5
    """)
    days = c.fetchall()
    losing_run = 0
    for d in days:
        if d[1] < 0:
            losing_run += 1
        else:
            break
    if losing_run >= 3:
        alerts.append(f"⚠️ {losing_run} consecutive losing days detected — stakes reduced automatically")

    conn.close()
    return alerts

# ─────────────────────────────────────────────────────────────────────────────
# FULL HISTORICAL INTELLIGENCE
# ─────────────────────────────────────────────────────────────────────────────
def build_intelligence():
    conn = get_db()
    c = conn.cursor()
    rules = get_strategy_rules()

    def query(sql, params=()):
        c.execute(sql, params)
        return c.fetchall()

    settled = "result != 'pending'"

    # Overall
    c.execute(f"SELECT COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE {settled}")
    tot, wins, pnl, staked = c.fetchone()
    tot = tot or 0; wins = wins or 0; pnl = round(pnl or 0,2); staked = round(staked or 0,2)

    # By sport
    sport_stats = {}
    for row in query(f"SELECT sport, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE {settled} GROUP BY sport"):
        sp, t, w, pl, st = row
        sport_stats[sp] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2),"roi":round((pl or 0)/(st or 1)*100,1)}

    # By tier
    tier_stats = {}
    for row in query(f"SELECT tier, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE {settled} GROUP BY tier"):
        ti, t, w, pl, st = row
        tier_stats[ti] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2),"roi":round((pl or 0)/(st or 1)*100,1)}

    # By odds band
    band_stats = {}
    for row in query(f"SELECT odds_band, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE {settled} AND odds_band IS NOT NULL GROUP BY odds_band"):
        bd, t, w, pl, st = row
        band_stats[bd] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2),"roi":round((pl or 0)/(st or 1)*100,1)}

    # By bet type
    type_stats = {}
    for row in query(f"SELECT bet_type, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss) FROM picks WHERE {settled} AND bet_type IS NOT NULL GROUP BY bet_type HAVING COUNT(*) >= 3"):
        bt, t, w, pl = row
        type_stats[bt] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2)}

    # By day of week
    day_stats = {}
    for row in query(f"SELECT day_of_week, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss) FROM picks WHERE {settled} AND day_of_week IS NOT NULL GROUP BY day_of_week"):
        dy, t, w, pl = row
        day_stats[dy] = {"bets":t,"wins":w or 0,"win_rate":round((w or 0)/t*100,1) if t else 0,"pnl":round(pl or 0,2)}

    # Confidence calibration
    conf_cal = {}
    for row in query(f"""
        SELECT CASE WHEN confidence<30 THEN 'under30' WHEN confidence<40 THEN '30to40'
                    WHEN confidence<50 THEN '40to50' WHEN confidence<60 THEN '50to60'
                    ELSE 'over60' END as band,
               COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), AVG(confidence)
        FROM picks WHERE {settled} GROUP BY band
    """):
        bd, t, w, avg = row
        conf_cal[bd] = {"bets":t,"stated_avg":round(avg or 0,1),"actual_rate":round((w or 0)/t*100,1) if t else 0}

    # Monthly trend
    m30 = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")
    m60 = (datetime.now()-timedelta(days=60)).strftime("%Y-%m-%d")
    c.execute("SELECT SUM(profit_loss), COUNT(*) FROM picks WHERE date>=? AND result!='pending'", (m30,))
    r30 = c.fetchone()
    c.execute("SELECT SUM(profit_loss), COUNT(*) FROM picks WHERE date>=? AND date<? AND result!='pending'", (m60,m30))
    r60 = c.fetchone()

    # Active strategy rules
    rule_summary = {k: f"{v['value']} ({v['reason']})" for k,v in rules.items()}

    conn.close()

    return {
        "overall": {"total":tot,"wins":wins,"win_rate":round(wins/tot*100,1) if tot else 0,"pnl":pnl,"staked":staked,"roi":round(pnl/staked*100,1) if staked else 0},
        "by_sport": sport_stats,
        "by_tier": tier_stats,
        "by_odds_band": band_stats,
        "by_bet_type": type_stats,
        "by_day": day_stats,
        "confidence_calibration": conf_cal,
        "last_30_days_pnl": round(r30[0] or 0,2) if r30 else 0,
        "prev_30_days_pnl": round(r60[0] or 0,2) if r60 else 0,
        "active_strategy_rules": rule_summary
    }

# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────
def fetch_football_odds():
    sports = ["soccer_epl","soccer_uefa_champs_league","soccer_efl_champ",
              "soccer_england_league1","soccer_england_league2"]
    all_odds = []
    for sport in sports:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                params={"apiKey":ODDS_API_KEY,"regions":"uk","markets":"h2h,totals,btts",
                        "oddsFormat":"decimal","dateFormat":"iso"},
                timeout=10
            )
            if r.status_code == 200:
                for event in r.json()[:6]:
                    all_odds.append({
                        "sport": sport,
                        "home_team": event.get("home_team",""),
                        "away_team": event.get("away_team",""),
                        "commence_time": event.get("commence_time",""),
                        "bookmakers": event.get("bookmakers",[])[:4],
                    })
        except Exception as e:
            print(f"Football fetch error {sport}: {e}")
    return all_odds

def fetch_weather_for_venues(venues):
    """Get going conditions proxy via weather for race venues"""
    if not WEATHER_API_KEY or not venues:
        return {}
    weather = {}
    for venue in venues[:3]:
        try:
            r = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q":f"{venue},UK","appid":WEATHER_API_KEY,"units":"metric"},
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                rain = data.get("rain",{}).get("1h",0)
                weather[venue] = {
                    "condition": data["weather"][0]["description"],
                    "rain_mm": rain,
                    "going_proxy": "soft/heavy" if rain > 2 else "good to soft" if rain > 0.5 else "good/firm"
                }
        except:
            pass
    return weather

# ─────────────────────────────────────────────────────────────────────────────
# MAIN ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def analyse_bets(football_odds, intelligence, strategy_changes, losing_alerts, weather={}):
    today    = datetime.now().strftime("%A %d %B %Y")
    day_name = datetime.now().strftime("%A")
    intel_str    = json.dumps(intelligence, indent=2)
    football_str = json.dumps(football_odds, indent=2)
    rules        = intelligence.get("active_strategy_rules", {})
    best_band    = intelligence.get("by_odds_band",{})

    # Build avoid list from rules
    avoid_list = [k.replace("avoid_","").replace("_"," ") for k,v in rules.items()
                  if "deprioritise" in str(v)]
    prioritise_list = [k.replace("avoid_","").replace("_"," ") for k,v in rules.items()
                       if "prioritise" in str(v)]

    # Confidence calibration instructions
    cal = intelligence.get("confidence_calibration", {})
    cal_str = ""
    for band, stats in cal.items():
        if stats["bets"] >= 3:
            diff = stats["stated_avg"] - stats["actual_rate"]
            if abs(diff) > 5:
                direction = "LOWER" if diff > 0 else "RAISE"
                cal_str += f"\n- {band}% confidence picks: {direction} by ~{abs(diff):.0f}% (stated {stats['stated_avg']}% but actual win rate {stats['actual_rate']}%)"

    weather_str = json.dumps(weather, indent=2) if weather else "No weather data available"

    prompt = f"""You are an advanced self-learning UK sports betting AI. Today is {today}.

━━━ FULL HISTORICAL INTELLIGENCE ━━━
{intel_str}

━━━ ACTIVE STRATEGY RULES (auto-evolved from your history) ━━━
{json.dumps(rules, indent=2)}

━━━ BET TYPES TO AVOID (losing streaks detected) ━━━
{', '.join(avoid_list) if avoid_list else 'None currently'}

━━━ BET TYPES TO PRIORITISE (strong performers) ━━━
{', '.join(prioritise_list) if prioritise_list else 'Use best judgment'}

━━━ CONFIDENCE CALIBRATION (MUST APPLY) ━━━
{cal_str if cal_str else 'No calibration data yet — use best judgment'}

━━━ WEATHER / GOING CONDITIONS ━━━
{weather_str}

━━━ HORSE RACING — search and find 4 picks ━━━
Search: "best horse racing tips today {today} value UK"
Search: "horse racing nap today {today} racing post each way"
Search: "horse racing tips {today} Cheltenham Newmarket Ascot Sandown"

For each horse find:
- Horse name, venue, race time
- Best decimal odds and which bookmaker
- Recent form (last 5 runs)
- Trainer and jockey
- Distance and going suitability
- Whether each way adds value

Apply weather/going data where relevant.
AVOID bet types flagged above. PRIORITISE bet types in strong form.

━━━ FOOTBALL — analyse live odds ━━━
{football_str}

Find 2 best football value bets. Look at ALL markets — h2h, BTTS, over/under, handicap.
Compare odds across all bookmakers listed to find genuine value.
DO NOT pick bet types flagged as avoid above.

━━━ TODAY'S DAY CONTEXT ━━━
Historical P&L on {day_name}s: {intelligence.get('by_day',{}).get(day_name,{}).get('pnl','N/A')}
Best odds band historically: {max(intelligence.get('by_odds_band',{}).items(), key=lambda x: x[1].get('roi',0), default=('unknown',''))[0] if intelligence.get('by_odds_band') else 'unknown'}

━━━ OUTPUT FORMAT ━━━
Respond ONLY in valid JSON, no markdown, no extra text:
{{
  "date": "{today}",
  "strategy_note": "1 sentence on how historical data shaped today's picks",
  "horse_racing": {{
    "best": [
      {{"event":"Horse - Venue HH:MM","bet":"Win/EW","odds":3.50,"best_bookie":"Paddy Power","confidence":50,"value":"High","bet_type":"win","analysis":"Form, going, trainer, why value."}}
    ],
    "medium": [
      {{"event":"Horse - Venue HH:MM","bet":"Win","odds":5.00,"best_bookie":"Bet365","confidence":40,"value":"High","bet_type":"win","analysis":"Reasoning."}},
      {{"event":"Horse - Venue HH:MM","bet":"Each Way","odds":8.00,"best_bookie":"William Hill","confidence":32,"value":"Medium","bet_type":"each_way","analysis":"Reasoning."}}
    ],
    "risky": [
      {{"event":"Horse - Venue HH:MM","bet":"Win","odds":12.00,"best_bookie":"Betfair","confidence":20,"value":"High","bet_type":"win","analysis":"Reasoning."}}
    ]
  }},
  "football": {{
    "best": [
      {{"event":"Team A vs Team B","bet":"Bet description","odds":1.90,"best_bookie":"Bet365","confidence":60,"value":"High","bet_type":"win","analysis":"Reasoning."}}
    ],
    "risky": [
      {{"event":"Team C vs Team D","bet":"Bet description","odds":3.80,"best_bookie":"Coral","confidence":34,"value":"High","bet_type":"btts","analysis":"Reasoning."}}
    ]
  }}
}}"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3500,
        tools=[{"type":"web_search_20250305","name":"web_search"}],
        messages=[{"role":"user","content":prompt}]
    )

    raw = ""
    for block in message.content:
        if hasattr(block,"text"):
            raw = block.text

    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"): part = part[4:].strip()
            if part.startswith("{"): raw = part; break

    start = raw.find("{"); end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    return json.loads(raw)

# ─────────────────────────────────────────────────────────────────────────────
# STAKES + SAVE
# ─────────────────────────────────────────────────────────────────────────────
def calculate_and_save(picks):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT SUM(profit_loss) FROM picks WHERE result!='pending'")
    row = c.fetchone()
    current_bankroll = BANKROLL + (row[0] or 0)
    conn.close()

    today    = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")
    now      = datetime.now().isoformat()

    def process_tier(tier_picks, tier, sport):
        for p in tier_picks:
            stake = kelly_stake(p["confidence"], p["odds"], current_bankroll, tier)
            p["stake"] = stake
            p["potential_return"] = round(stake * p["odds"], 2)

            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute("""
                INSERT INTO picks (date,sport,event,bet,odds,confidence,value,tier,
                                   stake,best_bookie,analysis,odds_band,day_of_week,bet_type,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (today, sport, p["event"], p["bet"], p["odds"], p["confidence"],
                  p["value"], tier, stake, p.get("best_bookie",""), p["analysis"],
                  get_odds_band(p["odds"]), day_name,
                  p.get("bet_type", extract_bet_type(p["bet"])), now))
            conn2.commit()
            conn2.close()

    hr = picks.get("horse_racing",{})
    for tier in ["best","medium","risky"]:
        process_tier(hr.get(tier,[]), tier, "horse_racing")

    fb = picks.get("football",{})
    for tier in ["best","medium","risky"]:
        process_tier(fb.get(tier,[]), tier, "football")

# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY DEEP REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_weekly_report():
    """Full weekly breakdown — only sent on Sundays"""
    if datetime.now().strftime("%A") != "Sunday":
        return None

    conn = get_db()
    c = conn.cursor()
    week_ago = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")

    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN result='won' THEN 1 ELSE 0 END),
               SUM(profit_loss), SUM(stake)
        FROM picks WHERE date>=? AND result!='pending'
    """, (week_ago,))
    row = c.fetchone()
    if not row or not row[0]:
        conn.close()
        return None

    total, wins, pnl, staked = row
    wins = wins or 0; pnl = round(pnl or 0,2); staked = round(staked or 0,2)

    # Best pick of the week
    c.execute("""
        SELECT event, bet, odds, profit_loss FROM picks
        WHERE date>=? AND result='won'
        ORDER BY profit_loss DESC LIMIT 1
    """, (week_ago,))
    best = c.fetchone()

    # Worst pick
    c.execute("""
        SELECT event, bet, odds, profit_loss FROM picks
        WHERE date>=? AND result='lost'
        ORDER BY profit_loss ASC LIMIT 1
    """, (week_ago,))
    worst = c.fetchone()

    # Sport breakdown
    c.execute("""
        SELECT sport, COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss)
        FROM picks WHERE date>=? AND result!='pending' GROUP BY sport
    """, (week_ago,))
    sport_breakdown = c.fetchall()

    conn.close()

    roi = round(pnl/staked*100,1) if staked else 0
    win_rate = round(wins/total*100,1) if total else 0

    report = f"""
📊 *LFT WEEKLY REPORT*
Week ending {datetime.now().strftime('%d %B %Y')}
━━━━━━━━━━━━━━━━━━━━

📈 *This Week's Summary*
Record: {wins}W / {total-wins}L
Win Rate: {win_rate}%
P&L: £{pnl}
ROI: {roi}%
Total Staked: £{staked}

🏆 *Best Pick*
{f"{best[0]} — {best[1]} @ {best[2]} | +£{round(best[3],2)}" if best else "N/A"}

❌ *Worst Pick*
{f"{worst[0]} — {worst[1]} @ {worst[2]} | £{round(worst[3],2)}" if worst else "N/A"}

⚽🐴 *Sport Breakdown*"""

    for sport, t, w, pl in sport_breakdown:
        w = w or 0
        report += f"\n{sport}: {w}W/{t-w}L | P&L: £{round(pl or 0,2)}"

    report += "\n\n// lf-trades · weekly analysis"
    return report

# ─────────────────────────────────────────────────────────────────────────────
# STATS SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def get_stats():
    conn = get_db()
    c = conn.cursor()

    week_ago  = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")

    c.execute("SELECT COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE date>=? AND result!='pending'", (week_ago,))
    w = c.fetchone()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), SUM(profit_loss), SUM(stake) FROM picks WHERE date>=? AND result!='pending'", (month_ago,))
    m = c.fetchone()
    conn.close()

    week = {"bets":w[0] or 0,"wins":w[1] or 0,"pnl":round(w[2] or 0,2)} if w else None
    month = {"bets":m[0] or 0,"wins":m[1] or 0,"pnl":round(m[2] or 0,2),
             "roi":round((m[2] or 0)/(m[3] or 1)*100,1) if m and m[3] else 0,
             "win_rate":round((m[1] or 0)/(m[0] or 1)*100,1) if m and m[0] else 0} if m else None
    return week, month

# ─────────────────────────────────────────────────────────────────────────────
# FORMATTING
# ─────────────────────────────────────────────────────────────────────────────
def format_telegram(picks, week, month, strategy_changes, losing_alerts, strategy_note=""):
    date = picks["date"]
    lines = ["🏆 *LFT DAILY BETTING PICKS*", f"📅 {date}", "━━━━━━━━━━━━━━━━━━━━"]

    # Stats
    if month and month["bets"] >= 5:
        pnl_emoji = "📈" if month["pnl"] >= 0 else "📉"
        lines.append(f"\n{pnl_emoji} *30-Day:* {month['wins']}W/{month['bets']-month['wins']}L | ROI: {month['roi']}% | P&L: £{month['pnl']}")
    if week and week["bets"] >= 2:
        lines.append(f"📊 *Week:* {week['wins']}W/{week['bets']-week['wins']}L | P&L: £{week['pnl']}")

    # Strategy alerts
    if losing_alerts:
        lines.append("\n⚠️ *STRATEGY ALERTS*")
        for alert in losing_alerts:
            lines.append(alert)

    if strategy_changes:
        lines.append("\n🔄 *AUTO-ADJUSTMENTS*")
        for change in strategy_changes:
            lines.append(change)

    if strategy_note:
        lines.append(f"\n💡 _{strategy_note}_")

    lines.append("━━━━━━━━━━━━━━━━━━━━")

    def add_section(title, emoji, data, pick_emoji):
        lines.append(f"\n{emoji} *{title}*")
        lines.append("──────────────────")
        for tier_key, tier_label in [("best","🟢 BEST"),("medium","🟡 MEDIUM"),("risky","🔴 RISKY")]:
            tier_picks = data.get(tier_key,[])
            if not tier_picks: continue
            lines.append(f"\n*{tier_label}*")
            for p in tier_picks:
                lines.append(f"\n{pick_emoji} *{p['event']}*")
                lines.append(f"📌 {p['bet']}")
                bookie = f" @ {p.get('best_bookie','')}" if p.get('best_bookie') else ""
                lines.append(f"💰 {p['odds']}{bookie} | 🎯 {p['confidence']}% | ⚡ {p['value']}")
                if p.get("stake",0) > 0:
                    lines.append(f"💵 £{p.get('stake',0)} → £{p.get('potential_return',0)}")
                lines.append(f"📊 _{p['analysis']}_")
                lines.append("──────────────────")

    add_section("Horse Racing","🐴",picks["horse_racing"],"🐴")
    add_section("Football","⚽",picks["football"],"⚽")
    lines.append("\n// lf-trades · daily picks")
    lines.append("⚠️ _Gamble responsibly. 18+ only._")
    return "\n".join(lines)

def format_email_html(picks, week, month, strategy_changes, losing_alerts, strategy_note=""):
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
    if losing_alerts or strategy_changes:
        all_alerts = losing_alerts + strategy_changes
        alert_items = "".join([f'<div style="padding:6px 0;border-bottom:1px solid #222;font-size:13px;color:#ccc;">{a}</div>' for a in all_alerts])
        alerts_html = f'<div style="background:#1a1a1a;border-radius:8px;padding:14px;margin-bottom:20px;"><div style="font-size:11px;color:#f0b429;font-weight:bold;margin-bottom:10px;">⚠️ STRATEGY UPDATES</div>{alert_items}</div>'

    strategy_html = ""
    if strategy_note:
        strategy_html = f'<div style="background:#111;border-left:3px solid #f0b429;padding:10px 14px;border-radius:0 6px 6px 0;margin-bottom:20px;font-size:13px;color:#aaa;font-style:italic;">💡 {strategy_note}</div>'

    def pick_card(p, colour, emoji):
        bookie = f'<span style="background:#2a2000;padding:2px 8px;border-radius:4px;font-size:11px;color:#f0b429;">📍 {p.get("best_bookie","")}</span>' if p.get("best_bookie") else ""
        stake_row = f'<div style="margin-top:8px;background:#111;border-radius:6px;padding:7px 12px;font-size:12px;color:#aaa;">💵 Stake: <b style="color:#fff">£{p.get("stake",0)}</b> → Return: <b style="color:#22c55e">£{p.get("potential_return",0)}</b></div>' if p.get("stake",0) > 0 else ""
        return f"""
        <div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:14px;margin-bottom:10px;">
            <div style="font-size:12px;color:#666;margin-bottom:4px;">{emoji} {p['event']}</div>
            <div style="font-size:15px;font-weight:bold;color:#f0b429;margin-bottom:8px;">📌 {p['bet']}</div>
            <div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap;align-items:center;">
                <span style="background:#222;padding:3px 8px;border-radius:4px;font-size:11px;color:#ccc;">💰 {p['odds']}</span>
                <span style="background:#222;padding:3px 8px;border-radius:4px;font-size:11px;color:#ccc;">🎯 {p['confidence']}%</span>
                <span style="background:#222;padding:3px 8px;border-radius:4px;font-size:11px;color:#ccc;">⚡ {p['value']}</span>
                {bookie}
            </div>
            {stake_row}
            <div style="font-size:13px;color:#aaa;line-height:1.6;margin-top:8px;">{p['analysis']}</div>
        </div>"""

    def section_html(title, emoji, data, pick_emoji):
        tc = {"best":"#22c55e","medium":"#f59e0b","risky":"#ef4444"}
        tl = {"best":"🟢 BEST","medium":"🟡 MEDIUM","risky":"🔴 RISKY"}
        html = f'<div style="font-size:17px;font-weight:bold;color:#fff;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #222;">{emoji} {title}</div>'
        for tk in ["best","medium","risky"]:
            tp = data.get(tk,[])
            if tp:
                html += f'<div style="font-size:10px;font-weight:bold;letter-spacing:0.1em;color:{tc[tk]};margin:12px 0 8px;">{tl[tk]}</div>'
                for p in tp:
                    html += pick_card(p, tc[tk], pick_emoji)
        return f'<div style="margin-bottom:28px;">{html}</div>'

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">
    <div style="text-align:center;margin-bottom:20px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:6px;">LOOKING FOR TRADES</div>
        <div style="font-size:24px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div>
        <div style="font-size:12px;color:#555;margin-top:4px;">{date}</div>
    </div>
    {stats_html}
    {alerts_html}
    {strategy_html}
    {section_html("Horse Racing","🐴",picks["horse_racing"],"🐴")}
    {section_html("Football","⚽",picks["football"],"⚽")}
    <div style="border-top:1px solid #1a1a1a;padding-top:14px;text-align:center;font-size:11px;color:#333;margin-top:8px;">
        // lf-trades · daily picks &nbsp;|&nbsp; Gamble responsibly. 18+ only.
    </div>
</div>
</body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
# SEND
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram(text):
    # Telegram has 4096 char limit — split if needed
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
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
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    print("Email sent")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] LFT Bot v3 starting...")
    init_db()

    # 1. Check ALL pending results
    print("Checking pending results...")
    check_all_pending_results()

    # 2. Evolve strategy based on full history
    print("Evolving strategy...")
    strategy_changes = evolve_strategy()
    for change in strategy_changes:
        print(change)

    # 3. Detect losing streaks
    losing_alerts = detect_losing_streaks()
    for alert in losing_alerts:
        print(alert)

    # 4. Build full intelligence
    print("Building intelligence from full history...")
    intelligence = build_intelligence()
    print(f"Settled bets in DB: {intelligence['overall']['total']}")

    # 5. Fetch live data
    print("Fetching football odds...")
    football_odds = fetch_football_odds()
    print(f"Football: {len(football_odds)} events")

    # 6. Weather for race venues (optional)
    weather = {}
    if WEATHER_API_KEY:
        weather = fetch_weather_for_venues(["Cheltenham","Newmarket","Sandown"])

    # 7. Analyse
    print("Analysing with Claude + web search...")
    picks = analyse_bets(football_odds, intelligence, strategy_changes, losing_alerts, weather)
    strategy_note = picks.get("strategy_note","")

    # 8. Stakes + save
    calculate_and_save(picks)

    # 9. Stats
    week, month = get_stats()

    # 10. Send daily picks
    tg_msg    = format_telegram(picks, week, month, strategy_changes, losing_alerts, strategy_note)
    email_html = format_email_html(picks, week, month, strategy_changes, losing_alerts, strategy_note)
    send_telegram(tg_msg)
    send_email(email_html, f"🏆 LFT Daily Picks — {picks['date']}")

    # 11. Weekly deep report (Sundays only)
    weekly_report = build_weekly_report()
    if weekly_report:
        send_telegram(weekly_report)
        print("Weekly report sent")

    print("Done! LFT Bot v3 complete.")

if __name__ == "__main__":
    main()
