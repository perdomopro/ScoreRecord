import sqlite3

c = sqlite3.connect('/home/adrian/scorerecord/data/scorerecord.db')
print("Total rows:", c.execute("SELECT COUNT(*) FROM historical_odds").fetchone()[0])
res = c.execute("SELECT * FROM historical_odds LIMIT 5").fetchall()
for r in res:
    print(r)
print("Count by market:")
res2 = c.execute("SELECT market, COUNT(*) FROM historical_odds GROUP BY market").fetchall()
for r in res2:
    print(r)
