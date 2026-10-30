import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# Alpaca IEX feed captures roughly 2-3% of consolidated tape volume.
# This multiplier scales IEX bar volume to approximate the full-tape equivalent
# so that downstream thresholds (MIN_AVG_VOLUME, RVOL) remain calibrated.
# Empirically derived: AAPL IEX ~1.5M vs ~60M real = ~40x.
ALPACA_IEX_VOLUME_MULTIPLIER = int(os.getenv("ALPACA_IEX_VOLUME_MULTIPLIER", "40"))

MIN_PRICE = float(os.getenv("MIN_PRICE", "10.0"))
MIN_AVG_VOLUME = float(os.getenv("MIN_AVG_VOLUME", "1000000"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "2000000000"))
WATCHLIST_MIN_SCORE = float(os.getenv("WATCHLIST_MIN_SCORE", "4.0"))
BEAR_REGIME_MIN_SCORE = float(os.getenv("BEAR_REGIME_MIN_SCORE", "5.0"))
SHORT_INTEREST_MAX_PCT = float(os.getenv("SHORT_INTEREST_MAX_PCT", "10.0"))
DAYS_TO_COVER_MAX = float(os.getenv("DAYS_TO_COVER_MAX", "3.0"))
VOLUME_SURGE_RATIO = float(os.getenv("VOLUME_SURGE_RATIO", "2.0"))

# Layer 1 -- volume surge
RVOL_THRESHOLD = float(os.getenv("RVOL_THRESHOLD", "1.5"))
UP_DOWN_VOL_RATIO_MIN = float(os.getenv("UP_DOWN_VOL_RATIO_MIN", "1.5"))
OBV_LOOKBACK_DAYS = int(os.getenv("OBV_LOOKBACK_DAYS", "20"))
BLOCK_TRADE_MIN_SIZE = int(os.getenv("BLOCK_TRADE_MIN_SIZE", "10000"))
LAYER1_MIN_SIGNALS = int(os.getenv("LAYER1_MIN_SIGNALS", "2"))

# Layer 2 -- price structure & momentum
ADX_MIN = int(os.getenv("ADX_MIN", "20"))
ROC_PERIOD = int(os.getenv("ROC_PERIOD", "10"))
MA_SLOPE_LOOKBACK = int(os.getenv("MA_SLOPE_LOOKBACK", "5"))
GOLDEN_CROSS_LOOKBACK = int(os.getenv("GOLDEN_CROSS_LOOKBACK", "10"))
BREAKOUT_PCT_MIN = float(os.getenv("BREAKOUT_PCT_MIN", "0.08"))
BREAKOUT_VOLUME_RATIO = float(os.getenv("BREAKOUT_VOLUME_RATIO", "2.0"))
BREAKOUT_HOLD_SESSIONS = int(os.getenv("BREAKOUT_HOLD_SESSIONS", "2"))
DISTRIBUTION_DAY_MAX = int(os.getenv("DISTRIBUTION_DAY_MAX", "3"))
UPPER_RANGE_CLOSE_MIN = float(os.getenv("UPPER_RANGE_CLOSE_MIN", "0.7"))
MA_BONUS_PER_LEVEL = float(os.getenv("MA_BONUS_PER_LEVEL", "0.17"))
L2_MOMENTUM_A_SCORE = float(os.getenv("L2_MOMENTUM_A_SCORE", "0.25"))
L2_MOMENTUM_B_SCORE = float(os.getenv("L2_MOMENTUM_B_SCORE", "0.25"))

# Step 4 (L1 + L2) on the C++ engine (engine/, `pip install -e engine`).
# Off by default; scorer falls back to Python if the module is missing or fails.
USE_CPP_ENGINE = os.getenv("USE_CPP_ENGINE", "false").strip().lower() in ("1", "true", "yes", "on")

# Layer 3 -- options flow
# Signal A: call_vol/call_OI ratio threshold, tiered by avg daily option volume
# (call_vol + put_vol from front expiry chain)
L3_SIG_A_VOL_CUTOFF_HIGH  = int(os.getenv("L3_SIG_A_VOL_CUTOFF_HIGH",  "100000"))  # mega-cap cutoff
L3_SIG_A_VOL_CUTOFF_LOW   = int(os.getenv("L3_SIG_A_VOL_CUTOFF_LOW",   "10000"))   # large-cap cutoff
L3_SIG_A_RATIO_MEGA        = float(os.getenv("L3_SIG_A_RATIO_MEGA",  "3.0"))  # adov > 100k
L3_SIG_A_RATIO_LARGE       = float(os.getenv("L3_SIG_A_RATIO_LARGE", "1.5"))  # adov > 10k
L3_SIG_A_RATIO_MID         = float(os.getenv("L3_SIG_A_RATIO_MID",   "0.5"))  # adov <= 10k
LAYER3_MIN_SIGNALS = int(os.getenv("LAYER3_MIN_SIGNALS", "2"))

# Layer 4 -- short interest gate
FINRA_SHORT_DATA_DIR = os.getenv("FINRA_SHORT_DATA_DIR", "backend/data/finra_cache")
REVENUE_GROWTH_MIN = float(os.getenv("REVENUE_GROWTH_MIN", "0.05"))

# Layer 5 -- sector & macro alignment
RS_RANK_THRESHOLD = int(os.getenv("RS_RANK_THRESHOLD", "5"))
SECTOR_SLOPE_MIN = float(os.getenv("SECTOR_SLOPE_MIN", "0.0"))

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///backend/db/scanner.db")

# API
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-secret-change-me")

# Alerts -- email (Gmail SMTP)
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", SMTP_USER)
ALERT_EMAIL_TO   = os.getenv("ALERT_EMAIL_TO",   "")

# Alerts -- Slack
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
