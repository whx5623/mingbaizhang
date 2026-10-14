import streamlit as st
import sqlite3
import pandas as pd
import os
import requests
import plotly.express as px
from datetime import datetime

# -------------------------- 数据库初始化 --------------------------
def init_db():
    conn = sqlite3.connect("finance.db")
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            merchant TEXT,
            amount REAL,
            payment_method TEXT,
            type TEXT
        )
    ''')
    # 检查表是否为空，为空则插入模拟数据
    df_check = pd.read_sql_query("SELECT * FROM bills", conn)
    if len(df_check) == 0:
        sample_data = [
            ("2026-10-01", "食堂", 12.5, "微信", "支出"),
            ("2026-10-02", "兼职收入", 800, "支付宝", "收入"),
            ("2026-10-03", "奶茶店", 18, "微信", "支出"),
            ("2026-10-04", "超市", 56.2, "支付宝", "支出"),
            ("2026-10-05", "外卖", 24, "微信", "支出")
        ]
        c.executemany("INSERT INTO bills(date, merchant, amount, payment_method, type) VALUES (?,?,?,?,?)", sample_data)
    conn.commit()
    conn.close()

def load_demo_data():
    conn = sqlite3.connect("finance.db")
    c = conn.cursor()
    c.execute("DELETE FROM bills")
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
    c.executemany("INSERT INTO bills(date, merchant, amount, payment_method, type) VALUES (?,?,?,?,?)", demo_10rows)
    conn.commit()
    conn.close()

@st.cache_data
def load_data():
    conn = sqlite3.connect("finance.db")
    df = pd.read_sql_query("SELECT * FROM bills", conn)
    conn.close()
    return df

if not os.path.exists("finance.db"):
    init_db()

# -------------------------- 读取数据库数据 --------------------------
df = load_data()
total_income = df[df["type"] == "收入"]["amount"].sum()
total_expense = df[df["type"] == "支出"]["amount"].sum()
balance = total_income - total_expense
recent_3 = df.tail(3)

# -------------------------- 侧边栏个人设定 --------------------------
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
        load_demo_data()
        st.cache_data.clear()
        st.success("演示数据加载成功，请刷新查看")
        st.rerun()

# -------------------------- 页面标题 --------------------------
st.title("明白账 - 财务健康看板")

# -------------------------- 上传账单模块 --------------------------
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
            conn_u = sqlite3.connect("finance.db")
            c_u = conn_u.cursor()
            c_u.execute("DELETE FROM bills")
            conn_u.commit()
            df_upload.to_sql("bills", conn_u, if_exists="append", index=False)
            conn_u.close()
            st.cache_data.clear()
            st.success("账单上传成功！页面即将刷新")
            st.rerun()
    except Exception as e:
        st.error(f"读取文件失败：{str(e)}")

# -------------------------- 财务指标卡片 --------------------------
st.subheader("收支概览")
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("总收入", f"{total_income:.2f}")
with col2:
    st.metric("总支出", f"{total_expense:.2f}")
with col3:
    st.metric("当前结余", f"{balance:.2f}")

# -------------------------- 账单明细表 --------------------------
st.subheader("账单明细")
st.dataframe(df, use_container_width=True)

# -------------------------- Plotly 商户支出柱状图 --------------------------
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

# -------------------------- AI 金融导师聊天模块 --------------------------
st.subheader("AI 金融导师")
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
    st.session_state.chat_history.append({"role": "assistant", "content": "你好！我是明白账AI财务管家，可以帮你分析账单，给你适合大学生的省钱小建议，有什么想问我的吗？"})

# 渲染对话历史
for msg in st.session_state.chat_history:
    st.chat_message(msg["role"]).write(msg["content"])

user_input = st.chat_input("请输入你的问题")
if user_input:
    st.chat_message("user").write(user_input)
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    # 获取API Key
    try:
        api_key = st.secrets["DEEPSEEK_API_KEY"]
    except Exception:
        api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        st.error("请先设置 DEEPSEEK_API_KEY 环境变量")
    else:
        # 拼接财务背景信息
        recent_text = recent_3.to_string(index=False)
        background_info = f"""【实时财务数据】
总收入：{total_income:.2f}元
总支出：{total_expense:.2f}元
当前结余：{balance:.2f}元
每月生活费设定：{st.session_state.monthly_living:.2f}元
存钱目标：{st.session_state.save_target:.2f}元
所在城市等级：{st.session_state.city_level}
最近3笔账单：
{recent_text}
"""
        system_prompt = f"""你是「明白账」的 AI 财务管家，专为大学生服务，语气亲切、不爹味。
你需要结合下面的实时财务数据回答用户问题：
{background_info}
回答要求：必须结合这些实时数据回答。如果用户超支了，提醒他并给出奶茶、外卖等校园场景的省钱建议。
"""
        messages = [{"role": "system", "content": system_prompt}] + st.session_state.chat_history
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 0.7
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload, timeout=20)
            if resp.status_code != 200:
                st.error(f"API请求失败，状态码：{resp.status_code}，返回信息：{resp.text}")
            else:
                res_data = resp.json()
                ai_answer = res_data["choices"][0]["message"]["content"]
                st.chat_message("assistant").write(ai_answer)
                st.session_state.chat_history.append({"role": "assistant", "content": ai_answer})
        except requests.exceptions.Timeout:
            st.error("请求超时，请稍后重试")
        except requests.exceptions.ConnectionError:
            st.error("网络连接失败，请检查网络")
        except Exception as err:
            st.error(f"发生错误：{str(err)}")

# -------------------------- 导出功能模块 --------------------------
st.subheader("数据导出")
export_col1, export_col2 = st.columns(2)

# 导出CSV账单明细
csv_data = df.to_csv(index=False, encoding="utf-8-sig")
with export_col1:
    st.download_button(
        label="导出账单明细CSV",
        data=csv_data,
        file_name="明白账_账单明细.csv",
        mime="text/csv"
    )

# 生成TXT诊断报告
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
