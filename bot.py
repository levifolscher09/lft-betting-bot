import os
import requests
import smtplib
import json
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic
import anthropic as anthropic_lib

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "8580976907:AAGR4jmA0qrn6vw8RlSoszpP7uIxnYUuc98")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "7711190329")
ODDS_API_KEY      = os.getenv("ODDS_API_KEY", "08c2d60df2bf862873f6f3a32e93ad1c")
GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS", "LFT.Trades05@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS", "xutl ivtg whbf rree")
RECIPIENT_EMAIL   = os.getenv("RECIPIENT_EMAIL", "LFT.Trades05@gmail.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BANKROLL          = float(os.getenv("BANKROLL", "1000"))
SUPABASE_URL      = os.getenv("SUPABASE_URL", "https://vdpmcejtczdnsifpqahk.supabase.co")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZkcG1jZWp0Y3pkbnNpZnBxYWhrIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzA1NzYyMiwiZXhwIjoyMDg4NjMzNjIyfQ.W62ZYLiH-RYw5VbFWs4EeQ496WPpd4x0GquybtuT364")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SPORTS = {
    "horse_racing": {"emoji": "🐴", "label": "Horse Racing"},
    "nfl":          {"emoji": "🏈", "label": "NFL"},
    "nba":          {"emoji": "🏀", "label": "NBA"},
}

# ── Supabase DB Layer ──────────────────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table, params=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    r = requests.get(url, headers=SB_HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"SB GET error {table}: {r.status_code} {r.text[:100]}")
    return []

def sb_post(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=SB_HEADERS, json=data, timeout=15)
    if r.status_code in [200, 201]:
        return r.json()
    print(f"SB POST error {table}: {r.status_code} {r.text[:100]}")
    return None

def sb_patch(table, match_col, match_val, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match_col}=eq.{match_val}"
    r = requests.patch(url, headers=SB_HEADERS, json=data, timeout=15)
    return r.status_code in [200, 204]

def sb_count(table, params=""):
    headers = {**SB_HEADERS, "Prefer": "count=exact"}
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}&select=id"
    r = requests.get(url, headers=headers, timeout=15)
    count = r.headers.get("content-range","0/0").split("/")[-1]
    try: return int(count)
    except: return 0

def init_tables():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/lft_picks?limit=1", headers=SB_HEADERS, timeout=10)
    if r.status_code == 404 or (r.status_code == 200 and isinstance(r.json(), dict) and "code" in r.json()):
        print("WARNING: lft_picks table not found — please create it in Supabase dashboard")
        return False
    print("Supabase connection OK")
    return True

# ── Kelly + Helpers ────────────────────────────────────────────────────────────
def get_odds_band(odds):
    if odds < 2.0:  return "odds-on (<2.0)"
    if odds < 3.0:  return "short (2.0-3.0)"
    if odds < 5.0:  return "medium (3.0-5.0)"
    if odds < 10.0: return "big (5.0-10.0)"
    return "outsider (10.0+)"

def kelly_stake(confidence_pct, odds, bankroll, tier="medium"):
    rules = sb_get("lft_strategy_rules", "rule_key=eq.kelly_fraction&select=rule_value")
    fraction = float(rules[0]["rule_value"]) if rules else 0.25
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
    if "future" in bet_l or "draft" in bet_l: return "future"
    if "win" in bet_l: return "win"
    return "other"

# ── Claude API call with retry + backoff ──────────────────────────────────────
def claude_call_with_retry(messages, max_tokens=800, use_search=False, max_retries=5):
    """
    Wraps client.messages.create with exponential backoff on rate limit errors.
    Web search is OFF by default — it injects huge search results and blows the TPM limit.
    Only enable use_search=True for the results-checker which needs live data.
    """
    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model="claude-sonnet-4-5",
                max_tokens=max_tokens,
                messages=messages,
            )
            if use_search:
                kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
            return client.messages.create(**kwargs)
        except anthropic_lib.RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = 30 * (2 ** attempt)   # 30s, 60s, 120s, 240s
            print(f"  ⏳ Rate limit hit — waiting {wait}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(wait)
        except Exception as e:
            raise

def extract_text(message):
    raw = ""
    for block in message.content:
        if hasattr(block, "text"):
            raw = block.text
    return raw.strip()

# ── Strategy Evolution ─────────────────────────────────────────────────────────
def evolve_strategy():
    changes = []
    sports_types = sb_get("lft_picks", "result=neq.pending&select=sport,bet_type")
    combos = set((r["sport"], r["bet_type"]) for r in sports_types if r.get("bet_type"))

    for sport, bet_type in combos:
        recent = sb_get("lft_picks",
            f"sport=eq.{sport}&bet_type=eq.{bet_type}&result=neq.pending&select=result&order=date.desc&limit=8")
        if len(recent) < 5: continue
        losses = sum(1 for r in recent if r["result"] == "lost")
        if losses / len(recent) >= 0.7:
            key = f"avoid_{sport}_{bet_type}".replace(" ","_").lower()
            sb_post("lft_strategy_rules", {"rule_key":key,"rule_value":"deprioritise",
                "reason":f"{bet_type} ({sport}) losing {losses}/{len(recent)}","updated_at":datetime.now().isoformat()})
            changes.append(f"⚠️ Deprioritising {bet_type} ({sport})")

    picks_by_date = sb_get("lft_picks", "result=neq.pending&select=date,profit_loss&order=date.desc&limit=100")
    date_pnl = {}
    for p in picks_by_date:
        d = p["date"]
        date_pnl[d] = date_pnl.get(d, 0) + (p["profit_loss"] or 0)
    sorted_dates = sorted(date_pnl.keys(), reverse=True)[:7]
    losing_run = 0
    for d in sorted_dates:
        if date_pnl[d] < 0: losing_run += 1
        else: break

    if losing_run >= 3:
        sb_post("lft_strategy_rules", {"rule_key":"kelly_fraction","rule_value":"0.15",
            "reason":f"{losing_run} losing days","updated_at":datetime.now().isoformat()})
        changes.append(f"💰 Stakes reduced — {losing_run} losing days")

    return changes

# ── Results Check ──────────────────────────────────────────────────────────────
def check_pending_results():
    today = datetime.now().strftime("%Y-%m-%d")
    pending = sb_get("lft_picks", f"result=eq.pending&date=lt.{today}&select=id,date,event,bet,odds,stake,sport&limit=20")
    if not pending:
        print("No pending picks")
        return

    picks_list = [{"id":p["id"],"date":p["date"],"event":p["event"],"bet":p["bet"],"sport":p["sport"]} for p in pending]
    prompt = f"""Check results for these bets:
{json.dumps(picks_list, indent=1)}
Return ONLY JSON: {{"results":[{{"id":1,"result":"won"}},{{"id":2,"result":"lost"}},{{"id":3,"result":"unknown"}}]}}"""

    try:
        message = claude_call_with_retry(
            messages=[{"role":"user","content":prompt}],
            max_tokens=800
        )
        raw = extract_text(message)
        start = raw.find("{"); end = raw.rfind("}") + 1
        if start == -1: return
        data = json.loads(raw[start:end])
        for r in data.get("results",[]):
            if r["result"] == "unknown": continue
            pick = next((p for p in pending if p["id"] == r["id"]), None)
            if not pick: continue
            stake = pick.get("stake", 0) or 0
            odds  = pick.get("odds", 2) or 2
            pnl   = round(stake*(odds-1),2) if r["result"]=="won" else -stake
            sb_patch("lft_picks", "id", r["id"], {"result":r["result"],"profit_loss":pnl})
        print(f"Results checked for {len(data.get('results',[]))} picks")
    except Exception as e:
        print(f"Result check error: {e}")

# ── Historical Intelligence ────────────────────────────────────────────────────
def build_intelligence():
    all_picks = sb_get("lft_picks", "result=neq.pending&select=sport,bet_type,tier,odds_band,result,profit_loss,stake,day_of_week,confidence")
    if not all_picks:
        return "No historical data yet — use best judgment for all picks."

    total = len(all_picks)
    wins  = sum(1 for p in all_picks if p["result"]=="won")
    pnl   = sum(p.get("profit_loss",0) or 0 for p in all_picks)
    staked = sum(p.get("stake",0) or 0 for p in all_picks)

    sport_stats = {}
    for p in all_picks:
        sp = p["sport"]
        if sp not in sport_stats: sport_stats[sp] = {"t":0,"w":0,"pnl":0,"st":0}
        sport_stats[sp]["t"] += 1
        if p["result"]=="won": sport_stats[sp]["w"] += 1
        sport_stats[sp]["pnl"] += p.get("profit_loss",0) or 0
        sport_stats[sp]["st"]  += p.get("stake",0) or 0

    type_stats = {}
    for p in all_picks:
        bt = p.get("bet_type","")
        if not bt: continue
        if bt not in type_stats: type_stats[bt] = {"t":0,"w":0}
        type_stats[bt]["t"] += 1
        if p["result"]=="won": type_stats[bt]["w"] += 1

    rules = sb_get("lft_strategy_rules", "select=rule_key,rule_value")
    avoid = [r["rule_key"].replace("avoid_","") for r in rules if "deprioritise" in str(r.get("rule_value",""))]

    sport_summary = {}
    for sp, s in sport_stats.items():
        wr = round(s["w"]/s["t"]*100,1) if s["t"] else 0
        roi = round(s["pnl"]/s["st"]*100,1) if s["st"] else 0
        sport_summary[sp] = f"{wr}%WR {roi}%ROI"

    type_summary = {}
    for bt, s in type_stats.items():
        if s["t"] >= 3:
            wr = round(s["w"]/s["t"]*100,1)
            type_summary[bt] = f"{wr}%WR ({s['t']} bets)"

    win_rate = round(wins/total*100,1) if total else 0
    roi = round(pnl/staked*100,1) if staked else 0

    return f"""Settled: {total} bets | WR: {win_rate}% | P&L: £{round(pnl,2)} | ROI: {roi}%
By sport: {json.dumps(sport_summary)}
By bet type: {json.dumps(type_summary)}
Avoid: {', '.join(avoid) if avoid else 'none'}"""

# ── Historical Seeder ──────────────────────────────────────────────────────────
def seed_if_empty():
    count = sb_count("lft_picks", "result=neq.pending")
    if count >= 20:
        print(f"DB has {count} settled picks — skipping seed")
        return False

    print(f"DB has {count} picks — seeding history...")
    prompt = """Search for UK horse racing results from December 2024 to February 2025.
Find 20 real races with results.
Return ONLY a JSON array:
[{"date":"2024-12-15","sport":"horse_racing","event":"Horse Name - Venue","bet":"Win","odds":3.5,"confidence":45,"value":"High","tier":"medium","stake":20,"best_bookie":"Bet365","analysis":"Strong form","result":"won","profit_loss":50,"bet_type":"win","odds_band":"medium (3.0-5.0)"},
{"date":"2024-12-16","sport":"horse_racing","event":"Horse Name - Venue","bet":"Each Way","odds":7.0,"confidence":30,"value":"High","tier":"risky","stake":10,"best_bookie":"Paddy Power","analysis":"Each way value","result":"lost","profit_loss":-10,"bet_type":"each_way","odds_band":"big (5.0-10.0)"}]
Mix results: ~30% wins for win bets, ~40% for each way. Use real horse/venue names."""

    try:
        message = claude_call_with_retry(
            messages=[{"role":"user","content":prompt}],
            max_tokens=2500
        )
        raw = extract_text(message)
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip()
                if part.startswith("json"): part = part[4:].strip()
                if part.startswith("["): raw = part; break
        start = raw.find("["); end = raw.rfind("]") + 1
        if start == -1: return False
        picks = json.loads(raw[start:end])

        now = datetime.now().isoformat()
        for p in picks:
            try:
                date = p.get("date","2025-01-15")
                try: day_name = datetime.strptime(date,"%Y-%m-%d").strftime("%A")
                except: day_name = "Monday"
                sb_post("lft_picks", {
                    "date":date, "sport":p.get("sport","horse_racing"),
                    "event":p.get("event",""), "bet":p.get("bet",""),
                    "odds":float(p.get("odds",2.0)), "confidence":int(p.get("confidence",40)),
                    "value":p.get("value","Medium"), "tier":p.get("tier","medium"),
                    "stake":float(p.get("stake",20)), "best_bookie":p.get("best_bookie","Bet365"),
                    "analysis":p.get("analysis",""), "result":p.get("result","lost"),
                    "profit_loss":float(p.get("profit_loss",0)),
                    "odds_band":p.get("odds_band",get_odds_band(float(p.get("odds",2.0)))),
                    "day_of_week":day_name, "bet_type":p.get("bet_type","win"),
                    "created_at":now
                })
            except Exception as e:
                print(f"Seed insert error: {e}")

        print(f"Seeded {len(picks)} historical picks")
        return True
    except Exception as e:
        print(f"Seed error: {e}")
        return False

# ── Fetch Live Odds ────────────────────────────────────────────────────────────
def fetch_live_odds():
    sports_map = {"nba":"basketball_nba","nfl":"americanfootball_nfl"}
    odds_data = {}
    for label, sport_key in sports_map.items():
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={"apiKey":ODDS_API_KEY,"regions":"uk","markets":"h2h",
                        "oddsFormat":"decimal","dateFormat":"iso"},
                timeout=10
            )
            if r.status_code == 200:
                events = r.json()[:5]
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
                    compact.append({"match":f"{e.get('home_team','')} vs {e.get('away_team','')}","odds":best})
                odds_data[label] = compact
                print(f"{label}: {len(compact)} events")
            else:
                odds_data[label] = []
        except Exception as e:
            print(f"Odds error {label}: {e}")
            odds_data[label] = []
    return odds_data

# ── Per-Sport Prompts (NO web search — keeps tokens tiny) ────────────────────
SPORT_PROMPTS = {
    "horse_racing": lambda today, intel: f"""UK horse racing tipster. Today: {today}. History: {intel}
Pick 4 UK horse racing bets: 1 BEST, 2 MEDIUM, 1 RISKY. Use realistic UK venues/times.
Output ONLY valid JSON, no markdown:
{{"best":[{{"event":"Horse - Venue HH:MM","bet":"Win","odds":3.5,"best_bookie":"Paddy Power","confidence":52,"value":"High","bet_type":"win","analysis":"reason"}}],"medium":[{{"event":"Horse - Venue HH:MM","bet":"Win","odds":5.0,"best_bookie":"Bet365","confidence":40,"value":"High","bet_type":"win","analysis":"reason"}},{{"event":"Horse - Venue HH:MM","bet":"EW","odds":8.0,"best_bookie":"William Hill","confidence":33,"value":"Medium","bet_type":"each_way","analysis":"reason"}}],"risky":[{{"event":"Horse - Venue HH:MM","bet":"Win","odds":12.0,"best_bookie":"Betfair","confidence":20,"value":"High","bet_type":"win","analysis":"reason"}}]}}""",

    "nfl": lambda today, intel: f"""NFL betting analyst. Today: {today}. History: {intel}
Pick 4 NFL futures/draft value bets: 1 BEST, 2 MEDIUM, 1 RISKY.
Output ONLY valid JSON, no markdown:
{{"best":[{{"event":"NFL Future","bet":"Team X Super Bowl","odds":6.0,"best_bookie":"Bet365","confidence":25,"value":"High","bet_type":"future","analysis":"reason"}}],"medium":[{{"event":"NFL Draft","bet":"Player top 5","odds":2.5,"best_bookie":"Coral","confidence":45,"value":"High","bet_type":"future","analysis":"reason"}},{{"event":"NFL Season","bet":"Team over 9.5 wins","odds":1.9,"best_bookie":"William Hill","confidence":55,"value":"Medium","bet_type":"future","analysis":"reason"}}],"risky":[{{"event":"NFL Future","bet":"Team division","odds":4.0,"best_bookie":"Ladbrokes","confidence":30,"value":"High","bet_type":"future","analysis":"reason"}}]}}""",

    "nba": lambda today, intel, odds_str="[]": f"""NBA betting analyst. Today: {today}. History: {intel}
Live odds: {odds_str}
Pick 4 NBA bets: 1 BEST, 2 MEDIUM, 1 RISKY. Use live odds where available.
Output ONLY valid JSON, no markdown:
{{"best":[{{"event":"Team A vs Team B","bet":"Team A Win","odds":1.85,"best_bookie":"Bet365","confidence":58,"value":"High","bet_type":"win","analysis":"reason"}}],"medium":[{{"event":"Team C vs Team D","bet":"Team C -4.5","odds":1.9,"best_bookie":"William Hill","confidence":52,"value":"Medium","bet_type":"spread","analysis":"reason"}},{{"event":"Team E vs Team F","bet":"Over 224.5","odds":1.88,"best_bookie":"Betfair","confidence":54,"value":"Medium","bet_type":"over","analysis":"reason"}}],"risky":[{{"event":"Team G vs Team H","bet":"Team H upset","odds":3.5,"best_bookie":"Coral","confidence":32,"value":"High","bet_type":"win","analysis":"reason"}}]}}""",
}

# ── Main Analysis — one call per sport, no web search, 15s gaps ──────────────
def analyse_all_sports(odds_data, intelligence):
    today = datetime.now().strftime("%A %d %B %Y")
    results = {"date": today}
    nba_odds_str = json.dumps(odds_data.get("nba", [])[:3])

    sport_order = ["horse_racing", "nfl", "nba"]

    for i, sport in enumerate(sport_order):
        print(f"  Analysing {sport}...")

        if sport == "nba":
            prompt = SPORT_PROMPTS["nba"](today, intelligence, nba_odds_str)
        else:
            prompt = SPORT_PROMPTS[sport](today, intelligence)

        try:
            message = claude_call_with_retry(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                use_search=False   # NO web search — keeps input tokens tiny
            )
            raw = extract_text(message)

            # Strip markdown fences if present
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip()
                    if part.startswith("json"): part = part[4:].strip()
                    if part.startswith("{"): raw = part; break

            start = raw.find("{"); end = raw.rfind("}") + 1
            if start != -1 and end > start:
                results[sport] = json.loads(raw[start:end])
                picks_count = sum(len(results[sport].get(t, [])) for t in ["best","medium","risky"])
                print(f"  ✅ {sport}: {picks_count} picks")
            else:
                print(f"  ⚠️ {sport}: could not parse response")
                results[sport] = {"best":[],"medium":[],"risky":[]}

        except Exception as e:
            print(f"  ❌ {sport} error: {e}")
            results[sport] = {"best":[],"medium":[],"risky":[]}

        # 15s gap between calls to stay under TPM limit
        if i < len(sport_order) - 1:
            print(f"  ⏸ Waiting 15s before next sport...")
            time.sleep(15)

    return results

# ── Save Picks ─────────────────────────────────────────────────────────────────
def save_and_stake(picks):
    all_settled = sb_get("lft_picks","result=neq.pending&select=profit_loss")
    total_pnl = sum(p.get("profit_loss",0) or 0 for p in all_settled)
    current_bankroll = BANKROLL + total_pnl

    today    = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")
    now      = datetime.now().isoformat()

    for sport in SPORTS.keys():
        for tier in ["best","medium","risky"]:
            for p in picks.get(sport,{}).get(tier,[]):
                stake = kelly_stake(p["confidence"], p["odds"], current_bankroll, tier)
                p["stake"] = stake
                p["potential_return"] = round(stake * p["odds"], 2)
                bet_type = p.get("bet_type") or extract_bet_type(p["bet"])
                sb_post("lft_picks", {
                    "date":today, "sport":sport,
                    "event":p.get("event",""), "bet":p.get("bet",""),
                    "odds":float(p.get("odds",2)), "confidence":int(p.get("confidence",40)),
                    "value":p.get("value","Medium"), "tier":tier,
                    "stake":stake, "best_bookie":p.get("best_bookie",""),
                    "analysis":p.get("analysis",""), "result":"pending",
                    "profit_loss":0,
                    "odds_band":get_odds_band(float(p.get("odds",2))),
                    "day_of_week":day_name, "bet_type":bet_type, "created_at":now
                })

# ── Stats ──────────────────────────────────────────────────────────────────────
def get_stats():
    week_ago  = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")

    w_picks = sb_get("lft_picks", f"result=neq.pending&date=gte.{week_ago}&select=result,profit_loss,stake")
    m_picks = sb_get("lft_picks", f"result=neq.pending&date=gte.{month_ago}&select=result,profit_loss,stake,sport")

    def calc(picks):
        if not picks: return None
        t = len(picks); w = sum(1 for p in picks if p["result"]=="won")
        pnl = sum(p.get("profit_loss",0) or 0 for p in picks)
        st  = sum(p.get("stake",0) or 0 for p in picks)
        return {"bets":t,"wins":w,"pnl":round(pnl,2),
                "roi":round(pnl/st*100,1) if st else 0,
                "win_rate":round(w/t*100,1) if t else 0}

    week  = calc(w_picks)
    month = calc(m_picks)

    sport_month = {}
    for p in m_picks:
        sp = p["sport"]
        if sp not in sport_month: sport_month[sp] = {"bets":0,"wins":0,"pnl":0}
        sport_month[sp]["bets"] += 1
        if p["result"]=="won": sport_month[sp]["wins"] += 1
        sport_month[sp]["pnl"] += p.get("profit_loss",0) or 0

    return week, month, sport_month

# ── Format Telegram ────────────────────────────────────────────────────────────
def format_telegram(picks, week, month, sport_month, strategy_changes):
    date = picks["date"]
    lines = ["🏆 *LFT DAILY BETTING PICKS*", f"📅 {date}", "━━━━━━━━━━━━━━━━━━━━"]

    if month and month["bets"] >= 5:
        pc = "📈" if month["pnl"] >= 0 else "📉"
        lines.append(f"\n{pc} *30-Day:* {month['wins']}W/{month['bets']-month['wins']}L | ROI: {month['roi']}% | P&L: £{month['pnl']}")
    if week and week["bets"] >= 2:
        lines.append(f"📊 *Week:* {week['wins']}W/{week['bets']-week['wins']}L | P&L: £{week['pnl']}")
    if strategy_changes:
        lines.append("\n🔄 *AUTO-ADJUSTMENTS*")
        for ch in strategy_changes: lines.append(ch)
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    tier_labels = {"best":"🟢 BEST","medium":"🟡 MEDIUM","risky":"🔴 RISKY"}
    for sport_key, sport_info in SPORTS.items():
        sport_picks = picks.get(sport_key, {})
        if not any(sport_picks.get(t) for t in ["best","medium","risky"]): continue
        lines.append(f"\n{sport_info['emoji']} *{sport_info['label'].upper()}*")
        sm = sport_month.get(sport_key)
        if sm and sm["bets"] >= 3:
            wr = round(sm["wins"]/sm["bets"]*100,1)
            lines.append(f"_{wr}% WR this month | P&L: £{round(sm['pnl'],2)}_")
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
        stats_html = f"""<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px;">
<div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">30-DAY WIN RATE</div><div style="font-size:20px;font-weight:bold;color:#fff;">{month['win_rate']}%</div></div>
<div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">30-DAY ROI</div><div style="font-size:20px;font-weight:bold;color:{pc};">{month['roi']}%</div></div>
<div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">MONTHLY P&L</div><div style="font-size:20px;font-weight:bold;color:{pc};">£{month['pnl']}</div></div>
<div style="background:#1a1a1a;border-radius:8px;padding:12px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">WEEK P&L</div><div style="font-size:20px;font-weight:bold;color:{wc};">£{week['pnl'] if week else 'N/A'}</div></div>
</div>"""

    alerts_html = ""
    if strategy_changes:
        items = "".join([f'<div style="padding:5px 0;border-bottom:1px solid #222;font-size:12px;color:#ccc;">{a}</div>' for a in strategy_changes])
        alerts_html = f'<div style="background:#1a1a1a;border-radius:8px;padding:14px;margin-bottom:18px;"><div style="font-size:10px;color:#f0b429;font-weight:bold;margin-bottom:8px;">🔄 AUTO-ADJUSTMENTS</div>{items}</div>'

    def pick_card(p, colour, emoji):
        bookie = f'<span style="background:#2a2000;padding:2px 8px;border-radius:4px;font-size:11px;color:#f0b429;">📍 {p.get("best_bookie","")}</span>' if p.get("best_bookie") else ""
        stake_row = f'<div style="margin-top:6px;background:#111;border-radius:5px;padding:6px 10px;font-size:12px;color:#aaa;">💵 £{p.get("stake",0)} → <b style="color:#22c55e">£{p.get("potential_return",0)}</b></div>' if p.get("stake",0) > 0 else ""
        return f'<div style="background:#1a1a1a;border-left:4px solid {colour};border-radius:8px;padding:14px;margin-bottom:10px;"><div style="font-size:12px;color:#666;margin-bottom:3px;">{emoji} {p["event"]}</div><div style="font-size:14px;font-weight:bold;color:#f0b429;margin-bottom:7px;">📌 {p["bet"]}</div><div style="display:flex;gap:6px;margin-bottom:5px;flex-wrap:wrap;align-items:center;"><span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">💰 {p["odds"]}</span><span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">🎯 {p["confidence"]}%</span><span style="background:#222;padding:3px 7px;border-radius:4px;font-size:11px;color:#ccc;">⚡ {p["value"]}</span>{bookie}</div>{stake_row}<div style="font-size:12px;color:#aaa;line-height:1.6;margin-top:8px;">{p["analysis"]}</div></div>'

    def sport_section(sport_key, sport_info, sport_picks, sm):
        if not any(sport_picks.get(t) for t in ["best","medium","risky"]): return ""
        tc = {"best":"#22c55e","medium":"#f59e0b","risky":"#ef4444"}
        tl = {"best":"🟢 BEST","medium":"🟡 MEDIUM","risky":"🔴 RISKY"}
        stats_row = ""
        if sm and sm["bets"] >= 3:
            wr = round(sm["wins"]/sm["bets"]*100,1)
            pc = "#22c55e" if sm["pnl"] >= 0 else "#ef4444"
            stats_row = f'<div style="font-size:11px;color:#666;margin-bottom:12px;">{wr}% WR | P&L: <span style="color:{pc}">£{round(sm["pnl"],2)}</span></div>'
        html = f'<div style="font-size:16px;font-weight:bold;color:#fff;margin-bottom:6px;padding-bottom:8px;border-bottom:1px solid #222;">{sport_info["emoji"]} {sport_info["label"]}</div>{stats_row}'
        for tk in ["best","medium","risky"]:
            tp = sport_picks.get(tk,[])
            if tp:
                html += f'<div style="font-size:10px;font-weight:bold;letter-spacing:0.1em;color:{tc[tk]};margin:12px 0 7px;">{tl[tk]}</div>'
                for p in tp: html += pick_card(p, tc[tk], sport_info["emoji"])
        return f'<div style="margin-bottom:28px;">{html}</div>'

    sports_html = ""
    for sport_key, sport_info in SPORTS.items():
        sports_html += sport_section(sport_key, sport_info, picks.get(sport_key,{}), sport_month.get(sport_key))

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#111;font-family:'Segoe UI',sans-serif;">
<div style="max-width:620px;margin:0 auto;padding:24px;">
<div style="text-align:center;margin-bottom:20px;"><div style="font-size:11px;letter-spacing:0.2em;color:#f0b429;margin-bottom:5px;">LOOKING FOR TRADES</div><div style="font-size:24px;font-weight:bold;color:#fff;">🏆 Daily Betting Picks</div><div style="font-size:12px;color:#555;margin-top:4px;">{date}</div></div>
{stats_html}{alerts_html}{sports_html}
<div style="border-top:1px solid #1a1a1a;padding-top:12px;text-align:center;font-size:11px;color:#333;margin-top:6px;">// lf-trades · daily picks &nbsp;|&nbsp; Gamble responsibly. 18+ only.</div>
</div></body></html>"""

# ── Weekly Report ──────────────────────────────────────────────────────────────
def build_weekly_report():
    if datetime.now().strftime("%A") != "Sunday": return None
    week_ago = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    picks = sb_get("lft_picks", f"result=neq.pending&date=gte.{week_ago}&select=sport,result,profit_loss,stake,event,bet,odds")
    if not picks: return None
    total = len(picks); wins = sum(1 for p in picks if p["result"]=="won")
    pnl = sum(p.get("profit_loss",0) or 0 for p in picks)
    staked = sum(p.get("stake",0) or 0 for p in picks)
    wr = round(wins/total*100,1) if total else 0
    roi = round(pnl/staked*100,1) if staked else 0
    best = max((p for p in picks if p["result"]=="won"), key=lambda x: x.get("profit_loss",0), default=None)
    report = f"""📊 *LFT WEEKLY REPORT*
{datetime.now().strftime('%d %B %Y')}
━━━━━━━━━━━━━━━━━━━━
*{wins}W/{total-wins}L | {wr}% WR | ROI: {roi}% | P&L: £{round(pnl,2)}*"""
    if best: report += f"\n🏆 *Best:* {best['event']} @ {best['odds']} | +£{round(best['profit_loss'],2)}"
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
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as server:
        server.login(GMAIL_ADDRESS,GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS,RECIPIENT_EMAIL,msg.as_string())
    print("Email sent")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] LFT Bot v5 starting...")

    if not init_tables():
        print("ERROR: Cannot connect to Supabase — check credentials")
        return

    seed_if_empty()

    print("Checking pending results...")
    check_pending_results()

    print("Evolving strategy...")
    strategy_changes = evolve_strategy()

    print("Building intelligence...")
    intelligence = build_intelligence()

    print("Fetching live odds...")
    odds_data = fetch_live_odds()

    print("Analysing all sports (1 call per sport, 20s gaps)...")
    picks = analyse_all_sports(odds_data, intelligence)

    save_and_stake(picks)

    week, month, sport_month = get_stats()

    tg_msg = format_telegram(picks, week, month, sport_month, strategy_changes)
    email_html = format_email_html(picks, week, month, sport_month, strategy_changes)
    send_telegram(tg_msg)
    send_email(email_html, f"🏆 LFT Daily Picks — {picks['date']}")

    weekly = build_weekly_report()
    if weekly: send_telegram(weekly)

    print("Done! LFT Bot v5 complete.")

if __name__ == "__main__":
    main()
