# 🖤 dark Life — life-sim Telegram bot (Python)
# Install: pip install -U python-telegram-bot==21.6
#
# Run (Windows CMD):
#   set DARKLIFE_TOKEN=YOUR_TOKEN
#   python dark_life_bot.py
#
# Persistence: SQLite (darklife.db)
# Buttons: status/inventory/work/eat/shop/rent/bank/city/sleep/event/top

import os
import json
import time
import random
import sqlite3
from typing import Dict, Any, Tuple, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

DB_PATH = os.environ.get("DARKLIFE_DB", "darklife.db")
TOKEN = os.environ.get("DARKLIFE_TOKEN", "")

# ---------- Game tuning ----------
START_MONEY = 5000
MAX_HEALTH = 100
MAX_HUNGER = 100   # 0 = starving, 100 = full
MAX_ENERGY = 100
MAX_MOOD = 100
MAX_STRESS = 100

# time decay per hour (applied on user interaction based on last_seen timestamp)
HUNGER_DECAY_PER_HOUR = 6
ENERGY_DECAY_PER_HOUR = 4
STRESS_GROW_PER_HOUR = 2
MOOD_DROP_PER_HOUR = 1

# bank interest per in-game day (on sleep)
BANK_DAILY_INTEREST = 0.01  # 1%

# ---------- SQLite ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    def add_col(name: str, sql_type: str, default_sql: str) -> None:
        if name not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {sql_type} NOT NULL DEFAULT {default_sql}")

    add_col("mood", "INTEGER", "60")
    add_col("stress", "INTEGER", "20")
    add_col("bank", "INTEGER", "0")

def init_db() -> None:
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            money INTEGER NOT NULL,
            health INTEGER NOT NULL,
            hunger INTEGER NOT NULL,
            energy INTEGER NOT NULL,
            day INTEGER NOT NULL,
            location TEXT NOT NULL,
            job TEXT NOT NULL,
            inventory TEXT NOT NULL,
            last_seen INTEGER NOT NULL
        );
        """)
        ensure_columns(conn)
        conn.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def upsert_user(user_id: int, data: Dict[str, Any]) -> None:
    with db() as conn:
        conn.execute("""
        INSERT INTO users (
            user_id, money, health, hunger, energy, day, location, job, inventory, last_seen, mood, stress, bank
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            money=excluded.money,
            health=excluded.health,
            hunger=excluded.hunger,
            energy=excluded.energy,
            day=excluded.day,
            location=excluded.location,
            job=excluded.job,
            inventory=excluded.inventory,
            last_seen=excluded.last_seen,
            mood=excluded.mood,
            stress=excluded.stress,
            bank=excluded.bank
        """, (
            user_id,
            data["money"], data["health"], data["hunger"], data["energy"],
            data["day"], data["location"], data["job"], data["inventory"],
            data["last_seen"], data.get("mood", 60), data.get("stress", 20), data.get("bank", 0)
        ))
        conn.commit()

# ---------- Helpers ----------
def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def now_ts() -> int:
    return int(time.time())

def default_state() -> Dict[str, Any]:
    return {
        "money": START_MONEY,
        "bank": 0,
        "health": 90,
        "hunger": 70,
        "energy": 80,
        "mood": 60,
        "stress": 20,
        "day": 1,
        "location": "🚉 Вокзал",
        "job": "Безработный",
        "inventory": json.dumps({"еда": 0, "аптечка": 0, "билет": 0}, ensure_ascii=False),
        "last_seen": now_ts(),
    }

def get_inventory(state: Dict[str, Any]) -> Dict[str, int]:
    try:
        return json.loads(state["inventory"])
    except Exception:
        return {"еда": 0, "аптечка": 0, "билет": 0}

def set_inventory(state: Dict[str, Any], inv: Dict[str, int]) -> None:
    state["inventory"] = json.dumps(inv, ensure_ascii=False)

def is_dead(state: Dict[str, Any]) -> bool:
    return state["health"] <= 0

def apply_time_decay(state: Dict[str, Any], last_seen: int) -> Tuple[Dict[str, Any], str]:
    """Apply hunger/energy/stress/mood changes based on real time passed since last interaction."""
    dt = max(0, now_ts() - last_seen)
    hours = dt / 3600.0
    if hours < 0.2:
        state["last_seen"] = now_ts()
        return state, ""

    hunger_loss = int(hours * HUNGER_DECAY_PER_HOUR)
    energy_loss = int(hours * ENERGY_DECAY_PER_HOUR)
    stress_gain = int(hours * STRESS_GROW_PER_HOUR)
    mood_drop = int(hours * MOOD_DROP_PER_HOUR)

    if any([hunger_loss, energy_loss, stress_gain, mood_drop]):
        state["hunger"] = clamp(state["hunger"] - hunger_loss, 0, MAX_HUNGER)
        state["energy"] = clamp(state["energy"] - energy_loss, 0, MAX_ENERGY)
        state["stress"] = clamp(state.get("stress", 20) + stress_gain, 0, MAX_STRESS)
        state["mood"] = clamp(state.get("mood", 60) - mood_drop, 0, MAX_MOOD)

        hp_loss = 0
        if state["hunger"] <= 10:
            hp_loss += int(hours * 3)
        if state["energy"] <= 10:
            hp_loss += int(hours * 2)
        if state.get("stress", 0) >= 85:
            hp_loss += int(hours * 2)

        state["health"] = clamp(state["health"] - hp_loss, 0, MAX_HEALTH)

    state["last_seen"] = now_ts()

    note = f"⏳ Прошло ~{hours:.1f} ч.: голод -{hunger_loss}, энергия -{energy_loss}, стресс +{stress_gain}, настроение -{mood_drop}."
    if state["health"] == 0:
        note += "\n💀 Ты умер(ла). Нажми /start чтобы начать заново."
    return state, note

def render_state(state: Dict[str, Any]) -> str:
    return (
        f"📍 Локация: {state['location']}\n"
        f"📅 День: {state['day']}\n"
        f"💼 Работа: {state['job']}\n\n"
        f"💵 Наличные: {state['money']} ₽\n"
        f"🏦 Банк: {state.get('bank', 0)} ₽\n\n"
        f"❤️ Здоровье: {state['health']}/{MAX_HEALTH}\n"
        f"🍗 Сытость: {state['hunger']}/{MAX_HUNGER}\n"
        f"⚡ Энергия: {state['energy']}/{MAX_ENERGY}\n"
        f"🙂 Настроение: {state.get('mood', 60)}/{MAX_MOOD}\n"
        f"😰 Стресс: {state.get('stress', 20)}/{MAX_STRESS}\n"
    )

# ---------- Keyboards ----------
def kb_main() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Статус", callback_data="status"),
         InlineKeyboardButton("🎒 Инвентарь", callback_data="inv")],
        [InlineKeyboardButton("💼 Работа", callback_data="work"),
         InlineKeyboardButton("🍜 Поесть", callback_data="eat_menu")],
        [InlineKeyboardButton("🛒 Магазин", callback_data="shop_menu"),
         InlineKeyboardButton("🏠 Жильё", callback_data="rent_menu")],
        [InlineKeyboardButton("🏦 Банк", callback_data="bank_menu"),
         InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("🗺️ Город", callback_data="city"),
         InlineKeyboardButton("😴 Сон", callback_data="sleep")],
        [InlineKeyboardButton("🎲 Случай", callback_data="event")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]])

def kb_shop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍞 Еда — 300₽", callback_data="buy_food"),
         InlineKeyboardButton("🩹 Аптечка — 650₽", callback_data="buy_med")],
        [InlineKeyboardButton("🎫 Билет — 900₽", callback_data="buy_ticket")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

def kb_rent() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛏️ Хостел (700₽)", callback_data="rent_hostel"),
         InlineKeyboardButton("🚪 Комната (1200₽)", callback_data="rent_room")],
        [InlineKeyboardButton("🏢 Квартира (2400₽)", callback_data="rent_flat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

def kb_bank() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Положить 1000₽", callback_data="bank_deposit_1000"),
         InlineKeyboardButton("➖ Снять 1000₽", callback_data="bank_withdraw_1000")],
        [InlineKeyboardButton("➕ Положить всё", callback_data="bank_deposit_all"),
         InlineKeyboardButton("➖ Снять всё", callback_data="bank_withdraw_all")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

def kb_eat() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎒 Съесть из инвентаря", callback_data="eat_inv"),
         InlineKeyboardButton("🍽️ Кафе (450₽)", callback_data="eat_cafe")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

# ---------- Core game actions ----------
def do_work(state: Dict[str, Any]) -> str:
    if state["energy"] < 20 or state["hunger"] < 15:
        return "😵 Ты слишком голоден/уставший, чтобы работать. Поешь или поспишь."

    jobs = [
        ("Разнорабочий", 800, 18, 12),
        ("Курьер", 1100, 22, 15),
        ("Бариста", 1400, 25, 14),
    ]
    if state["job"] == "Безработный":
        state["job"] = random.choice([j[0] for j in jobs])

    j = next((x for x in jobs if x[0] == state["job"]), jobs[0])
    base_pay, e_cost, h_cost = j[1], j[2], j[3]

    # mood/stress affect pay
    mood = state.get("mood", 60)
    stress = state.get("stress", 20)
    pay_mult = 1.0
    if mood >= 75:
        pay_mult += 0.10
    if mood <= 25:
        pay_mult -= 0.12
    if stress >= 70:
        pay_mult -= 0.15
    if stress <= 20:
        pay_mult += 0.05

    luck = random.randint(-150, 250)
    earned = int(max(200, (base_pay + luck) * pay_mult))

    state["money"] += earned
    state["energy"] = clamp(state["energy"] - e_cost, 0, MAX_ENERGY)
    state["hunger"] = clamp(state["hunger"] - h_cost, 0, MAX_HUNGER)

    # stress up, mood down a bit
    state["stress"] = clamp(stress + 6, 0, MAX_STRESS)
    state["mood"] = clamp(mood - 2, 0, MAX_MOOD)

    return f"💼 Работа: {state['job']}\n✅ Заработок: +{earned} ₽ (учтены настроение/стресс + удача 🎲)"

def do_eat_inv(state: Dict[str, Any]) -> str:
    inv = get_inventory(state)
    if inv.get("еда", 0) <= 0:
        return "🎒 В инвентаре нет еды. Купи 🍞 в магазине."
    inv["еда"] -= 1
    set_inventory(state, inv)
    state["hunger"] = clamp(state["hunger"] + 35, 0, MAX_HUNGER)
    state["energy"] = clamp(state["energy"] + 5, 0, MAX_ENERGY)
    state["mood"] = clamp(state.get("mood", 60) + 4, 0, MAX_MOOD)
    state["stress"] = clamp(state.get("stress", 20) - 3, 0, MAX_STRESS)
    return "🍜 Ты поел(а) (инвентарь): сытость +35, энергия +5, настроение +4, стресс -3."

def do_eat_cafe(state: Dict[str, Any]) -> str:
    cost = 450
    if state["money"] < cost:
        return f"🍽️ В кафе стоит {cost} ₽. Денег не хватает 😬"
    state["money"] -= cost
    state["hunger"] = clamp(state["hunger"] + 40, 0, MAX_HUNGER)
    state["energy"] = clamp(state["energy"] + 8, 0, MAX_ENERGY)
    state["mood"] = clamp(state.get("mood", 60) + 8, 0, MAX_MOOD)
    state["stress"] = clamp(state.get("stress", 20) - 5, 0, MAX_STRESS)
    return f"🍽️ Кафе: -{cost} ₽, сытость +40, энергия +8, настроение +8, стресс -5."

def do_sleep(state: Dict[str, Any]) -> str:
    state["day"] += 1

    # bank interest
    bank = int(state.get("bank", 0))
    interest = int(bank * BANK_DAILY_INTEREST)
    if interest > 0:
        state["bank"] = bank + interest

    state["energy"] = clamp(state["energy"] + 55, 0, MAX_ENERGY)
    state["hunger"] = clamp(state["hunger"] - 10, 0, MAX_HUNGER)

    # mood +, stress -
    state["mood"] = clamp(state.get("mood", 60) + 10, 0, MAX_MOOD)
    state["stress"] = clamp(state.get("stress", 20) - 12, 0, MAX_STRESS)

    # heal if not starving
    if state["hunger"] >= 40:
        state["health"] = clamp(state["health"] + 6, 0, MAX_HEALTH)

    extra = f"\n🏦 Банк начислил: +{interest} ₽" if interest > 0 else ""
    return "😴 Сон: энергия +55, здоровье +6 (если не голоден), сытость -10, настроение +10, стресс -12. Новый день 🌅" + extra

def rent_apply(state: Dict[str, Any], name: str, price: int, energy_bonus: int, mood_bonus: int, stress_delta: int) -> str:
    if state["money"] < price:
        return f"🏚️ {name} стоит {price} ₽. Не хватает денег 😬"
    state["money"] -= price
    state["energy"] = clamp(state["energy"] + energy_bonus, 0, MAX_ENERGY)
    state["mood"] = clamp(state.get("mood", 60) + mood_bonus, 0, MAX_MOOD)
    state["stress"] = clamp(state.get("stress", 20) + stress_delta, 0, MAX_STRESS)
    state["location"] = "🏠 Дом"
    return f"🏠 {name}: -{price} ₽, энергия +{energy_bonus}, настроение +{mood_bonus}, стресс {stress_delta:+}."

def buy_item(state: Dict[str, Any], item: str, price: int, title: str) -> str:
    if state["money"] < price:
        return f"🛒 {title} стоит {price} ₽. Не хватает денег 😬"
    inv = get_inventory(state)
    inv[item] = inv.get(item, 0) + 1
    set_inventory(state, inv)
    state["money"] -= price
    return f"🛒 Куплено: {title} (-{price} ₽)."

def bank_deposit(state: Dict[str, Any], amount: int) -> str:
    if amount <= 0:
        return "🏦 Сумма должна быть > 0."
    if state["money"] < amount:
        return "🏦 Не хватает наличных."
    state["money"] -= amount
    state["bank"] = int(state.get("bank", 0)) + amount
    return f"🏦 Положил(а) в банк: +{amount} ₽."

def bank_withdraw(state: Dict[str, Any], amount: int) -> str:
    bank = int(state.get("bank", 0))
    if amount <= 0:
        return "🏦 Сумма должна быть > 0."
    if bank < amount:
        return "🏦 Не хватает денег на счёте."
    state["bank"] = bank - amount
    state["money"] += amount
    return f"🏦 Снял(а) с банка: +{amount} ₽ наличными."

def do_city(state: Dict[str, Any]) -> str:
    inv = get_inventory(state)
    if inv.get("билет", 0) <= 0:
        return "🗺️ Для поездки нужен 🎫 билет. Купи его в магазине."
    inv["билет"] -= 1
    set_inventory(state, inv)

    places = ["🏙️ Центр", "🏭 Промзона", "🌳 Парк", "🎡 Площадь", "🧱 Спальник"]
    state["location"] = random.choice(places)

    money_delta = random.randint(-250, 450)
    state["money"] = max(0, state["money"] + money_delta)
    state["energy"] = clamp(state["energy"] - 10, 0, MAX_ENERGY)
    state["hunger"] = clamp(state["hunger"] - 8, 0, MAX_HUNGER)

    # mood/stress swing
    state["mood"] = clamp(state.get("mood", 60) + random.randint(-4, 6), 0, MAX_MOOD)
    state["stress"] = clamp(state.get("stress", 20) + random.randint(-2, 6), 0, MAX_STRESS)

    return f"🗺️ Ты поехал(а) в {state['location']}.\n💸 По дороге деньги: {money_delta:+} ₽"

def do_event(state: Dict[str, Any]) -> str:
    inv = get_inventory(state)

    def apply(delta: Dict[str, int]) -> None:
        for k, v in delta.items():
            if k == "money":
                state["money"] = max(0, state["money"] + v)
            elif k == "health":
                state["health"] = clamp(state["health"] + v, 0, MAX_HEALTH)
            elif k == "energy":
                state["energy"] = clamp(state["energy"] + v, 0, MAX_ENERGY)
            elif k == "hunger":
                state["hunger"] = clamp(state["hunger"] + v, 0, MAX_HUNGER)
            elif k == "mood":
                state["mood"] = clamp(state.get("mood", 60) + v, 0, MAX_MOOD)
            elif k == "stress":
                state["stress"] = clamp(state.get("stress", 20) + v, 0, MAX_STRESS)

    events = [
        ("🧑‍🎤 Позвали подработать на концерте", "+900 ₽", {"money": +900, "energy": -12, "hunger": -6, "stress": +5, "mood": +3}),
        ("🚓 Проверка документов", "-200 ₽ (штраф)", {"money": -200, "stress": +8, "mood": -2}),
        ("🤕 Подвернул(а) ногу", "-12 ❤️", {"health": -12, "stress": +6, "mood": -4}),
        ("🎁 Нашел(ла) кошелек", "+600 ₽", {"money": +600, "mood": +4}),
        ("☕ Угостили кофе", "+10 ⚡", {"energy": +10, "mood": +2, "stress": -2}),
        ("🗯️ Ссора на улице", "-6 🙂, +10 😰", {"mood": -6, "stress": +10}),
        ("🧘 Нашел(ла) тихое место и выдохнул(а)", "+6 🙂, -8 😰", {"mood": +6, "stress": -8}),
    ]

    title, label, delta = random.choice(events)

    healed = ""
    if delta.get("health", 0) < 0 and inv.get("аптечка", 0) > 0:
        inv["аптечка"] -= 1
        set_inventory(state, inv)
        apply({"health": +9, "stress": -2})
        healed = " (использована 🩹 аптечка: +9 ❤️)"

    apply(delta)
    return f"🎲 Событие: {title}\nРезультат: {label}{healed}"

# ---------- Top ----------
def get_top_text(limit: int = 10) -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, money, bank, (money + bank) as total FROM users ORDER BY total DESC LIMIT ?",
            (limit,)
        ).fetchall()

    if not rows:
        return "🏆 Пока никого нет в топе."
    lines = ["🏆 *Топ игроков (нал+банк)*"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. ID `{r['user_id']}` — {r['total']} ₽ (нал {r['money']}, банк {r['bank']})")
    return "\n".join(lines)

# ---------- Telegram handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = default_state()
    upsert_user(user_id, st)

    text = (
        "🖤 *dark Life*\n\n"
        "Ты приезжаешь на вокзал. Тебе выдали *5000 ₽* — дальше выживай как в жизни.\n\n"
        + render_state(st)
        + "\nВыбирай действие кнопками 👇"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — новая жизнь\n"
        "/help — помощь\n\n"
        "Игра идёт через кнопки. Состояние сохраняется ✅"
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    row = get_user(user_id)
    st = dict(row) if row else default_state()

    # Apply real-time decay
    st, decay_note = apply_time_decay(st, st.get("last_seen", now_ts()))

    if is_dead(st):
        upsert_user(user_id, st)
        await q.edit_message_text("💀 Ты умер(ла). Нажми /start чтобы начать заново.")
        return

    action = q.data
    msg = ""
    reply_kb = kb_main()

    # navigation
    if action == "back_main":
        msg = "🏁 Главное меню"
        reply_kb = kb_main()

    # main
    elif action == "status":
        msg = "📊 *Твой статус*\n\n" + render_state(st)

    elif action == "inv":
        inv = get_inventory(st)
        msg = "🎒 *Инвентарь*\n" + "\n".join([f"• {k}: {v}" for k, v in inv.items()])

    elif action == "work":
        msg = do_work(st)

    elif action == "eat_menu":
        msg = "🍜 *Еда*\nВыбирай:"
        reply_kb = kb_eat()

    elif action == "eat_inv":
        msg = do_eat_inv(st)
        reply_kb = kb_main()

    elif action == "eat_cafe":
        msg = do_eat_cafe(st)
        reply_kb = kb_main()

    elif action == "shop_menu":
        msg = "🛒 *Магазин*\nВыбирай покупку:"
        reply_kb = kb_shop()

    elif action == "buy_food":
        msg = buy_item(st, "еда", 300, "🍞 Еда")
        reply_kb = kb_shop()

    elif action == "buy_med":
        msg = buy_item(st, "аптечка", 650, "🩹 Аптечка")
        reply_kb = kb_shop()

    elif action == "buy_ticket":
        msg = buy_item(st, "билет", 900, "🎫 Билет")
        reply_kb = kb_shop()

    elif action == "rent_menu":
        msg = "🏠 *Жильё*\nВыбирай где переночевать:"
        reply_kb = kb_rent()

    elif action == "rent_hostel":
        msg = rent_apply(st, "Ночь в хостеле", 700, energy_bonus=20, mood_bonus=2, stress_delta=-3)
        reply_kb = kb_rent()

    elif action == "rent_room":
        msg = rent_apply(st, "Комната на сутки", 1200, energy_bonus=30, mood_bonus=4, stress_delta=-5)
        reply_kb = kb_rent()

    elif action == "rent_flat":
        msg = rent_apply(st, "Квартира на сутки", 2400, energy_bonus=45, mood_bonus=7, stress_delta=-8)
        reply_kb = kb_rent()

    elif action == "bank_menu":
        msg = "🏦 *Банк*\nМожно хранить деньги и получать +1% за день (при сне)."
        reply_kb = kb_bank()

    elif action == "bank_deposit_1000":
        msg = bank_deposit(st, 1000)
        reply_kb = kb_bank()

    elif action == "bank_withdraw_1000":
        msg = bank_withdraw(st, 1000)
        reply_kb = kb_bank()

    elif action == "bank_deposit_all":
        amt = int(st["money"])
        msg = bank_deposit(st, amt) if amt > 0 else "🏦 Нечего класть."
        reply_kb = kb_bank()

    elif action == "bank_withdraw_all":
        amt = int(st.get("bank", 0))
        msg = bank_withdraw(st, amt) if amt > 0 else "🏦 Нечего снимать."
        reply_kb = kb_bank()

    elif action == "city":
        msg = do_city(st)

    elif action == "sleep":
        msg = do_sleep(st)

    elif action == "event":
        msg = do_event(st)

    elif action == "top":
        msg = get_top_text(10)
        reply_kb = kb_back()

    else:
        msg = "🤔 Неизвестное действие."

    # If dead after action
    if is_dead(st):
        msg += "\n\n💀 Ты умер(ла). Нажми /start чтобы начать заново."

    upsert_user(user_id, st)

    full = (
        "🖤 *dark Life*\n"
        + (f"{decay_note}\n\n" if decay_note else "")
        + msg
        + "\n\n"
        + render_state(st)
        + "\nВыбирай действие 👇"
    )

    await q.edit_message_text(full, parse_mode="Markdown", reply_markup=reply_kb)

def main() -> None:
    if not TOKEN:
        raise SystemExit("Set env var DARKLIFE_TOKEN with your Telegram bot token.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))

    print("🖤 dark Life is running... (Ctrl+C to stop)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
