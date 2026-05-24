import asyncio
import logging
import asyncpg
from datetime import datetime, timedelta
from decimal import Decimal
from os import getenv
from zoneinfo import ZoneInfo  # для часового пояса

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = getenv("TELEGRAM_TOKEN")
ADMIN_PASSWORD = getenv("ADMIN_PASSWORD")
DATABASE_URL = getenv("DATABASE_URL")

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы TELEGRAM_TOKEN или DATABASE_URL")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальный пул соединений с БД
db_pool = None

# Часовой пояс Барнаул (UTC+7)
TZ = ZoneInfo("Asia/Barnaul")

def now_local():
    """Возвращает текущее время в Барнауле"""
    return datetime.now(TZ)

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    # Таблицы уже есть, но для надёжности проверим
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS croupiers (
                tg_id BIGINT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                city TEXT NOT NULL,
                level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
                hourly_rate INTEGER NOT NULL,
                tg_username TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS shifts (
                id SERIAL PRIMARY KEY,
                croupier_tg_id BIGINT REFERENCES croupiers(tg_id) ON DELETE CASCADE,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                hours_worked REAL,
                salary REAL,
                status TEXT DEFAULT 'active'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS managers (
                tg_id BIGINT PRIMARY KEY,
                city TEXT NOT NULL,
                role TEXT DEFAULT 'tournament_manager'
            )
        ''')

# ---------- Функции работы с БД (асинхронные) ----------

async def get_croupier(tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tg_id, first_name, last_name, city, level, hourly_rate, tg_username FROM croupiers WHERE tg_id = $1",
            tg_id
        )
        if row:
            return dict(row)
    return None

async def get_all_croupiers():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id, first_name, last_name, city, level, hourly_rate, tg_username FROM croupiers ORDER BY city, last_name")
        return [dict(r) for r in rows]

async def add_croupier(tg_id, first_name, last_name, city, level, hourly_rate, tg_username=None):
    async with db_pool.acquire() as conn:
        try:
            await conn.execute('''
                INSERT INTO croupiers (tg_id, first_name, last_name, city, level, hourly_rate, tg_username)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', tg_id, first_name, last_name, city, level, hourly_rate, tg_username)
            return True
        except asyncpg.UniqueViolationError:
            return False

async def delete_croupier(tg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM croupiers WHERE tg_id = $1", tg_id)

async def get_active_shift(croupier_tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, start_time, status FROM shifts WHERE croupier_tg_id = $1 AND status = 'active'",
            croupier_tg_id
        )
        if row:
            return {"id": row["id"], "start_time": row["start_time"], "status": row["status"]}
    return None

async def start_shift(croupier_tg_id: int):
    async with db_pool.acquire() as conn:
        now = now_local()
        row = await conn.fetchrow(
            "INSERT INTO shifts (croupier_tg_id, start_time, status) VALUES ($1, $2, 'active') RETURNING id",
            croupier_tg_id, now
        )
        shift_id = row["id"]
        return shift_id, now

async def close_shift(croupier_tg_id: int, hourly_rate: float):
    active = await get_active_shift(croupier_tg_id)
    if not active:
        return None
    end_time = now_local()
    start_time = active["start_time"]
    # start_time уже имеет часовой пояс, так как мы сохранили как timestamp with time zone
    hours_worked = (end_time - start_time).total_seconds() / 3600.0
    salary = Decimal(str(hours_worked)) * Decimal(str(hourly_rate))
    salary = round(salary, 2)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            UPDATE shifts
            SET end_time = $1, hours_worked = $2, salary = $3, status = 'closed'
            WHERE id = $4
        ''', end_time, hours_worked, float(salary), active["id"])
    return hours_worked, float(salary), start_time, end_time

async def get_shifts_for_manager(city: str = None, limit=50):
    async with db_pool.acquire() as conn:
        if city:
            rows = await conn.fetch('''
                SELECT s.id, c.first_name, c.last_name, c.city, s.start_time, s.end_time, s.hours_worked, s.salary
                FROM shifts s
                JOIN croupiers c ON s.croupier_tg_id = c.tg_id
                WHERE c.city = $1 AND s.status = 'closed'
                ORDER BY s.start_time DESC
                LIMIT $2
            ''', city, limit)
        else:
            rows = await conn.fetch('''
                SELECT s.id, c.first_name, c.last_name, c.city, s.start_time, s.end_time, s.hours_worked, s.salary
                FROM shifts s
                JOIN croupiers c ON s.croupier_tg_id = c.tg_id
                WHERE s.status = 'closed'
                ORDER BY s.start_time DESC
                LIMIT $1
            ''', limit)
        return rows

# Новая функция расчёта зарплаты за текущий период (1–15 или 15–конец месяца)
async def get_croupier_salary_current_period(tg_id: int):
    now = now_local()
    day = now.day
    if day < 15:
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        period_start = now.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
    async with db_pool.acquire() as conn:
        total = await conn.fetchval('''
            SELECT COALESCE(SUM(salary), 0) FROM shifts
            WHERE croupier_tg_id = $1 AND status = 'closed' AND start_time >= $2
        ''', tg_id, period_start)
        return total, period_start

async def is_manager(tg_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchval("SELECT 1 FROM managers WHERE tg_id = $1", tg_id)
        return row is not None

async def get_manager_city(tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchval("SELECT city FROM managers WHERE tg_id = $1", tg_id)
        return row

async def get_managers_by_city(city: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM managers WHERE city = $1", city)
        return [r["tg_id"] for r in rows]

# ---------- Клавиатуры ----------
def main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Список крупье")],
        [KeyboardButton(text="✏️ Изменить данные крупье")],
        [KeyboardButton(text="⏱ Начать смену"), KeyboardButton(text="⏹ Закрыть смену")],
        [KeyboardButton(text="💰 Расчет ЗП"), KeyboardButton(text="📊 Таблица")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

admin_menu_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="➕ Добавить крупье"), KeyboardButton(text="❌ Удалить крупье")],
    [KeyboardButton(text="🔙 Назад")]
], resize_keyboard=True)

def get_croupiers_list_inline(croupiers_list):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for c in croupiers_list:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{c['first_name']} {c['last_name']} ({c['city']})",
                                                        callback_data=f"del_{c['tg_id']}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_del")])
    return kb

# ---------- FSM состояния ----------
class AdminStates(StatesGroup):
    waiting_password = State()
    adding_first_name = State()
    adding_last_name = State()
    adding_city = State()
    adding_level = State()
    adding_tg_id = State()
    adding_username = State()

# ---------- Хендлеры ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать, крупье! 👋\n\n"
        "Используй кнопки меню для работы:\n"
        "• Начать / закрыть смену\n"
        "• Посмотреть список крупье\n"
        "• Рассчитать ЗП\n"
        "• Изменить данные крупье (требуется пароль)\n"
        "• Таблица смен (только для турнирных менеджеров)",
        reply_markup=main_keyboard()
    )

@dp.message(F.text == "📋 Список крупье")
async def list_croupiers(message: Message):
    croupiers = await get_all_croupiers()
    if not croupiers:
        await message.answer("Список крупье пуст.")
        return
    text = "📋 Список крупье:\n\n"
    for c in croupiers:
        text += f"👤 {c['first_name']} {c['last_name']} (@{c['tg_username'] or 'нет'})\n"
        text += f"   🏙 {c['city']} | Уровень {c['level']} | 💵 {c['hourly_rate']}₽/час\n"
        text += f"   🆔 TG ID: {c['tg_id']}\n\n"
    await message.answer(text)

@dp.message(F.text == "✏️ Изменить данные крупье")
async def change_croupier_data(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_password)
    await message.answer("Введите пароль администратора для изменения данных крупье:")

@dp.message(AdminStates.waiting_password)
async def check_password(message: Message, state: FSMContext):
    if message.text == ADMIN_PASSWORD:
        await state.clear()
        await message.answer("✅ Доступ разрешён. Выберите действие:", reply_markup=admin_menu_kb)
    else:
        await message.answer("❌ Неверный пароль. Доступ запрещён.")
        await state.clear()

@dp.message(F.text == "➕ Добавить крупье")
async def add_croupier_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.adding_first_name)
    await message.answer("Введите имя крупье:")

@dp.message(AdminStates.adding_first_name)
async def add_first_name(message: Message, state: FSMContext):
    await state.update_data(first_name=message.text)
    await state.set_state(AdminStates.adding_last_name)
    await message.answer("Введите фамилию крупье:")

@dp.message(AdminStates.adding_last_name)
async def add_last_name(message: Message, state: FSMContext):
    await state.update_data(last_name=message.text)
    await state.set_state(AdminStates.adding_city)
    await message.answer("Введите город (например: Москва, Барнаул):")

@dp.message(AdminStates.adding_city)
async def add_city(message: Message, state: FSMContext):
    await state.update_data(city=message.text)
    await state.set_state(AdminStates.adding_level)
    await message.answer("Введите уровень крупье (число от 1 до 5):\n"
                         "1 → 250₽/ч\n2 → 300₽/ч\n3 → 350₽/ч\n4 → 400₽/ч\n5 → 450₽/ч")

@dp.message(AdminStates.adding_level)
async def add_level(message: Message, state: FSMContext):
    try:
        level = int(message.text)
        if level not in range(1, 6):
            raise ValueError
        rate = {1:250, 2:300, 3:350, 4:400, 5:450}[level]
        await state.update_data(level=level, hourly_rate=rate)
        await state.set_state(AdminStates.adding_tg_id)
        await message.answer("Введите Telegram ID крупье (число).\nКак узнать ID: отправьте @userinfobot")
    except ValueError:
        await message.answer("Ошибка. Введите число от 1 до 5.")

@dp.message(AdminStates.adding_tg_id)
async def add_tg_id(message: Message, state: FSMContext):
    try:
        tg_id = int(message.text)
        if await get_croupier(tg_id):
            await message.answer("Крупье с таким Telegram ID уже существует! Используйте другой ID или удалите старого.")
            return
        await state.update_data(tg_id=tg_id)
        await state.set_state(AdminStates.adding_username)
        await message.answer("Введите @username крупье (можно без @ или просто '-' для пропуска):")
    except ValueError:
        await message.answer("ID должен быть числом. Попробуйте снова.")

@dp.message(AdminStates.adding_username)
async def add_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if username == "-" or username == "":
        username = None
    elif username.startswith("@"):
        username = username[1:]
    data = await state.get_data()
    success = await add_croupier(
        tg_id=data['tg_id'],
        first_name=data['first_name'],
        last_name=data['last_name'],
        city=data['city'],
        level=data['level'],
        hourly_rate=data['hourly_rate'],
        tg_username=username
    )
    await state.clear()
    if success:
        await message.answer(f"✅ Крупье {data['first_name']} {data['last_name']} добавлен!", reply_markup=admin_menu_kb)
    else:
        await message.answer("❌ Ошибка добавления. Возможно, ID уже существует.", reply_markup=admin_menu_kb)

@dp.message(F.text == "❌ Удалить крупье")
async def delete_croupier_list(message: Message):
    croupiers = await get_all_croupiers()
    if not croupiers:
        await message.answer("Нет зарегистрированных крупье.")
        return
    await message.answer("Выберите крупье для удаления:", reply_markup=get_croupiers_list_inline(croupiers))

@dp.callback_query(F.data.startswith("del_"))
async def delete_croupier_callback(callback: CallbackQuery):
    tg_id = int(callback.data.split("_")[1])
    await delete_croupier(tg_id)
    await callback.answer("Крупье удалён.")
    await callback.message.edit_text("✅ Крупье удалён. Можно закрыть окно.")
    await callback.message.answer("Продолжайте работу.", reply_markup=admin_menu_kb)

@dp.callback_query(F.data == "cancel_del")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer("Удаление отменено.")

@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат в главное меню.", reply_markup=main_keyboard())

@dp.message(F.text == "⏱ Начать смену")
async def begin_shift(message: Message):
    user_id = message.from_user.id
    croupier = await get_croupier(user_id)
    if not croupier:
        await message.answer("❌ Вы не зарегистрированы как крупье. Обратитесь к администратору.")
        return
    active = await get_active_shift(user_id)
    if active:
        await message.answer("⚠️ У вас уже есть активная смена. Закройте её, прежде чем начать новую.")
        return
    shift_id, start_time = await start_shift(user_id)
    managers = await get_managers_by_city(croupier['city'])
    full_name = f"{croupier['first_name']} {croupier['last_name']}"
    start_fmt = start_time.strftime("%d.%m.%Y %H:%M:%S")
    for mgr_id in managers:
        try:
            await bot.send_message(mgr_id,
                                   f"🟢 Крупье {full_name} (@{croupier['tg_username'] or 'нет'})\n"
                                   f"🏙 {croupier['city']} начал смену в {start_fmt} (Барнаул).")
        except:
            pass
    await message.answer(f"✅ Смена начата в {start_fmt} (Барнаул).\nНе забудьте закрыть смену по окончании работы.")

@dp.message(F.text == "⏹ Закрыть смену")
async def end_shift(message: Message):
    user_id = message.from_user.id
    croupier = await get_croupier(user_id)
    if not croupier:
        await message.answer("❌ Вы не зарегистрированы как крупье.")
        return
    active = await get_active_shift(user_id)
    if not active:
        await message.answer("У вас нет активной смены. Начните смену командой 'Начать смену'.")
        return
    result = await close_shift(user_id, croupier['hourly_rate'])
    if result is None:
        await message.answer("Ошибка закрытия смены.")
        return
    hours, salary, start_dt, end_dt = result
    hours_rounded = round(hours, 2)
    salary_rounded = round(salary, 2)
    full_name = f"{croupier['first_name']} {croupier['last_name']}"
    start_fmt = start_dt.strftime("%d.%m.%Y %H:%M:%S")
    end_fmt = end_dt.strftime("%d.%m.%Y %H:%M:%S")
    await message.answer(
        f"🔒 Смена закрыта.\n"
        f"⏱ Время работы: {hours_rounded} ч.\n"
        f"💰 Заработано: {salary_rounded} ₽\n"
        f"🕒 {start_fmt} → {end_fmt} (Барнаул)"
    )
    managers = await get_managers_by_city(croupier['city'])
    for mgr_id in managers:
        try:
            await bot.send_message(mgr_id,
                                   f"🔴 Крупье {full_name} (@{croupier['tg_username'] or 'нет'})\n"
                                   f"🏙 {croupier['city']} закрыл смену.\n"
                                   f"⏱ {hours_rounded} ч. | 💵 {salary_rounded} ₽\n"
                                   f"{start_fmt} → {end_fmt} (Барнаул)")
        except:
            pass

@dp.message(F.text == "💰 Расчет ЗП")
async def calculate_salary(message: Message):
    user_id = message.from_user.id
    croupier = await get_croupier(user_id)
    if not croupier:
        await message.answer("Вы не зарегистрированы как крупье. Данные о зарплате недоступны.")
        return
    total, period_start = await get_croupier_salary_current_period(user_id)
    period_str = period_start.strftime("%d.%m.%Y")
    await message.answer(f"💰 Ваша зарплата за период с {period_str} по сегодня: {total:.2f} ₽\n"
                         f"(смены считаются по Барнаульскому времени)")

@dp.message(F.text == "📊 Таблица")
async def show_table(message: Message):
    user_id = message.from_user.id
    if not await is_manager(user_id):
        await message.answer("❌ Эта функция доступна только турнирным менеджерам.")
        return
    city = await get_manager_city(user_id)
    shifts = await get_shifts_for_manager(city=city)
    if not shifts:
        await message.answer("Нет закрытых смен для отображения.")
        return
    text = "📊 Таблица смен\n\n"
    for s in shifts:
        start_dt = s["start_time"].strftime("%d.%m %H:%M")
        end_dt = s["end_time"].strftime("%d.%m %H:%M") if s["end_time"] else "—"
        hours = round(s["hours_worked"], 2) if s["hours_worked"] else 0
        salary = round(s["salary"], 2) if s["salary"] else 0
        text += f"👤 {s['first_name']} {s['last_name']} ({s['city']})\n"
        text += f"   🕒 {start_dt} → {end_dt}\n"
        text += f"   ⏱ {hours} ч. | 💵 {salary} ₽\n\n"
    await message.answer(text)

# ---------- Запуск ----------
async def main():
    await init_db()
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())