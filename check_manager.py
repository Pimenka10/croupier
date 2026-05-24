import sqlite3
conn = sqlite3.connect('croupier_bot.db')
cur = conn.cursor()
cur.execute("SELECT tg_id, city FROM managers")
rows = cur.fetchall()
for r in rows:
    print(f"Менеджер ID: {r[0]}, город: {r[1]}")
conn.close()