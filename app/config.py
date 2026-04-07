import os

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://model-runner.docker.internal/engines/llama.cpp/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "hf.co/unsloth/Qwen3.5-9B-GGUF:UD-Q5_K_XL")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "600"))
LLM_NUM_PREDICT = int(os.getenv("LLM_NUM_PREDICT", "8192"))
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))
GMAIL_LOOKBACK_HOURS = int(os.getenv("GMAIL_LOOKBACK_HOURS", "24"))
EMAIL_BODY_TRUNCATION = int(os.getenv("EMAIL_BODY_TRUNCATION", "3000"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
MIN_POLL_INTERVAL = int(os.getenv("MIN_POLL_INTERVAL", "30"))
HISTORY_MAX_LIMIT = int(os.getenv("HISTORY_MAX_LIMIT", "500"))
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "0") == "1"
