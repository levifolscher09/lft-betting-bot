import schedule
import time
from datetime import datetime
from bot import main

def job():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running LFT Betting Bot...")
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")

# Schedule for 7:00 AM UK time every day
schedule.every().day.at("07:00").do(job)

print("LFT Betting Bot scheduler started — running daily at 07:00 AM")
print("Press Ctrl+C to stop\n")

# Uncomment the line below to test immediately on startup:
job()

while True:
    schedule.run_pending()
    time.sleep(60)
