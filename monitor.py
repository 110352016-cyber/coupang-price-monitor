from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "history.json"
BASE_URL = "https://www.tw.coupang.com"
TW_TZ = ZoneInfo("Asia/Taipei")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

DROP_THRESHOLD = 50.0
CHECK_INTERVAL = 3600
INITIAL_WAIT_MS = 7000
SCROLL_WAIT_MS = 1500
MAX_SCROLL_ROUNDS = 8

SEARCH_TERMS = [
    "iPhone 17",
    "iPhone 17e",
    "iPhone Air",
    "iPhone 17 Pro",
    "iPhone 17 Pro Max",
    "iPad",
    "MacBook",
    "iMac",
    "Mac mini",
    "Mac Studio",
    "Mac Pro",
    "PS5 主機",
    "PS5 Pro 主機",
    "PS5 Slim 主機",
]

BLOCKLIST = (
    "保護殼", "保護套", "手機殼", "皮套", "保護貼", "玻璃貼", "鏡頭貼",
    "支架", "底座", "充電座", "充電器", "充電線", "傳輸線", "線材",
    "轉接器", "轉接頭", "變壓器", "電源供應器", "貼紙", "收納包",
    "鍵盤", "滑鼠", "觸控板", "apple pencil", "pencil", "airpods",
    "earpods", "homepod", "apple watch", "airtag", "magsafe",
    "dualsense", "手把", "控制器", "耳機", "耳麥", "ssd", "硬碟",
    "遊戲片", "遊戲光碟", "遊戲", "遙控器", "散熱器", "風扇",
    "hdmi", "搖桿帽", "按鍵帽", "光碟機", "維修", "租借",
    "適用 iphone", "適用於 iphone", "for iphone", "compatible", "副廠",
    "ibox", "carplay", "影音盒", "android", "安卓", "導航", "gps",
)


@dataclass(frozen=True)
class Product:
    key: str
    name: str
    price: int
    url: str
    category: str


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def log(message: str) -> None:
    print(f"[{now_tw():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def send_tg(message: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram Secret 未設定，跳過通知")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"Telegram 傳送失敗：{exc}")


def load_history() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(history: dict[str, dict[str, Any]]) -> None:
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_url(href: str | None) -> str:
    if not href:
        return ""
    absolute = urljoin(BASE_URL, href)
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_name(text: str) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def classify(name: str) -> str | None:
    n = re.sub(r"\s+", " ", name.casefold()).strip()

    if any(word in n for word in BLOCKLIST):
        return None

    apple_title = n.startswith("apple") or n.startswith("蘋果")

    iphone_match = re.search(r"\biphone\s*(\d{2})\b", n)
    if iphone_match:
        if not apple_title:
            return None
        return "iPhone" if int(iphone_match.group(1)) >= 17 else None

    if re.search(r"\biphone\s+air\b", n):
        return "iPhone" if apple_title else None

    if "ipad" in n:
        return "iPad" if apple_title else None

    if any(word in n for word in ("macbook", "imac", "mac mini", "mac studio", "mac pro")):
        return "Mac" if apple_title else None

    if "ps5" in n or "playstation 5" in n or "play station 5" in n:
        if any(word in n for word in ("主機", "console", "pro", "slim", "光碟版", "數位版", "標準版")):
            return "PS5"

    return None


def minimum_price(category: str) -> int:
    return {
        "iPhone": 8000,
        "iPad": 4500,
        "Mac": 9000,
        "PS5": 7000,
    }.get(category, 1)


def parse_price(category: str, text: str) -> int | None:
    values: list[int] = []

    for raw in re.findall(r"\$\s*([\d,]+)", text or ""):
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue

        if value >= minimum_price(category):
            values.append(value)

    if not values:
        return None

    values = values[:4]

    if len(values) >= 2 and values[1] <= values[0]:
        return values[1]

    return values[0]


def is_product_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return any(part in path for part in ("/vp/products/", "/products/", "/product/"))


def product_key(url: str, name: str) -> str:
    normalized_name = re.sub(r"\s+", " ", name.casefold()).strip()
    return f"{url}||{normalized_name}"


def scroll_page(page) -> None:
    last_height = -1
    last_links = -1
    stable = 0

    for round_no in range(1, MAX_SCROLL_ROUNDS + 1):
        links = page.locator("a").count()
        height = page.evaluate("document.body.scrollHeight")
        log(f"捲動 {round_no:02d}：連結 {links}｜高度 {height}")

        if links == last_links and height == last_height:
            stable += 1
        else:
            stable = 0

        if stable >= 2:
            break

        last_links = links
        last_height = height

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(SCROLL_WAIT_MS)


def collect(page) -> list[Product]:
    found: dict[str, Product] = {}
    links = page.locator("a")

    for index in range(links.count()):
        try:
            element = links.nth(index)
            url = normalize_url(element.get_attribute("href"))

            if not is_product_url(url):
                continue

            text = element.inner_text(timeout=1500).strip()
            name = clean_name(text)
            category = classify(name)

            if not name or category is None:
                continue

            price = parse_price(category, text)

            if price is None:
                continue

            key = product_key(url, name)
            product = Product(key, name, price, url, category)

            old = found.get(key)
            if old is None or product.price < old.price:
                found[key] = product

        except PlaywrightError:
            continue

    return list(found.values())


def crawl_once() -> list[Product]:
    all_products: dict[str, Product] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.new_page(
            viewport={"width": 1440, "height": 920},
            locale="zh-TW",
        )

        try:
            for term in SEARCH_TERMS:
                url = f"{BASE_URL}/np/search?q={quote_plus(term)}"
                log(f"搜尋：{term}")

                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=90000,
                    )
                except PlaywrightTimeoutError:
                    log("頁面載入逾時，繼續讀取已載入內容")

                page.wait_for_timeout(INITIAL_WAIT_MS)
                scroll_page(page)

                for product in collect(page):
                    old = all_products.get(product.key)

                    if old is None or product.price < old.price:
                        all_products[product.key] = product

            return list(all_products.values())

        finally:
            browser.close()


def crawl_with_retry() -> list[Product]:
    for attempt in range(1, 3):
        products = crawl_once()

        if products:
            return products

        log(f"第 {attempt} 次未抓到商品")

        if attempt < 2:
            time.sleep(15)

    return []


def run_once() -> None:
    now = now_tw()
    products = crawl_with_retry()

    if not products:
        send_tg(
            "\n".join(
                [
                    "⚠️ 酷澎監控失敗",
                    "",
                    f"時間：{now:%Y-%m-%d %H:%M}",
                    "原因：未抓到任何符合條件的商品",
                ]
            )
        )
        return

    history = load_history()
    alerts: list[str] = []
    new_items = 0

    for product in products:
        record = history.get(product.key)

        if record is None:
            history[product.key] = {
                "name": product.name,
                "url": product.url,
                "category": product.category,
                "base_price": product.price,
                "last_price": product.price,
                "alerted": False,
            }
            new_items += 1
            continue

        base_price = int(record.get("base_price") or product.price)
        drop = ((base_price - product.price) / base_price * 100) if base_price > 0 else 0.0
        alerted = bool(record.get("alerted", False))

        record.update(
            {
                "name": product.name,
                "url": product.url,
                "category": product.category,
                "last_price": product.price,
            }
        )

        if drop >= DROP_THRESHOLD and not alerted:
            alerts.append(
                "\n".join(
                    [
                        "🚨 酷澎 Price Error",
                        "",
                        f"類別：{product.category}",
                        f"商品：{product.name}",
                        f"基準價格：${base_price:,}",
                        f"目前價格：${product.price:,}",
                        f"跌幅：{drop:.1f}%",
                        "",
                        f"商品連結：{product.url}",
                    ]
                )
            )
            record["alerted"] = True

        elif drop < DROP_THRESHOLD:
            record["alerted"] = False

        history[product.key] = record

    save_history(history)

    for alert in alerts:
        send_tg(alert)

    counts = {
        category: sum(1 for p in products if p.category == category)
        for category in ("iPhone", "iPad", "Mac", "PS5")
    }

    next_check = now + timedelta(seconds=CHECK_INTERVAL)

    send_tg(
        "\n".join(
            [
                "🟢 酷澎 Price Error Monitor",
                "",
                f"檢查時間：{now:%Y-%m-%d %H:%M}",
                f"iPhone 17+：{counts['iPhone']} 件",
                f"iPad：{counts['iPad']} 件",
                f"Mac：{counts['Mac']} 件",
                f"PS5：{counts['PS5']} 件",
                f"總商品：{len(products)} 件",
                f"新建基準：{new_items} 件",
                f"異常：{len(alerts)} 件",
                f"下次檢查：{next_check:%H:%M}",
            ]
        )
    )


def main() -> None:
    if os.getenv("GITHUB_ACTIONS", "").casefold() == "true":
        run_once()
        return

    while True:
        run_once()

        try:
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("監控已停止")
            break


if __name__ == "__main__":
    main()
