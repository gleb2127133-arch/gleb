"""
AutoHunter Bot — парсер выгодных объявлений о продаже авто
Источники: Авито, Авто.ру, Дром, Юла
"""

import asyncio
import logging
import json
import os
import re
import random
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
SEEN_FILE = "seen_ads.json"

DEFAULT_CONFIG = {
    "BOT_TOKEN": "8729431872:AAEMuCl2pEx8zd8_o1Twvvy4LeGB-oNMW7E",
    "CHAT_ID": "423771186",
    "CHECK_INTERVAL": 10,
    "filters": {
        "price_min": 300000,
        "price_max": 3000000,
        "year_min": 2012,
        "year_max": 2025,
        "mileage_max": 200000,
        "discount_min": 10,
        "regions": ["москва", "московская"],
        "brands": [],
        "keywords_urgent": ["срочно","срочная","торг","уезжаю","переезд","продам быстро","дёшево","дешево","отдам","срочно продам"],
        "sources": ["avito","autoru","drom","youla"]
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE,"w",encoding="utf-8") as f:
        json.dump(cfg,f,ensure_ascii=False,indent=2)

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE,"r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE,"w") as f:
        json.dump(list(seen)[-5000:],f)

HEADERS_POOL = [
    {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Accept-Language":"ru-RU,ru;q=0.9","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Referer":"https://www.google.com/"},
    {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15","Accept-Language":"ru-RU,ru;q=0.8","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36","Accept-Language":"ru,en;q=0.9","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"},
]

def get_headers():
    return random.choice(HEADERS_POOL)

MARKET_PRICES = {
    "toyota camry":2200000,"toyota corolla":1400000,"toyota rav4":2800000,
    "bmw 3":2000000,"bmw 5":2800000,"bmw x5":4500000,
    "mercedes c":2500000,"mercedes e":3200000,
    "kia optima":1600000,"kia rio":1100000,"kia sportage":2200000,"kia k5":2000000,
    "hyundai solaris":1100000,"hyundai creta":1900000,"hyundai elantra":1600000,
    "volkswagen polo":1100000,"volkswagen tiguan":2600000,"volkswagen passat":1800000,
    "audi a4":2700000,"audi a6":3500000,"audi q5":3800000,
    "nissan qashqai":2000000,"nissan x-trail":2100000,"nissan teana":1500000,
    "ford focus":900000,"ford kuga":1800000,
    "mazda 6":1600000,"mazda cx-5":2200000,"mazda 3":1200000,
    "honda cr-v":2400000,"honda accord":1800000,
    "skoda octavia":1500000,"skoda kodiaq":2500000,
    "lada vesta":900000,"lada granta":650000,"lada x-ray":800000,
    "renault duster":1200000,"renault logan":800000,
    "default":1500000,
}

def get_market_price(title):
    t = title.lower()
    for key,price in MARKET_PRICES.items():
        if key in t:
            return price
    return MARKET_PRICES["default"]

def parse_price(text):
    text = str(text).replace('\xa0',' ').replace(' ','').replace(',','')
    nums = re.findall(r'\d+',text)
    if nums:
        try:
            val = int(''.join(nums[:2]))
            if val > 100000000: val = val // 100  # копейки
            return val
        except:pass
    return 0

def parse_mileage(text):
    m = re.search(r'(\d[\d\s]*)\s*км',text.replace('\xa0',' '))
    if m:
        try: return int(m.group(1).replace(' ',''))
        except: pass
    return 0

def parse_year(text):
    m = re.search(r'\b(19[89]\d|20[012]\d)\b',text)
    return int(m.group(0)) if m else 0

def parse_date_text(text):
    text = text.lower().strip()
    now = datetime.now()
    if 'только что' in text: return now
    if 'сегодня' in text:
        m = re.search(r'(\d+):(\d+)',text)
        if m: return now.replace(hour=int(m.group(1)),minute=int(m.group(2)),second=0)
        return now
    if 'вчера' in text:
        m = re.search(r'(\d+):(\d+)',text)
        if m: return (now-timedelta(days=1)).replace(hour=int(m.group(1)),minute=int(m.group(2)))
        return now-timedelta(days=1)
    m = re.search(r'(\d+)\s*мин',text)
    if m: return now-timedelta(minutes=int(m.group(1)))
    m = re.search(r'(\d+)\s*час',text)
    if m: return now-timedelta(hours=int(m.group(1)))
    return now

class Ad:
    def __init__(self):
        self.id=self.title=self.price_str=self.url=self.source=self.region=self.description=""
        self.price=self.year=self.mileage=self.score=self.market_price=0
        self.discount_pct=0.0
        self.signals=[]
        self.posted_at=None

async def fetch(client,url):
    try:
        await asyncio.sleep(random.uniform(1.5,3.5))
        r = await client.get(url,headers=get_headers(),timeout=25,follow_redirects=True)
        return r.text if r.status_code==200 else None
    except Exception as e:
        log.error(f"Fetch error {url}: {e}")
        return None

async def parse_avito(client,filters):
    ads=[]
    rmap={"москва":"moskva","московская":"moskovskaya_oblast","санкт-петербург":"sankt-peterburg","спб":"sankt-peterburg","екатеринбург":"ekaterinburg","новосибирск":"novosibirsk","краснодар":"krasnodar","казань":"kazan","нижний новгород":"nizhegorodskaya_oblast","ростов":"rostov-na-donu","самара":"samara","уфа":"ufa"}
    regions=filters.get("regions",["москва"])
    pmin=filters.get("price_min",0)
    pmax=filters.get("price_max",9999999)
    for reg in regions[:2]:
        slug=rmap.get(reg.lower(),"rossiya")
        url=f"https://www.avito.ru/{slug}/avtomobili?pmin={pmin}&pmax={pmax}&s=104"
        html=await fetch(client,url)
        if not html: continue
        soup=BeautifulSoup(html,"lxml")
        items=soup.select('[data-marker="item"]') or soup.select('.iva-item-root')
        for item in items[:25]:
            try:
                ad=Ad(); ad.source="avito"
                te=item.select_one('[itemprop="name"]') or item.select_one('.iva-item-title')
                if te: ad.title=te.get_text(strip=True)
                pe=item.select_one('[itemprop="price"]') or item.select_one('[data-marker="item-price"]')
                if pe:
                    pv=pe.get('content') or pe.get_text()
                    ad.price=parse_price(pv); ad.price_str=pe.get_text(strip=True)
                le=item.select_one('a[data-marker="item-title"]') or item.select_one('a.iva-item-title-py3i_')
                if le:
                    href=le.get('href','')
                    ad.url=f"https://www.avito.ru{href}" if href.startswith('/') else href
                    m=re.search(r'_(\d+)$',href)
                    ad.id=f"avito_{m.group(1)}" if m else f"avito_{hash(href)}"
                ge=item.select_one('[data-marker="item-location"]')
                if ge: ad.region=ge.get_text(strip=True)
                de=item.select_one('[data-marker="item-description"]')
                if de: ad.description=de.get_text(strip=True)
                ft=f"{ad.title} {ad.description}"
                ad.year=parse_year(ft); ad.mileage=parse_mileage(ft)
                date_el=item.select_one('[data-marker="item-date"]')
                ad.posted_at=parse_date_text(date_el.get_text()) if date_el else datetime.now()
                if ad.title and ad.price>0 and ad.url: ads.append(ad)
            except Exception as e: log.debug(f"avito item: {e}")
    return ads

async def parse_autoru(client,filters):
    ads=[]
    rmap={"москва":"moskva","санкт-петербург":"sankt-peterburg","спб":"sankt-peterburg","екатеринбург":"ekaterinburg","новосибирск":"novosibirsk","краснодар":"krasnodar","казань":"kazan"}
    regions=filters.get("regions",["москва"])
    slug=rmap.get(regions[0].lower(),"rossiya") if regions else "rossiya"
    pmin=filters.get("price_min",0); pmax=filters.get("price_max",9999999)
    url=f"https://auto.ru/{slug}/cars/used/?price_from={pmin}&price_to={pmax}&sort=fresh_relevance_1-desc&output_type=list"
    html=await fetch(client,url)
    if not html: return ads
    soup=BeautifulSoup(html,"lxml")
    items=soup.select('.ListingItem') or soup.select('[class*="ListingItem_"]')
    for item in items[:25]:
        try:
            ad=Ad(); ad.source="autoru"
            te=item.select_one('[class*="ListingItemTitle"]')
            if te: ad.title=te.get_text(strip=True)
            pe=item.select_one('[class*="ListingItemPrice"]')
            if pe: ad.price_str=pe.get_text(strip=True); ad.price=parse_price(ad.price_str)
            le=item.select_one('a[href*="/cars/"]')
            if le:
                href=le.get('href','')
                ad.url=href if href.startswith('http') else f"https://auto.ru{href}"
                m=re.search(r'/(\d+)-',href)
                ad.id=f"autoru_{m.group(1)}" if m else f"autoru_{hash(href)}"
            pt=item.get_text()
            ad.year=parse_year(ad.title or pt); ad.mileage=parse_mileage(pt)
            re_el=item.select_one('[class*="MetroListPlace"]')
            if re_el: ad.region=re_el.get_text(strip=True)
            de=item.select_one('[class*="date"]')
            ad.posted_at=parse_date_text(de.get_text()) if de else datetime.now()
            if ad.title and ad.price>0 and ad.url: ads.append(ad)
        except Exception as e: log.debug(f"autoru item: {e}")
    return ads

async def parse_drom(client,filters):
    ads=[]
    rmap={"москва":"moscow","санкт-петербург":"spb","спб":"spb","екатеринбург":"ekaterinburg","новосибирск":"novosibirsk","краснодар":"krasnodar"}
    regions=filters.get("regions",[""])
    slug=rmap.get(regions[0].lower(),"") if regions else ""
    rpart=f"{slug}/" if slug else ""
    pmin=filters.get("price_min",0); pmax=filters.get("price_max",9999999)
    url=f"https://auto.drom.ru/{rpart}all/?minprice={pmin}&maxprice={pmax}&order=date_desc"
    html=await fetch(client,url)
    if not html: return ads
    soup=BeautifulSoup(html,"lxml")
    items=soup.select('[data-ftid="bull_item"]') or soup.select('.bull-item')
    for item in items[:25]:
        try:
            ad=Ad(); ad.source="drom"
            te=item.select_one('[data-ftid="bull_item_title"]') or item.select_one('h3')
            if te: ad.title=te.get_text(strip=True)
            pe=item.select_one('[data-ftid="bull_item_price"]')
            if pe: ad.price_str=pe.get_text(strip=True); ad.price=parse_price(ad.price_str)
            le=item.select_one('a[data-ftid="bull_item_title"]') or item.select_one('h3 a')
            if le:
                href=le.get('href','')
                ad.url=href if href.startswith('http') else f"https://auto.drom.ru{href}"
                m=re.search(r'/(\d+)\.html',href)
                ad.id=f"drom_{m.group(1)}" if m else f"drom_{hash(href)}"
            pt=item.get_text()
            ad.year=parse_year(ad.title or pt); ad.mileage=parse_mileage(pt)
            ce=item.select_one('[data-ftid="bull_item_location"]')
            if ce: ad.region=ce.get_text(strip=True)
            de=item.select_one('[data-ftid="bull_item_date"]')
            ad.posted_at=parse_date_text(de.get_text()) if de else datetime.now()
            desc_e=item.select_one('[data-ftid="bull_item_description"]')
            if desc_e: ad.description=desc_e.get_text(strip=True)
            if ad.title and ad.price>0 and ad.url: ads.append(ad)
        except Exception as e: log.debug(f"drom item: {e}")
    return ads

async def parse_youla(client,filters):
    ads=[]
    pmin=filters.get("price_min",0); pmax=filters.get("price_max",9999999)
    try:
        await asyncio.sleep(random.uniform(1,2))
        r=await client.get("https://youla.ru/web-api/feed/listing",params={"category_slug":"transport_cars","price__gte":pmin,"price__lte":pmax,"sort_field":"date"},headers=get_headers(),timeout=20)
        if r.status_code!=200: return ads
        items=r.json().get("data",{}).get("products",[]) or []
        for item in items[:20]:
            try:
                ad=Ad(); ad.source="youla"
                ad.id=f"youla_{item.get('id','')}"
                ad.title=item.get("name","")
                pr=item.get("price",0)
                ad.price=int(pr)//100 if pr>100000 else int(pr)
                ad.price_str=f"{ad.price:,} ₽"
                ad.url=f"https://youla.ru/p/{item.get('slug',item.get('id',''))}"
                ad.region=item.get("location",{}).get("name","")
                ad.description=item.get("description","")
                for prop in item.get("attributes",[]):
                    if prop.get("slug")=="year": ad.year=int(prop.get("value",0) or 0)
                    if prop.get("slug")=="mileage": ad.mileage=int(prop.get("value",0) or 0)
                ts=item.get("publishedAt") or item.get("sortDate")
                if ts: ad.posted_at=datetime.fromtimestamp(ts)
                if ad.title and ad.price>0: ads.append(ad)
            except: pass
    except Exception as e: log.error(f"Youla: {e}")
    return ads

def analyze_ad(ad,filters):
    signals=[]; score=0
    desc=(ad.description+" "+ad.title).lower()
    for w in filters.get("keywords_urgent",[]):
        if w in desc: signals.append("🔴 Срочная продажа"); score+=20; break
    if ad.posted_at:
        h=ad.posted_at.hour
        if h>=22 or h<=6: signals.append("🌙 Выложено ночью"); score+=10
        age=datetime.now()-ad.posted_at
        if age<timedelta(hours=3): signals.append("🟢 Новое (< 3ч)"); score+=15
        elif age<timedelta(hours=24): signals.append("📅 Сегодня"); score+=5
    if ad.price>0:
        market=get_market_price(ad.title); ad.market_price=market
        disc=((market-ad.price)/market)*100; ad.discount_pct=round(disc,1)
        if disc>=30: signals.append(f"💰 -{disc:.0f}% от рынка"); score+=35
        elif disc>=20: signals.append(f"💰 -{disc:.0f}% от рынка"); score+=25
        elif disc>=10: signals.append(f"📉 -{disc:.0f}% от рынка"); score+=15
    if 0<ad.mileage<80000: signals.append(f"📍 Малый пробег"); score+=10
    if any(w in desc for w in ["торг","торгуюсь","цена снижена","снизил"]): signals.append("💬 Есть торг"); score+=8
    ad.signals=signals; ad.score=min(score,100)
    return ad

def apply_filters(ads,filters):
    out=[]
    pmin=filters.get("price_min",0); pmax=filters.get("price_max",99999999)
    ymin=filters.get("year_min",0); ymax=filters.get("year_max",9999)
    mmax=filters.get("mileage_max",9999999); dmin=filters.get("discount_min",0)
    brands=[b.lower() for b in filters.get("brands",[])]
    for ad in ads:
        if ad.price and (ad.price<pmin or ad.price>pmax): continue
        if ad.year and (ad.year<ymin or ad.year>ymax): continue
        if ad.mileage and ad.mileage>mmax: continue
        if dmin and ad.discount_pct<dmin: continue
        if brands and not any(b in ad.title.lower() for b in brands): continue
        out.append(ad)
    return out

SRC_NAMES={"avito":"🟢 Авито","autoru":"🔵 Авто.ру","drom":"🟠 Дром","youla":"🟣 Юла"}

def format_ad(ad):
    src=SRC_NAMES.get(ad.source,ad.source)
    bar="█"*(ad.score//10)+"░"*(10-ad.score//10)
    lines=[f"🚗 <b>{ad.title}</b>","",f"💵 <b>{ad.price_str or f'{ad.price:,} ₽'}</b>"]
    if ad.market_price and ad.discount_pct>0:
        lines+=[f"📊 Рынок: ~{ad.market_price:,} ₽  <b>(-{ad.discount_pct:.0f}%)</b>",f"💰 Выгода: ~{(ad.market_price-ad.price):,} ₽"]
    if ad.year: lines.append(f"📅 Год: {ad.year}")
    if ad.mileage: lines.append(f"🛣 Пробег: {ad.mileage:,} км")
    if ad.region: lines.append(f"📍 {ad.region}")
    lines+=["",f"🎯 Рейтинг: {ad.score}/100  {bar}"]
    if ad.signals: lines+=["","<b>Сигналы:</b>"]+[f"  {s}" for s in ad.signals]
    if ad.posted_at:
        age=datetime.now()-ad.posted_at
        if age.total_seconds()<3600: ts=f"{int(age.total_seconds()//60)} мин назад"
        elif age.total_seconds()<86400: ts=f"{int(age.total_seconds()//3600)} ч назад"
        else: ts=ad.posted_at.strftime("%d.%m %H:%M")
        lines.append(f"🕐 Опубликовано: {ts}")
    lines+=["",f"📢 {src}",f"🔗 <a href='{ad.url}'>Открыть объявление →</a>"]
    return "\n".join(lines)

class Scanner:
    def __init__(self,bot):
        self.bot=bot; self.running=False; self.task=None
        self.seen=load_seen(); self.last_scan=None; self.total_sent=0

    async def scan_once(self,cfg):
        filters=cfg.get("filters",{}); sources=filters.get("sources",["avito","autoru","drom"])
        all_ads=[]
        async with httpx.AsyncClient() as client:
            tasks=[]
            if "avito" in sources: tasks.append(parse_avito(client,filters))
            if "autoru" in sources: tasks.append(parse_autoru(client,filters))
            if "drom" in sources: tasks.append(parse_drom(client,filters))
            if "youla" in sources: tasks.append(parse_youla(client,filters))
            results=await asyncio.gather(*tasks,return_exceptions=True)
            for r in results:
                if isinstance(r,list): all_ads.extend(r)
        for ad in all_ads: analyze_ad(ad,filters)
        filtered=apply_filters(all_ads,filters)
        new=[ad for ad in filtered if ad.id and ad.id not in self.seen]
        new.sort(key=lambda x:x.score,reverse=True)
        self.last_scan=datetime.now()
        return new

    async def run_loop(self,cfg):
        self.running=True
        chat_id=cfg.get("CHAT_ID"); interval=cfg.get("CHECK_INTERVAL",15)
        await self.bot.send_message(chat_id,
            f"🟢 <b>AutoHunter запущен!</b>\n\n⏱ Проверка каждые <b>{interval} мин</b>\n"
            f"📡 Источники: {', '.join(cfg['filters'].get('sources',[]))}\n"
            f"💰 Цена: {cfg['filters'].get('price_min',0):,} — {cfg['filters'].get('price_max',0):,} ₽\n"
            f"📅 Год: {cfg['filters'].get('year_min')} — {cfg['filters'].get('year_max')}\n\nЖду выгодных объявлений... 🎯",parse_mode="HTML")
        while self.running:
            try:
                cfg=load_config()
                new=await self.scan_once(cfg)
                log.info(f"Scan done. New: {len(new)}")
                for ad in new[:10]:
                    try:
                        await self.bot.send_message(chat_id,format_ad(ad),parse_mode="HTML")
                        self.seen.add(ad.id); self.total_sent+=1
                        await asyncio.sleep(1.2)
                    except Exception as e: log.error(f"Send: {e}")
                save_seen(self.seen)
            except Exception as e: log.error(f"Loop: {e}")
            await asyncio.sleep(interval*60)

    def start(self,cfg):
        if not self.running: self.task=asyncio.create_task(self.run_loop(cfg))

    def stop(self):
        self.running=False
        if self.task: self.task.cancel()

class S(StatesGroup):
    setting=State()

scanner=None

def kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="▶️ Запустить"),KeyboardButton(text="⏹ Остановить")],
        [KeyboardButton(text="📊 Статус"),KeyboardButton(text="🔍 Проверить сейчас")],
        [KeyboardButton(text="⚙️ Настройки"),KeyboardButton(text="❓ Помощь")],
    ],resize_keyboard=True)

async def cmd_start(msg):
    global scanner
    cfg=load_config(); cfg["CHAT_ID"]=str(msg.chat.id); save_config(cfg)
    if scanner is None: scanner=Scanner(msg.bot)
    await msg.answer("👋 <b>AutoHunter — охотник за выгодными авто</b>\n\n🎯 Слежу за Авито, Авто.ру, Дромом и Юлой\n💰 Нахожу машины дешевле рынка\n\n<b>Начни с настройки фильтров:</b> /settings\nПотом запусти мониторинг: /run",parse_mode="HTML",reply_markup=kb())

async def cmd_run(msg):
    global scanner
    cfg=load_config()
    if not cfg.get("CHAT_ID"): cfg["CHAT_ID"]=str(msg.chat.id); save_config(cfg)
    if scanner is None: scanner=Scanner(msg.bot)
    if scanner.running: await msg.answer("⚠️ Уже запущен. /stop чтобы остановить."); return
    scanner.start(cfg)
    await msg.answer(f"✅ Мониторинг запущен!\n⏱ Проверка каждые {cfg.get('CHECK_INTERVAL',15)} мин",reply_markup=kb())

async def cmd_stop(msg):
    global scanner
    if scanner and scanner.running: scanner.stop(); await msg.answer("⏹ Остановлен.",reply_markup=kb())
    else: await msg.answer("ℹ️ Не запущен.")

async def cmd_status(msg):
    global scanner
    cfg=load_config(); f=cfg.get("filters",{})
    st="🟢 Запущен" if (scanner and scanner.running) else "🔴 Остановлен"
    last=scanner.last_scan.strftime("%H:%M:%S") if (scanner and scanner.last_scan) else "—"
    sent=scanner.total_sent if scanner else 0
    await msg.answer(
        f"📊 <b>Статус</b>\n\n{st}\nПоследняя проверка: {last}\nОтправлено: {sent}\n\n"
        f"<b>Фильтры:</b>\n💰 {f.get('price_min',0):,} — {f.get('price_max',0):,} ₽\n"
        f"📅 {f.get('year_min')} — {f.get('year_max')}\n"
        f"🛣 Пробег до: {f.get('mileage_max',0):,} км\n"
        f"📉 Скидка от: {f.get('discount_min',0)}%\n"
        f"📍 Регионы: {', '.join(f.get('regions',[]))}\n"
        f"🚗 Марки: {', '.join(f.get('brands',[])) or 'все'}\n"
        f"📡 Источники: {', '.join(f.get('sources',[]))}\n"
        f"⏱ Интервал: {cfg.get('CHECK_INTERVAL',15)} мин",parse_mode="HTML")

async def cmd_scan(msg):
    global scanner
    cfg=load_config()
    if not cfg.get("CHAT_ID"): cfg["CHAT_ID"]=str(msg.chat.id); save_config(cfg)
    if scanner is None: scanner=Scanner(msg.bot)
    await msg.answer("🔍 Запускаю проверку...")
    try:
        new=await scanner.scan_once(cfg)
        if not new: await msg.answer("😔 Новых выгодных объявлений нет.\n\nРасширь фильтры через /settings"); return
        await msg.answer(f"✅ Найдено {len(new)} объявлений! Отправляю...")
        for ad in new[:10]:
            await msg.bot.send_message(cfg["CHAT_ID"],format_ad(ad),parse_mode="HTML")
            scanner.seen.add(ad.id); await asyncio.sleep(0.8)
        save_seen(scanner.seen); scanner.total_sent+=len(new[:10])
    except Exception as e: await msg.answer(f"❌ Ошибка: {e}")

async def cmd_settings(msg):
    b=InlineKeyboardBuilder()
    opts=[("💰 Цена","set_price"),("📅 Год","set_year"),("🛣 Пробег","set_mileage"),("📍 Регион","set_region"),("🚗 Марка","set_brand"),("📡 Источники","set_sources"),("⏱ Интервал","set_interval"),("📉 Скидка","set_discount")]
    for label,cb in opts: b.button(text=label,callback_data=cb)
    b.adjust(2)
    await msg.answer("⚙️ <b>Настройки</b>\n\nЧто изменить?",parse_mode="HTML",reply_markup=b.as_markup())

SETTING_PROMPTS={
    "set_price":("price","💰 Введи диапазон цен через пробел (₽)\nПример: <code>500000 2000000</code>"),
    "set_year":("year","📅 Введи диапазон годов через пробел\nПример: <code>2015 2024</code>"),
    "set_mileage":("mileage","🛣 Введи макс. пробег (км)\nПример: <code>150000</code>"),
    "set_region":("region","📍 Регион(ы) через запятую\nПример: <code>москва, московская</code>"),
    "set_brand":("brand","🚗 Марки через запятую (или 'все')\nПример: <code>toyota, bmw</code>"),
    "set_sources":("sources","📡 Источники через запятую\nДоступны: avito, autoru, drom, youla\nПример: <code>avito, autoru, drom</code>"),
    "set_interval":("interval","⏱ Интервал проверки в минутах (мин. 5)\nПример: <code>10</code>"),
    "set_discount":("discount","📉 Мин. скидка от рынка (%)\nПример: <code>15</code>"),
}

async def cb_set(cb,state:FSMContext):
    key,prompt=SETTING_PROMPTS[cb.data]
    await state.set_state(S.setting); await state.update_data(k=key)
    await cb.message.answer(prompt,parse_mode="HTML"); await cb.answer()

async def process_set(msg,state:FSMContext):
    d=await state.get_data(); k=d.get("k",""); t=msg.text.strip()
    cfg=load_config(); f=cfg.setdefault("filters",{})
    try:
        if k=="price":
            p=t.split(); f["price_min"]=int(p[0]); f["price_max"]=int(p[1]) if len(p)>1 else 99999999
            await msg.answer(f"✅ Цена: {f['price_min']:,} — {f['price_max']:,} ₽")
        elif k=="year":
            p=t.split(); f["year_min"]=int(p[0]); f["year_max"]=int(p[1]) if len(p)>1 else 2025
            await msg.answer(f"✅ Год: {f['year_min']} — {f['year_max']}")
        elif k=="mileage":
            f["mileage_max"]=int(t); await msg.answer(f"✅ Пробег до: {f['mileage_max']:,} км")
        elif k=="region":
            f["regions"]=[r.strip().lower() for r in t.split(",")]; await msg.answer(f"✅ Регионы: {', '.join(f['regions'])}")
        elif k=="brand":
            f["brands"]=[] if t.lower() in ("все","all","") else [b.strip().lower() for b in t.split(",")]
            await msg.answer(f"✅ Марки: {', '.join(f['brands']) or 'все'}")
        elif k=="sources":
            valid={"avito","autoru","drom","youla"}
            f["sources"]=[s.strip().lower() for s in t.split(",") if s.strip().lower() in valid]
            await msg.answer(f"✅ Источники: {', '.join(f['sources'])}")
        elif k=="interval":
            cfg["CHECK_INTERVAL"]=max(5,int(t)); await msg.answer(f"✅ Интервал: {cfg['CHECK_INTERVAL']} мин")
        elif k=="discount":
            f["discount_min"]=int(t); await msg.answer(f"✅ Мин. скидка: {f['discount_min']}%")
        save_config(cfg)
    except Exception as e: await msg.answer(f"❌ Ошибка: {e}")
    await state.clear()

async def cmd_help(msg):
    await msg.answer(
        "❓ <b>Инструкция</b>\n\n1️⃣ /settings — настрой фильтры\n2️⃣ /run — запусти мониторинг\n3️⃣ Получай уведомления автоматически\n4️⃣ /scan — проверить прямо сейчас\n\n"
        "<b>Сигналы в сообщениях:</b>\n🔴 Срочная — слово 'срочно' в тексте\n🌙 Ночное — выложено в 22:00–06:00\n🟢 Новое — меньше 3 часов\n💰 Скидка — ниже рыночной цены\n📉 Мин. скидка — выставлен порог\n📍 Малый пробег — до 80 000 км\n🎯 Рейтинг — итоговый балл сделки",
        parse_mode="HTML")

async def handle_btns(msg):
    t=msg.text
    if t=="▶️ Запустить": await cmd_run(msg)
    elif t=="⏹ Остановить": await cmd_stop(msg)
    elif t=="📊 Статус": await cmd_status(msg)
    elif t=="🔍 Проверить сейчас": await cmd_scan(msg)
    elif t=="⚙️ Настройки": await cmd_settings(msg)
    elif t=="❓ Помощь": await cmd_help(msg)

async def main():
    cfg=load_config()
    token=cfg.get("BOT_TOKEN","")
    if not token or token=="8729431872:AAEMuCl2pEx8zd8_o1Twvvy4LeGB-oNMW7E":
        print("\n"+"="*55)
        print("  ❌  ТОКЕН НЕ НАСТРОЕН!")
        print("="*55)
        print("\n📋 Что делать:")
        print("  1. Открой Telegram → найди @BotFather")
        print("  2. Напиши /newbot → придумай имя → получи токен")
        print("  3. Открой файл  config.json")
        print('  4. Замени "8729431872:AAEMuCl2pEx8zd8_o1Twvvy4LeGB-oNMW7E"  на свой токен')
        print("  5. python bot.py  — запусти снова")
        print("="*55+"\n")
        return
    bot=Bot(token=token)
    dp=Dispatcher(storage=MemoryStorage())
    dp.message.register(cmd_start,Command("start"))
    dp.message.register(cmd_run,Command("run"))
    dp.message.register(cmd_stop,Command("stop"))
    dp.message.register(cmd_status,Command("status"))
    dp.message.register(cmd_scan,Command("scan"))
    dp.message.register(cmd_settings,Command("settings"))
    dp.message.register(cmd_help,Command("help"))
    dp.message.register(process_set,StateFilter(S.setting))
    dp.message.register(handle_btns,F.text)
    dp.callback_query.register(cb_set,F.data.startswith("set_"))
    log.info("AutoHunter starting...")
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
