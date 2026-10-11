import streamlit as st
import pandas as pd
import sqlite3
import requests
import os
import plotly.express as px
import plotly.graph_objects as go

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
st.subheader("上传账单")
upload_file = st.file_uploader("上传账单文件，支持 .csv 和 .xlsx", type=["csv", "xlsx"])
if upload_file is not None:
    try:
        if upload_file.name.endswith(".csv"):
            df_upload = pd.read_csv(upload_file, encoding="utf-8-sig")
        elif upload_file.name.endswith(".xlsx"):
            df_upload = pd.read_excel(upload_file)
        else:
            st.error("不支持的文件格式！")
            st.stop()
        required_cols = ["date", "merchant", "amount", "payment_method", "type"]
        if not all(col in df_upload.columns for col in required_cols):
            st.error("文件缺少必要列！必须包含：date, merchant, amount, payment_method, type")
            st.stop()
        df_upload["amount"] = pd.to_numeric(df_upload["amount"])
        conn = sqlite3.connect("finance.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bills")
        df_upload[required_cols].to_sql("bills", conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        st.success("账单上传成功，页面自动刷新！")
        st.rerun()
    except Exception as e:
        st.error(f"读取或写入数据失败：{str(e)}")

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
    merchant_sum = expense_df.groupby("merchant")["amount"].sum().reset_index()
    fig = px.bar(merchant_sum, x="merchant", y="amount",
                 title="各商户支出金额统计",
                 labels={"merchant": "商户", "amount": "金额(元)"},
                 color_discrete_sequence=["#4a90e2", "#50c878"])
    fig.update_traces(hovertemplate="商户：%{x}<br>金额：%{y} 元")
    st.plotly_chart(fig, use_container_width=True)

except sqlite3.Error:
    st.error("数据库读取异常。")

st.subheader("AI 金融导师")

if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.messages.append({"role": "assistant", "content": "你好！我是你的AI金融导师，我可以结合你的账单给你财务建议，请问有什么想咨询的？"})

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
            system_prompt = f"""你的身份是大学生财务导师。用户财务数据：
总收入：{total_income}
总支出：{total_outcome}
当前结余：{balance}
最近5笔账单：
{recent_bills}
请结合账单数据给出实用、口语化的财务建议。"""

            st.session_state.messages.append({"role": "user", "content": user_input})
            st.chat_message("user").write(user_input)

            request_messages = [{"role": "system", "content": system_prompt}] + st.session_state.messages

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "deepseek-chat",
                "messages": request_messages,
                "temperature": 0.7
            }
            response = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, timeout=15)
            if response.status_code != 200:
                st.error(f"API调用失败，状态码：{response.status_code}，返回信息：{response.text}")
            else:
                res_data = response.json()
                ai_content = res_data["choices"][0]["message"]["content"]
                st.session_state.messages.append({"role": "assistant", "content": ai_content})
                st.chat_message("assistant").write(ai_content)
        except requests.exceptions.Timeout:
            st.error("请求超时，请检查网络后重试")
        except requests.exceptions.RequestException as e:
            st.error(f"网络请求异常：{str(e)}")
