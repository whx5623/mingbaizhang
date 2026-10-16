from sqlalchemy import create_engine, text
import pandas as pd
from datetime import datetime, date, timedelta

engine = None

def init_db():
    global engine
    from config import DATABASE_URL
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                credits INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bills (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                date VARCHAR(50),
                merchant VARCHAR(100),
                amount FLOAT,
                payment_method VARCHAR(50),
                type VARCHAR(20),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                message_text TEXT,
                role VARCHAR(20),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """))
        conn.commit()

def create_user(username, password_hash):
    global engine
    with engine.connect() as conn:
        stmt = text("INSERT INTO users (username, password_hash) VALUES (:un, :ph) RETURNING id;")
        result = conn.execute(stmt, {"un": username, "ph": password_hash})
        row = result.fetchone()
        conn.commit()
        return row[0] if row else None

def verify_user(username, password_hash):
    global engine
    with engine.connect() as conn:
        stmt = text("SELECT id FROM users WHERE username = :un AND password_hash = :ph")
        res = conn.execute(stmt, {"un": username, "ph": password_hash})
        row = res.fetchone()
        return row[0] if row else None

def check_user_exists(user_id):
    global engine
    with engine.connect() as conn:
        stmt = text("SELECT COUNT(id) FROM users WHERE id = :uid")
        cnt = conn.execute(stmt, {"uid": user_id}).scalar() or 0
        return cnt > 0

def add_bill(user_id, date, merchant, amount, payment_method, type):
    global engine
    with engine.connect() as conn:
        stmt = text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
            VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
        """)
        conn.execute(stmt, {
            "uid": user_id,
            "dt": date,
            "mer": merchant,
            "amt": amount,
            "pm": payment_method,
            "tp": type
        })
        conn.commit()

def get_all_bills(user_id):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid")
    df = pd.read_sql(sql, engine, params={"uid": user_id})
    return df

def get_bills_paginated(user_id, limit, offset):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid ORDER BY date DESC LIMIT :lim OFFSET :off")
    df = pd.read_sql(sql, engine, params={"uid": user_id, "lim": limit, "off": offset})
    return df

def get_total_bill_count(user_id):
    global engine
    with engine.connect() as conn:
        stmt = text("SELECT COUNT(id) FROM bills WHERE user_id = :uid")
        return conn.execute(stmt, {"uid": user_id}).scalar() or 0

def insert_demo_bills(user_id):
    global engine
    demo_data = [
        {"date":"2026-10-01","merchant":"食堂","amount":12.5,"payment_method":"校园卡","type":"支出"},
        {"date":"2026-10-02","merchant":"蜜雪冰城","amount":8.0,"payment_method":"微信","type":"支出"},
        {"date":"2026-10-03","merchant":"淘宝网购","amount":129.0,"payment_method":"支付宝","type":"支出"},
        {"date":"2026-10-04","merchant":"兼职","amount":400.0,"payment_method":"银行卡","type":"收入"},
        {"date":"2026-10-05","merchant":"白条分期","amount":99.0,"payment_method":"白条","type":"支出"},
        {"date":"2026-10-06","merchant":"食堂","amount":15.0,"payment_method":"校园卡","type":"支出"},
        {"date":"2026-10-07","merchant":"瑞幸咖啡","amount":14.0,"payment_method":"微信","type":"支出"},
        {"date":"2026-10-08","merchant":"拼多多","amount":45.5,"payment_method":"支付宝","type":"支出"},
        {"date":"2026-10-09","merchant":"家教兼职","amount":300.0,"payment_method":"微信","type":"收入"},
        {"date":"2026-10-10","merchant":"超市","amount":56.2,"payment_method":"支付宝","type":"支出"},
    ]
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM bills WHERE user_id = :uid"), {"uid": user_id})
        for row in demo_data:
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
            """)
            conn.execute(stmt, {
                "uid": user_id,
                "dt": row["date"],
                "mer": row["merchant"],
                "amt": row["amount"],
                "pm": row["payment_method"],
                "tp": row["type"]
            })
        conn.commit()
    return True

def import_csv_to_bills(user_id, df):
    global engine
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM bills WHERE user_id = :uid"), {"uid": user_id})
        for _, r in df.iterrows():
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
            """)
            conn.execute(stmt, {
                "uid": user_id,
                "dt": r["date"],
                "mer": r["merchant"],
                "amt": float(r["amount"]),
                "pm": r["payment_method"],
                "tp": r["type"]
            })
        conn.commit()

def add_feedback(user_id, content):
    global engine
    with engine.connect() as conn:
        stmt = text("INSERT INTO feedback(user_id, content) VALUES (:uid, :cnt)")
        conn.execute(stmt, {"uid": user_id, "cnt": content})
        conn.commit()

def add_user_credits(user_id, delta):
    global engine
    with engine.connect() as conn:
        stmt = text("UPDATE users SET credits = credits + :delta WHERE id = :uid")
        conn.execute(stmt, {"delta": delta, "uid": user_id})
        conn.commit()

def get_admin_operate_data():
    global engine
    end_date = date.today()
    start_date = end_date - timedelta(days=6)
    with engine.connect() as conn:
        reg_sql = text("""
            SELECT DATE(created_at) as date, COUNT(id) as count
            FROM users
            WHERE DATE(created_at) BETWEEN :start AND :end
            GROUP BY DATE(created_at)
            ORDER BY DATE(created_at)
        """)
        reg_df = pd.read_sql(reg_sql, conn, params={"start": start_date, "end": end_date})

        active_sql = text("""
            SELECT DATE(date) as date, COUNT(DISTINCT user_id) as count
            FROM bills
            WHERE DATE(date) BETWEEN :start AND :end
            GROUP BY DATE(date)
            ORDER BY DATE(date)
        """)
        active_df = pd.read_sql(active_sql, conn, params={"start": start_date, "end": end_date})

        sponsor_sql = text("SELECT COUNT(DISTINCT id) FROM users WHERE credits > 0")
        sponsor_count = conn.execute(sponsor_sql).scalar() or 0
    return reg_df, active_df, sponsor_count

def get_admin_funnel_data():
    global engine
    end_date = date.today()
    start_7d = end_date - timedelta(days=7)
    with engine.connect() as conn:
        total_reg_sql = text("SELECT COUNT(id) FROM users")
        total_reg = conn.execute(total_reg_sql).scalar() or 0

        bill_user_sql = text("SELECT COUNT(DISTINCT user_id) FROM bills")
        bill_user_count = conn.execute(bill_user_sql).scalar() or 0

        login_7d_sql = text("SELECT COUNT(DISTINCT id) FROM users WHERE DATE(last_login) >= :start")
        login_7d_user = conn.execute(login_7d_sql, {"start": start_7d}).scalar() or 0

        ai_user_sql = text("SELECT COUNT(DISTINCT user_id) FROM chat_history")
        ai_user_count = conn.execute(ai_user_sql).scalar() or 0
    return total_reg, bill_user_count, login_7d_user, ai_user_count
def register_user(username, password_hash):
    global engine
    from sqlalchemy.exc import IntegrityError
    with engine.connect() as conn:
        try:
            stmt = text("INSERT INTO users (username, password_hash) VALUES (:username, :password_hash) RETURNING id;")
            result = conn.execute(stmt, {"username": username, "password_hash": password_hash})
            row = result.fetchone()
            conn.commit()
            return row[0] if row else None
        except IntegrityError:
            conn.rollback()
            return None
