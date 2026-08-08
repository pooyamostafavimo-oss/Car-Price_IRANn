#!/usr/bin/env python3
"""
اسکریپت دریافت قیمت روز خودرو از سایت ایران جیب و ارسال/پین آن در یک کانال یا گروه تلگرام.

نحوه کار:
1. صفحه‌ی قیمت خودروهای داخلی ایران جیب را دانلود می‌کند.
2. هر جدول قیمت (هر شرکت خودروسازی) را با نزدیک‌ترین تیتر بالای آن جفت می‌کند.
3. متن پیام(های) تلگرام را می‌سازد (در صورت طولانی بودن، به چند پیام تقسیم می‌شود).
4. پیام(های) جدید را ارسال می‌کند، پیام اول را پین می‌کند و پیام(های) پین‌شده‌ی روز قبل را آن‌پین می‌کند.
5. شناسه‌ی پیام‌های ارسالی را در state.json ذخیره می‌کند تا در اجرای بعدی استفاده شود.

متغیرهای محیطی مورد نیاز:
    TELEGRAM_BOT_TOKEN   توکن ربات (از BotFather)
    TELEGRAM_CHAT_ID     شناسه‌ی چت/کانال/گروه مقصد (مثلاً -1001234567890 یا @mychannel)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.iranjib.ir/showgroup/45/%D9%82%DB%8C%D9%85%D8%AA-%D8%AE%D9%88%D8%AF%D8%B1%D9%88-%D8%AA%D9%88%D9%84%DB%8C%D8%AF-%D8%AF%D8%A7%D8%AE%D9%84/"
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state.json")
TELEGRAM_MAX_LEN = 4096
# کمی حاشیه‌ی امن نسبت به سقف واقعی تلگرام تا فرمت HTML اضافه هم جا شود
CHUNK_SOFT_LIMIT = 3500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_html() -> str:
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_groups(html: str):
    """
    خروجی: لیستی از دیکشنری‌ها به شکل:
        {"title": "ایران خودرو", "rows": [(نام خودرو, قیمت بازار, قیمت کارخانه), ...]}
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    groups = []

    for table in tables:
        # نزدیک‌ترین تیتر (h1..h4) قبل از جدول را به‌عنوان اسم شرکت در نظر می‌گیریم
        title_tag = table.find_previous(["h1", "h2", "h3", "h4"])
        title = title_tag.get_text(strip=True) if title_tag else "بدون‌عنوان"

        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            # ردیف‌های هدر یا خالی را رد کن
            if len(cells) < 2:
                continue
            name = cells[0]
            if not name or name in ("نام خودرو",):
                continue
            market_price = cells[1] if len(cells) > 1 else "-"
            factory_price = cells[2] if len(cells) > 2 else "-"
            rows.append((name, market_price, factory_price))

        if rows:
            groups.append({"title": title, "rows": rows})

    return groups


def build_messages(groups) -> list:
    """متن(های) نهایی HTML برای ارسال به تلگرام را می‌سازد و در صورت لزوم تقسیم می‌کند."""
    tehran_now = datetime.now(timezone(timedelta(hours=3, minutes=30)))
    date_str = tehran_now.strftime("%Y-%m-%d %H:%M")

    header = f"🚗 <b>قیمت روز خودرو</b>\n🕒 به‌روزرسانی: {date_str} (تهران)\nمنبع: ایران جیب\n"

    messages = []
    current = header + "\n"

    for group in groups:
        block_lines = [f"\n<b>▫️ {group['title']}</b>"]
        for name, market_price, factory_price in group["rows"]:
            block_lines.append(f"• {name}: <b>{market_price}</b> تومان")
        block = "\n".join(block_lines) + "\n"

        if len(current) + len(block) > CHUNK_SOFT_LIMIT:
            messages.append(current)
            current = block
        else:
            current += block

    if current.strip():
        messages.append(current)

    return messages


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        print(f"⚠️ خطا در متد {method}: {data}", file=sys.stderr)
    return data


def unpin_previous(token: str, chat_id: str, message_ids: list) -> None:
    for mid in message_ids:
        tg_api(token, "unpinChatMessage", {"chat_id": chat_id, "message_id": mid})


def send_and_pin(token: str, chat_id: str, messages: list) -> list:
    sent_ids = []
    for i, text in enumerate(messages):
        result = tg_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            sent_ids.append(msg_id)
            # فقط اولین پیام (که شامل تاریخ به‌روزرسانی است) را پین می‌کنیم
            if i == 0:
                tg_api(
                    token,
                    "pinChatMessage",
                    {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "disable_notification": False,
                    },
                )
        else:
            raise RuntimeError(f"ارسال پیام {i+1} با شکست مواجه شد: {result}")
    return sent_ids


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ متغیرهای TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID تنظیم نشده‌اند.", file=sys.stderr)
        sys.exit(1)

    print("در حال دریافت صفحه‌ی قیمت خودرو...")
    html = fetch_html()

    print("در حال استخراج جدول‌های قیمت...")
    groups = parse_groups(html)
    if not groups:
        print("❌ هیچ جدولی پیدا نشد. احتمالاً ساختار سایت تغییر کرده است.", file=sys.stderr)
        sys.exit(1)

    total_rows = sum(len(g["rows"]) for g in groups)
    print(f"✅ {len(groups)} گروه و {total_rows} ردیف قیمت پیدا شد.")

    messages = build_messages(groups)
    print(f"پیام در {len(messages)} بخش ارسال خواهد شد.")

    state = load_state()
    old_pinned_ids = state.get("last_message_ids", [])

    new_ids = send_and_pin(token, chat_id, messages)

    if old_pinned_ids:
        print("در حال آن‌پین کردن پیام‌های روز قبل...")
        unpin_previous(token, chat_id, old_pinned_ids)

    state["last_message_ids"] = new_ids
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print("✅ تمام شد.")


if __name__ == "__main__":
    main()
