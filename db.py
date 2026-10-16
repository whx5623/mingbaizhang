# db.py
import sqlite3
import bcrypt
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def get_engine() -> Engine:
    import os
    db_url = os.environ.get("DATABASE_URL")
    if db_url and db_url.startswith("postgresql"):
        return create_engine(db_url)
    else:
        return create_engine("sqlite:///finance.db")


engine = get_engine()


def is_sqlite():
    return engine.dialect.name == "sqlite"


def init_db():
    with engine.connect() as conn:
        # 1. 创建 users 表
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # 2. 创建 bills 表（如果不存在）
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bills (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                date VARCHAR(50),
                merchant VARCHAR(100),
                amount FLOAT,
                payment_method VARCHAR(50),
                type VARCHAR(20)
            );
        """))

        # 3. 创建 feedbacks 反馈表
        if is_sqlite():
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))
        conn.commit()


def create_user(username, password_hash):
    with engine.connect() as conn:
        try:
            stmt = text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id;")
            result = conn.execute(stmt, {"u": username, "h": password_hash})
            row = result.fetchone()
            conn.commit()
            if row:
                return row[0]
            return None
        except Exception as e:
            conn.rollback()
            raise e


def register_user(username, plain_password):
    password_bytes = plain_password.encode("utf-8")
    hash_str = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")
    return create_user(username, hash_str)


def verify_user(username, plain_password):
    with engine.connect() as conn:
        stmt = text("SELECT id, password_hash FROM users WHERE username = :u")
        row = conn.execute(stmt, {"u": username}).fetchone()
        if not row:
            return None
        user_id, pw_hash = row
        if bcrypt.checkpw(plain_password.encode("utf-8"), pw_hash.encode("utf-8")):
            return user_id
        return None


def check_user_exists(user_id):
    with engine.connect() as conn:
        stmt = text("SELECT id FROM users WHERE id = :uid")
        row = conn.execute(stmt, {"uid": user_id}).fetchone()
        return row is not None


def get_all_bills(user_id):
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM bills WHERE user_id = :uid"), conn, params={"uid": user_id})
    return df


def get_bills_paginated(user_id, limit=10, offset=0):
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM bills WHERE user_id = :uid LIMIT :limit OFFSET :offset"),
                         conn, params={"uid": user_id, "limit": limit, "offset": offset})
    return df


def get_total_bill_count(user_id):
    with engine.connect() as conn:
        cnt = conn.execute(text("SELECT COUNT(*) FROM bills WHERE user_id = :uid"), {"uid": user_id}).scalar()
    return cnt


def add_bill(user_id, date, merchant, amount, payment_method, bill_type):
    with engine.connect() as conn:
        stmt = text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
            VALUES (:uid, :date, :merchant, :amount, :pm, :t)
        """)
        conn.execute(stmt, {
            "uid": user_id,
            "date": date,
            "merchant": merchant,
            "amount": amount,
            "pm": payment_method,
            "t": bill_type
        })
        conn.commit()


def clear_all_bills(user_id):
    with engine.connect() as conn:
        stmt = text("DELETE FROM bills WHERE user_id = :uid")
        conn.execute(stmt, {"uid": user_id})
        conn.commit()


def insert_demo_bills(user_id):
    if not check_user_exists(user_id):
        return False
    clear_all_bills(user_id)
    demo_10rows = [
        ("2026-10-01", "学校食堂", 11.8, "微信", "支出"),
        ("2026-10-02", "线上家教兼职", 600.0, "支付宝", "收入"),
        ("2026-10-03", "蜜雪冰城", 7.5, "微信", "支出"),
        ("2026-10-04", "淘宝网购服饰", 129.0, "支付宝", "支出"),
        ("2026-10-06", "食堂早餐", 6.0, "校园卡", "支出"),
        ("2026-10-08", "白条分期", 85.0, "支付宝", "支出"),
        ("2026-10-10", "超市零食饮料", 42.3, "微信", "支出"),
        ("2026-10-12", "美团外卖", 23.6, "微信", "支出"),
        ("2026-10-14", "打印店", 3.5, "微信", "支出"),
        ("2026-10-15", "奖学金补贴", 1200.0, "银行卡", "收入")
    ]
    with engine.connect() as conn:
        insert_stmt = text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
            VALUES (:uid, :date, :merchant, :amount, :pm, :t)
        """)
        for row in demo_10rows:
            conn.execute(insert_stmt, {
                "uid": user_id,
                "date": row[0],
                "merchant": row[1],
                "amount": row[2],
                "pm": row[3],
                "t": row[4]
            })
        conn.commit()
    return True


def import_csv_to_bills(user_id, df_upload):
    clear_all_bills(user_id)
    df_upload["user_id"] = user_id
    with engine.connect() as conn:
        df_upload.to_sql("bills", conn, if_exists="append", index=False)
        conn.commit()


def add_feedback(user_id, content):
    with engine.connect() as conn:
        stmt = text("INSERT INTO feedbacks(user_id, content) VALUES (:uid, :cnt)")
        conn.execute(stmt, {"uid": user_id, "cnt": content})
        conn.commit()
def add_user_credits(user_id, delta):
    with engine.connect() as conn:
        stmt = text("UPDATE users SET credits = credits + :delta WHERE id = :uid")
        conn.execute(stmt, {"delta": delta, "uid": user_id})
        conn.commit()
