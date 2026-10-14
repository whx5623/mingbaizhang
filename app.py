# app.py
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from config import get_deepseek_api_key
from db import init_db, get_all_bills, insert_demo_bills, import_csv_to_bills, register_user, verify_user, add_bill, check_user_exists
import requests
import json

st.set_page_config(layout="wide")
init_db()

if "user_id" not in st.session_state:
    st.session_state.user_id = None

def logout():
    st.session_state.user_id = None
    st.cache_data.clear()

if st.session_state.user_id is not None:
    if not check_user_exists(st.session_state.user_id):
        st.warning("当前用户不存在，请重新登录")
        logout()
        st.rerun()

if st.session_state.user_id is None:
    st.title("明白账 - 登录 / 注册")
    username = st.text_input("用户名")
    password = st.text_input("密码", type="password")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("登录"):
            uid = verify_user(username, password)
            if uid:
                st.session_state.user_id = uid
                st.rerun()
            else:
                st.error("用户名或密码错误")
    with col2:
        if st.button("注册"):
            if not username or not password:
                st.warning("用户名和密码不能为空")
            else:
                try:
                    new_uid = register_user(username, password)
                    if new_uid:
                        st.session_state.user_id = new_uid
                        st.success("注册成功，已自动登录")
                        st.rerun()
                    else:
                        st.error("注册失败，未获取用户ID")
                except Exception as e:
                    st.error(f"注册失败：{str(e)}")
    st.stop()

@st.cache_data
def load_data(user_id):
    return get_all_bills(user_id)

# Sidebar
with st.sidebar:
    st.header("个人设定")
    if "monthly_living" not in st.session_state:
        st.session_state.monthly_living = 1500.0
    if "save_target" not in st.session_state:
        st.session_state.save_target = 200.0
    if "city_level" not in st.session_state:
        st.session_state.city_level = "二线"

    st.session_state.monthly_living = st.number_input("每月生活费（元）", min_value=0.0, value=st.session_state.monthly_living, step=50.0)
    st.session_state.save_target = st.number_input("每月存钱目标（元）", min_value=0.0, value=st.session_state.save_target, step=50.0)
    st.session_state.city_level = st.selectbox("所在城市", ["一线", "二线", "三线"], index=["一线", "二线", "三线"].index(st.session_state.city_level))
    st.divider()
    if st.button("一键加载演示数据（答辩专用）"):
        if st.session_state.get("user_id") and check_user_exists(st.session_state.user_id):
            ok = insert_demo_bills(st.session_state.user_id)
            if ok:
                st.cache_data.clear()
                st.success("演示数据加载成功")
                st.rerun()
            else:
                st.error("用户校验失败，无法插入演示数据")
        else:
            st.warning("请先登录或注册")
    if st.button("退出登录"):
        logout()
        st.rerun()

# Load data
df = load_data(st.session_state.user_id)
total_income = df[df["type"] == "收入"]["amount"].sum()
total_expense = df[df["type"] == "支出"]["amount"].sum()
balance = total_income - total_expense

# Page Title
st.title("明白账 - 财务健康看板")

# Upload section
st.subheader("上传账单")
uploaded_file = st.file_uploader("上传账单文件", type=['csv'])
if uploaded_file is not None:
    try:
        df_upload = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        required_cols = ["date", "merchant", "amount", "payment_method", "type"]
        missing_cols = [col for col in required_cols if col not in df_upload.columns]
        if missing_cols:
            st.error(f"文件缺少必要列：{','.join(missing_cols)}")
        else:
            import_csv_to_bills(st.session_state.user_id, df_upload)
            st.cache_data.clear()
            st.success("账单上传成功！页面即将刷新")
            st.rerun()
    except Exception as e:
        st.error(f"读取文件失败：{str(e)}")

# Metric cards
st.subheader("收支概览")
col1, col2 = st.columns(2)
with col1:
    st.metric("总收入", f"{total_income:.2f}")
with col2:
    col_a, col_b = st.columns(2)
    with col_a:
        st.metric("总支出", f"{total_expense:.2f}")
    with col_b:
        st.metric("当前结余", f"{balance:.2f}")

# Bill table
st.subheader("账单明细")
st.dataframe(df, use_container_width=True)

# Plotly chart
st.subheader("各商户支出金额")
exp_df = df[df["type"] == "支出"]
bar_data = exp_df.groupby("merchant")["amount"].sum().reset_index()
fig_bar = px.bar(
    bar_data,
    x="merchant",
    y="amount",
    title="各商户支出统计",
    labels={"merchant": "商户", "amount": "金额(元)"},
    color_discrete_sequence=["#638bd6", "#62c490"]
)
fig_bar.update_traces(hovertemplate="商户：%{x}<br>金额：%{y:.2f}元")
st.plotly_chart(fig_bar, use_container_width=True)

# AI Chat
st.subheader("AI 金融导师")
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
    st.session_state.chat_history.append({"role": "assistant", "content": "你好！我是明白账AI财务管家，可以帮你分析账单，也可以直接帮你记账，有什么想问我的吗？"})

for msg in st.session_state.chat_history:
    st.chat_message(msg["role"]).write(msg["content"])

user_input = st.chat_input("可以直接让我帮你记账，例如：今天喝奶茶花了15元")
if user_input:
    st.chat_message("user").write(user_input)
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    api_key = get_deepseek_api_key()
    if not api_key:
        st.error("请先设置 DEEPSEEK_API_KEY 环境变量")
    else:
        try:
            from ai_agent import get_ai_response
            resp = get_ai_response(
                st.session_state.chat_history,
                df,
                total_income,
                total_expense,
                balance,
                st.session_state.monthly_living,
                st.session_state.save_target,
                st.session_state.city_level,
                api_key
            )
            if resp.status_code != 200:
                st.error(f"API请求失败，状态码：{resp.status_code}，返回信息：{resp.text}")
            else:
                res_data = resp.json()
                msg = res_data["choices"][0]["message"]
                if "tool_calls" in msg and msg["tool_calls"]:
                    tool_call = msg["tool_calls"][0]
                    func_name = tool_call["function"]["name"]
                    func_args = json.loads(tool_call["function"]["arguments"])
                    tool_result = ""
                    if func_name == "add_bill":
                        bill_date = func_args.get("date", datetime.now().strftime("%Y-%m-%d"))
                        merchant = func_args["merchant"]
                        amount = float(func_args["amount"])
                        bill_type = func_args["type"]
                        add_bill(st.session_state.user_id, bill_date, merchant, amount, "未知", bill_type)
                        tool_result = f"已成功添加账单：日期{bill_date}，商户{merchant}，金额{amount}，类型{bill_type}"
                        st.cache_data.clear()
                    elif func_name == "get_summary":
                        tool_result = f"用户财务汇总：总收入{total_income:.2f}，总支出{total_expense:.2f}，结余{balance:.2f}"
                    st.session_state.chat_history.append(msg)
                    st.session_state.chat_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_result
                    })
                    resp2 = get_ai_response(
                        st.session_state.chat_history,
                        get_all_bills(st.session_state.user_id),
                        total_income,
                        total_expense,
                        balance,
                        st.session_state.monthly_living,
                        st.session_state.save_target,
                        st.session_state.city_level,
                        api_key
                    )
                    res2 = resp2.json()
                    ai_answer = res2["choices"][0]["message"]["content"]
                else:
                    ai_answer = msg["content"]
                st.chat_message("assistant").write(ai_answer)
                st.session_state.chat_history.append({"role": "assistant", "content": ai_answer})
        except requests.exceptions.Timeout:
            st.error("请求超时，请稍后重试")
        except requests.exceptions.ConnectionError:
            st.error("网络连接失败，请检查网络")
        except Exception as err:
            st.error(f"发生错误：{str(err)}")

# Export
st.subheader("数据导出")
export_col1, export_col2 = st.columns(2)
csv_data = df.to_csv(index=False, encoding="utf-8-sig")
with export_col1:
    st.download_button(
        label="导出账单明细CSV",
        data=csv_data,
        file_name="明白账_账单明细.csv",
        mime="text/csv"
    )
over_status = "已超支" if total_expense > st.session_state.monthly_living else "未超支"
suggestion = "平时可以减少奶茶和外卖的消费，优先选择食堂就餐，轻松省下不少生活费。"
report_text = f"""明白账 - 财务诊断报告
统计时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
总收入：{total_income:.2f} 元
总支出：{total_expense:.2f} 元
当前结余：{balance:.2f} 元
设定每月生活费：{st.session_state.monthly_living:.2f} 元
是否超支：{over_status}
省钱建议：{suggestion}
"""
with export_col2:
    st.download_button(
        label="导出诊断报告TXT",
        data=report_text,
        file_name="明白账_诊断报告.txt",
        mime="text/plain"
    )
