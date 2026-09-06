"""Settings, all env-driven with demo-safe defaults."""
import os

from dotenv import load_dotenv

load_dotenv()

# sqlite by default so `python demo.py` runs with zero setup; .env points at Postgres.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./datapipe.db")

# How often the scheduler re-evaluates every source.
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

# Volume check: alert when today's rows fall this far below the 7-day average.
VOLUME_DROP_THRESHOLD = float(os.getenv("VOLUME_DROP_THRESHOLD", "0.4"))
# Below this many rows/day the average is too noisy to call a drop.
VOLUME_MIN_BASELINE = int(os.getenv("VOLUME_MIN_BASELINE", "10"))

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
