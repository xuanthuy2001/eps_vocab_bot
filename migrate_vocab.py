#!/usr/bin/env python3
"""
migrate_vocab.py
Chạy 1 lần duy nhất để import vocab.json cũ → Google Sheets tab 'vocab'
Usage: python migrate_vocab.py
"""

import json
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def main():
    print("🔄 Bắt đầu migrate vocab.json → Google Sheets...")

    # Load vocab.json
    vocab_file = Path("vocab.json")
    if not vocab_file.exists():
        print("❌ Không tìm thấy vocab.json!")
        return

    with open(vocab_file, encoding="utf-8") as f:
        vocab = json.load(f)
    print(f"✅ Đọc được {len(vocab)} từ từ vocab.json")

    # Kết nối Sheets
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(config.SPREADSHEET_ID)
    sheet = spreadsheet.worksheet("vocab")

    # Xóa dữ liệu cũ và tạo header
    sheet.clear()
    sheet.append_row(["id", "korean", "vietnamese", "category"])
    print("✅ Đã tạo header: id | korean | vietnamese | category")

    # Ghi dữ liệu theo batch (nhanh hơn từng dòng)
    rows = []
    for i, item in enumerate(vocab, 1):
        rows.append([
            i,
            item.get("korean", ""),
            item.get("vietnamese", ""),
            item.get("category", ""),
        ])

    # Ghi theo batch 500 dòng
    batch_size = 500
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        sheet.append_rows(batch, value_input_option="RAW")
        print(f"  → Đã ghi {min(start + batch_size, len(rows))}/{len(vocab)} từ...")

    print(f"\n🎉 Migrate xong! {len(vocab)} từ đã được lưu vào Google Sheets.")
    print(f"📊 Xem tại: https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}")

if __name__ == "__main__":
    main()
