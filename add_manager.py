import sqlite3

DB_NAME = "croupier_bot.db"

def add_manager(tg_id, city):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT INTO managers (tg_id, city, role) VALUES (?, ?, 'tournament_manager')", (tg_id, city))
    conn.commit()
    conn.close()
    print(f"Менеджер {tg_id} добавлен в {city}")

if __name__ == "__main__":
    tg_id = int(input("Telegram ID менеджера: "))
    city = input("Город (например, Москва): ")
    add_manager(tg_id, city)