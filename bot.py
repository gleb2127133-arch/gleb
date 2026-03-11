import asyncio
import logging
import json
import os
import re
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters as tg_filters

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

REGION_SLUGS = {
    "москва":           "moskva",
    "спб":              "sankt-peterburg",
    "екатеринбург":     "ekaterinburg",
    "новосибирск":      "novosibirsk",
    "краснодар":        "krasnodar",
    "казань":           "kazan",
    "нижний новгород":  "nizhniy_novgorod",
    "ростов":           "rostov-na-donu",
    "уфа":              "ufa",
    "самара":           "samara",
}

REGION_NAMES_RU = {
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

# Русские названия параметров -> внутренние ключи
PARAM_MAP = {
    "минимальная цена":    "price_min",
    "максимальная цена":   "price_max",
    "год от":              "year_min",
    "год до":              "year_max",
    "пробег до":           "mileage_max",
    "скидка от":           "discount_min",
    "рейтинг от":          "score_min",
    "интервал":            "check_interval",
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

def save_filters(f):
    with open(FILTERS_FILE, "w") as fp:
        json.dump(f, fp, ensure_ascii=False, indent=2)


class AvitoRSSParser:

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
        )

    def _build_url(self, region_key, filters):
        slug = region_key if region_key in REGION_NAMES_RU.values() else region_key
        # Найдём slug по внутреннему ключу
        slug_map = {v: k for k, v in {
            "moskva": "moskva", "spb": "sankt-peterburg",
            "ekaterinburg": "ekaterinburg", "novosibirsk": "novosibirsk",
            "krasnodar": "krasnodar", "kazan": "kazan",
            "nizhniy_novgorod": "nizhniy_novgorod", "rostov": "rostov-na-donu",
            "ufa": "ufa", "samara": "samara",
        }.items()}
        avito_slug = {
            "moskva": "moskva", "spb": "sankt-peterburg",
            "ekaterinburg": "ekaterinburg", "novosibirsk": "novosibirsk",
            "krasnodar": "krasnodar", "kazan": "kazan",
            "nizhniy_novgorod": "nizhniy_novgorod", "rostov": "rostov-na-donu",
            "ufa": "ufa", "samara": "samara",
        }.get(region_key, "moskva")

        params = ["output=rss", "s=104"]
        brands = filters.get("brands", [])
        if brands:
            params.append(f"q={brands[0]}")
        pmin = filters.get("price_min", 0)
        pmax = filters.get("price_max", 0)
        if pmin > 0:
            params.append(f"pmin={pmin}")
        if 0 < pmax < 9_999_999:
            params.append(f"pmax={pmax}")

        return f"https://www.avito.ru/{avito_slug}/avtomobili?{'&'.join(params)}"

    def _num(self, text):
        nums = re.sub(r"[^\d]", "", str(text))
        return int(nums) if nums else 0

    def _year(self, text):
        m = re.search(r"\b(20[0-2]\d|199\d)\b", text)
        return int(m.group()) if m else 0

    def _mileage(self, text):
        m = re.search(r"(\d[\d\s]{1,6})\s*(?:тыс\.?\s*км|000\s*км)", text, re.IGNORECASE)
        if m:
            n = int(re.sub(r"\s", "", m.group(1)))
            return n * 1000 if n < 1000 else n
        return 0

    def _is_urgent(self, text):
        kw = ["срочно", "срочная", "уезжаю", "переезд", "вынужден", "нужны деньги", "торг уместен"]
        return any(k in text.lower() for k in kw)

    def _market_price(self, price, year, mileage):
        age = max(0, 2025 - (year or 2018))
        dep = max(0.40, 1.0 - age * 0.055)
        base = price / dep
        km = mileage or 80_000
        exp = age * 15_000
        if km < exp * 0.5:   base *= 1.10
        elif km > exp * 1.8: base *= 0.93
        return int(base)

    def _score(self, ad):
        s = min(50, ad.discount_pct * 2.5)
        if "urgent"      in ad.signals: s += 15
        if "price_drop"  in ad.signals: s += 12
        if "low_mileage" in ad.signals: s += 8
        if ad.year >= 2018:             s += 5
        return min(100, int(s))

    async def fetch(self, region_key, filters):
        url = self._build_url(region_key, filters)
        log.info(f"RSS: {url}")
        try:
            await asyncio.sleep(1.5)
            r = await self.client.get(url)
            log.info(f"RSS статус: {r.status_code}")
            if r.status_code != 200:
                return []
        except Exception as e:
            log.error(f"RSS: {e}")
            return []

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            log.error(f"XML: {e}")
            return []

        items = root.findall(".//item")
        log.info(f"[{region_key}]: {len(items)} объявлений в RSS")

        ads = []
        for item in items[:50]:
            try:
                title = item.findtext("title", "").strip()
                if not title: continue
                link  = item.findtext("link", "").strip()
                desc  = item.findtext("description", "").strip()

                price = 0
                for tag in ["price", "{http://www.avito.ru/rss}price"]:
                    v = item.findtext(tag, "")
                    if v:
                        price = self._num(v)
                        break
                if price == 0:
                    m = re.search(r"\d[\d\s]{3,}", title + " " + desc)
                    if m: price = self._num(m.group())
                if price <= 0: continue
                if price < filters["price_min"]: continue
                if price > filters["price_max"]: continue

                full    = title + " " + desc
                year    = self._year(full)
                mileage = self._mileage(full)

                if year    and year    < filters["year_min"]:    continue
                if year    and year    > filters["year_max"]:    continue
                if mileage and mileage > filters["mileage_max"]: continue

                market = self._market_price(price, year or 2018, mileage or 80_000)
                disc   = (market - price) / market * 100 if market else 0
                if disc < filters["discount_min"]: continue

                signals = []
                if self._is_urgent(full):                               signals.append("urgent")
                if re.search(r"снизил|снижена|снижу", full.lower()):   signals.append("price_drop")
                if mileage and mileage < 80_000:                        signals.append("low_mileage")

                if filters.get("urgent_only") and "urgent" not in signals: continue

                ad = CarAd(
                    id=hashlib.md5(link.encode()).hexdigest()[:12],
                    title=title, price=price, market_price=market,
                    year=year, mileage=mileage,
                    region=REGION_NAMES_RU.get(region_key, region_key),
                    url=link, posted_at="свежее", signals=signals,
                )
                ad.score = self._score(ad)
                if ad.score >= filters.get("score_min", 20):
                    ads.append(ad)
            except Exception as e:
                log.debug(f"item: {e}")
        return ads

    async def close(self):
        await self.client.aclose()


SIGNAL_LABELS = {
    "urgent":      "🔴 Срочная продажа",
    "price_drop":  "📉 Снижена цена",
    "low_mileage": "📍 Малый пробег",
}

def format_ad(ad):
    stars  = "★" * (ad.score // 20) + "☆" * (5 - ad.score // 20)
    sigs   = "\n".join(f"  {SIGNAL_LABELS.get(s,s)}" for s in ad.signals) or "  —"
    d_rub  = f"{ad.discount_rub:,}".replace(",", " ")
    p_fmt  = f"{ad.price:,}".replace(",", " ")
    m_fmt  = f"{ad.market_price:,}".replace(",", " ")
    km_fmt = f"{ad.mileage:,} км".replace(",", " ") if ad.mileage else "не указан"
    return (
        f"🚗 {ad.title}\n\n"
        f"💰 {p_fmt} ₽  (~рынок: {m_fmt} ₽)\n"
        f"📉 Ниже рынка: {ad.discount_pct}% ({d_rub} ₽)\n\n"
        f"📅 Год: {ad.year or '?'}  |  🛣 Пробег: {km_fmt}\n"
        f"📍 {ad.region}  |  📡 АВИТО\n\n"
        f"⚡ Сигналы:\n{sigs}\n\n"
        f"⭐ Рейтинг: {stars} {ad.score}/100"
    )

def make_kb(ad):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Открыть объявление", url=ad.url)]])

def main_keyboard():
    return ReplyKeyboardMarkup([
        ["🔍 Искать сейчас", "⚙️ Фильтры"],
        ["⏸ Пауза", "▶️ Возобновить"],
        ["🗑 Сбросить историю", "📊 Статус"],
    ], resize_keyboard=True)


class AutoHunterBot:
    def __init__(self):
        self.app     = Application.builder().token(BOT_TOKEN).build()
        self.parser  = AvitoRSSParser()
        self.seen    = load_seen()
        self.filters = load_filters()
        self.running = False
        self.waiting_input = {}  # chat_id -> параметр который ждём

        for cmd, fn in [
            ("start",   self.cmd_start),
            ("искать",  self.cmd_hunt),
            ("фильтры", self.cmd_filters),
            ("помощь",  self.cmd_help),
        ]:
            self.app.add_handler(CommandHandler(cmd, fn))

        self.app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, self.on_text))

    async def cmd_start(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "🎯 AutoHunter запущен!\n\n"
            "Я ищу выгодные авто на Авито и присылаю тебе лучшие сделки.\n\n"
            "Используй кнопки внизу 👇",
            reply_markup=main_keyboard()
        )
        self.running = True
        asyncio.create_task(self._loop(ctx))

    async def on_text(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = u.message.text.strip()
        chat_id = u.effective_chat.id

        # Кнопки меню
        if text == "🔍 Искать сейчас":
            await self.cmd_hunt(u, ctx)
            return
        if text == "⚙️ Фильтры":
            await self.cmd_filters(u, ctx)
            return
        if text == "⏸ Пауза":
            self.running = False
            await u.message.reply_text("⏸ Бот поставлен на паузу.", reply_markup=main_keyboard())
            return
        if text == "▶️ Возобновить":
            if not self.running:
                self.running = True
                asyncio.create_task(self._loop(ctx))
            await u.message.reply_text("▶️ Возобновлён!", reply_markup=main_keyboard())
            return
        if text == "🗑 Сбросить историю":
            self.seen.clear()
            save_seen(self.seen)
            await u.message.reply_text("🗑 История сброшена — получу объявления заново.", reply_markup=main_keyboard())
            return
        if text == "📊 Статус":
            status = "✅ Работает" if self.running else "⏸ Пауза"
            await u.message.reply_text(
                f"Статус: {status}\n"
                f"Проверок выполнено: {ctx.bot_data.get('checks', 0)}\n"
                f"Отправлено объявлений: {ctx.bot_data.get('sent', 0)}\n"
                f"Проверка каждые: {self.filters['check_interval']} мин\n"
                f"Регионы: {', '.join(REGION_NAMES_RU.get(r, r) for r in self.filters['regions'])}",
                reply_markup=main_keyboard()
            )
            return

        # Ожидаем ввод значения параметра
        if chat_id in self.waiting_input:
            param = self.waiting_input.pop(chat_id)
            try:
                val = int(text.replace(" ", "").replace(",", ""))
                self.filters[param] = val
                save_filters(self.filters)
                param_ru = {v: k for k, v in PARAM_MAP.items()}.get(param, param)
                await u.message.reply_text(f"✅ {param_ru.capitalize()} = {val:,} ₽".replace(",", " "), reply_markup=main_keyboard())
            except ValueError:
                await u.message.reply_text("❌ Введи число, например: 1500000", reply_markup=main_keyboard())
            return

        # Настройка фильтров через кнопки
        filter_actions = {
            "💰 Минимальная цена":  "price_min",
            "💰 Максимальная цена": "price_max",
            "📅 Год от":            "year_min",
            "📅 Год до":            "year_max",
            "🛣 Пробег до (км)":    "mileage_max",
            "📉 Скидка от (%)":     "discount_min",
            "⭐ Рейтинг от":        "score_min",
            "⏱ Интервал (мин)":    "check_interval",
        }

        if text in filter_actions:
            param = filter_actions[text]
            self.waiting_input[chat_id] = param
            current = self.filters[param]
            await u.message.reply_text(
                f"Введи новое значение для «{text}»\n"
                f"Сейчас: {current:,}".replace(",", " ")
            )
            return

        region_actions = {
            "📍 Москва":           "moskva",
            "📍 Санкт-Петербург":  "spb",
            "📍 Екатеринбург":     "ekaterinburg",
            "📍 Новосибирск":      "novosibirsk",
            "📍 Краснодар":        "krasnodar",
            "📍 Казань":           "kazan",
            "📍 Нижний Новгород":  "nizhniy_novgorod",
            "📍 Ростов-на-Дону":   "rostov",
            "📍 Уфа":              "ufa",
            "📍 Самара":           "samara",
        }

        if text in region_actions:
            region = region_actions[text]
            if region in self.filters["regions"]:
                self.filters["regions"].remove(region)
                await u.message.reply_text(f"❌ Регион убран: {text[2:]}", reply_markup=main_keyboard())
            else:
                self.filters["regions"].append(region)
                await u.message.reply_text(f"✅ Регион добавлен: {text[2:]}", reply_markup=main_keyboard())
            save_filters(self.filters)
            return

        if text == "🔴 Только срочные: ВКЛ":
            self.filters["urgent_only"] = False
            save_filters(self.filters)
            await u.message.reply_text("✅ Показывать все объявления", reply_markup=main_keyboard())
            return
        if text == "🔴 Только срочные: ВЫКЛ":
            self.filters["urgent_only"] = True
            save_filters(self.filters)
            await u.message.reply_text("✅ Только срочные продажи", reply_markup=main_keyboard())
            return
        if text == "◀️ Назад":
            await u.message.reply_text("Главное меню", reply_markup=main_keyboard())
            return

    async def cmd_hunt(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text("🔍 Ищу выгодные авто...")
        found = await self._scan(ctx)
        if found == 0:
            await u.message.reply_text(
                "😔 Новых объявлений не найдено.\n\n"
                "Попробуй снизить фильтры в ⚙️ Фильтры:\n"
                "• Уменьши «Скидка от»\n"
                "• Уменьши «Рейтинг от»\n"
                "• Расширь диапазон цен",
                reply_markup=main_keyboard()
            )

    async def cmd_filters(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        f = self.filters
        brands = ", ".join(f["brands"]) if f["brands"] else "все марки"
        regions_ru = ", ".join(REGION_NAMES_RU.get(r, r) for r in f["regions"])
        urgent = "только срочные" if f["urgent_only"] else "все"

        filter_keyboard = ReplyKeyboardMarkup([
            ["💰 Минимальная цена", "💰 Максимальная цена"],
            ["📅 Год от",           "📅 Год до"],
            ["🛣 Пробег до (км)",   "📉 Скидка от (%)"],
            ["⭐ Рейтинг от",       "⏱ Интервал (мин)"],
            ["📍 Регионы",          "🔴 Только срочные: " + ("ВКЛ" if f["urgent_only"] else "ВЫКЛ")],
            ["◀️ Назад"],
        ], resize_keyboard=True)

        await u.message.reply_text(
            f"⚙️ Текущие фильтры:\n\n"
            f"💰 Цена: {f['price_min']:,} – {f['price_max']:,} ₽\n"
            f"📅 Год: {f['year_min']} – {f['year_max']}\n"
            f"🛣 Пробег до: {f['mileage_max']:,} км\n"
            f"📉 Скидка от рынка: {f['discount_min']}%\n"
            f"⭐ Рейтинг сделки от: {f['score_min']}/100\n"
            f"📍 Регионы: {regions_ru}\n"
            f"🚗 Марки: {brands}\n"
            f"🔴 Объявления: {urgent}\n"
            f"⏱ Проверка каждые: {f['check_interval']} мин\n\n"
            f"Нажми кнопку чтобы изменить 👇".replace(",", " "),
            reply_markup=filter_keyboard
        )

        # Показываем кнопки регионов если нажали "Регионы"
    async def show_regions(self, u: Update):
        regions_keyboard = ReplyKeyboardMarkup([
            ["📍 Москва",          "📍 Санкт-Петербург"],
            ["📍 Екатеринбург",    "📍 Новосибирск"],
            ["📍 Краснодар",       "📍 Казань"],
            ["📍 Нижний Новгород", "📍 Ростов-на-Дону"],
            ["📍 Уфа",             "📍 Самара"],
            ["◀️ Назад"],
        ], resize_keyboard=True)
        active = ", ".join(REGION_NAMES_RU.get(r, r) for r in self.filters["regions"])
        await u.message.reply_text(
            f"Нажми на регион чтобы добавить/убрать.\n"
            f"Активные: {active}",
            reply_markup=regions_keyboard
        )

    async def cmd_help(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text(
            "📖 Как пользоваться AutoHunter\n\n"
            "🔍 Искать сейчас — запустить поиск\n"
            "⚙️ Фильтры — настроить параметры поиска\n"
            "⏸ Пауза — остановить автопоиск\n"
            "▶️ Возобновить — запустить снова\n"
            "🗑 Сбросить историю — получить старые объявления заново\n"
            "📊 Статус — посмотреть статистику\n\n"
            "Как работает рейтинг сделки (0-100):\n"
            "  Скидка от рынка: до 50 очков\n"
            "  Срочная продажа: +15\n"
            "  Снижена цена: +12\n"
            "  Малый пробег: +8\n"
            "  Год от 2018: +5\n\n"
            "Советы перекупщику:\n"
            "• Скидка от 10-15% = реально выгодные\n"
            "• Включи «только срочные» для быстрых сделок\n"
            "• Интервал 5 мин = самые свежие объявления",
            reply_markup=main_keyboard()
        )

    async def _scan(self, ctx):
        all_ads = []
        for region in self.filters["regions"]:
            try:
                ads = await self.parser.fetch(region, self.filters)
                all_ads.extend(ads)
            except Exception as e:
                log.error(f"[{region}]: {e}")

        new_ads = [a for a in all_ads if a.id not in self.seen]
        new_ads.sort(key=lambda a: a.score, reverse=True)

        sent = 0
        for ad in new_ads[:10]:
            try:
                await ctx.bot.send_message(chat_id=CHAT_ID, text=format_ad(ad), reply_markup=make_kb(ad))
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
            await asyncio.sleep(self.filters.get("check_interval", 15) * 60)

    def run(self):
        log.info("AutoHunter стартует...")
        self.app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    AutoHunterBot().run()



