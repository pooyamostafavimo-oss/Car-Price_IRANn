#!/usr/bin/env python3
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.iranjib.ir/showgroup/45/%D9%82%DB%8C%D9%85%D8%AA-%D8%AE%D9%88%D8%AF%D8%B1%D9%88-%D8%AA%D9%88%D9%84%DB%8C%D8%AF-%D8%AF%D8%A7%D8%AE%D9%84/"
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state.json")
CHUNK_SOFT_LIMIT = 3500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

def clean_text(value):
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()

def fetch_html():
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def parse_page_date(soup):
    # Use the site's own last-update line, so the Telegram message shows
    # the actual date/time of the data rather than the runner's clock.
    text = clean_text(soup.get_text(" ", strip=True))
    m = re.search(
        r"آخرین\s+به\s*روز\s*رسانی\s+در\s+تاریخ\s+(.+?)\s*،\s*([۰-۹0-9]{1,2}:\s*[۰-۹0-9]{2}:\s*[۰-۹0-9]{2})",
        text,
    )
    if m:
        return clean_text(m.group(1)), clean_text(m.group(2))
    m = re.search(
        r"آخرین\s+به\s*روز\s*رسانی\s+در\s+تاریخ\s+(.+?)(?:\s+آخرین|\s+جستجو|$)",
        text,
    )
    if m:
        return clean_text(m.group(1)), ""
    return "", ""

def parse_groups(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    groups = []

    for table in soup.find_all("table"):
        rows_html = table.find_all("tr")
        if not rows_html:
            continue

        header = None
        header_row_index = -1

        for idx, tr in enumerate(rows_html):
            cells = [clean_text(c.get_text(" ", strip=True))
                     for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            market_idx = next((i for i, x in enumerate(cells)
                               if "قیمت بازار" in x), None)
            factory_idx = next((i for i, x in enumerate(cells)
                                if "قیمت کارخانه" in x), None)
            name_idx = next((i for i, x in enumerate(cells)
                             if "نام خودرو" in x), 0)
            if market_idx is not None and factory_idx is not None:
                header = (name_idx, market_idx, factory_idx)
                header_row_index = idx
                break

        if header is None:
            continue

        name_idx, market_idx, factory_idx = header

        # Prefer the heading immediately associated with this table.
        title_tag = table.find_previous(["h2", "h3", "h4", "h1"])
        title = clean_text(title_tag.get_text(" ", strip=True)) if title_tag else "قیمت خودرو"

        rows = []
        seen = set()

        for tr in rows_html[header_row_index + 1:]:
            cells = [clean_text(c.get_text(" ", strip=True))
                     for c in tr.find_all(["td", "th"])]
            if len(cells) <= max(name_idx, market_idx, factory_idx):
                continue

            name = cells[name_idx]
            market = cells[market_idx]
            factory = cells[factory_idx]

            # Skip separator/header rows only. Keep "ناموجود", "---", "به زودی", etc.
            if not name or name in ("نام خودرو", "---"):
                continue
            if name == title:
                continue

            key = (name, market, factory)
            if key in seen:
                continue
            seen.add(key)
            rows.append((name, market or "-", factory or "-"))

        if rows:
            groups.append({"title": title, "rows": rows})

    return groups, parse_page_date(soup)

def build_messages(groups, page_date, page_time):
    if page_date:
        date_line = f"📅 تاریخ: {html.escape(page_date)}"
        if page_time:
            date_line += f" | 🕒 {html.escape(page_time)}"
    else:
        tehran_now = datetime.now(timezone(timedelta(hours=3, minutes=30)))
        date_line = f"📅 تاریخ: {tehran_now.strftime('%Y-%m-%d')} | 🕒 {tehran_now.strftime('%H:%M:%S')}"

    header = f"🚗 <b>قیمت روز خودرو</b>\n{date_line}\n"
    messages = []
    current = header

    for group in groups:
        title = html.escape(group["title"])
        group_header = f"\n<b>▫️ {title}</b>\n"

        # Start a new message if needed.
        if len(current) + len(group_header) > CHUNK_SOFT_LIMIT:
            messages.append(current)
            current = header

        current += group_header

        for name, market, factory in group["rows"]:
            block = (
                f"• {html.escape(name)}\n"
                f"  💰 بازار: <b>{html.escape(market)}</b> تومان\n"
                f"  🏭 کارخانه: <b>{html.escape(factory)}</b> تومان\n"
            )
            if len(current) + len(block) > CHUNK_SOFT_LIMIT and current.strip() != header.strip():
                messages.append(current)
                current = header + f"\n<b>▫️ {title}</b>\n"
            current += block

    if current.strip():
        messages.append(current)

    return messages

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def tg_api(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Telegram API پاسخ نامعتبر داد: HTTP {resp.status_code}")
    if not data.get("ok"):
        print(f"⚠️ خطا در {method}: {data}", file=sys.stderr)
    return data

def send_new_messages(token, chat_id, messages):
    """
    هر بار پیام‌های تازه ارسال می‌کند (بدون ادیت پیام قبلی و بدون پین کردن).
    """
    new_ids = []

    for i, text in enumerate(messages):
        result = tg_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if not result.get("ok"):
            raise RuntimeError(f"ارسال پیام {i + 1} با شکست مواجه شد: {result}")
        new_ids.append(result["result"]["message_id"])

    return new_ids

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID تنظیم نشده‌اند.", file=sys.stderr)
        sys.exit(1)

    print("در حال دریافت قیمت‌ها...")
    html_text = fetch_html()

    groups, (page_date, page_time) = parse_groups(html_text)
    if not groups:
        print("❌ هیچ جدول قیمت معتبری پیدا نشد.", file=sys.stderr)
        sys.exit(1)

    total_rows = sum(len(g["rows"]) for g in groups)
    print(f"✅ {len(groups)} گروه و {total_rows} خودرو پیدا شد.")
    if page_date:
        print(f"📅 تاریخ داده: {page_date} {page_time}")

    messages = build_messages(groups, page_date, page_time)
    print(f"📨 {len(messages)} پیام ارسال خواهد شد.")

    new_ids = send_new_messages(token, chat_id, messages)

    state = load_state()
    state["last_message_ids"] = new_ids
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["vehicle_count"] = total_rows
    save_state(state)

    print("✅ پیام جدید قیمت‌ها ارسال شد.")

if __name__ == "__main__":
    main()
