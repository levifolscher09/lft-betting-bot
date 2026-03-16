import schedule
import time
from datetime import datetime
from bot import main

def job():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running LFT Bot v4...")
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

schedule.every().day.at("07:00").do(job)

print("LFT Bot v4 — Horse Racing | NFL | NBA | Golf")
print("Firing daily at 07:00 AM UK time\n")

# Uncomment to test immediately:
# job()

while True:
    schedule.run_pending()
    time.sleep(60)
