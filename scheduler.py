import time, subprocess, os

interval = int(os.getenv("INTERVAL_SECONDS", "600"))         # 10 minutes
duration = int(os.getenv("RUN_FOR_MINUTES", "60")) * 60      # run for 60 minutes

end = time.time() + duration
n = 0
while time.time() < end:
    n += 1
    # run your existing script once (the one that posts a price to Telegram)
 subprocess.run(["python", "runner.py"], check=False)
    # wait for the next tick, unless we’re out of time
    remaining = end - time.time()
    if remaining > interval:
        time.sleep(interval)
