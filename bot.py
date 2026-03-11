import asyncio
import logging
import json
import os
import re
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import httpx
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
    "discount_min":   5,
    "regions":        ["moskva"],
    "brands":         [],
    "check_interval": 15,
    "urgent_only":    False,
    "score_min":      20,
}

# Коды регионов для Авито RSS
REGION_CODES = {
    "moskva":           621540,
    "spb":              637640,
    "ekaterinburg":     631389,
    "novosibirsk":      659900,
    "krasnodar":        660765,
    "kazan":            648110,
    "nizhniy_novgorod": 653469,
    "rostov":           661270,
    "ufa":              664483,
    "samara":           662690,
}

REGION_NAMES = {
    "moskva":           "Москва",
    "spb":              "Санкт-Петербург",
    "ekaterinburg":     "Екатеринбург",
    "novosibirsk":      "Новосибирск",
    "krasnodar":        "Краснодар",
    "kazan":            "Казань",
    "nizhniy_novgorod": "Нижний Новгород",
    "rostov":           "Ростов-на-Дону",
    "ufa":              "Уфа",
    "samara":           "Самара",
}

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
    description:  str  = ""

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


class AvitoRSSParser:
    """Парсер через официальный RSS Авито — не блокируется"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; RSS reader)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            }
        )

    def _build_url(self, region_key: str, filters: dict) -> str:
        """Строим URL для RSS Авито"""
        region_id = REGION_CODES.get(region_key, 621540)

        # Базовый URL RSS для авто
        params = []
        params.append(f"locationId={region_id}")
        params.append("categoryId=9")  # категория Авто

        brand = filters.get("brands", [])
        if brand:
            params.append(f"q={brand[0]}")

        price_min = filters.get("price_min", 0)
        price_max = filters.get("price_max", 0)
        if price_min > 0:
            params.append(f"pmin={price_min}")
        if price_max > 0:
            params.append(f"pmax={price_max}")

        query_str = "&".join(params)
        return f"https://www.avito.ru/rss?{query_str}"

    def _parse_price(self, text: str) -> int:
        if not text:
            return 0
        nums = re.sub(r"[^\d]", "", text)
        return int(nums) if nums else 0

    def _parse_year(self, text: str) -> int:
        m = re.search(r"\b(20[0-2]\d|199\d)\b", text)
        return int(m.group()) if m else 0

    def _parse_mileage(self, text: str) -> int:
        # Ищем пробег в описании: "150 000 км" или "150тыс"
        m = re.search(r"(\d[\d\s]{1,6})\s*(?:тыс\.?\s*км|000\s*км|км)", text, re.IGNORECASE)
        if m:
            num = int(re.sub(r"\s", "", m.group(1)))
            if num < 1000:
                num *= 1000
            return num
        return 0

    def _is_urgent(self, text: str) -> bool:
        kw = ["срочно", "срочная", "уезжаю", "переезд", "вынужден",
              "нужны деньги", "торг уместен", "продам быстро"]
        t = text.lower()
        return any(k in t for k in kw)

    def _market_price(self, price: int, year: int, mileage: int) -> int:
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

    def _score(self, ad: CarAd) -> int:
        s = min(50, ad.discount_pct * 2.5)
        if "urgent"      in ad.signals: s += 15
        if "price_drop"  in ad.signals: s += 12
        if "low_mileage" in ad.signals: s += 8
        if ad.year >= 2018:             s += 5
        return min(100, int(s))

    async def fetch(self, region_key: str, filters: dict) -> list:
        url = self._build_url(region_key, filters)
        log.info(f"RSS запрос: {url}")

        try:
            await asyncio.sleep(1.0)
            r = await self.client.get(url)
            if r.status_code != 200:
                log.warning(f"RSS [{r.status_code}] для {region_key}")
                return []
        except Exception as e:
            log.error(f"RSS fetch: {e}")
            return []

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            log.error(f"XML parse: {e}")
            return []

        # Namespace для Авито RSS
        ns = {"avito": "http://www.avito.ru/rss"}

        items = root.findall(".//item")
        log.info(f"RSS [{region_key}]: {len(items)} объявлений")

        ads = []
        for item in items[:50]:
            try:
                title = item.findtext("title", "").strip()
                if not title:
                    continue

                link  = item.findtext("link", "").strip()
                desc  = item.findtext("description", "").strip()
                price_text = item.findtext("price") or item.findtext("{http://www.avito.ru/rss}price", "")

                # Цена
                price = self._parse_price(price_text or desc)
                if price <= 0:
                    # Попробуем найти цену в заголовке
                    price = self._parse_price(title)
                if price <= 0:
                    continue

                # Фильтр по цене
                if price < filters["price_min"]: continue
                if price > filters["price_max"]: continue

                # Год и пробег из заголовка/описания
                full_text = title + " " + desc
                year    = self._parse_year(full_text)
                mileage = self._parse_mileage(full_text)

                # Фильтры
                if year    and year    < filters["year_min"]:    continue
                if year    and year    > filters["year_max"]:    continue
                if mileage and mileage > filters["mileage_max"]: continue

                # Рыночная цена
                market = self._market_price(price, year or 2018, mileage or 80_000)
                disc   = (market - price) / market * 100 if market else 0

                if disc < filters["discount_min"]:
                    continue

                # Сигналы
                signals = []
                if self._is_urgent(full_text):
                    signals.append("urgent")
                if re.search(r"снизил|снижена|снижу", full_text.lower()):
                    signals.append("price_drop")
                if mileage and mileage < 80_000:
                    signals.append("low_mileage")

                if filters.get("urgent_only") and "urgent" not in signals:
                    continue

                region_name = REGION_NAMES.get(region_key, region_key)

                ad = CarAd(
                    id=hashlib.md5(link.encode()).hexdigest()[:12],
                    title=title,
                    price=price,
                    market_price=market,
                    year=year,
                    mileage=mileage,
                    region=region_name,
                    url=link,
                    source="avito",
                    posted_at="свежее",
                    signals=signals,
                    description=desc[:200],
                )
                ad.score = self._score(ad)

                if ad.score >= filters.get("score_min", 20):
                    ads.append(ad)

            except Exception as e:
                log.debug(f"RSS item: {e}")
                continue

        return ads

    async def close(self):
        await self.client.aclose()


SIGNAL_LABELS = {
    "urgent":      "🔴 Срочная продажа",
    "price_drop":  "📉 Снижена цена",
    "low_mileage": "📍 Малый пробег",
}

def format_ad(ad: CarAd) -> str:
    stars    = "★" * (ad.score // 20) + "☆" * (5 - ad.score // 20)
    sigs     = "\n".join(f"  {SIGNAL_LABELS.get(s, s)}" for s in ad.signals) or "  —"
    disc_rub = f"{ad.discount_rub:,}".replace(",", " ")
    price_f  = f"{ad.price:,}".replace(",", " ")
    mkt_f    = f"{ad.market_price:,}".replace(",", " ")
    mileage  = f"{ad.mileage:,} км".replace(",", " ") if ad.mileage else "не указан"

    return (
        f"🚗 {ad.title}\n\n"
        f"💰 {price_f} ₽  (~рынок: {mkt_f} ₽)\n"
        f"📉 Ниже рынка: {ad.discount_pct}% ({disc_rub} ₽)\n\n"
        f"📅 Год: {ad.year or '?'}  |  🛣 Пробег: {mileage}\n"
        f"📍 {ad.region}  |  📡 АВИТО\n\n"
        f"⚡ Сигналы:\n{sigs}\n\n"
        f"⭐ Рейтинг: {stars} {ad.score}/100"
    )

def make_kb(ad: CarAd) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Открыть объявление", url=ad.url)
    ]])


class AutoHunterBot:
    def __init__(self):
        self.app     = Application.builder().token(BOT_TOKEN).build()
        self.parser  = AvitoRSSParser()
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
        h(CommandHandler("region",  self.cmd_region))
        h(CommandHandler("brand",   self.cmd_brand))
        h(CommandHandler("help",    self.cmd_help))

    async def cmd_start(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "🎯 AutoHunter запущен!\n\n"
            "Ищу выгодные авто на Авито через RSS.\n\n"
            "/hunt — найти прямо сейчас\n"
            "/filters — текущие фильтры\n"
            "/set параметр значение — изменить\n"
            "/region add/remove регион\n"
            "/brand add/remove марка\n"
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
                "Параметры:\n"
                "price_min, price_max\n"
                "year_min, year_max\n"
                "mileage_max\n"
                "discount_min (% скидки от рынка)\n"
                "score_min (рейтинг 0-100)\n"
                "check_interval (минуты)\n"
                "urgent_only (true/false)\n\n"
                "Пример: /set discount_min 10"
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

    async def cmd_region(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if len(args) < 2:
            regions_list = "\n".join(f"  • {k}" for k in REGION_CODES)
            await u.message.reply_text(
                f"Использование:\n"
                f"/region add moskva\n"
                f"/region remove spb\n\n"
                f"Доступные:\n{regions_list}"
            )
            return
        action, name = args[0], args[1].lower()
        if action == "add":
            if name not in self.filters["regions"]:
                self.filters["regions"].append(name)
                save_filters(self.filters)
            await u.message.reply_text(f"✅ Добавлен: {name}\nСейчас: {', '.join(self.filters['regions'])}")
        elif action == "remove":
            self.filters["regions"] = [r for r in self.filters["regions"] if r != name]
            save_filters(self.filters)
            await u.message.reply_text(f"✅ Удалён: {name}\nСейчас: {', '.join(self.filters['regions'])}")

    async def cmd_brand(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if len(args) < 1:
            brands = ", ".join(self.filters["brands"]) or "все марки"
            await u.message.reply_text(
                f"Текущие марки: {brands}\n\n"
                f"/brand add Toyota\n"
                f"/brand remove Toyota\n"
                f"/brand clear — все марки"
            )
            return
        action = args[0]
        if action == "clear":
            self.filters["brands"] = []
        elif len(args) >= 2:
            brand = args[1].capitalize()
            if action == "add" and brand not in self.filters["brands"]:
                self.filters["brands"].append(brand)
            elif action == "remove":
                self.filters["brands"] = [b for b in self.filters["brands"] if b != brand]
        save_filters(self.filters)
        brands = ", ".join(self.filters["brands"]) or "все марки"
        await u.message.reply_text(f"✅ Марки: {brands}")

    async def cmd_hunt(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text("🔍 Запускаю поиск через RSS...")
        found = await self._scan(ctx)
        if found == 0:
            await u.message.reply_text(
                "😔 Новых объявлений не найдено.\n\n"
                "Попробуй расширить фильтры:\n"
                "/set discount_min 0\n"
                "/set score_min 0\n"
                "/set price_max 9999999"
            )

    async def cmd_pause(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.running = False
        await u.message.reply_text("⏸ Пауза. /resume — возобновить.")

    async def cmd_resume(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.running:
            self.running = True
            asyncio.create_task(self._loop(ctx))
        await u.message.reply_text("▶️ Возобновлён!")

    async def cmd_clear(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.seen.clear()
        save_seen(self.seen)
        await u.message.reply_text("🗑 История сброшена.")

    async def cmd_help(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "📖 Справка AutoHunter\n\n"
            "Бот парсит Авито через RSS и ищет авто ниже рынка.\n\n"
            "Рейтинг сделки (0–100):\n"
            "  Скидка от рынка: до 50 очков\n"
            "  Срочная продажа: +15\n"
            "  Снижена цена: +12\n"
            "  Пробег < 80к км: +8\n"
            "  Год от 2018: +5\n\n"
            "Советы перекупщику:\n"
            "• discount_min 10-15% = реально выгодные\n"
            "• urgent_only true = только срочники\n"
            "• check_interval 5 = самые свежие\n"
            "• Добавь несколько регионов для охвата"
        )

    async def _scan(self, ctx) -> int:
        all_ads = []
        for region in self.filters["regions"]:
            try:
                ads = await self.parser.fetch(region, self.filters)
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
                await asyncio.sleep(0.5)
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
        log.info("AutoHunter RSS стартует...")
        self.app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    AutoHunterBot().run()


