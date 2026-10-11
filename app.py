import streamlit as st
import pandas as pd
import sqlite3
import requests
import os

def init_database():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS bills (
        date TEXT,
        merchant TEXT,
        amount REAL,
        payment_method TEXT,
        type TEXT
    )
    ''')
    cursor.execute("SELECT COUNT(*) FROM bills")
    count = cursor.fetchone()[0]
    if count == 0:
        sample_data = [
            ("2026-09-01", "食堂", 12.5, "微信", "支出"),
            ("2026-09-03", "奶茶店", 18.0, "支付宝", "支出"),
            ("2026-09-05", "兼职收入", 800, "银行卡", "收入"),
            ("2026-09-08", "超市", 45.2, "微信", "支出"),
            ("2026-09-10", "文具店", 22.0, "支付宝", "支出")
        ]
        cursor.executemany("INSERT INTO bills (date, merchant, amount, payment_method, type) VALUES (?, ?, ?, ?, ?)", sample_data)
        conn.commit()
    conn.close()

init_database()

st.title("明白账 - 财务健康看板")

try:
    conn = sqlite3.connect("finance.db")
    df = pd.read_sql_query("SELECT * FROM bills", conn)
    conn.close()

    total_income = df[df["type"] == "收入"]["amount"].sum()
    total_outcome = df[df["type"] == "支出"]["amount"].sum()
    balance = total_income - total_outcome

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("总收入", total_income)
    with col2:
        st.metric("总支出", total_outcome)
    with col3:
        st.metric("当前结余", balance)

    st.subheader("账单明细")
    st.dataframe(df)

    st.subheader("各商户支出金额")
    expense_df = df[df["type"] == "支出"]
    merchant_sum = expense_df.groupby("merchant")["amount"].sum()
    st.bar_chart(merchant_sum)

except sqlite3.Error:
    st.error("数据库读取异常。")

st.subheader("AI 金融导师")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

user_input = st.chat_input()
if user_input:
    try:
        api_key = st.secrets["DEEPSEEK_API_KEY"]
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        st.error("请先设置 DEEPSEEK_API_KEY")
    else:
        try:
            conn = sqlite3.connect("finance.db")
            df_recent = pd.read_sql_query("SELECT * FROM bills ORDER BY date DESC LIMIT 5", conn)
            conn.close()
            recent_bills = df_recent.to_string(index=False)
            background = f"""你的身份是大学生财务导师。用户财务数据：
总收入：{total_income}
总支出：{total_outcome}
当前结余：{balance}
最近5笔账单：
{recent_bills}
请结合账单数据给出实用、口语化的财务建议。"""

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": background},
                    {"role": "user", "content": user_input}
                ],
                "temperature": 0.7
            }
            response = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, timeout=15)
            if response.status_code != 200:
                st.error(f"API调用失败，状态码：{response.status_code}，返回信息：{response.text}")
            else:
                res_data = response.json()
                ai_content = res_data["choices"][0]["message"]["content"]
                st.session_state.messages.append({"role": "user", "content": user_input})
                st.chat_message("user").write(user_input)
                st.session_state.messages.append({"role": "assistant", "content": ai_content})
                st.chat_message("assistant").write(ai_content)
        except requests.exceptions.Timeout:
            st.error("请求超时，请检查网络后重试")
        except requests.exceptions.RequestException as e:
            st.error(f"网络请求异常：{str(e)}")
