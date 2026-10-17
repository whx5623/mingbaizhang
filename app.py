import streamlit as st
import pandas as pd
from streamlit_echarts import st_echarts
from datetime import datetime, date, timedelta
from config import get_deepseek_api_key
from db import init_db, get_all_bills, get_bills_paginated, get_total_bill_count, insert_demo_bills, import_csv_to_bills, register_user, verify_user, add_bill, check_user_exists, add_feedback, add_user_credits, is_user_admin, get_admin_operate_data, get_admin_funnel_data
import requests
import json
import hashlib
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
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

def logout():
    st.session_state.user_id = None
    st.session_state.is_admin = False
    st.session_state.pop("last_upload_sig", None)   # 换人登录后不能沿用上一个人的文件签名
    st.session_state.pop("flash", None)             # 上一个人的提示（可能含商户名）不能带给下一个人
    st.session_state["page"] = 0
    st.session_state["chat_history"] = []           # 换人登录不应看到上一个人的 AI 聊天记录
    st.cache_data.clear()

# ========== 未登录分支 ==========
if st.session_state.user_id is None:
    st.title("明白账 - 登录 / 注册")
    # 用户名：库里是 VARCHAR(50)，前端先卡住长度，避免提交后被数据库截断/报错
    username = st.text_input("用户名", max_chars=50, placeholder="3-50 个字符")
    # 密码：长度规则与 db 层 bcrypt 的硬限制保持一致（8 位起，UTF-8 72 字节封顶）
    password = st.text_input("密码", type="password", help="至少 8 位；最长 72 字节（约 24 个汉字）")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("登录"):
            login_name = (username or "").strip()  # 入库时不含首尾空格，登录也要对齐
            if not login_name or not password:
                st.warning("请输入用户名和密码")
            else:
                try:
                    uid = verify_user(login_name, password)
                except Exception as exc:
                    # 数据库连不上等程序性错误：详情只写服务端日志，页面给通用文案
                    # （异常里可能带 host/port/SQL/参数，绝不能出现在未认证页面）
                    print(f"[login] 登录异常：{type(exc).__name__}: {exc}")
                    st.error("登录失败：服务暂时不可用，请稍后再试")
                else:
                    if uid:
                        st.session_state.user_id = uid
                        # 不在这里查 is_admin：紧接的 st.rerun() 会在下一次 run 开头由下方"每次 run 统一复检"重算，
                        # 此处赋值没人读，纯多一次查询
                        st.rerun()
                    else:
                        # “用户不存在”和“密码错误”共用一句提示，避免用户名枚举
                        st.error("用户名或密码错误")
    with col2:
        if st.button("注册"):
            # 全部校验都在调用 db 之前完成：任何一条不过就绝不写库
            reg_name = (username or "").strip()
            # 密码的 UTF-8 字节数提前安全计算：孤立代理字符（如 "\ud800"）会让 encode 抛
            # UnicodeEncodeError，若在按钮回调里没兜住就是整页红屏 traceback
            try:
                pw_bytes = len(password.encode("utf-8"))
            except UnicodeEncodeError:
                pw_bytes = None
            if not reg_name:
                st.warning("用户名不能为空")
            elif len(reg_name) > 50:
                st.warning("用户名不能超过 50 个字符")
            elif not password:
                st.warning("密码不能为空")
            elif len(password) < 8:
                st.warning("密码至少 8 位")
            elif pw_bytes is None:
                st.warning("密码包含无法处理的字符，请换一个")
            elif pw_bytes > 72:
                st.warning("密码过长：最长 72 字节（约 24 个汉字）")
            else:
                try:
                    new_uid = register_user(reg_name, password)
                    if new_uid:
                        # 成功提示走 flash：紧跟 rerun 的 st.success 会被吞掉
                        # 文案固定，不拼接用户名：flash 用 markdown 渲染，用户名可控会变成 [点我](http://evil)
                        st.session_state["flash"] = {"level": "success", "msg": "注册成功，已自动登录"}
                        st.session_state.user_id = new_uid
                        st.rerun()
                    else:
                        st.error("注册失败，未获取用户ID")
                except ValueError as e:
                    # 数据层主动抛的中文业务错误（密码不合规 / 用户名已被占用），可以直接展示
                    st.error(str(e))
                except Exception as e:
                    # 其它异常可能含 SQL / 参数（如 bcrypt hash），只进服务端日志
                    print(f"[register] 注册异常：{type(e).__name__}: {e}")
                    st.error("注册失败：系统繁忙，请稍后再试")
    st.stop()

# 校验用户有效性
if not check_user_exists(st.session_state.user_id):
    st.warning("当前用户不存在，请重新登录")
    logout()
    st.rerun()

# 每次 rerun 都按数据库的真实标记刷新一次，避免撤销管理员后旧会话仍能看到面板
try:
    st.session_state.is_admin = is_user_admin(st.session_state.user_id)
except Exception as exc:
    print(f"[admin] 鉴权检查失败：{type(exc).__name__}: {exc}")
    st.session_state.is_admin = False

# ========== 管理员面板 ==========
if st.session_state.is_admin:
    st.header("管理员面板")
    try:
        st.subheader("运营数据")
        reg_7df, active_7df, sponsor_user_count = get_admin_operate_data()
        col_m1, col_m2, col_m3 = st.columns(3)
        with col_m1:
            st.metric("近 7 个自然日新增注册", reg_7df["count"].sum())
        with col_m2:
            st.metric("近 7 个自然日活跃用户", active_7df["count"].sum())
        with col_m3:
            st.metric("模拟赞助用户总数", sponsor_user_count)

        st.subheader("近 7 个自然日每日注册用户")
        reg_option = {
            "xAxis": {"type": "category", "data": reg_7df["date"].tolist()},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "data": reg_7df["count"].tolist()}],
            "tooltip": {"trigger": "axis"}
        }
        st_echarts(reg_option, height=300)

        st.subheader("近 7 个自然日每日活跃用户")
        active_option = {
            "xAxis": {"type": "category", "data": active_7df["date"].tolist()},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "data": active_7df["count"].tolist()}],
            "tooltip": {"trigger": "axis"}
        }
        st_echarts(active_option, height=300)
        st.divider()

        st.subheader("用户漏斗分析")
        st.caption("注册/活跃按 UTC 自然日统计；登录用户按滚动 168 小时统计，两者口径不同。")
        total_reg, bill_user_count, login_7d_user, ai_user_count = get_admin_funnel_data()
        activate_rate = bill_user_count / total_reg * 100 if total_reg > 0 else 0
        retain_rate = login_7d_user / total_reg * 100 if total_reg > 0 else 0
        ai_usage_rate = ai_user_count / total_reg * 100 if total_reg > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("总注册用户数", total_reg)
        with c2:
            st.metric("填写账单用户", bill_user_count, f"{activate_rate:.1f}% 激活率")
        with c3:
            st.metric("近 7 天（滚动 168 小时）登录用户", login_7d_user, f"{retain_rate:.1f}% 留存率")
        with c4:
            st.metric("使用AI聊天用户", ai_user_count, f"{ai_usage_rate:.1f}% AI使用率")

        if activate_rate < 30:
            st.warning("⚠️ 激活率过低，请优化新手引导")
        if retain_rate < 30:
            st.warning("⚠️ 7天留存率过低，请优化用户留存策略")
        if ai_usage_rate < 30:
            st.warning("⚠️ AI功能使用率偏低，可以增加AI记账引导提示")
        st.divider()
    except Exception as exc:
        # 细节（可能含 SQL / 主机 / 参数）只进服务端日志，页面上只给一句通用文案
        print(f"[admin] 运营数据加载失败：{type(exc).__name__}: {exc}")
        st.error("运营数据暂时不可用，请稍后再试")

# ========== 已登录普通用户业务代码 ==========
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
    st.session_state.save_target = st.number_input("目标存款（元）", min_value=0.0, value=st.session_state.save_target, step=50.0)
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

    st.divider()
    with st.expander("☕ 赞助我们"):
        st.write("感谢你支持明白账！学生免费，如果你愿意请我们喝杯奶茶，可以扫码赞助。")
        st.image("static/qrcode.png")
        if st.button("模拟赞助"):
            add_user_credits(st.session_state.user_id, 10)
            st.success("赞助模拟成功！已为你增加10积分")

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

# 计算本月结余（本月收入 - 本月支出），空数据时 sum() 返回 0，不会抛异常
month_income = df_current_month[df_current_month["type"] == "收入"]["amount"].sum()
month_balance = month_income - month_expense
save_target = st.session_state.save_target

# Page Title
st.title("明白账 - 财务健康看板")

# flash 消息：提交后写入 session_state，跨 rerun 在这里显示一次（success / warning 双态）
flash = st.session_state.pop("flash", None)
if flash:
    if flash.get("level") == "warning":
        st.warning(flash["msg"])
    else:
        st.success(flash["msg"])

# 目标存款达成提示（HTML 中只拼接数字与固定文案，不拼接用户输入字符串）
if save_target > 0 and month_balance >= save_target:
    st.success(f"🎉 恭喜！本月结余 {month_balance:.2f} 元，已达成目标存款 {save_target:.2f} 元，继续保持！")
elif save_target > 0:
    diff = save_target - month_balance
    st.markdown(
        '<div style="background:#f5f5f5;border-left:4px solid #b0b0b0;color:#666;'
        'border-radius:8px;padding:10px 14px;font-size:14px;">'
        f"💪 继续努力！本月结余 {month_balance:.2f} 元，距离目标存款 {save_target:.2f} 元还差 {diff:.2f} 元。"
        "</div>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div style="background:#f5f5f5;border-left:4px solid #b0b0b0;color:#666;'
        'border-radius:8px;padding:10px 14px;font-size:14px;">'
        "💡 请在侧边栏设置目标存款金额，我会帮你盯着本月结余。"
        "</div>",
        unsafe_allow_html=True,
    )

# Upload section
st.subheader("上传账单")
uploaded_file = st.file_uploader("上传账单文件", type=['csv'], key=f"uploader_{st.session_state.user_id}")
if uploaded_file is not None:
    # 文件级去重：st.file_uploader 的值跨 rerun 保留，imported>0 分支里的 st.rerun()
    # 会让脚本重跑并再次读到同一个文件 → 无限「导入→rerun→再导入」循环。
    # 签名 = 用户ID + 文件名 + 字节数 + 内容哈希：
    #   - 带 user_id：换人登录后不沿用上一个人的签名，同名同大小的另一份文件照常导入；
    #   - 带内容哈希：同名同大小但内容不同的文件不会被误判成「已处理过」而静默不导入。
    # 签名只在真正写库成功后落位（见 imported > 0 分支），读取/校验/导入失败都不留签名，用户可立即重试。
    upload_sig = f"{st.session_state.user_id}:{uploaded_file.name}:{uploaded_file.size}:{hashlib.sha256(uploaded_file.getvalue()).hexdigest()[:16]}"
    if st.session_state.get("last_upload_sig") == upload_sig:
        st.info(f"文件「{uploaded_file.name}」本次已处理过；如需重新导入同一文件，请修改内容或改名后重新上传。")
    else:
        df_upload = None
        try:
            df_upload = pd.read_csv(uploaded_file, encoding="utf-8-sig")
            required_cols = ["date", "merchant", "amount", "payment_method", "type"]
            missing_cols = [col for col in required_cols if col not in df_upload.columns]
            if missing_cols:
                st.error(f"文件缺少必要列：{','.join(missing_cols)}")
                df_upload = None  # 缺列不进入导入分支，也不写签名
        except Exception as e:
            st.error(f"读取文件失败：{str(e)}")
            df_upload = None

        if df_upload is not None:
            try:
                ret = import_csv_to_bills(st.session_state.user_id, df_upload)
                imported = ret.get("imported", 0)
                skipped = ret.get("skipped", []) or []
                total = ret.get("total", len(df_upload))

                # 跳过明细最多展示 5 条，避免刷屏
                detail = "；".join(f"第{s['row']}行 {s['reason']}" for s in skipped[:5])
                if len(skipped) > 5:
                    detail += f"；另有 {len(skipped) - 5} 行未列出"

                if imported == 0:
                    # 一行都没写成功：数据层完全没动库，页面数据也没变 → 就地警告，不 rerun、不清缓存
                    # 不写签名：用户可以改文件后用同一个文件名直接重传
                    msg = f"未导入任何账单（共 {total} 行均无效），原有账单未改动。"
                    if detail:
                        msg += f"跳过明细：{detail}"
                    st.warning(msg)
                else:
                    # 真正写库成功之后才记签名：此时库已被替换，再处理一次就会重复导入
                    st.session_state["last_upload_sig"] = upload_sig
                    st.cache_data.clear()
                    st.session_state.page = 0
                    if skipped:
                        st.session_state["flash"] = {
                            "level": "warning",
                            "msg": f"已导入 {imported} 行，跳过 {len(skipped)} 行（原有账单已被替换）。跳过明细：{detail}",
                        }
                    else:
                        st.session_state["flash"] = {
                            "level": "success",
                            "msg": f"成功导入 {imported} 行账单（原有账单已被替换）",
                        }
                    st.rerun()
            except Exception as e:
                # 导入（写库）失败：不写签名，用户可以立刻重试同一个文件
                st.error(f"导入失败：{str(e)}")

# 手动记账
submitted = False  # 先初始化，避免 expander 将来被条件包裹时 submitted 未定义
with st.expander("手动记一笔", expanded=False):
    with st.form("manual_add_bill_form", clear_on_submit=True):
        row1_col1, row1_col2 = st.columns(2)
        with row1_col1:
            bill_date = st.date_input("日期", value=date.today())
        with row1_col2:
            merchant = st.text_input("商户", placeholder="例如：食堂、蜜雪冰城", max_chars=100)
        row2_col1, row2_col2 = st.columns(2)
        with row2_col1:
            amount = st.number_input("金额（元）", min_value=0.0, step=1.0, format="%.2f")
        with row2_col2:
            bill_type = st.selectbox("类型", ["支出", "收入"])
        submitted = st.form_submit_button("保存这笔")

# 提交逻辑放在 expander 外，避免校验/错误提示被折叠面板收起；
# 成功提示用 session_state flash 传递，跨 rerun 在页面顶部展示一次
if submitted:
    merchant = merchant.strip()
    if not bill_date:
        st.warning("请选择日期")
    elif not merchant:
        st.warning("请填写商户名称")
    elif amount <= 0:
        st.warning("金额必须大于 0")
    elif bill_date > date.today():
        st.warning("记账日期不能晚于今天")
    else:
        try:
            add_bill(st.session_state.user_id, bill_date.strftime("%Y-%m-%d"), merchant, float(amount), "手动录入", bill_type)
            st.session_state["flash"] = {"level": "success", "msg": f"已记录：{bill_date.strftime('%Y-%m-%d')} {merchant} ¥{amount:.2f}（{bill_type}）"}
            st.cache_data.clear()          # 清缓存，保证下面的指标/图表/提示条立刻更新
            st.session_state.page = 0      # 回到第一页，新账单按日期倒序通常在最前
            st.rerun()                     # 刷新页面，成功提示由顶部 flash 显示
        except Exception as exc:
            st.error(f"保存失败：{exc}")

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
