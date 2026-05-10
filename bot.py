#!/usr/bin/env python3
"""
Telegram Vocabulary Bot - EPS Korean
- Gửi 10 từ vựng + 10 câu giao tiếp ngẫu nhiên mỗi ngày lúc 22:30 KST
- Gửi ảnh lên → Gemini Vision đọc → lưu vào Google Sheets
- Dữ liệu có thể thêm/sửa trực tiếp trên Google Sheets
"""
from dotenv import load_dotenv
load_dotenv()

import json
import random
import logging
import asyncio
import httpx
import base64
import os
import tempfile
from datetime import datetime, timezone, timedelta

from telegram import Bot, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from google.oauth2.service_account import Credentials

import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Google Sheets Setup ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheet_client():
    """
    Ưu tiên dùng GOOGLE_CREDENTIALS_B64 (biến môi trường cho Railway/Render).
    Fallback về credentials.json khi chạy local.
    """
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if creds_b64:
        creds_json = base64.b64decode(creds_b64).decode("utf-8")
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheets():
    client = get_sheet_client()
    spreadsheet = client.open_by_key(config.SPREADSHEET_ID)
    vocab_sheet   = spreadsheet.worksheet("vocab")
    phrases_sheet = spreadsheet.worksheet("phrases")
    return vocab_sheet, phrases_sheet

def load_vocab_from_sheets() -> list[dict]:
    """Tải từ vựng từ Sheet 'vocab'."""
    try:
        vocab_sheet, _ = get_worksheets()
        records = vocab_sheet.get_all_records()
        return [
            {"korean": r["korean"], "vietnamese": r["vietnamese"], "category": r.get("category", "")}
            for r in records if r.get("korean")
        ]
    except Exception as e:
        logger.error(f"Lỗi load vocab từ Sheets: {e}")
        return []

def load_phrases_from_sheets() -> list[dict]:
    """Tải câu giao tiếp từ Sheet 'phrases'."""
    try:
        _, phrases_sheet = get_worksheets()
        records = phrases_sheet.get_all_records()
        return [
            {"korean": r["korean"], "vietnamese": r["vietnamese"], "category": r.get("category", "")}
            for r in records if r.get("korean")
        ]
    except Exception as e:
        logger.error(f"Lỗi load phrases từ Sheets: {e}")
        return []

def append_phrases_to_sheet(phrases: list[dict]) -> int:
    """Thêm câu mới vào Sheet 'phrases'. Trả về số câu đã thêm."""
    try:
        _, phrases_sheet = get_worksheets()
        existing = phrases_sheet.get_all_records()
        next_id = max((r.get("id", 0) for r in existing), default=0) + 1

        rows = [
            [next_id + i, p["korean"], p["vietnamese"], p.get("category", "")]
            for i, p in enumerate(phrases)
        ]
        if rows:
            phrases_sheet.append_rows(rows, value_input_option="RAW")
        return len(rows)
    except Exception as e:
        logger.error(f"Lỗi ghi phrases vào Sheets: {e}")
        return 0

def init_sheet_headers():
    """Tạo header cho 2 sheet nếu chưa có."""
    try:
        vocab_sheet, phrases_sheet = get_worksheets()
        if not vocab_sheet.row_values(1):
            vocab_sheet.append_row(["id", "korean", "vietnamese", "category"])
            logger.info("Đã tạo header cho sheet 'vocab'")
        if not phrases_sheet.row_values(1):
            phrases_sheet.append_row(["id", "korean", "vietnamese", "category"])
            logger.info("Đã tạo header cho sheet 'phrases'")
    except Exception as e:
        logger.error(f"Lỗi init sheet headers: {e}")

# ── Gemini API ────────────────────────────────────────────────────────────────
GEMINI_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

async def gemini_post(payload: dict, timeout: int = 30, retries: int = 3) -> dict | None:
    keys = config.GEMINI_API_KEYS if config.GEMINI_API_KEYS else [config.GEMINI_API_KEY]

    for key in keys:
        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        GEMINI_API_BASE,
                        params={"key": key},
                        json=payload,
                    )
                    if resp.status_code == 429:
                        wait = 15 * attempt  # 15s, 30s, 45s
                        logger.warning(f"Key ...{key[-6:]} bị 429 — chờ {wait}s (lần {attempt}/{retries})")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:
                logger.error(f"Gemini error key ...{key[-6:]} lần {attempt}: {e}")
                if attempt < retries:
                    await asyncio.sleep(5)
        logger.warning(f"Key ...{key[-6:]} đã hết retry, chuyển key tiếp theo")

    logger.error("Tất cả Gemini API keys đều thất bại")
    return None


def _parse_gemini_json(data: dict) -> str:
    """Lấy text từ Gemini response và strip markdown fences."""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return (
            text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Lỗi parse Gemini response: {e} | data: {str(data)[:200]}")
        return ""

async def get_banmal(words: list[dict]) -> dict[str, str]:
    """Gọi Gemini để lấy dạng thân mật (반말) cho danh sách từ."""
    word_list = "\n".join(f"- {w['korean']} ({w['vietnamese']})" for w in words)
    prompt = (
        "Bạn là chuyên gia tiếng Hàn. Với mỗi từ dưới đây:\n"
        "- Nếu là động từ/tính từ (kết thúc bằng 다): cho dạng thân mật 반말 "
        "(ví dụ: 앉다→앉아, 가다→가, 예쁘다→예뻐, 먹다→먹어)\n"
        "- Nếu là danh từ hoặc cụm từ: trả về dấu gạch ngang '-'\n\n"
        f"{word_list}\n\n"
        "Trả về JSON thuần túy, không markdown, định dạng:\n"
        '{"từ_hàn_quốc": "dạng_반말", ...}'
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    data = await gemini_post(payload, timeout=20)
    if not data:
        return {}
    try:
        return json.loads(_parse_gemini_json(data))
    except Exception as e:
        logger.error(f"Lỗi parse banmal JSON: {e}")
        return {}
def detect_mime_type(image_bytes: bytes) -> str:
    """Detect mime type từ magic bytes của ảnh."""
    signatures = {
        b"\xff\xd8\xff":          "image/jpeg",
        b"\x89PNG\r\n\x1a\n":    "image/png",
        b"GIF87a":                "image/gif",
        b"GIF89a":                "image/gif",
        b"RIFF":                  "image/webp",
        b"\x00\x00\x00\x0cjP":   "image/jp2",
        b"BM":                    "image/bmp",
    }
    for magic, mime in signatures.items():
        if image_bytes[:len(magic)] == magic:
            if mime == "image/webp" and image_bytes[8:12] != b"WEBP":
                continue
            return mime
    return "image/jpeg"  # fallback

async def extract_phrases_from_image(image_bytes: bytes) -> list[dict]:
    """Gọi Gemini Vision để đọc câu Hàn-Việt từ ảnh."""
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = detect_mime_type(image_bytes)
    logger.info(f"Detected image mime type: {mime_type}")
    prompt = (
        "Đây là ảnh chụp màn hình TikTok/mạng xã hội chứa danh sách câu tiếng Hàn kèm nghĩa tiếng Việt.\n"
        "Chỉ đọc các dòng có dạng: [số]. [tiếng Hàn] → [tiếng Việt]\n"
        "Bỏ qua hoàn toàn: tiêu đề video, tên tác giả, caption, số like/comment/share, "
        "icon UI, nhạc nền, watermark, nút Bình luận/GIF.\n"
        "Nếu dòng cuối bị che khuất một phần vẫn cố đọc.\n"
        "Trả về JSON thuần túy, không markdown.\n"
        "Định dạng:\n"
        '[\n'
        '  {"korean": "câu tiếng Hàn", "vietnamese": "nghĩa tiếng Việt", "category": ""},\n'
        '  ...\n'
        ']\n'
        "Lưu ý:\n"
        "- Giữ nguyên tiếng Hàn và tiếng Việt chính xác như trong ảnh\n"
        "- Không thêm số thứ tự vào trường korean hoặc vietnamese\n"
        "- Chỉ trả về JSON, không giải thích thêm"
    )
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                {"text": prompt},
            ]
        }]
    }
    data = await gemini_post(payload, timeout=30)
    if not data:
        return []
    try:
        return json.loads(_parse_gemini_json(data))
    except Exception as e:
        logger.error(f"Lỗi parse image JSON: {e}")
        return []

# ── Message builder ───────────────────────────────────────────────────────────
def escape(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

def build_vocab_section(words: list[dict], banmal: dict[str, str]) -> str:
    lines = ["📚 *TỪ VỰNG HÔM NAY*\n"]
    for i, w in enumerate(words, 1):
        ban = banmal.get(w["korean"], "-")
        ban_str = f"  _\\({escape(ban)}\\)_" if ban and ban != "-" else ""
        lines.append(
            f"{i}\\. 🇰🇷 *{escape(w['korean'])}*{ban_str}  ➜  🇻🇳 {escape(w['vietnamese'])}"
        )
    return "\n".join(lines)

def build_phrases_section(phrases: list[dict]) -> str:
    lines = ["\n\n💬 *CÂU GIAO TIẾP HÔM NAY*\n"]
    prev_cat = ""
    counter = 1
    for p in phrases:
        cat = p.get("category", "")
        if cat and cat != prev_cat:
            lines.append(f"\n_📂 {escape(cat)}_")
            prev_cat = cat
        lines.append(
            f"{counter}\\. 🗣 *{escape(p['korean'])}*\n"
            f"    ➜ {escape(p['vietnamese'])}"
        )
        counter += 1
    return "\n".join(lines)

def build_daily_message(
    words: list[dict],
    banmal: dict[str, str],
    phrases: list[dict],
) -> str:
    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).strftime("%d/%m/%Y")

    header = (
        f"🌅 *Học tiếng Hàn mỗi ngày* — {escape(today)}\n"
        f"_Kiên trì mỗi ngày — giỏi lúc nào không hay\\!_\n"
    )
    vocab_part   = build_vocab_section(words, banmal)
    phrases_part = build_phrases_section(phrases) if phrases else ""
    footer = "\n\n💪 _Hwaiting\\! 화이팅\\!_ 🔥"

    return header + "\n" + vocab_part + phrases_part + footer

# ── Send daily message ────────────────────────────────────────────────────────
async def send_daily_vocab(bot: Bot):
    vocab_list   = load_vocab_from_sheets()
    phrases_list = load_phrases_from_sheets()

    if not vocab_list:
        logger.warning("Không có từ vựng trong Sheets!")
        return

    words   = random.sample(vocab_list, min(10, len(vocab_list)))
    phrases = random.sample(phrases_list, min(10, len(phrases_list))) if phrases_list else []
    banmal  = await get_banmal(words)
    message = build_daily_message(words, banmal, phrases)

    try:
        await bot.send_message(
            chat_id=config.CHAT_ID,
            text=message,
            parse_mode="MarkdownV2",
        )
        logger.info(f"Daily message sent to {config.CHAT_ID}")
    except Exception as e:
        logger.error(f"Failed to send daily message: {e}")

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận ảnh từ admin → Gemini đọc → lưu vào Sheets."""
    user_id = str(update.message.from_user.id)

    if user_id != str(config.ADMIN_ID):
        await update.message.reply_text("⛔ Bạn không có quyền thêm dữ liệu.")
        return

    await update.message.reply_text("⏳ Đang đọc ảnh bằng Gemini AI...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        phrases = await extract_phrases_from_image(bytes(image_bytes))

        if not phrases:
            await update.message.reply_text(
                "❌ Không đọc được dữ liệu từ ảnh. Hãy thử lại với ảnh rõ hơn."
            )
            return

        count = append_phrases_to_sheet(phrases)

        preview = "\n".join(
            f"• {p['korean']} → {p['vietnamese']}"
            for p in phrases[:5]
        )
        if len(phrases) > 5:
            preview += f"\n... và {len(phrases) - 5} câu khác"

        await update.message.reply_text(
            f"✅ Đã thêm <b>{count} câu</b> vào Google Sheets!\n\n"
            f"<b>Xem trước:</b>\n{preview}\n\n"
            f"📊 <a href='https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}'>Mở Google Sheets</a>",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"handle_photo error: {e}")
        await update.message.reply_text(f"❌ Lỗi xử lý ảnh: {e}")

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot từ vựng EPS tiếng Hàn.\n\n"
        "⏰ Mỗi ngày lúc *22:30 KST* mình gửi:\n"
        "  • 📚 10 từ vựng ngẫu nhiên\n"
        "  • 💬 10 câu giao tiếp thực tế\n\n"
        "📌 Lệnh khả dụng:\n"
        "/vocab — Nhận từ vựng + câu giao tiếp ngay\n"
        "/stats — Xem thống kê dữ liệu\n"
        "/sheet — Link Google Sheets\n\n"
        "📷 *Admin:* Gửi ảnh danh sách câu để bot tự động thêm vào database!",
        parse_mode="Markdown",
    )

async def cmd_vocab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Đang tải dữ liệu từ Google Sheets...")
    vocab_list   = load_vocab_from_sheets()
    phrases_list = load_phrases_from_sheets()

    words   = random.sample(vocab_list, min(10, len(vocab_list))) if vocab_list else []
    phrases = random.sample(phrases_list, min(10, len(phrases_list))) if phrases_list else []

    if not words:
        await update.message.reply_text("❌ Chưa có từ vựng trong database!")
        return

    banmal  = await get_banmal(words)
    message = build_daily_message(words, banmal, phrases)
    await update.message.reply_text(message, parse_mode="MarkdownV2")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vocab_list   = load_vocab_from_sheets()
    phrases_list = load_phrases_from_sheets()
    await update.message.reply_text(
        f"📊 *Thống kê EPS Vocab Bot*\n\n"
        f"• Từ vựng: *{len(vocab_list)}* từ\n"
        f"• Câu giao tiếp: *{len(phrases_list)}* câu\n"
        f"• Gửi mỗi ngày: *10 từ + 10 câu* ngẫu nhiên\n"
        f"• Giờ gửi: *22:30 KST* (20:30 VN)\n"
        f"• Database: Google Sheets ✅\n"
        f"• AI: Gemini 2\\.5 Flash ✨",
        parse_mode="Markdown",
    )

async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 *Google Sheets Database*\n\n"
        f"[👉 Mở Google Sheets](https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID})\n\n"
        f"• Tab `vocab` — từ vựng EPS\n"
        f"• Tab `phrases` — câu giao tiếp",
        parse_mode="Markdown",
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_sheet_headers()

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("vocab",  cmd_vocab))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("sheet",  cmd_sheet))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # ✅ FIX: Scheduler đúng 22:30 KST
    async def scheduled_job():
        await send_daily_vocab(app.bot)

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        scheduled_job,
        trigger="cron",
        hour=22,        # ✅ 22:30 KST (không phải 22:30)
        minute=30,
        id="daily_vocab",
    )
    scheduler.start()
    logger.info("Scheduler started — daily message at 22:30 KST")
    logger.info("Bot is running... Press Ctrl+C to stop.")

    app.run_polling(drop_pending_updates=True, poll_interval=1, timeout=1)

if __name__ == "__main__":
    main()
