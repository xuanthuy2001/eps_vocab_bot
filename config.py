import os

BOT_TOKEN            = os.environ.get("BOT_TOKEN", "")
CHAT_ID              = os.environ.get("CHAT_ID", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
SPREADSHEET_ID       = os.environ.get("SPREADSHEET_ID", "")
ADMIN_ID             = os.environ.get("ADMIN_ID", "")
GOOGLE_CREDENTIALS_B64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")  # ✅ Dùng cho Railway/Render
GEMINI_API_KEYS = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]