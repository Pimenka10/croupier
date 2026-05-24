import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from os import getenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = getenv("BOT_TOKEN")
ADMIN_PASSWORD = getenv("ADMIN_PASSWORD")

if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN в .env")

# Настройка логов
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- База данных ----------
DB_NAME = "croupier_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Таблица крупье
    cur.execute('''
        CREATE TABLE IF NOT EXISTS croupiers (
            tg_id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            city TEXT NOT NULL,
            level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 5),
            hourly_rate INTEGER NOT NULL,
            tg_username TEXT
        )
    ''')
    # Таблица смен
    cur.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            croupier_tg_id INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            hours_worked REAL,
            salary REAL,
            status TEXT DEFAULT 'active',
            FOREIGN KEY(croupier_tg_id) REFERENCES croupiers(tg_id)
        )
    ''')
    # Таблица менеджеров (турнирные менеджеры)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS managers (
            tg_id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            role TEXT DEFAULT 'tournament_manager'
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Вспомогательные функции для БД
def get_croupier(tg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT tg_id, first_name, last_name, city, level, hourly_rate, tg_username FROM croupiers WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"tg_id": row[0], "first_name": row[1], "last_name": row[2], "city": row[3],
                "level": row[4], "hourly_rate": row[5], "tg_username": row[6]}
    return None

def get_all_croupiers():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT tg_id, first_name, last_name, city, level, hourly_rate, tg_username FROM croupiers ORDER BY city, last_name")
    rows = cur.fetchall()
    conn.close()
    return [{"tg_id": r[0], "first_name": r[1], "last_name": r[2], "city": r[3],
             "level": r[4], "hourly_rate": r[5], "tg_username": r[6]} for r in rows]

def add_croupier(tg_id, first_name, last_name, city, level, hourly_rate, tg_username=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute('''
            INSERT INTO croupiers (tg_id, first_name, last_name, city, level, hourly_rate, tg_username)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (tg_id, first_name, last_name, city, level, hourly_rate, tg_username))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_croupier(tg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM croupiers WHERE tg_id = ?", (tg_id,))
    # Также можно удалить или пометить смены – для простоты оставим
    conn.commit()
    conn.close()

def get_active_shift(croupier_tg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, start_time, status FROM shifts WHERE croupier_tg_id = ? AND status = 'active'", (croupier_tg_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "start_time": row[1], "status": row[2]}
    return None

def start_shift(croupier_tg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("INSERT INTO shifts (croupier_tg_id, start_time, status) VALUES (?, ?, 'active')",
                (croupier_tg_id, now))
    shift_id = cur.lastrowid
    conn.commit()
    conn.close()
    return shift_id, now

def close_shift(croupier_tg_id: int, hourly_rate: float):
    active = get_active_shift(croupier_tg_id)
    if not active:
        return None
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    end_time = datetime.now()
    start_time = datetime.fromisoformat(active["start_time"])
    hours_worked = (end_time - start_time).total_seconds() / 3600.0
    salary = Decimal(str(hours_worked)) * Decimal(str(hourly_rate))
    salary = round(salary, 2)
    cur.execute('''
        UPDATE shifts
        SET end_time = ?, hours_worked = ?, salary = ?, status = 'closed'
        WHERE id = ?
    ''', (end_time.isoformat(), hours_worked, float(salary), active["id"]))
    conn.commit()
    conn.close()
    return hours_worked, float(salary), start_time, end_time

def get_shifts_for_manager(city: str = None, limit=50):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    if city:
        cur.execute('''
            SELECT s.id, c.first_name, c.last_name, c.city, s.start_time, s.end_time, s.hours_worked, s.salary
            FROM shifts s
            JOIN croupiers c ON s.croupier_tg_id = c.tg_id
            WHERE c.city = ? AND s.status = 'closed'
            ORDER BY s.start_time DESC
            LIMIT ?
        ''', (city, limit))
    else:
        cur.execute('''
            SELECT s.id, c.first_name, c.last_name, c.city, s.start_time, s.end_time, s.hours_worked, s.salary
            FROM shifts s
            JOIN croupiers c ON s.croupier_tg_id = c.tg_id
            WHERE s.status = 'closed'
            ORDER BY s.start_time DESC
            LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_croupier_salary_last_days(tg_id: int, days=7):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cur.execute('''
        SELECT SUM(salary) FROM shifts
        WHERE croupier_tg_id = ? AND status = 'closed' AND start_time >= ?
    ''', (tg_id, since))
    total = cur.fetchone()[0]
    conn.close()
    return total if total else 0.0

def is_manager(tg_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM managers WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def get_manager_city(tg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT city FROM managers WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def get_managers_by_city(city: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM managers WHERE city = ?", (city,))
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ---------- Клавиатуры ----------
def main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton(text="📋 Список крупье")],
        [KeyboardButton(text="✏️ Изменить данные крупье")],
        [KeyboardButton(text="⏱ Начать смену"), KeyboardButton(text="⏹ Закрыть смену")],
        [KeyboardButton(text="💰 Расчет ЗП"), KeyboardButton(text="📊 Таблица")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# Клавиатура для админ-меню после ввода пароля
admin_menu_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="➕ Добавить крупье"), KeyboardButton(text="❌ Удалить крупье")],
    [KeyboardButton(text="🔙 Назад")]
], resize_keyboard=True)

# Инлайн клавиатура для выбора крупье на удаление
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
    user_id = message.from_user.id
    # Приветствие
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
    croupiers = get_all_croupiers()
    if not croupiers:
        await message.answer("Список крупье пуст.")
        return
    text = "📋 *Список крупье:*\n\n"
    for c in croupiers:
        text += f"👤 {c['first_name']} {c['last_name']} (@{c['tg_username'] or 'нет'})\n"
        text += f"   🏙 {c['city']} | Уровень {c['level']} | 💵 {c['hourly_rate']}₽/час\n"
        text += f"   🆔 TG ID: `{c['tg_id']}`\n\n"
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
    await message.answer("Введите город (например: Москва, Сочи):")

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
        # Проверим, не занят ли уже
        if get_croupier(tg_id):
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
    success = add_croupier(
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
    croupiers = get_all_croupiers()
    if not croupiers:
        await message.answer("Нет зарегистрированных крупье.")
        return
    await message.answer("Выберите крупье для удаления:", reply_markup=get_croupiers_list_inline(croupiers))

@dp.callback_query(F.data.startswith("del_"))
async def delete_croupier_callback(callback: CallbackQuery):
    tg_id = int(callback.data.split("_")[1])
    delete_croupier(tg_id)
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

# ---------- Смены ----------
@dp.message(F.text == "⏱ Начать смену")
async def begin_shift(message: Message):
    user_id = message.from_user.id
    croupier = get_croupier(user_id)
    if not croupier:
        await message.answer("❌ Вы не зарегистрированы как крупье. Обратитесь к администратору.")
        return
    active = get_active_shift(user_id)
    if active:
        await message.answer("⚠️ У вас уже есть активная смена. Закройте её, прежде чем начать новую.")
        return
    shift_id, start_time_str = start_shift(user_id)
    # Уведомление менеджеров города
    managers = get_managers_by_city(croupier['city'])
    full_name = f"{croupier['first_name']} {croupier['last_name']}"
    start_dt = datetime.fromisoformat(start_time_str)
    start_fmt = start_dt.strftime("%d.%m.%Y %H:%M:%S")
    for mgr_id in managers:
        try:
            await bot.send_message(mgr_id,
                                   f"🟢 Крупье {full_name} (@{croupier['tg_username'] or 'нет'})\n"
                                   f"🏙 {croupier['city']} начал смену в {start_fmt}.")
        except:
            pass
    await message.answer(f"✅ Смена начата в {start_fmt}.\nНе забудьте закрыть смену по окончании работы.")

@dp.message(F.text == "⏹ Закрыть смену")
async def end_shift(message: Message):
    user_id = message.from_user.id
    croupier = get_croupier(user_id)
    if not croupier:
        await message.answer("❌ Вы не зарегистрированы как крупье.")
        return
    active = get_active_shift(user_id)
    if not active:
        await message.answer("У вас нет активной смены. Начните смену командой 'Начать смену'.")
        return
    result = close_shift(user_id, croupier['hourly_rate'])
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
        f"🕒 {start_fmt} → {end_fmt}"
    )
    # Уведомление менеджеров
    managers = get_managers_by_city(croupier['city'])
    for mgr_id in managers:
        try:
            await bot.send_message(mgr_id,
                                   f"🔴 Крупье {full_name} (@{croupier['tg_username'] or 'нет'})\n"
                                   f"🏙 {croupier['city']} закрыл смену.\n"
                                   f"⏱ {hours_rounded} ч. | 💵 {salary_rounded} ₽\n"
                                   f"{start_fmt} → {end_fmt}")
        except:
            pass

# ---------- Расчет ЗП ----------
@dp.message(F.text == "💰 Расчет ЗП")
async def calculate_salary(message: Message):
    user_id = message.from_user.id
    croupier = get_croupier(user_id)
    if not croupier:
        await message.answer("Вы не зарегистрированы как крупье. Данные о зарплате недоступны.")
        return
    total = get_croupier_salary_last_days(user_id, days=7)
    await message.answer(f"💰 Ваша зарплата за последние 7 дней: {total:.2f} ₽\n"
                         f"(Учитываются только закрытые смены)")

# ---------- Таблица для админов ----------
@dp.message(F.text == "📊 Таблица")
async def show_table(message: Message):
    user_id = message.from_user.id
    if not is_manager(user_id):
        await message.answer("❌ Эта функция доступна только турнирным менеджерам.")
        return
    city = get_manager_city(user_id)
    shifts = get_shifts_for_manager(city=city)
    if not shifts:
        await message.answer("Нет закрытых смен для отображения.")
        return
    text = "📊 *Таблица смен*\n\n"
    for s in shifts:
        # s: id, first_name, last_name, city, start_time, end_time, hours_worked, salary
        start_dt = datetime.fromisoformat(s[4]).strftime("%d.%m %H:%M")
        end_dt = datetime.fromisoformat(s[5]).strftime("%d.%m %H:%M") if s[5] else "—"
        hours = round(s[6], 2) if s[6] else 0
        salary = round(s[7], 2) if s[7] else 0
        text += f"👤 {s[1]} {s[2]} ({s[3]})\n"
        text += f"   🕒 {start_dt} → {end_dt}\n"
        text += f"   ⏱ {hours} ч. | 💵 {salary} ₽\n\n"
    await message.answer(text, parse_mode="Markdown")

# ---------- Запуск ----------
async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())