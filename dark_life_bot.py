# 🖤 dark Life — upgraded life-sim Telegram bot (Python)
# pip install -U python-telegram-bot==21.6
# Run:
#   export DARKLIFE_TOKEN="YOUR_TOKEN"
#   python dark_life_bot.py
#
# Features:
# - Levels & XP + job selection (higher level => better jobs)
# - Businesses: buy/upgrade, daily income on sleep
# - Crypto market: BTC/ETH/TON/USDT + fiat (RUB/USD/EUR), buy/sell, portfolio
# - SQLite persistence

import os, json, time, random, sqlite3
from typing import Dict, Any, Optional, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("DARKLIFE_TOKEN", "")
DB_PATH = os.environ.get("DARKLIFE_DB", "darklife.db")

# ---------- Core caps ----------
MAX_HEALTH = 100
MAX_HUNGER = 100
MAX_ENERGY = 100

# decay per hour (real time)
HUNGER_DECAY_PER_HOUR = 6
ENERGY_DECAY_PER_HOUR = 4

START_MONEY = 5000

# ---------- Jobs (level gated) ----------
# name, min_level, base_pay, xp_gain, energy_cost, hunger_cost
JOBS = [
    ("Разнорабочий", 1, 800, 14, 18, 12),
    ("Курьер",       2, 1100, 18, 22, 15),
    ("Бариста",      3, 1400, 22, 25, 14),
    ("Охранник",     5, 1900, 28, 30, 16),
    ("Слесарь",      7, 2400, 34, 34, 18),
    ("Сисадмин",     10, 3200, 44, 30, 14),
    ("Разработчик",  15, 4800, 62, 32, 12),
]

def xp_needed(level: int) -> int:
    # мягкая прогрессия
    return 60 + level * 45

# ---------- Businesses ----------
# id, name, buy_price, base_daily_income, upgrade_base_cost
BUSINESSES = [
    ("coffee",  "☕ Кофейня",          25000, 900,  6000),
    ("shop",    "🏪 Магазин у дома",   45000, 1400, 9000),
    ("carwash", "🚗 Автомойка",        80000, 2400, 15000),
    ("it",      "💻 IT-студия",        160000, 5200, 28000),
    ("club",    "🎶 Ночной клуб",      260000, 8800, 45000),
]
# income formula: base_daily_income * (1 + 0.35*(level-1))

# ---------- Crypto market ----------
ASSETS = [
    ("RUB", "₽", "fiat"),
    ("USD", "$", "fiat"),
    ("EUR", "€", "fiat"),
    ("BTC", "₿", "crypto"),
    ("ETH", "Ξ", "crypto"),
    ("TON", "💎", "crypto"),
    ("USDT","🪙", "stable"),
]
# prices are quoted in RUB (simplify)
DEFAULT_PRICES_RUB = {
    "USD": 95.0,
    "EUR": 103.0,
    "BTC": 5_800_000.0,
    "ETH": 280_000.0,
    "TON": 220.0,
    "USDT": 95.0,
    "RUB": 1.0,
}
# random walk tuning
CRYPTO_VOL = {"BTC": 0.020, "ETH": 0.028, "TON": 0.055, "USDT": 0.004, "USD": 0.010, "EUR": 0.012, "RUB": 0.0}

# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            money INTEGER NOT NULL,
            health INTEGER NOT NULL,
            hunger INTEGER NOT NULL,
            energy INTEGER NOT NULL,
            day INTEGER NOT NULL,
            location TEXT NOT NULL,
            job TEXT NOT NULL,
            level INTEGER NOT NULL,
            xp INTEGER NOT NULL,
            inventory TEXT NOT NULL,
            last_seen INTEGER NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_businesses(
            user_id INTEGER NOT NULL,
            biz_id TEXT NOT NULL,
            biz_level INTEGER NOT NULL,
            last_paid_day INTEGER NOT NULL,
            PRIMARY KEY(user_id, biz_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio(
            user_id INTEGER NOT NULL,
            asset TEXT NOT NULL,
            amount REAL NOT NULL,
            PRIMARY KEY(user_id, asset)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS market(
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS market_meta(
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        """)
        # seed market
        cur = conn.execute("SELECT COUNT(*) AS c FROM market").fetchone()["c"]
        if cur == 0:
            for k, v in DEFAULT_PRICES_RUB.items():
                conn.execute("INSERT INTO market(key,value) VALUES(?,?)", (k, float(v)))
        meta = conn.execute("SELECT COUNT(*) AS c FROM market_meta").fetchone()["c"]
        if meta == 0:
            conn.execute("INSERT INTO market_meta(key,value) VALUES('last_update', ?)", (int(time.time()),))
        conn.commit()

# ---------- Helpers ----------
def now_ts() -> int:
    return int(time.time())

def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        r = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else None

def save_user(user_id: int, st: Dict[str, Any]) -> None:
    with db() as conn:
        conn.execute("""
        INSERT INTO users(user_id, money, health, hunger, energy, day, location, job, level, xp, inventory, last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          money=excluded.money,
          health=excluded.health,
          hunger=excluded.hunger,
          energy=excluded.energy,
          day=excluded.day,
          location=excluded.location,
          job=excluded.job,
          level=excluded.level,
          xp=excluded.xp,
          inventory=excluded.inventory,
          last_seen=excluded.last_seen
        """, (
            user_id, st["money"], st["health"], st["hunger"], st["energy"], st["day"],
            st["location"], st["job"], st["level"], st["xp"], st["inventory"], st["last_seen"]
        ))
        conn.commit()

def default_state() -> Dict[str, Any]:
    return {
        "money": START_MONEY,
        "health": 90,
        "hunger": 70,
        "energy": 80,
        "day": 1,
        "location": "🚉 Вокзал",
        "job": "Безработный",
        "level": 1,
        "xp": 0,
        "inventory": json.dumps({"еда": 0, "аптечка": 0, "билет": 0}, ensure_ascii=False),
        "last_seen": now_ts(),
    }

def inv_get(st: Dict[str, Any]) -> Dict[str, int]:
    try:
        return json.loads(st["inventory"])
    except Exception:
        return {"еда": 0, "аптечка": 0, "билет": 0}

def inv_set(st: Dict[str, Any], inv: Dict[str, int]) -> None:
    st["inventory"] = json.dumps(inv, ensure_ascii=False)

def apply_decay(st: Dict[str, Any]) -> str:
    last = int(st.get("last_seen", now_ts()))
    dt = max(0, now_ts() - last)
    hours = dt / 3600.0
    if hours < 0.2:
        st["last_seen"] = now_ts()
        return ""
    hunger_loss = int(hours * HUNGER_DECAY_PER_HOUR)
    energy_loss = int(hours * ENERGY_DECAY_PER_HOUR)
    st["hunger"] = clamp(st["hunger"] - hunger_loss, 0, MAX_HUNGER)
    st["energy"] = clamp(st["energy"] - energy_loss, 0, MAX_ENERGY)

    hp_loss = 0
    if st["hunger"] <= 10: hp_loss += int(hours * 3)
    if st["energy"] <= 10: hp_loss += int(hours * 2)
    st["health"] = clamp(st["health"] - hp_loss, 0, MAX_HEALTH)

    st["last_seen"] = now_ts()
    note = f"⏳ Прошло ~{hours:.1f}ч: голод -{hunger_loss}, энергия -{energy_loss}."
    if st["health"] <= 0:
        note += "\n💀 Ты умер(ла). Нажми /start."
    return note

def render(st: Dict[str, Any]) -> str:
    need = xp_needed(st["level"])
    return (
        f"📍 {st['location']} | 📅 День {st['day']}\n"
        f"🧠 Уровень: {st['level']} (XP {st['xp']}/{need})\n"
        f"💼 Работа: {st['job']}\n\n"
        f"💰 Деньги: {st['money']} ₽\n"
        f"❤️ {st['health']}/{MAX_HEALTH}  🍗 {st['hunger']}/{MAX_HUNGER}  ⚡ {st['energy']}/{MAX_ENERGY}\n"
    )

def maybe_level_up(st: Dict[str, Any]) -> str:
    msg = ""
    while st["xp"] >= xp_needed(st["level"]):
        st["xp"] -= xp_needed(st["level"])
        st["level"] += 1
        msg += f"⬆️ *Уровень повышен!* Теперь ты {st['level']} lvl.\n"
    return msg

# ---------- Market ----------
def market_update_if_needed() -> None:
    with db() as conn:
        last = conn.execute("SELECT value FROM market_meta WHERE key='last_update'").fetchone()
        last_ts = int(last["value"]) if last else 0
        if now_ts() - last_ts < 300:  # обновляем не чаще чем раз в 5 минут
            return

        prices = {r["key"]: float(r["value"]) for r in conn.execute("SELECT key,value FROM market")}
        # random walk
        for sym, _, _ in ASSETS:
            if sym == "RUB":
                prices[sym] = 1.0
                continue
            vol = CRYPTO_VOL.get(sym, 0.01)
            drift = random.uniform(-vol, vol)
            # USDT near USD
            if sym == "USDT":
                anchor = prices.get("USD", DEFAULT_PRICES_RUB["USD"])
                prices[sym] = max(1.0, anchor * (1 + drift))
            else:
                prices[sym] = max(0.0001, prices.get(sym, DEFAULT_PRICES_RUB.get(sym, 1.0)) * (1 + drift))

        for k, v in prices.items():
            conn.execute("UPDATE market SET value=? WHERE key=?", (float(v), k))
        conn.execute("UPDATE market_meta SET value=? WHERE key='last_update'", (now_ts(),))
        conn.commit()

def get_price(sym: str) -> float:
    with db() as conn:
        r = conn.execute("SELECT value FROM market WHERE key=?", (sym,)).fetchone()
    return float(r["value"]) if r else float(DEFAULT_PRICES_RUB.get(sym, 1.0))

def portfolio_get(user_id: int) -> Dict[str, float]:
    with db() as conn:
        rows = conn.execute("SELECT asset, amount FROM portfolio WHERE user_id=?", (user_id,)).fetchall()
    d = {r["asset"]: float(r["amount"]) for r in rows}
    if "RUB" not in d:
        d["RUB"] = 0.0
    return d

def portfolio_set(user_id: int, asset: str, amount: float) -> None:
    with db() as conn:
        conn.execute("""
        INSERT INTO portfolio(user_id, asset, amount)
        VALUES(?,?,?)
        ON CONFLICT(user_id, asset) DO UPDATE SET amount=excluded.amount
        """, (user_id, asset, float(amount)))
        conn.commit()

# ---------- Businesses ----------
def user_biz_list(user_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT biz_id, biz_level, last_paid_day FROM user_businesses WHERE user_id=?",
            (user_id,)
        ).fetchall()

def user_biz_get(user_id: int, biz_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT biz_id, biz_level, last_paid_day FROM user_businesses WHERE user_id=? AND biz_id=?",
            (user_id, biz_id)
        ).fetchone()

def user_biz_upsert(user_id: int, biz_id: str, biz_level: int, last_paid_day: int) -> None:
    with db() as conn:
        conn.execute("""
        INSERT INTO user_businesses(user_id, biz_id, biz_level, last_paid_day)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id, biz_id) DO UPDATE SET
          biz_level=excluded.biz_level,
          last_paid_day=excluded.last_paid_day
        """, (user_id, biz_id, int(biz_level), int(last_paid_day)))
        conn.commit()

def biz_info(biz_id: str) -> Tuple[str, int, int, int]:
    for _id, name, buy, inc, upc in BUSINESSES:
        if _id == biz_id:
            return name, buy, inc, upc
    return ("❓", 10**9, 0, 10**9)

def biz_income(base_income: int, lvl: int) -> int:
    return int(base_income * (1 + 0.35 * max(0, lvl - 1)))

def biz_upgrade_cost(base_cost: int, lvl: int) -> int:
    # cost grows
    return int(base_cost * (1.55 ** max(0, lvl - 1)))

# ---------- UI Keyboards ----------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус", callback_data="status"),
         InlineKeyboardButton("🎒 Инвентарь", callback_data="inv")],
        [InlineKeyboardButton("💼 Работа", callback_data="work_menu"),
         InlineKeyboardButton("🍜 Еда", callback_data="eat_menu")],
        [InlineKeyboardButton("🏢 Бизнес", callback_data="biz_menu"),
         InlineKeyboardButton("🪙 Крипта", callback_data="crypto_menu")],
        [InlineKeyboardButton("😴 Сон (новый день)", callback_data="sleep"),
         InlineKeyboardButton("🎲 Событие", callback_data="event")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])

def kb_eat() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎒 Съесть из инвентаря", callback_data="eat_inv"),
         InlineKeyboardButton("🍽️ Кафе (450₽)", callback_data="eat_cafe")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")]
    ])

def kb_work(st: Dict[str, Any]) -> InlineKeyboardMarkup:
    lvl = st["level"]
    rows = []
    for name, min_lvl, _, _, _, _ in JOBS:
        if lvl >= min_lvl:
            rows.append([InlineKeyboardButton(f"✅ {name} (с {min_lvl} lvl)", callback_data=f"job_set|{name}")])
        else:
            rows.append([InlineKeyboardButton(f"🔒 {name} (нужен {min_lvl} lvl)", callback_data="noop")])
    rows.append([InlineKeyboardButton("🔨 Работать сейчас", callback_data="work_do")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

def kb_biz_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Купить бизнес", callback_data="biz_shop"),
         InlineKeyboardButton("📈 Мои бизнесы", callback_data="biz_my")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")]
    ])

def kb_biz_shop(user_id: int) -> InlineKeyboardMarkup:
    owned = {r["biz_id"] for r in user_biz_list(user_id)}
    rows = []
    for biz_id, name, buy_price, base_inc, _ in BUSINESSES:
        if biz_id in owned:
            rows.append([InlineKeyboardButton(f"✅ {name} (уже есть)", callback_data="noop")])
        else:
            rows.append([InlineKeyboardButton(f"{name} — {buy_price}₽", callback_data=f"biz_buy|{biz_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="biz_menu")])
    return InlineKeyboardMarkup(rows)

def kb_biz_my(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    owned = user_biz_list(user_id)
    if not owned:
        rows.append([InlineKeyboardButton("Пусто 😅", callback_data="noop")])
    else:
        for r in owned:
            biz_id = r["biz_id"]
            name, _, base_inc, upc = biz_info(biz_id)
            lvl = int(r["biz_level"])
            inc = biz_income(base_inc, lvl)
            cost = biz_upgrade_cost(upc, lvl+1)
            rows.append([InlineKeyboardButton(f"{name} • lvl {lvl} • {inc}₽/день", callback_data=f"biz_view|{biz_id}")])
            rows.append([InlineKeyboardButton(f"⬆️ Апгрейд ({cost}₽)", callback_data=f"biz_up|{biz_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="biz_menu")])
    return InlineKeyboardMarkup(rows)

def kb_crypto_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📉 Рынок", callback_data="crypto_market"),
         InlineKeyboardButton("💼 Портфель", callback_data="crypto_port")],
        [InlineKeyboardButton("🟢 Купить", callback_data="crypto_buy_menu"),
         InlineKeyboardButton("🔴 Продать", callback_data="crypto_sell_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")]
    ])

def kb_crypto_pick(action: str) -> InlineKeyboardMarkup:
    # action = buy or sell
    rows = []
    for sym, icon, kind in ASSETS:
        if sym == "RUB":
            continue
        rows.append([InlineKeyboardButton(f"{icon} {sym}", callback_data=f"crypto_{action}|{sym}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="crypto_menu")])
    return InlineKeyboardMarkup(rows)

def kb_crypto_amount(action: str, sym: str) -> InlineKeyboardMarkup:
    # quick amounts in RUB for buy, units for sell (simplify)
    if action == "buy":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить на 500₽", callback_data=f"crypto_buy_do|{sym}|500"),
             InlineKeyboardButton("Купить на 2000₽", callback_data=f"crypto_buy_do|{sym}|2000")],
            [InlineKeyboardButton("Купить на 10000₽", callback_data=f"crypto_buy_do|{sym}|10000")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="crypto_buy_menu")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Продать 10%", callback_data=f"crypto_sell_do|{sym}|0.1"),
             InlineKeyboardButton("Продать 50%", callback_data=f"crypto_sell_do|{sym}|0.5")],
            [InlineKeyboardButton("Продать 100%", callback_data=f"crypto_sell_do|{sym}|1.0")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="crypto_sell_menu")]
        ])

# ---------- Actions ----------
def do_eat_inv(st: Dict[str, Any]) -> str:
    inv = inv_get(st)
    if inv.get("еда", 0) <= 0:
        return "🎒 Нет еды. Купи еду позже (можем добавить магазин — скажи)."
    inv["еда"] -= 1
    inv_set(st, inv)
    st["hunger"] = clamp(st["hunger"] + 35, 0, MAX_HUNGER)
    st["energy"] = clamp(st["energy"] + 5, 0, MAX_ENERGY)
    return "🍜 Поел(а) из инвентаря: сытость +35, энергия +5."

def do_eat_cafe(st: Dict[str, Any]) -> str:
    cost = 450
    if st["money"] < cost:
        return f"🍽️ Кафе стоит {cost}₽. Не хватает денег 😬"
    st["money"] -= cost
    st["hunger"] = clamp(st["hunger"] + 40, 0, MAX_HUNGER)
    st["energy"] = clamp(st["energy"] + 8, 0, MAX_ENERGY)
    return f"🍽️ Кафе: -{cost}₽, сытость +40, энергия +8."

def do_work(st: Dict[str, Any]) -> str:
    if st["job"] == "Безработный":
        return "💼 Сначала выбери работу в меню 💼 Работа."
    if st["energy"] < 20 or st["hunger"] < 15:
        return "😵 Слишком голоден/уставший. Поешь или поспи."

    job = next((j for j in JOBS if j[0] == st["job"]), None)
    if not job:
        st["job"] = "Безработный"
        return "🤔 Работа слетела. Выбери снова."
    name, min_lvl, base_pay, xp_gain, e_cost, h_cost = job
    if st["level"] < min_lvl:
        return f"🔒 Эта работа требует {min_lvl} lvl. Выбери другую."

    # pay random + small level bonus
    luck = random.randint(-200, 350)
    lvl_bonus = 1.0 + min(0.35, st["level"] * 0.01)
    earned = int(max(200, (base_pay + luck) * lvl_bonus))

    st["money"] += earned
    st["xp"] += xp_gain
    st["energy"] = clamp(st["energy"] - e_cost, 0, MAX_ENERGY)
    st["hunger"] = clamp(st["hunger"] - h_cost, 0, MAX_HUNGER)

    up = maybe_level_up(st)
    return f"🔨 Ты отработал(а) как *{name}*.\n💸 +{earned}₽ | 🧠 +{xp_gain} XP\n{up}".strip()

def do_sleep(st: Dict[str, Any], user_id: int) -> str:
    st["day"] += 1
    st["energy"] = clamp(st["energy"] + 55, 0, MAX_ENERGY)
    st["hunger"] = clamp(st["hunger"] - 10, 0, MAX_HUNGER)
    if st["hunger"] >= 40:
        st["health"] = clamp(st["health"] + 6, 0, MAX_HEALTH)

    # businesses pay per day
    total_income = 0
    owned = user_biz_list(user_id)
    for r in owned:
        biz_id = r["biz_id"]
        lvl = int(r["biz_level"])
        last_paid = int(r["last_paid_day"])
        if last_paid >= st["day"]:
            continue
        name, _, base_inc, _ = biz_info(biz_id)
        inc = biz_income(base_inc, lvl)
        total_income += inc
        user_biz_upsert(user_id, biz_id, lvl, st["day"])

    if total_income > 0:
        st["money"] += total_income
        return f"😴 Новый день 🌅\n🏢 Бизнесы принесли: +{total_income}₽"
    return "😴 Новый день 🌅"

def do_event(st: Dict[str, Any]) -> str:
    events = [
        ("🎁 Нашел кошелек", {"money": +600}),
        ("🚓 Штраф", {"money": -200}),
        ("🤕 Упал", {"health": -10}),
        ("☕ Угостили кофе", {"energy": +10}),
        ("🧑‍🎤 Подработка", {"money": +900, "energy": -10}),
    ]
    title, delta = random.choice(events)
    for k, v in delta.items():
        if k == "money":
            st["money"] = max(0, st["money"] + v)
        elif k == "health":
            st["health"] = clamp(st["health"] + v, 0, MAX_HEALTH)
        elif k == "energy":
            st["energy"] = clamp(st["energy"] + v, 0, MAX_ENERGY)
    return f"🎲 Событие: {title}"

def biz_buy(st: Dict[str, Any], user_id: int, biz_id: str) -> str:
    name, buy_price, _, _ = biz_info(biz_id)
    if user_biz_get(user_id, biz_id):
        return f"✅ {name} уже куплен."
    if st["money"] < buy_price:
        return f"❌ Не хватает денег на {name}. Нужно {buy_price}₽."
    st["money"] -= buy_price
    user_biz_upsert(user_id, biz_id, 1, st["day"])  # pay starts next day
    return f"🏢 Куплен бизнес: {name} ✅"

def biz_upgrade(st: Dict[str, Any], user_id: int, biz_id: str) -> str:
    r = user_biz_get(user_id, biz_id)
    name, _, base_inc, base_up = biz_info(biz_id)
    if not r:
        return "❌ У тебя нет этого бизнеса."
    lvl = int(r["biz_level"])
    cost = biz_upgrade_cost(base_up, lvl + 1)
    if st["money"] < cost:
        return f"❌ Апгрейд стоит {cost}₽. Не хватает денег."
    st["money"] -= cost
    new_lvl = lvl + 1
    user_biz_upsert(user_id, biz_id, new_lvl, int(r["last_paid_day"]))
    inc = biz_income(base_inc, new_lvl)
    return f"⬆️ {name} улучшен до lvl {new_lvl}. Теперь приносит ~{inc}₽/день."

def crypto_market_text() -> str:
    market_update_if_needed()
    lines = ["📉 *Рынок (в ₽)*"]
    for sym, icon, _ in ASSETS:
        if sym == "RUB": 
            continue
        p = get_price(sym)
        lines.append(f"{icon} {sym}: {p:,.2f} ₽".replace(",", " "))
    return "\n".join(lines)

def crypto_port_text(user_id: int) -> str:
    market_update_if_needed()
    port = portfolio_get(user_id)
    # show only non-zero
    items = [(a, amt) for a, amt in port.items() if abs(amt) > 1e-9]
    if not items:
        return "💼 Портфель пуст."
    total_rub = 0.0
    lines = ["💼 *Портфель*"]
    for asset, amt in items:
        if asset == "RUB":
            total_rub += amt
            lines.append(f"₽ RUB: {amt:,.2f}".replace(",", " "))
        else:
            p = get_price(asset)
            val = amt * p
            total_rub += val
            lines.append(f"{asset}: {amt:.6f} (~{val:,.2f} ₽)".replace(",", " "))
    lines.append(f"\n🧾 Итого ~{total_rub:,.2f} ₽".replace(",", " "))
    return "\n".join(lines)

def crypto_buy(st: Dict[str, Any], user_id: int, sym: str, rub_amount: int) -> str:
    market_update_if_needed()
    if rub_amount <= 0:
        return "❌ Сумма неверная."
    if st["money"] < rub_amount:
        return "❌ Не хватает денег."
    price = get_price(sym)
    units = rub_amount / price
    st["money"] -= rub_amount
    port = portfolio_get(user_id)
    port[sym] = port.get(sym, 0.0) + units
    portfolio_set(user_id, sym, port[sym])
    return f"🟢 Куплено {sym}: {units:.6f} на {rub_amount}₽"

def crypto_sell(st: Dict[str, Any], user_id: int, sym: str, fraction: float) -> str:
    market_update_if_needed()
    fraction = max(0.0, min(1.0, float(fraction)))
    port = portfolio_get(user_id)
    have = port.get(sym, 0.0)
    if have <= 0:
        return f"❌ У тебя нет {sym}."
    sell_units = have * fraction
    if sell_units <= 0:
        return "❌ Нечего продавать."
    price = get_price(sym)
    rub = sell_units * price
    port[sym] = have - sell_units
    portfolio_set(user_id, sym, port[sym])
    st["money"] += int(rub)
    return f"🔴 Продано {sym}: {sell_units:.6f} (~{int(rub)}₽)"

# ---------- Telegram ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = default_state()
    save_user(user_id, st)
    await update.message.reply_text(
        "🖤 *dark Life*\n\nТы приехал(а) на вокзал. У тебя *5000₽*.\nЖиви как в реальной жизни.\n\n"
        + render(st) + "\nВыбирай действие 👇",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/start — начать заново\n/help — помощь")

async def on_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    st = get_user(user_id) or default_state()

    note = apply_decay(st)
    if st["health"] <= 0:
        save_user(user_id, st)
        await q.edit_message_text("💀 Ты умер(ла). Нажми /start.")
        return

    data = q.data or ""
    msg = ""
    kb = kb_main()

    if data == "noop":
        msg = "🤐"
    elif data == "back":
        msg = "🏁 Главное меню"
    elif data == "status":
        msg = "📊 *Статус*\n\n" + render(st)
    elif data == "inv":
        inv = inv_get(st)
        msg = "🎒 *Инвентарь*\n" + "\n".join([f"• {k}: {v}" for k, v in inv.items()])
    elif data == "eat_menu":
        msg = "🍜 *Еда*"
        kb = kb_eat()
    elif data == "eat_inv":
        msg = do_eat_inv(st)
    elif data == "eat_cafe":
        msg = do_eat_cafe(st)

    elif data == "work_menu":
        msg = "💼 *Работа*\nВыбери работу по уровню, затем жми «Работать сейчас»."
        kb = kb_work(st)
    elif data.startswith("job_set|"):
        st["job"] = data.split("|", 1)[1]
        msg = f"✅ Выбрана работа: *{st['job']}*"
        kb = kb_work(st)
    elif data == "work_do":
        msg = do_work(st)
        kb = kb_work(st)

    elif data == "biz_menu":
        msg = "🏢 *Бизнес*"
        kb = kb_biz_menu()
    elif data == "biz_shop":
        msg = "🛒 *Купить бизнес*"
        kb = kb_biz_shop(user_id)
    elif data == "biz_my":
        msg = "📈 *Мои бизнесы*"
        kb = kb_biz_my(user_id)
    elif data.startswith("biz_buy|"):
        biz_id = data.split("|", 1)[1]
        msg = biz_buy(st, user_id, biz_id)
        kb = kb_biz_shop(user_id)
    elif data.startswith("biz_up|"):
        biz_id = data.split("|", 1)[1]
        msg = biz_upgrade(st, user_id, biz_id)
        kb = kb_biz_my(user_id)
    elif data.startswith("biz_view|"):
        biz_id = data.split("|", 1)[1]
        name, _, base_inc, base_up = biz_info(biz_id)
        r = user_biz_get(user_id, biz_id)
        if not r:
            msg = "❌ Нет такого бизнеса."
        else:
            lvl = int(r["biz_level"])
            inc = biz_income(base_inc, lvl)
            cost = biz_upgrade_cost(base_up, lvl + 1)
            msg = f"{name}\n📈 Уровень: {lvl}\n💵 Доход: ~{inc}₽/день\n⬆️ Апгрейд: {cost}₽"
        kb = kb_biz_my(user_id)

    elif data == "crypto_menu":
        msg = "🪙 *Крипта*"
        kb = kb_crypto_menu()
    elif data == "crypto_market":
        msg = crypto_market_text()
        kb = kb_crypto_menu()
    elif data == "crypto_port":
        msg = crypto_port_text(user_id)
        kb = kb_crypto_menu()
    elif data == "crypto_buy_menu":
        msg = "🟢 *Купить* — выбери актив"
        kb = kb_crypto_pick("buy")
    elif data == "crypto_sell_menu":
        msg = "🔴 *Продать* — выбери актив"
        kb = kb_crypto_pick("sell")
    elif data.startswith("crypto_buy|"):
        sym = data.split("|", 1)[1]
        msg = f"🟢 Купить {sym}: выбери сумму"
        kb = kb_crypto_amount("buy", sym)
    elif data.startswith("crypto_sell|"):
        sym = data.split("|", 1)[1]
        msg = f"🔴 Продать {sym}: выбери долю"
        kb = kb_crypto_amount("sell", sym)
    elif data.startswith("crypto_buy_do|"):
        _, sym, amt = data.split("|")
        msg = crypto_buy(st, user_id, sym, int(float(amt)))
        kb = kb_crypto_menu()
    elif data.startswith("crypto_sell_do|"):
        _, sym, frac = data.split("|")
        msg = crypto_sell(st, user_id, sym, float(frac))
        kb = kb_crypto_menu()

    elif data == "sleep":
        msg = do_sleep(st, user_id)
    elif data == "event":
        msg = do_event(st)

    else:
        msg = "🤔 Не понял кнопку."

    save_user(user_id, st)

    full = (
        "🖤 *dark Life*\n"
        + (f"{note}\n\n" if note else "")
        + msg
        + "\n\n"
        + render(st)
        + "\nВыбирай действие 👇"
    )
    await q.edit_message_text(full, parse_mode="Markdown", reply_markup=kb)

def main() -> None:
    if not TOKEN:
        raise SystemExit("Set DARKLIFE_TOKEN env var.")
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_btn))
    print("🖤 dark Life running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
