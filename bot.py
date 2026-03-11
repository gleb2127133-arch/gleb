import asyncio
import logging
import json
import os
import re
import random
import hashlib
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

BOT_TOKEN = "8729431872:AAEMuCl2pEx8zd8_o1Twvvy4LeGB-oNMW7E"
CHAT_ID   = "423771186"

SEEN_FILE    = "seen_ads.json"
FILTERS_FILE = "filters.json"

DEFAULT_FILTERS = {
    "price_min":      0,
    "price_max":      5_000_000,
    "year_min":       2010,
    "year_max":       2025,
    "mileage_max":    200_000,
    "discount_min":   10,
    "regions":        ["moskva"],
    "brands":         [],
    "check_interval": 15,
    "urgent_only":    False,
    "score_min":      40,
}

AVITO_REGIONS = {
    "moskva":           "moskva",
    "spb":              "sankt-peterburg",
    "ekaterinburg":     "ekaterinburg",
    "novosibirsk":      "novosibirsk",
    "krasnodar":        "krasnodar",
    "kazan":            "kazan",
    "nizhniy_novgorod": "nizhniy_novgorod",
    "rostov":           "rostov-na-donu",
    "ufa":              "ufa",
    "samara":           "samara",
}

HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
        "Accept-Language": "ru-RU,ru;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    },
]

@dataclass
class CarAd:
    id:           str
    title:        str
    price:        int
    market_price: int
    year:         int
    mileage:      int
    region:       str
    url:          str
    source:       str
    posted_at:    str
    signals:      list = field(default_factory=list)
    score:        int  = 0

    @property
    def discount_pct(self):
        if self.market_price <= 0:
            return 0.0
        return round((self.market_price - self.price) / self.market_price * 100, 1)

    @property
    def discount_rub(self):
        return max(0, self.market_price - self.price)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def load_filters():
    if os.path.exists(FILTERS_FILE):
        with open(FILTERS_FILE) as f:
            return {**DEFAULT_FILTERS, **json.load(f)}
    return DEFAULT_FILTERS.copy()

def save_filters(filters):
    with open(FILTERS_FILE, "w") as f:
        json.dump(filters, f, ensure_ascii=False, indent=2)


class AvitoParser:
    BASE = "https://www.avito.ru"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=25, follow_redirects=True)

    async def _get(self, url):
        headers = random.choice(HEADERS_POOL)
        try:
            await asyncio.sleep(random.uniform(2.0, 4.0))
            r = await self.client.get(url, headers=headers)
            if r.status_code == 200:
                return r.text
            log.warning(f"Авито [{r.status_code}]")
        except Exception as e:
            log.error(f"Авито: {e}")
        return None

    def _num(self, text):
        nums = re.sub(r"[^\d]", "", text)
        return int(nums) if nums else 0

    def _year(self, text):
        m = re.search(r"\b(20[0-2]\d|199\d)\b", text)
        return int(m.group()) if m else 0

    def _is_night(self, posted):
        m = re.search(r"(\d{1,2}):(\d{2})", posted)
        return 0 <= int(m.group(1)) < 7 if m else False

    def _is_urgent(self, text):
        kw = ["срочно", "срочная", "уезжаю", "переезд", "вынужден", "нужны деньги", "торг уместен"]
        return any(k in text.lower() for k in kw)

    def _market_price(self, price, year, mileage):
        age = max(0, 2025 - (year or 2018))
        depreciation = max(0.40, 1.0 - age * 0.055)
        base = price / depreciation
        km = mileage or 80_000
        expected = age * 15_000
        if km < expected * 0.5:
            base *= 1.10
        elif km > expected * 1.8:
            base *= 0.93
        return int(base)

    def _score(self, ad):
        s = min(50, ad.discount_pct * 2.5)
        if "urgent"      in ad.signals: s += 15
        if "night"       in ad.signals: s += 10
        if "price_drop"  in ad.signals: s += 12
        if "low_mileage" in ad.signals: s += 8
        if ad.year >= 2018:             s += 5
        return min(100, int(s))

    async def fetch(self, region, filters):
        brand_part = ""
        if filters.get("brands"):
            brand_part = filters["brands"][0].lower() + "/"
        url = (
            f"{self.BASE}/{region}/avtomobili/{brand_part}"
            f"?s=104&pmin={filters.get('price_min',0)}&pmax={filters.get('price_max',9999999)}"
        )
        html = await self._get(url)
        if not html:
            return []

        soup  = BeautifulSoup(html, "html.parser")
        items = soup.select("[data-marker='item']")
        log.info(f"Авито [{region}]: {len(items)} карточек")

        ads = []
        for item in items[:40]:
            try:
                title_el = item.select_one("[itemprop='name']") or item.select_one("h3")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)

                link_el = item.select_one("a[data-marker='item-title']") or item.select_one("a[href*='/avtomobili/']")
                href    = link_el.get("href", "") if link_el else ""
                full_url = (self.BASE + href) if href.startswith("/") else href

                price_el  = item.select_one("[itemprop='price']") or item.select_one("[data-marker='item-price']")
                price_raw = (price_el.get("content") or price_el.get_text() if price_el else "0")
                price     = self._num(price_raw)
                if price <= 0:
                    continue

                params  = " ".join(el.get_text() for el in item.select("[data-marker='item-specific-params'] li"))
                year    = self._year(title + " " + params)
                mileage = self._num(params) * (1000 if "тыс" in params.lower() else 1)

                date_el = item.select_one("[data-marker='item-date']")
                posted  = date_el.get_text(strip=True) if date_el else ""

                desc_el = item.select_one("[data-marker='item-description']")
                desc    = desc_el.get_text(strip=True)[:300] if desc_el else ""

                if year    and year    < filters["year_min"]:    continue
                if year    and year    > filters["year_max"]:    continue
                if mileage and mileage > filters["mileage_max"]: continue
                if price < filters["price_min"]: continue
                if price > filters["price_max"]: continue

                market = self._market_price(price, year, mileage)
                disc   = (market - price) / market * 100 if market else 0
                if disc < filters["discount_min"]:
                    continue

                signals = []
                if self._is_urgent(title + " " + desc):  signals.append("urgent")
                if self._is_night(posted):                signals.append("night")
                if re.search(r"снизил|снижена", desc.lower()): signals.append("price_drop")
                if mileage and mileage < 80_000:          signals.append("low_mileage")

                if filters.get("urgent_only") and "urgent" not in signals:
                    continue

                ad = CarAd(
                    id=hashlib.md5(full_url.encode()).hexdigest()[:12],
                    title=title, price=price, market_price=market,
                    year=year, mileage=mileage, region=region,
                    url=full_url, source="avito", posted_at=posted, signals=signals,
                )
                ad.score = self._score(ad)
                if ad.score >= filters.get("score_min", 40):
                    ads.append(ad)
            except Exception as e:
                log.debug(f"Карточка: {e}")
        return ads

    async def close(self):
        await self.client.aclose()


SIGNAL_LABELS = {
    "urgent":      "🔴 Срочная продажа",
    "night":       "🌙 Выложено ночью",
    "price_drop":  "📉 Снижена цена",
    "low_mileage": "📍 Малый пробег",
}

def format_ad(ad):
    stars    = "★" * (ad.score // 20) + "☆" * (5 - ad.score // 20)
    sigs     = "\n".join(f"  {SIGNAL_LABELS.get(s,s)}" for s in ad.signals) or "  —"
    disc_rub = f"{ad.discount_rub:,}".replace(",", " ")
    price_f  = f"{ad.price:,}".replace(",", " ")
    mkt_f    = f"{ad.market_price:,}".replace(",", " ")
    return (
        f"🚗 {ad.title}\n\n"
        f"💰 {price_f} ₽  (~рынок: {mkt_f} ₽)\n"
        f"📉 Ниже рынка: {ad.discount_pct}% ({disc_rub} ₽)\n\n"
        f"📅 Год: {ad.year or '?'}  |  🛣 Пробег: {ad.mileage:,} км\n"
        f"📍 {ad.region}  |  📡 {ad.source.upper()}\n"
        f"🕐 {ad.posted_at}\n\n"
        f"⚡ Сигналы:\n{sigs}\n\n"
        f"⭐ Рейтинг: {stars} {ad.score}/100"
    )

def make_kb(ad):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Открыть объявление", url=ad.url)]])


class AutoHunterBot:
    def __init__(self):
        self.app     = Application.builder().token(BOT_TOKEN).build()
        self.avito   = AvitoParser()
        self.seen    = load_seen()
        self.filters = load_filters()
        self.running = False
        self._reg()

    def _reg(self):
        h = self.app.add_handler
        h(CommandHandler("start",   self.cmd_start))
        h(CommandHandler("status",  self.cmd_status))
        h(CommandHandler("filters", self.cmd_filters))
        h(CommandHandler("hunt",    self.cmd_hunt))
        h(CommandHandler("pause",   self.cmd_pause))
        h(CommandHandler("resume",  self.cmd_resume))
        h(CommandHandler("clear",   self.cmd_clear))
        h(CommandHandler("set",     self.cmd_set))
        h(CommandHandler("help",    self.cmd_help))

    async def cmd_start(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "🎯 AutoHunter запущен!\n\n"
            "Буду присылать выгодные авто с Авито.\n\n"
            "/hunt — найти прямо сейчас\n"
            "/filters — текущие фильтры\n"
            "/set параметр значение — изменить\n"
            "/pause / /resume — пауза\n"
            "/clear — сбросить историю\n"
            "/status — статус\n"
            "/help — справка"
        )
        self.running = True
        asyncio.create_task(self._loop(ctx))

    async def cmd_status(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        status = "✅ Работает" if self.running else "⏸ Пауза"
        await u.message.reply_text(
            f"Статус: {status}\n"
            f"Проверок: {ctx.bot_data.get('checks', 0)}\n"
            f"Отправлено: {ctx.bot_data.get('sent', 0)}\n"
            f"Интервал: {self.filters['check_interval']} мин\n"
            f"Регионы: {', '.join(self.filters['regions'])}"
        )

    async def cmd_filters(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        f = self.filters
        brands = ", ".join(f["brands"]) if f["brands"] else "все марки"
        await u.message.reply_text(
            f"⚙️ Фильтры:\n\n"
            f"💰 Цена: {f['price_min']:,} – {f['price_max']:,} ₽\n"
            f"📅 Год: {f['year_min']} – {f['year_max']}\n"
            f"🛣 Пробег до: {f['mileage_max']:,} км\n"
            f"📉 Скидка от: {f['discount_min']}%\n"
            f"⭐ Рейтинг от: {f['score_min']}/100\n"
            f"📍 Регионы: {', '.join(f['regions'])}\n"
            f"🚗 Марки: {brands}\n"
            f"🔴 Только срочные: {'да' if f['urgent_only'] else 'нет'}\n"
            f"⏱ Интервал: {f['check_interval']} мин\n\n"
            f"Изменить: /set параметр значение\n"
            f"Пример: /set price_max 2000000"
        )

    async def cmd_set(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if len(args) < 2:
            await u.message.reply_text(
                "Использование: /set параметр значение\n\n"
                "Параметры: price_min, price_max, year_min, year_max,\n"
                "mileage_max, discount_min, score_min,\n"
                "check_interval, urgent_only (true/false)\n\n"
                "Пример: /set discount_min 15"
            )
            return
        key, val = args[0], args[1]
        if key not in self.filters:
            await u.message.reply_text(f"Неизвестный параметр: {key}")
            return
        try:
            if key == "urgent_only":
                self.filters[key] = val.lower() in ("true", "1", "да")
            else:
                self.filters[key] = int(val)
            save_filters(self.filters)
            await u.message.reply_text(f"✅ {key} = {self.filters[key]}")
        except ValueError:
            await u.message.reply_text("Ошибка: введи число")

    async def cmd_hunt(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text("🔍 Запускаю поиск...")
        found = await self._scan(ctx)
        if found == 0:
            await u.message.reply_text(
                "😔 Новых объявлений не найдено.\n\n"
                "Попробуй:\n"
                "• /set discount_min 5\n"
                "• /set score_min 20"
            )

    async def cmd_pause(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.running = False
        await u.message.reply_text("⏸ Бот на паузе. /resume — возобновить.")

    async def cmd_resume(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.running:
            self.running = True
            asyncio.create_task(self._loop(ctx))
        await u.message.reply_text("▶️ Бот возобновлён!")

    async def cmd_clear(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.seen.clear()
        save_seen(self.seen)
        await u.message.reply_text("🗑 История сброшена.")

    async def cmd_help(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "📖 Справка AutoHunter\n\n"
            "Бот парсит Авито, ищет авто ниже рынка.\n\n"
            "Рейтинг сделки (0–100):\n"
            "  Скидка от рынка: до 50 очков\n"
            "  Срочная продажа: +15\n"
            "  Ночное объявление: +10\n"
            "  Снижена цена: +12\n"
            "  Пробег < 80к км: +8\n"
            "  Год от 2018: +5\n\n"
            "Советы:\n"
            "• discount_min 15-20% — реально выгодные\n"
            "• urgent_only true — только срочники\n"
            "• check_interval 5 — самые свежие"
        )

    async def _scan(self, ctx):
        all_ads = []
        for region in self.filters["regions"]:
            try:
                ads = await self.avito.fetch(region, self.filters)
                all_ads.extend(ads)
                log.info(f"[{region}]: {len(ads)} подходящих")
            except Exception as e:
                log.error(f"[{region}]: {e}")

        new_ads = [a for a in all_ads if a.id not in self.seen]
        new_ads.sort(key=lambda a: a.score, reverse=True)

        sent = 0
        for ad in new_ads[:10]:
            try:
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_ad(ad),
                    reply_markup=make_kb(ad),
                )
                self.seen.add(ad.id)
                sent += 1
                await asyncio.sleep(0.8)
            except Exception as e:
                log.error(f"Отправка: {e}")

        save_seen(self.seen)
        ctx.bot_data["checks"] = ctx.bot_data.get("checks", 0) + 1
        ctx.bot_data["sent"]   = ctx.bot_data.get("sent", 0) + sent
        log.info(f"Отправлено: {sent}")
        return sent

    async def _loop(self, ctx):
        while self.running:
            try:
                await self._scan(ctx)
            except Exception as e:
                log.error(f"Цикл: {e}")
            secs = self.filters.get("check_interval", 15) * 60
            await asyncio.sleep(secs)

    def run(self):
        log.info("AutoHunter стартует...")
        self.app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    AutoHunterBot().run()

