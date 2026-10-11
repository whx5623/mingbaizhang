import streamlit as st
import pandas as pd

st.title("明白账 - 财务健康看板")

try:
    df = pd.read_csv("bill.csv", encoding="utf-8-sig")
    total_income = df[df["类型"] == "收入"]["金额"].sum()
    total_outcome = df[df["类型"] == "支出"]["金额"].sum()
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
    expense_df = df[df["类型"] == "支出"]
    merchant_sum = expense_df.groupby("商户")["金额"].sum()
    st.bar_chart(merchant_sum)

except FileNotFoundError:
    st.error("未找到 bill.csv 文件，请确认文件放在程序同目录下。")

st.subheader("AI 金融导师")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

user_input = st.chat_input()
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.chat_message("user").write(user_input)
    ai_reply = "结合你的账单，你本月支出较高，建议少喝奶茶。"
    st.session_state.messages.append({"role": "assistant", "content": ai_reply})
    st.chat_message("assistant").write(ai_reply)
