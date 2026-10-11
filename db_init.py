import sqlite3
import csv
import os

def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance.db")
    print(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute('''
    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        merchant TEXT,
        amount REAL,
        payment_method TEXT,
        type TEXT
    )
    ''')

    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bill.csv")
    count = 0
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cur.execute(
                "INSERT INTO bills (date, merchant, amount, payment_method, type) VALUES (?, ?, ?, ?, ?)",
                (row["日期"], row["商户"], float(row["金额"]), row["支付方式"], row["类型"])
            )
            count += 1
    conn.commit()
    conn.close()
    print(f"数据库初始化完成，共插入 {count} 条记录")

if __name__ == "__main__":
    main()
