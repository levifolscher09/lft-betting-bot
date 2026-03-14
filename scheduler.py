import schedule
import time
from datetime import datetime
from bot import main

def job():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running LFT Bot v3...")
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

schedule.every().day.at("07:00").do(job)

print("LFT Bot v3 — running daily at 07:00 AM UK time")
print("Features:")
print("  - Full historical intelligence across all data")
print("  - Auto-evolving strategy rules")
print("  - Losing streak detection + auto-adjustment")
print("  - Confidence calibration")
print("  - Kelly staking with risk adjustment")
print("  - Football results API + web search fallback")
print("  - Weather/going conditions for horse racing")
print("  - Weekly deep report every Sunday\n")

# Uncomment to test immediately:
# job()

while True:
    schedule.run_pending()
    time.sleep(60)
