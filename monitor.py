from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "history.json"
BASE_URL = "https://www.tw.coupang.com"
TAIWAN_TZ = ZoneInfo("Asia/Taipei")


def taiwan_now() -> datetime:
    return datetime.now(TAIWAN_TZ)

# ===== 只需要修改這裡 =====
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID", "")
CHECK_INTERVAL_SECONDS = 3600   # 每 1 小時檢查一次
DROP_THRESHOLD_PERCENT = 50.0   # 跌價 50% 才通知
DOUBLE_CHECK_SECONDS = 20       # 20 秒後再確認一次
HEADLESS = True                 # GitHub Actions 必須使用無頭模式
# ============================

CONFIG = {
    "telegram": {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
    "monitor": {
        "drop_threshold_percent": DROP_THRESHOLD_PERCENT,
        "check_interval_seconds": CHECK_INTERVAL_SECONDS,
        "double_check_seconds": DOUBLE_CHECK_SECONDS,
        "headless": HEADLESS,
        "send_status_every_run": True,
        "minimum_expected_products": 5,
        "failure_alert_after": 3,
    },
    "browser": {
        "initial_wait_ms": 7000,
        "scroll_wait_ms": 1800,
        "max_scroll_rounds": 24,
        "stable_scroll_rounds": 4,
    },
}

SEARCH_TERMS = [
    # iPhone 17 系列：分開搜尋，避免酷澎只回部分型號
    "iPhone 17",
    "iPhone 17e",
    "iPhone Air",
    "iPhone 17 Pro",
    "iPhone 17 Pro Max",
    "Apple iPhone 17 256GB",
    "Apple iPhone 17 512GB",
    "Apple iPhone 17 Pro 256GB",
    "Apple iPhone 17 Pro 512GB",
    "Apple iPhone 17 Pro Max 256GB",
    "Apple iPhone 17 Pro Max 512GB",
    "Apple iPhone 17 Pro Max 1TB",
    "Apple iPhone 17 Pro Max 2TB",

    # 其他保留類別
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

ACCESSORY_BLOCKLIST = (
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
    card_reference_price: int | None = None


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def log(message: str, *args) -> None:
    if args:
        message = message % args
    print(f"[{taiwan_now():%Y-%m-%d %H:%M:%S}] {message}")


def send_tg(config: dict[str, Any], message: str) -> bool:
    tg = config.get("telegram", {})
    if not tg.get("enabled", True):
        return False
    token = str(tg.get("bot_token", "")).strip()
    chat_id = str(tg.get("chat_id", "")).strip()
    if not token or token.startswith("請填入") or not chat_id or chat_id.startswith("請填入"):
        log("Telegram 尚未設定，跳過通知")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        log("Telegram 傳送失敗：%s", exc)
        return False


def normalize_url(href: str | None) -> str:
    if not href:
        return ""
    parts = urlsplit(urljoin(BASE_URL, href))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_name(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
    return next((line for line in lines if line), "")


def product_variant_key(url: str, name: str) -> str:
    """
    酷澎同一商品頁可能包含多個顏色／容量。
    使用「商品網址＋規格名稱」避免不同 SKU 被網址去重合併。
    """
    normalized_name = re.sub(r"\s+", " ", name.casefold()).strip()
    return f"{url}||{normalized_name}"


def classify_product(name: str) -> str | None:
    n = re.sub(r"\s+", " ", name.casefold()).strip()

    if any(word in n for word in ACCESSORY_BLOCKLIST):
        return None

    # 只接受看起來像原廠主機商品的標題開頭。
    apple_title = n.startswith("apple") or n.startswith("蘋果")
    sony_title = (
        n.startswith("sony")
        or n.startswith("playstation")
        or n.startswith("ps5")
        or "主機" in n
    )

    iphone = re.search(r"\biphone\s*(\d{2})\b", n)
    if iphone:
        if not apple_title:
            return None
        return "iPhone" if int(iphone.group(1)) >= 17 else None

    # 酷澎可能把 17 Air 寫成「iPhone Air」，仍視為 17 世代。
    if re.search(r"\biphone\s+air\b", n):
        if not apple_title:
            return None
        return "iPhone"

    # iPad 必須是 Apple/蘋果開頭，而且要有容量、網路版本或正式型號特徵。
    if re.search(r"\bipad\b", n):
        if not apple_title:
            return None

        ipad_model_signal = (
            re.search(r"\b(64|128|256|512)\s*gb\b", n)
            or re.search(r"\b1\s*tb\b", n)
            or any(
                token in n
                for token in (
                    "wi-fi",
                    "wifi",
                    "行動網路",
                    "5g",
                    "原廠保固",
                    "台灣公司貨",
                    "ipad air",
                    "ipad pro",
                    "ipad mini",
                    "第十代",
                    "第十一代",
                )
            )
        )

        return "iPad" if ipad_model_signal else None

    # Mac 必須是 Apple/蘋果開頭，並且看起來是完整電腦規格，而非配件。
    if any(word in n for word in ("macbook", "imac", "mac mini", "mac studio", "mac pro")):
        if not apple_title:
            return None

        mac_model_signal = (
            re.search(r"\b(8|16|18|24|32|36|48|64)\s*gb\b", n)
            or re.search(r"\b(256|512)\s*gb\b", n)
            or re.search(r"\b[1248]\s*tb\b", n)
            or any(
                token in n
                for token in (
                    "m1",
                    "m2",
                    "m3",
                    "m4",
                    "原廠保固",
                    "mac os",
                )
            )
        )

        return "Mac" if mac_model_signal else None

    if "ps5" in n or "playstation 5" in n or "play station 5" in n:
        if not sony_title:
            return None

        ps5_console_signal = any(
            token in n
            for token in (
                "主機",
                "console",
                "slim",
                "pro",
                "光碟版",
                "數位版",
                "標準版",
            )
        )

        return "PS5" if ps5_console_signal else None

    return None


def minimum_reasonable_price(category: str) -> int:
    return {"iPhone": 8000, "iPad": 4500, "Mac": 9000, "PS5": 7000}.get(category, 1)


def extract_price_candidates(text: str) -> list[int]:
    values: list[int] = []
    for raw in re.findall(r"\$\s*([\d,]+)", text or ""):
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return values


def choose_prices(category: str, text: str) -> tuple[int | None, int | None]:
    minimum = minimum_reasonable_price(category)
    candidates = [p for p in extract_price_candidates(text) if p >= minimum][:4]
    if not candidates:
        return None, None
    current = candidates[1] if len(candidates) >= 2 and candidates[1] <= candidates[0] else candidates[0]
    references = [p for p in candidates if p > current]
    reference = max(references) if references else None
    return current, reference


def is_product_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return bool(url) and any(x in path for x in ("/vp/products/", "/products/", "/product/"))


def auto_scroll(page, browser_cfg: dict[str, Any]) -> None:
    previous_height = previous_links = stable = 0
    max_rounds = int(browser_cfg.get("max_scroll_rounds", 24))
    stable_limit = int(browser_cfg.get("stable_scroll_rounds", 4))
    wait_ms = int(browser_cfg.get("scroll_wait_ms", 1800))
    for round_number in range(1, max_rounds + 1):
        links = page.locator("a").count()
        height = page.evaluate("document.body.scrollHeight")
        log("捲動 %02d：連結 %d｜高度 %d", round_number, links, height)
        stable = stable + 1 if (links, height) == (previous_links, previous_height) else 0
        if stable >= stable_limit:
            break
        previous_links, previous_height = links, height
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)


def collect_products_from_page(page) -> list[Product]:
    found: dict[str, Product] = {}
    links = page.locator("a")
    for index in range(links.count()):
        try:
            element = links.nth(index)
            url = normalize_url(element.get_attribute("href"))
            if not is_product_url(url):
                continue
            text = element.inner_text(timeout=1800).strip()
            name = clean_name(text)
            category = classify_product(name)
            if not name or category is None:
                continue
            price, reference = choose_prices(category, text)
            if price is None:
                continue
            variant_key = product_variant_key(url, name)
            product = Product(
                variant_key,
                name,
                price,
                url,
                category,
                reference,
            )
            old = found.get(variant_key)
            if old is None or product.price < old.price:
                found[variant_key] = product
        except PlaywrightError:
            continue
    return list(found.values())


def crawl(config: dict[str, Any]) -> tuple[list[Product], dict[str, int]]:
    monitor_cfg = config.get("monitor", {})
    browser_cfg = config.get("browser", {})
    products: dict[str, Product] = {}
    counts: dict[str, int] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=bool(monitor_cfg.get("headless", False)),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 920}, locale="zh-TW")
        try:
            for term in SEARCH_TERMS:
                url = f"{BASE_URL}/np/search?q={quote_plus(term)}"
                log("搜尋：%s", term)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=90000)
                except PlaywrightTimeoutError:
                    log("頁面載入超時，使用已載入內容：%s", term)
                page.wait_for_timeout(int(browser_cfg.get("initial_wait_ms", 7000)))
                auto_scroll(page, browser_cfg)
                page_products = collect_products_from_page(page)
                counts[term] = len(page_products)
                for product in page_products:
                    old = products.get(product.key)
                    if old is None or product.price < old.price:
                        products[product.key] = product
            return list(products.values()), counts
        finally:
            browser.close()


def normalize_history(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            base = int(value.get("base_price") or value.get("base") or value.get("normal_price") or value.get("lowest") or value.get("last_price"))
        except (TypeError, ValueError):
            continue
        result[str(key)] = {
            "name": str(value.get("name") or ""), "url": str(value.get("url") or key),
            "base_price": base, "last_price": int(value.get("last_price") or base),
            "alerted": bool(value.get("alerted") or value.get("notified") or False),
            "missing_checks": int(value.get("missing_checks") or 0),
            "category": str(value.get("category") or ""),
        }
    return result


def detect_candidates(products: list[Product], history: dict[str, dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for product in products:
        record = history.get(product.key)
        if record is None:
            ref = product.card_reference_price
            if ref and ref > 0:
                drop = (ref - product.price) / ref * 100
                if drop >= threshold:
                    candidates.append({"product": product, "base": ref, "drop": drop, "reason": "首次出現且商品卡顯示異常折扣"})
            continue
        base = int(record.get("base_price") or product.price)
        if base > 0:
            drop = (base - product.price) / base * 100
            if drop >= threshold and not bool(record.get("alerted")):
                candidates.append({"product": product, "base": base, "drop": drop, "reason": "相較歷史基準異常下跌"})
    return candidates


def confirm_candidates(config: dict[str, Any], candidates: list[dict[str, Any]]) -> set[str]:
    if not candidates:
        return set()
    seconds = int(config.get("monitor", {}).get("double_check_seconds", 20))
    log("發現 %d 個候選異常，%d 秒後進行二次確認", len(candidates), seconds)
    time.sleep(max(0, seconds))
    second_products, _ = crawl(config)
    second = {p.key: p for p in second_products}
    confirmed: set[str] = set()
    for item in candidates:
        product: Product = item["product"]
        again = second.get(product.key)
        if again and again.price <= product.price:
            confirmed.add(product.key)
        else:
            log("二次確認未通過：%s", product.name)
    return confirmed


def update_history(products: list[Product], history: dict[str, dict[str, Any]], confirmed: set[str], threshold: float) -> tuple[int, int, int]:
    new_items = reset_count = missing_now = 0
    seen = {p.key for p in products}
    for key, record in history.items():
        record["missing_checks"] = 0 if key in seen else int(record.get("missing_checks", 0)) + 1
        if record["missing_checks"] == 1:
            missing_now += 1

    for product in products:
        record = history.get(product.key)
        if record is None:
            base = product.card_reference_price if product.key in confirmed and product.card_reference_price else product.price
            history[product.key] = {
                "name": product.name, "url": product.url, "base_price": int(base),
                "last_price": product.price, "alerted": product.key in confirmed,
                "missing_checks": 0, "category": product.category,
            }
            new_items += 1
            continue

        base = int(record.get("base_price") or product.price)
        minimum = minimum_reasonable_price(product.category)
        if base < minimum <= product.price:
            base = product.price
            record["base_price"] = product.price
            record["alerted"] = False
            reset_count += 1
        drop = (base - product.price) / base * 100 if base > 0 else 0
        record.update({"name": product.name, "url": product.url, "last_price": product.price,
                       "missing_checks": 0, "category": product.category})
        if product.key in confirmed:
            record["alerted"] = True
        elif drop < threshold:
            record["alerted"] = False
        history[product.key] = record
    save_json(HISTORY_FILE, history)
    return new_items, missing_now, reset_count


def alert_message(item: dict[str, Any]) -> str:
    product: Product = item["product"]
    return "\n".join([
        "🔥 酷澎 Price Error", "", f"類別：{product.category}", f"商品：{product.name}",
        f"判斷：{item['reason']}", f"基準價格：${int(item['base']):,}",
        f"目前價格：${product.price:,}", f"跌幅：{float(item['drop']):.1f}%", "",
        "✅ 已於二次抓取確認", f"商品連結：{product.url}",
    ])


def run_once(config: dict[str, Any]) -> None:
    now = taiwan_now()
    monitor_cfg = config.get("monitor", {})
    threshold = float(monitor_cfg.get("drop_threshold_percent", 50.0))
    interval = int(monitor_cfg.get("check_interval_seconds", 3600))
    minimum_expected = int(monitor_cfg.get("minimum_expected_products", 10))

    log("開始檢查：%s", now.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        products, counts = crawl(config)
        if not products:
            raise RuntimeError("未抓到任何符合條件的商品")
    except Exception as exc:
        log("抓取失敗：%s", exc)
        send_tg(config, f"⚠️ 酷澎監控失敗\n\n時間：{now:%Y-%m-%d %H:%M}\n錯誤：{exc}")
        return

    history = normalize_history(load_json(HISTORY_FILE, {}))
    candidates = detect_candidates(products, history, threshold)
    confirmed = confirm_candidates(config, candidates)
    confirmed_items = [item for item in candidates if item["product"].key in confirmed]
    new_items, missing_items, reset_count = update_history(products, history, confirmed, threshold)

    for item in confirmed_items:
        send_tg(config, alert_message(item))

    if len(products) < minimum_expected:
        details = "｜".join(f"{term} {count}" for term, count in counts.items())
        send_tg(config, "\n".join(["⚠️ 酷澎商品數量偏低", "", f"檢查時間：{now:%Y-%m-%d %H:%M}",
                                          f"目前只抓到：{len(products)} 件", f"搜尋明細：{details}",
                                          "可能是網站載入不完整或版面變更。"]))

    category_counts = {c: sum(1 for p in products if p.category == c) for c in ("iPhone", "iPad", "Mac", "PS5")}
    if bool(monitor_cfg.get("send_status_every_run", True)):
        next_check = now + timedelta(seconds=interval)
        send_tg(config, "\n".join([
            "🟢 酷澎 Price Error Monitor", "", f"檢查時間：{now:%Y-%m-%d %H:%M}",
            f"iPhone 17+：{category_counts['iPhone']} 件", f"iPad：{category_counts['iPad']} 件",
            f"Mac：{category_counts['Mac']} 件", f"PS5：{category_counts['PS5']} 件",
            f"總商品：{len(products)} 件", f"新建基準：{new_items} 件", f"本次未出現：{missing_items} 件",
            f"修正錯誤基準：{reset_count} 件", f"已確認異常：{len(confirmed_items)} 件",
            f"下次檢查：{next_check:%H:%M}",
        ]))
    log("完成：商品 %d｜異常 %d", len(products), len(confirmed_items))


def main() -> None:
    config = CONFIG

    # GitHub Actions 每次排程只執行一次，完成後由下一次排程再啟動。
    if os.getenv("GITHUB_ACTIONS", "").casefold() == "true":
        run_once(config)
        return

    # 在自己的 Mac 上執行時，仍維持每小時循環。
    interval = int(config.get("monitor", {}).get("check_interval_seconds", 3600))

    while True:
        run_once(config)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("監控已停止")
            break


if __name__ == "__main__":
    main()
