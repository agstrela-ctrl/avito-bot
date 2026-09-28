import html
import json
import os
import sys
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
PROXY_URL = os.environ.get("PROXY_URL", "")

SEARCHES_FILE = "searches.json"
DATA_FILE = "seen_items.json"
ALERT_FLAG_FILE = "blocked.flag"
MAX_SEEN_PER_SEARCH = 500


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    })
    # Авито отдаёт капчу "Доступ ограничен: проблема с IP" для зарубежных и
    # датацентровых IP (GitHub Actions, VPN), поэтому ходим через российский прокси
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return s


class Blocked(Exception):
    pass


def search_url(search):
    # s=104 — сортировка "по дате", чтобы новые объявления были на первой странице
    return f"https://www.avito.ru/{search.get('region', 'rossiya')}?q={quote_plus(search['query'])}&s=104"


def fetch_items(session, search):
    resp = session.get(search_url(search), timeout=20)
    if resp.status_code in (403, 429) or "firewall-container" in resp.text:
        raise Blocked(f"HTTP {resp.status_code}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    items = {}
    for card in soup.select('[data-marker="item"]'):
        item_id = card.get("data-item-id") or card.get("id", "").lstrip("i")
        title_el = card.select_one('[data-marker="item-title"]')
        if not (item_id and title_el):
            continue
        price_el = card.select_one('meta[itemprop="price"]')
        desc_el = card.select_one('meta[itemprop="description"]')
        date_el = card.select_one('[data-marker="item-date"]')
        href = title_el.get("href", "")
        items[item_id] = {
            "title": title_el.get("title") or title_el.get_text(strip=True),
            "price": int(price_el["content"]) if price_el and price_el.get("content", "").isdigit() else None,
            "description": desc_el.get("content", "") if desc_el else "",
            "date": date_el.get_text(strip=True) if date_el else "",
            "link": "https://www.avito.ru" + href.split("?")[0] if href.startswith("/") else href,
        }
    return items


def matches(search, item):
    text = f"{item['title']} {item['description']}".lower()
    include = [w.lower() for w in search.get("include", [])]
    exclude = [w.lower() for w in search.get("exclude", [])]
    if include and not any(w in text for w in include):
        return False
    if any(w in text for w in exclude):
        return False
    max_price = search.get("max_price")
    if max_price and item["price"] and item["price"] > max_price:
        return False
    return True


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram ответил {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"Ошибка Telegram: {e}")


def format_item(search, item):
    price = f"{item['price']:,} ₽".replace(",", " ") if item["price"] else "цена не указана"
    msg = f"🔔 <b>{html.escape(search['name'])}</b>\n\n<b>{html.escape(item['title'])}</b>\n💰 {price}"
    if item["date"]:
        msg += f" · {html.escape(item['date'])}"
    return msg + f"\n{item['link']}"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_seen(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def check_and_notify():
    searches = load_json(SEARCHES_FILE, {}).get("searches", [])
    seen = load_json(DATA_FILE, {})
    session = make_session()
    # Одно объявление может попасть в несколько поисков — уведомляем о нём один раз
    already_known = {item_id for ids in seen.values() for item_id in ids}

    for i, search in enumerate(searches):
        name = search["name"]
        if i:
            time.sleep(5)  # не долбим Авито запросами подряд
        try:
            items = fetch_items(session, search)
        except Exception as e:
            # Блокировка Авито или ошибка прокси — дальше пробовать бессмысленно
            print(f"[{name}] Не удалось получить объявления: {e}")
            if not os.path.exists(ALERT_FLAG_FILE):
                reason = ("Авито блокирует запросы — нужен другой российский IP в PROXY_URL."
                          if isinstance(e, Blocked) else
                          f"Ошибка подключения (скорее всего, прокси PROXY_URL):\n<code>{html.escape(str(e)[:300])}</code>")
                send_telegram(
                    f"⚠️ <b>Бот Авито не может получить объявления</b>\n{reason}\n"
                    "Бот будет пробовать дальше и напишет, когда заработает."
                )
                with open(ALERT_FLAG_FILE, "w") as f:
                    f.write("1")
            sys.exit(0)

        if os.path.exists(ALERT_FLAG_FILE):
            os.remove(ALERT_FLAG_FILE)
            send_telegram("✅ Авито снова отвечает, мониторинг работает.")

        print(f"[{name}] на странице объявлений: {len(items)}")
        if not items:
            continue

        first_run = name not in seen
        known = seen.get(name, [])
        new_ids = [k for k in items if k not in known]

        if first_run:
            # Первый запуск: запоминаем текущую выдачу, чтобы не завалить старыми объявлениями
            print(f"[{name}] первый запуск, запомнил {len(new_ids)} объявлений")
        else:
            hits = [items[k] for k in new_ids if k not in already_known and matches(search, items[k])]
            for item in reversed(hits):  # старые сначала, свежие последними
                send_telegram(format_item(search, item))
            print(f"[{name}] новых: {len(new_ids)}, подошло по фильтрам: {len(hits)}")

        already_known.update(new_ids)
        seen[name] = (new_ids + known)[:MAX_SEEN_PER_SEARCH]

    save_seen(seen)


if __name__ == "__main__":
    check_and_notify()
