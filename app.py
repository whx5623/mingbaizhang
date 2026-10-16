# app.py（仅追加注入PWA的markdown代码，其余原有代码保持不变，放在st.set_page_config之后）
import streamlit as st
import pandas as pd
from streamlit_echarts import st_echarts
from datetime import datetime, date
from config import get_deepseek_api_key
from db import init_db, get_all_bills, get_bills_paginated, get_total_bill_count, insert_demo_bills, import_csv_to_bills, register_user, verify_user, add_bill, check_user_exists, add_feedback
import requests
import json
import os
import tomllib

st.set_page_config(layout="wide")

# ========= PWA注入代码 =========
pwa_html = '''
<link rel="manifest" href="static/manifest.json">
<meta name="theme-color" content="#4191e1">
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function() {
    navigator.serviceWorker.register('static/sw.js');
  });
}
</script>
'''
st.markdown(pwa_html, unsafe_allow_html=True)
# ==============================

def get_secret(key: str):
    try:
        val = st.secrets.get(key)
        if val is not None:
            return val
    except Exception:
        pass
    try:
        secret_path = ".streamlit/secrets.toml"
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as f:
                secrets_data = tomllib.load(f)
                val = secrets_data.get(key)
                if val is not None:
                    return val
    except Exception:
        pass
    return os.environ.get(key)

ZHIPU_API_KEY = get_secret("ZHIPU_API_KEY")

init_db()

# 初始化session状态
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "page" not in st.session_state:
    st.session_state.page = 0
if "ai_call_timestamps" not in st.session_state:
    st.session_state.ai_call_timestamps = []
if "ai_daily_count" not in st.session_state:
    st.session_state.ai_daily_count = 0
if "ai_last_count_date" not in st.session_state:
    st.session_state.ai_last_count_date = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

def logout():
    st.session_state.user_id = None
    st.cache_data.clear()

# ========== 未登录分支 ==========
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

# 校验用户有效性
if not check_user_exists(st.session_state.user_id):
    st.warning("当前用户不存在，请重新登录")
    logout()
    st.rerun()

# ========== 已登录分支，全部业务代码放这里 ==========
@st.cache_data
def load_all_data(user_id):
    return get_all_bills(user_id)

# Sidebar
with st.sidebar:
    st.header("个人设定")
    if "monthly_budget" not in st.session_state:
        st.session_state.monthly_budget = 1500.0
    if "monthly_living" not in st.session_state:
        st.session_state.monthly_living = 1500.0
    if "save_target" not in st.session_state:
        st.session_state.save_target = 200.0
    if "city_level" not in st.session_state:
        st.session_state.city_level = "二线"

    st.session_state.monthly_budget = st.number_input("每月预算", min_value=0.0, value=st.session_state.monthly_budget, step=50.0)
    st.session_state.monthly_living = st.number_input("每月生活费（元）", min_value=0.0, value=st.session_state.monthly_living, step=50.0)
    st.session_state.save_target = st.number_input("每月存钱目标（元）", min_value=0.0, value=st.session_state.save_target, step=50.0)
    st.session_state.city_level = st.selectbox("所在城市", ["一线", "二线", "三线"], index=["一线", "二线", "三线"].index(st.session_state.city_level))
    st.divider()

    today = date.today()
    if st.session_state.ai_last_count_date != today:
        st.session_state.ai_daily_count = 0
        st.session_state.ai_last_count_date = today
    remaining = 20 - st.session_state.ai_daily_count
    st.info(f"今日剩余提问次数：{remaining}")

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

# Load full data for metrics and charts
df = load_all_data(st.session_state.user_id)
total_income = df[df["type"] == "收入"]["amount"].sum()
total_expense = df[df["type"] == "支出"]["amount"].sum()
balance = total_income - total_expense

# 计算本月支出
now = datetime.now()
current_month_str = now.strftime("%Y-%m")
df["date_parsed"] = pd.to_datetime(df["date"], errors="coerce")
df_current_month = df[df["date_parsed"].dt.strftime("%Y-%m") == current_month_str]
month_expense = df_current_month[df_current_month["type"] == "支出"]["amount"].sum()
budget = st.session_state.monthly_budget
percent = (month_expense / budget) * 100 if budget > 0 else 0

# 预算预警提示
if budget > 0:
    if month_expense > budget:
        over_amt = month_expense - budget
        st.error(f"🚨 严重超支！本月已超预算 {over_amt:.2f} 元！")
    elif month_expense > budget * 0.8:
        st.warning(f"⚠️ 注意：本月支出已达预算的 {percent:.1f}%！")

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
            st.session_state.page = 0
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

# Bill table with pagination
st.subheader("账单明细")
total_count = get_total_bill_count(st.session_state.user_id)
page_size = st.selectbox("每页条数", options=[10,20,50])
offset = st.session_state.page * page_size
paginated_df = get_bills_paginated(st.session_state.user_id, limit=page_size, offset=offset)

st.dataframe(paginated_df, use_container_width=True)

btn_left, btn_mid, btn_right = st.columns(3)
with btn_left:
    if st.button("上一页"):
        if st.session_state.page > 0:
            st.session_state.page -= 1
            st.rerun()
with btn_mid:
    st.write(f"第 {st.session_state.page + 1} 页，共 {total_count} 条记录")
with btn_right:
    max_page = (total_count -1) // page_size
    if st.button("下一页"):
        if st.session_state.page < max_page:
            st.session_state.page +=1
            st.rerun()

# ECharts 双图表
st.subheader("支出可视化图表")
chart_col1, chart_col2 = st.columns(2)
exp_df = df[df["type"] == "支出"].copy()

# 饼图：各商户支出占比
pie_data = exp_df.groupby("merchant")["amount"].sum().reset_index()
pie_list = []
for _, row in pie_data.iterrows():
    pie_list.append({"name": row["merchant"], "value": round(row["amount"],2)})
pie_option = {
    "title": {"text": "各商户支出占比", "left": "center"},
    "tooltip": {"trigger": "item", "formatter": "{b}<br/>金额：{c} 元"},
    "series": [{
        "type": "pie",
        "radius": ["40%", "70%"],
        "data": pie_list
    }],
    "color": ["#b8e1ff", "#b2e9cc", "#96ccf0", "#a9e6d0", "#c8e8ff"]
}
with chart_col1:
    st_echarts(pie_option, height="400px")

# 折线图：每日消费趋势
daily_exp = exp_df.groupby("date")["amount"].sum().reset_index()
x_data = daily_exp["date"].tolist()
y_data = daily_exp["amount"].round(2).tolist()
line_option = {
    "title": {"text": "每日消费趋势", "left": "center"},
    "tooltip": {"trigger": "axis", "formatter": "日期：{b}<br/>消费金额：{c} 元"},
    "xAxis": {"type": "category", "data": x_data, "axisLabel": {"rotate":30}},
    "yAxis": {"type": "value", "name": "金额(元)"},
    "series": [{
        "type": "line",
        "data": y_data,
        "itemStyle": {"color":"#638bd6"},
        "areaStyle": {"color":"rgba(178, 233, 204,0.3)"}
    }],
    "color":["#62c490"]
}
with chart_col2:
    st_echarts(line_option, height="400px")

# AI Chat
st.subheader("AI 金融导师")
if len(st.session_state.chat_history) == 0:
    st.session_state.chat_history.append({"role": "assistant", "content": "你好！我是明白账AI财务管家，可以帮你分析账单，也可以直接帮你记账，有什么想问我的吗？"})

for msg in st.session_state.chat_history:
    st.chat_message(msg["role"]).write(msg["content"])

user_input = st.chat_input("可以直接让我帮你记账，例如：今天喝奶茶花了15元")
if user_input:
    st.chat_message("user").write(user_input)
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    from ai_agent import get_ai_response
    api_key = get_deepseek_api_key()
    if not api_key:
        st.error("请先设置 DEEPSEEK_API_KEY 环境变量")
    else:
        current_ts = datetime.now().timestamp()
        st.session_state.ai_call_timestamps.append(current_ts)
        st.session_state.ai_call_timestamps = [t for t in st.session_state.ai_call_timestamps if current_ts - t <= 10]

        if len(st.session_state.ai_call_timestamps) > 3:
            warn_msg = "提问太快了，请休息 10 秒后再试"
            st.chat_message("assistant").write(warn_msg)
            st.session_state.chat_history.append({"role": "assistant", "content": warn_msg})
        elif st.session_state.ai_daily_count >=20:
            limit_msg = "今日免费额度已用完，请明天再来"
            st.chat_message("assistant").write(limit_msg)
            st.session_state.chat_history.append({"role": "assistant", "content": limit_msg})
        else:
            st.session_state.ai_daily_count += 1
            remaining = 20 - st.session_state.ai_daily_count
            with st.sidebar:
                st.info(f"今日剩余提问次数：{remaining}")
            try:
                recent_5_bills = df.sort_values("date_parsed", ascending=False).head(5)
                recent_bills_text = recent_5_bills.to_string(index=False)
                resp = get_ai_response(
                    messages=st.session_state.chat_history,
                    api_key=api_key,
                    zhipu_api_key=ZHIPU_API_KEY,
                    total_income=total_income,
                    total_expense=total_expense,
                    balance=balance,
                    recent_bills_text=recent_bills_text
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
                            payment_method = func_args.get("payment_method","未知")
                            bill_type = func_args["type"]
                            add_bill(st.session_state.user_id, bill_date, merchant, amount, payment_method, bill_type)
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
                            messages=st.session_state.chat_history,
                            api_key=api_key,
                            zhipu_api_key=ZHIPU_API_KEY,
                            total_income=total_income,
                            total_expense=total_expense,
                            balance=balance,
                            recent_bills_text=recent_bills_text
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

# 用户反馈模块
with st.expander("💬 给我们提建议"):
    feedback_content = st.text_area("请输入你的反馈内容：", height=120)
    if st.button("提交"):
        if feedback_content.strip():
            add_feedback(st.session_state.user_id, feedback_content.strip())
            st.success("感谢您的反馈！")
        else:
            st.warning("反馈内容不能为空！")
