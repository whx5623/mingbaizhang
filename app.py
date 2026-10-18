import streamlit as st
import pandas as pd
from streamlit_echarts import st_echarts
from datetime import datetime, date, timedelta
from config import get_deepseek_api_key
from finance_math import compute_irr, payment_from_fee, debt_ratio
from db import init_db, get_all_bills, get_bills_paginated, get_total_bill_count, insert_demo_bills, import_csv_to_bills, register_user, verify_user, add_bill, get_user_auth_state, add_feedback, add_user_credits, get_admin_operate_data, get_admin_funnel_data, get_login_daily_stats, login_events_available, LoginThrottled, set_goal, get_goal, add_to_goal
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
    st.session_state.pop("last_upload_sig", None)        # 换人登录后不能沿用上一个人的文件签名
    st.session_state.pop("last_screenshot_sig", None)     # 截图签名同理：换人后同一张图应能重新识别
    st.session_state.pop("flash", None)             # 上一个人的提示（可能含商户名）不能带给下一个人
    st.session_state.pop("month_review", None)      # AI 复盘含本月消费明细，绝不能带给下一个登录的人
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
                except LoginThrottled as exc:
                    # 业务状态（失败次数过多），不是程序故障：直接把中文文案给用户
                    st.error(str(exc))
                except Exception as exc:
                    # 数据库连不上等程序性错误：详情只写服务端日志，页面给通用文案
                    # （异常里可能带 host/port/SQL/参数，绝不能出现在未认证页面）
                    print(f"[login] 登录异常：{type(exc).__name__}: {exc}")
                    st.error("登录失败：服务暂时不可用，请稍后再试")
                else:
                    if uid:
                        st.session_state.user_id = uid
                        # 不在这里查管理员标记：紧接的 st.rerun() 会在下一次 run 开头的
                        # 统一鉴权里重算，此处赋值没人读，纯多一次查询
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

# 纵深防御：先清掉上一轮可能残留的管理员标记，只有本次鉴权成功才重新赋 True
st.session_state.is_admin = False

# 一次查询同时拿到「用户是否存在」与「是否管理员」，避免两次往返之间的 TOCTOU
try:
    user_exists, user_is_admin = get_user_auth_state(st.session_state.user_id)
except Exception as exc:
    print(f"[auth] 鉴权查询失败：{type(exc).__name__}: {exc}")   # 细节只进服务端日志
    st.error("登录状态校验失败，请稍后刷新重试")
    # 鉴权失败时下方侧边栏（含"退出登录"）不会渲染，这里就地给一个出口，
    # 否则 user_id 仍在、登录页也进不去，用户只能手动清 cookie
    if st.button("退出登录"):
        logout()
        st.rerun()
    st.stop()
if not user_exists:
    st.warning("当前用户不存在，请重新登录")
    logout()
    st.rerun()
st.session_state.is_admin = user_is_admin

# 管理员统计结果缓存：面板一次要跑 3 条（运营数据）+ 4 条（漏斗）SQL，
# 而侧边栏任何输入/按钮都会触发整脚本 rerun —— 不缓存的话管理员点一下就要等 7 条查询。
# 60 秒内复用上一次结果（聚合数据，短暂陈旧可接受；注册/登录变化 60 秒内会自然刷新）。
# ⚠ 只缓存统计，**鉴权不缓存**：get_user_auth_state 每轮都实时查，
#   撤销管理员必须立刻生效（见上面 user_is_admin 那段）。
@st.cache_data(ttl=60, show_spinner=False)
def load_admin_operate_data_cached():
    return get_admin_operate_data()


@st.cache_data(ttl=60, show_spinner=False)
def load_admin_funnel_data_cached():
    return get_admin_funnel_data()


@st.cache_data(ttl=60, show_spinner=False)
def load_admin_login_stats_cached():
    return get_login_daily_stats()


# ========== 管理员面板 ==========
if st.session_state.is_admin:
    st.header("管理员面板")
    try:
        st.subheader("运营数据")
        reg_7df, active_7df, sponsor_user_count = load_admin_operate_data_cached()
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

        st.subheader("近 7 个自然日每日登录用户")
        if not login_events_available():
            st.warning("登录事件表未建（请执行 migrations/009_login_events.sql）：登录限流与登录趋势当前不可用。")
        login_7df = load_admin_login_stats_cached()
        login_option = {
            "xAxis": {"type": "category", "data": login_7df["date"].tolist()},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "data": login_7df["count"].tolist()}],
            "tooltip": {"trigger": "axis"}
        }
        st_echarts(login_option, height=300)
        st.caption("登录趋势来自 login_events（009 执行后开始记录）；日期按 UTC 自然日分桶（北京时间 08:00 换日）；009 执行前该图为空属正常。")
        st.divider()

        st.subheader("用户漏斗分析")
        st.caption("注册/活跃按 UTC 自然日统计；登录用户按滚动 168 小时统计，两者口径不同。")
        total_reg, bill_user_count, login_7d_user, ai_user_count = load_admin_funnel_data_cached()
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
    # ---- 个人设定（含负债率常驻预警）----
    with st.container(border=True):
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

        # ===== 负债率常驻预警（>30% 红色，侧边栏始终可见）=====
        # 数据来源：「算算分期有多贵？」最近一次试算的**每期还款额**（存在 inst_result 里）。
        # 没试算过就没有“本月待还分期”这个数，因此不显示；每个月生活费 <= 0 也无法计算。
        _inst_snapshot = st.session_state.get("inst_result")
        if _inst_snapshot and st.session_state.monthly_living > 0:
            try:
                _ratio = debt_ratio(float(_inst_snapshot["payment"]), float(st.session_state.monthly_living))
            except Exception as _ratio_exc:  # 预警本身绝不能让侧边栏红屏
                print(f"[sidebar] 负债率预警计算失败：{type(_ratio_exc).__name__}: {_ratio_exc}")
                _ratio = None
            if _ratio is not None and _ratio > 0.3:
                st.error(
                    f"🚨 负债率 {_ratio * 100:.1f}% 已超过 30% 警戒线\n\n"
                    f"本月待还分期 {float(_inst_snapshot['payment']):.2f} 元，"
                    f"占每月生活费 {float(st.session_state.monthly_living):.2f} 元的 {_ratio * 100:.1f}%。"
                )

    # ---- 今日提问额度 ----
    with st.container(border=True):
        st.subheader("今日提问额度")
        today = date.today()
        if st.session_state.ai_last_count_date != today:
            st.session_state.ai_daily_count = 0
            st.session_state.ai_last_count_date = today
        remaining = 20 - st.session_state.ai_daily_count
        st.info(f"今日剩余提问次数：{remaining}")

    # ---- 数据导入（CSV 上传 + 演示数据）----
    with st.container(border=True):
        st.subheader("数据导入")
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

        if st.button("一键加载演示数据（答辩专用）"):
            # 用户有效性已在本次 run 开头的统一鉴权里校验过（不存在会被踢出、根本到不了这里），
            # 所以此处不再单独查一次库；数据层的日期校验可能抛 ValueError，这里兜住避免红屏
            try:
                insert_demo_bills(st.session_state.user_id)
                st.cache_data.clear()
                # 成功提示走 flash：紧跟 rerun 的 st.success 会被吞掉（同下方注册分支）
                st.session_state["flash"] = {"level": "success", "msg": "演示数据加载成功"}
                st.rerun()
            except Exception as exc:
                print(f"[demo] 演示数据加载失败：{type(exc).__name__}: {exc}")
                st.error("演示数据加载失败，请稍后再试")

    with st.expander("☕ 赞助我们"):
        st.write("感谢你支持明白账！学生免费，如果你愿意请我们喝杯奶茶，可以扫码赞助。")
        st.image("static/qrcode.png")
        if st.button("模拟赞助"):
            add_user_credits(st.session_state.user_id, 10)
            st.success("赞助模拟成功！已为你增加10积分")

    # 退出登录固定放在侧边栏最底部
    st.divider()
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

tab_dash, tab_tools, tab_ai = st.tabs(["📊 财务看板", "💡 实用工具", "🤖 AI 管家"])

with tab_dash:
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

    # 分类列只在迁移 012 执行后才存在：有列就在 merchant 之后展示，没列则原样展示并提示一次
    if "category" in paginated_df.columns:
        # 固定列序（category 紧跟 merchant），其余列保持原顺序补在后面，不依赖 db 返回顺序
        base_cols = ["date", "merchant", "category", "amount", "payment_method", "type"]
        display_cols = [col for col in base_cols if col in paginated_df.columns]
        display_cols += [col for col in paginated_df.columns if col not in display_cols]
        df_display = paginated_df[display_cols].copy()
        # NULL / 空串 / 纯空白统一归一为「其他」，与 db 层口径保持一致
        cat_series = df_display["category"].fillna("其他").astype(str).str.strip()
        df_display["category"] = cat_series.where(cat_series != "", "其他")
        st.dataframe(df_display, use_container_width=True)
    else:
        st.caption("提示：执行 migrations/012 后这里会显示消费分类")
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

    # 环形玫瑰图：本月支出按消费分类聚合（category 列在迁移 012 执行后才存在）
    st.subheader("消费分类占比")
    st.caption("本月支出按消费分类聚合，扇形半径越大代表该类花得越多")
    month_exp_df = df_current_month[df_current_month["type"] == "支出"]
    if "category" not in df_current_month.columns:
        st.info("分类功能未启用：请先执行 migrations/012_bills_category.sql")
    elif month_exp_df.empty:
        st.caption("本月还没有支出，先记几笔吧")
    else:
        # NULL / 空串 / 纯空白统一归到「其他」，与上方明细表口径一致
        month_cat = month_exp_df["category"].fillna("其他").astype(str).str.strip()
        cat_totals = (
            month_exp_df.assign(_category=month_cat.where(month_cat != "", "其他"))
            .groupby("_category")["amount"]
            .sum()
            .sort_values(ascending=False)
        )
        rose_data = [
            {"name": str(cat_name), "value": round(float(cat_amount), 2)}
            for cat_name, cat_amount in cat_totals.items()
            if float(cat_amount) > 0
        ]
        if not rose_data:
            st.caption("本月还没有支出，先记几笔吧")
        else:
            rose_option = {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c} 元 ({d}%)"},
                "legend": {"orient": "vertical", "left": "left"},
                "series": [{
                    "type": "pie",
                    "radius": ["30%", "70%"],
                    "center": ["50%", "55%"],
                    "roseType": "radius",
                    "itemStyle": {"borderRadius": 6},
                    "data": rose_data
                }]
            }
            st_echarts(rose_option, height=320)

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

with tab_tools:
    # ---- 手动记一笔 ----
    with st.container(border=True):
        st.subheader("✏️ 手动记一笔")
        # 手动记账
        submitted = False  # 先初始化，避免 expander 将来被条件包裹时 submitted 未定义
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

    # ---- 分期真实利率试算 ----
    with st.container(border=True):
        st.subheader("💰 算算分期有多贵？")
        st.caption("按现金流 IRR 计算真实年化利率，不是“手续费/本金”的简单除法")

        inst_mode = st.radio(
            "输入方式",
            ["每期还款总额", "每期手续费"],
            horizontal=True,
            key="inst_mode",
        )

        inst_col1, inst_col2 = st.columns(2)
        with inst_col1:
            inst_principal = st.number_input(
                "本金（元）",
                min_value=1.0,
                value=5000.0,
                step=100.0,
                format="%.2f",
                key="inst_principal",
            )
        with inst_col2:
            inst_periods = st.number_input(
                "期数（月）",
                min_value=1,
                max_value=60,
                value=12,
                step=1,
                key="inst_periods",
            )

        # 两种模式下金额的语义不同：总额模式直接就是每期还款，手续费模式要加上每期本金
        if inst_mode == "每期手续费":
            inst_amount = st.number_input(
                "每期手续费（元）",
                min_value=0.0,
                value=450.0,
                step=10.0,
                format="%.2f",
                key="inst_amount",
            )
        else:
            inst_amount = st.number_input(
                "每期还款额（元）",
                min_value=0.0,
                value=450.0,
                step=10.0,
                format="%.2f",
                key="inst_amount",
            )

        inst_clicked = st.button("计算真实利率", key="inst_calc_btn")

        if inst_clicked:
            # 控件本身已有下限，这里再校验一次业务口径：任何一项不合法都不调用后端
            try:
                if inst_principal <= 0:
                    raise ValueError("本金必须大于 0 元")
                if inst_periods < 1 or inst_periods > 60:
                    raise ValueError("期数需在 1~60 期之间")
                if inst_amount <= 0:
                    raise ValueError("每期还款额/手续费必须大于 0 元")
                if inst_mode == "每期手续费":
                    inst_payment = float(
                        payment_from_fee(float(inst_principal), int(inst_periods), float(inst_amount))
                    )
                else:
                    inst_payment = float(inst_amount)
                inst_irr = compute_irr(float(inst_principal), int(inst_periods), inst_payment)
                # 结果存进 session_state：侧边栏改生活费触发 rerun 时，试算结果和负债诊断不会消失
                st.session_state["inst_result"] = {
                    "mode": inst_mode,
                    "annual_rate": float(inst_irr["annual_rate"]),
                    "total_payment": float(inst_irr["total_payment"]),
                    "total_interest": float(inst_irr["total_interest"]),
                    "payment": inst_payment,
                    "periods": int(inst_periods),
                    "principal": float(inst_principal),
                }
            except ValueError as e:
                # 后端主动抛出的中文业务错误，可以直接展示给用户
                st.session_state.pop("inst_result", None)
                st.error(str(e))
            except Exception as e:
                # 其它异常可能包含实现细节，只进服务端日志，页面给通用文案（不红屏）
                st.session_state.pop("inst_result", None)
                print(f"[installment] 真实利率计算失败：{type(e).__name__}: {e}")
                st.error("计算失败，请检查输入")

        inst_result = st.session_state.get("inst_result")
        # 结果只在同一输入方式下展示：切换 radio 后语义已变，旧结果不应继续挂在页面上
        if inst_result and inst_result.get("mode") == inst_mode:
            inst_annual = inst_result["annual_rate"]
            inst_payment = inst_result["payment"]
            inst_periods_done = inst_result["periods"]
            inst_total_payment = inst_result["total_payment"]
            inst_total_interest = inst_result["total_interest"]

            # 醒目结论：标题 + 粗体，不用 unsafe_allow_html；年化 <= 0 时换掉“高达”措辞
            if inst_annual <= 0:
                st.markdown(f"### 这笔分期没有额外利息（年化 {inst_annual * 100:.2f}%）")
            else:
                st.markdown(f"### 这笔分期的真实年化利率高达 **{inst_annual * 100:.2f}%**！")

            st.caption(
                f"每期还款 {inst_payment:.2f} 元 × {inst_periods_done} 期 = 共还 {inst_total_payment:.2f} 元，"
                f"利息/手续费合计 {inst_total_interest:.2f} 元"
            )
            st.warning("这比一般理财收益高多了，建议谨慎超前消费")

            # ---- 负债健康诊断：用本次试算的每期还款额对比每月生活费 ----
            inst_living = st.session_state.monthly_living
            if inst_living <= 0:
                st.info("请先在侧边栏设置每月生活费，才能计算负债率")
            else:
                try:
                    ratio = debt_ratio(inst_payment, inst_living)
                except Exception as e:
                    print(f"[installment] 负债率计算失败：{type(e).__name__}: {e}")
                    ratio = None
                if ratio is None:
                    # 契约里生活费 <= 0 才返回 None（上面已拦住），这里兜底避免任何异常路径红屏
                    st.info("请先在侧边栏设置每月生活费，才能计算负债率")
                else:
                    st.metric("负债率", f"{ratio * 100:.1f}%")
                    if ratio > 0.3:
                        st.error(
                            f"🚨 负债率 {ratio * 100:.1f}% 已超过 30% 警戒线：本月待还分期 {inst_payment:.2f} 元，"
                            f"占每月生活费 {inst_living:.2f} 元的 {ratio * 100:.1f}%。"
                            "建议降低分期金额或延长前先重估还款能力。"
                        )
                    else:
                        st.success(
                            f"✅ 负债率 {ratio * 100:.1f}%，处于可承受范围（未超过 30% 警戒线）。"
                        )

    # ---- 我的心愿单 ----
    with st.container(border=True):
        st.subheader("🏺 我的心愿单")
        try:
            goal = get_goal(st.session_state.user_id)
        except Exception as goal_exc:
            # 数据层不可用（如 011 迁移未执行）：细节只进服务端日志，页面给黄色提示，不红屏
            print(f"[goal] 读取心愿目标失败：{type(goal_exc).__name__}: {goal_exc}")
            goal = None
            goal_available = False
        else:
            goal_available = True

        if not goal_available:
            st.warning("心愿单功能暂不可用（请确认已执行 migrations/011）")
        elif goal is None:
            # 还没有目标：用表单一次性收集名称 / 金额 / 截止日期
            st.caption("给未来的自己存点期待：先定一个小目标，存钱也会变得有点上头 ✨")
            with st.form("goal_form"):
                new_goal_name = st.text_input("心愿名称", max_chars=50, placeholder="换一台新手机")
                new_goal_amount = st.number_input(
                    "目标金额（元）",
                    min_value=1.0,
                    value=1000.0,
                    step=100.0,
                    format="%.2f",
                )
                new_goal_date = st.date_input("目标日期（可选）", value=date.today() + timedelta(days=1))
                goal_submit = st.form_submit_button("创建心愿目标")
            if goal_submit:
                goal_name_clean = (new_goal_name or "").strip()
                if not goal_name_clean:
                    st.warning("先给心愿起个名字吧～")
                else:
                    try:
                        set_goal(
                            st.session_state.user_id,
                            goal_name_clean,
                            float(new_goal_amount),
                            new_goal_date.strftime("%Y-%m-%d") if new_goal_date else None,
                        )
                    except ValueError as goal_err:
                        st.error(str(goal_err))
                    except Exception as goal_err:
                        print(f"[goal] 创建心愿目标失败：{type(goal_err).__name__}: {goal_err}")
                        st.error("创建失败，请稍后再试")
                    else:
                        # 成功提示走 flash：紧跟 rerun 的 st.success 会被吞掉
                        st.session_state["flash"] = {"level": "success", "msg": "心愿目标已创建"}
                        st.rerun()
        else:
            # 已有目标：名称是用户输入，只交给 st.subheader 渲染（纯文本，不拼 HTML）
            st.subheader(str(goal.get("goal_name") or "未命名心愿"))
            goal_progress = float(goal.get("progress") or 0)
            st.progress(min(1.0, max(0.0, goal_progress)))
            goal_current = float(goal.get("current_amount") or 0)
            goal_target = float(goal.get("target_amount") or 0)
            goal_remaining = float(goal.get("remaining") or 0)
            metric_col1, metric_col2, metric_col3 = st.columns(3)
            with metric_col1:
                st.metric("已存", f"¥{goal_current:,.2f}")
            with metric_col2:
                st.metric("目标", f"¥{goal_target:,.2f}")
            with metric_col3:
                st.metric("还差", f"¥{goal_remaining:,.2f}")
            goal_target_date = goal.get("target_date")
            if goal_target_date:
                st.caption(f"目标日期：{goal_target_date}")
            if goal.get("reached"):
                st.success("🎉 心愿达成！你太自律了，给自己一点掌声")

            st.divider()
            deposit_col, month_balance_col = st.columns(2)
            with deposit_col:
                deposit_amount = st.number_input(
                    "存入金额（元）",
                    min_value=1.0,
                    value=50.0,
                    step=10.0,
                    format="%.2f",
                )
                deposit_clicked = st.button("存入心愿", key="goal_deposit_btn")
            with month_balance_col:
                can_save_balance = float(month_balance) > 0
                balance_clicked = st.button(
                    "把本月结余存进去",
                    key="goal_balance_btn",
                    disabled=not can_save_balance,
                )
                if not can_save_balance:
                    st.caption("本月没有结余")

            if deposit_clicked or balance_clicked:
                deposit_value = float(deposit_amount) if deposit_clicked else float(month_balance)
                if deposit_value <= 0:
                    st.info("本月没有结余，等有结余再来存一笔吧")
                else:
                    try:
                        deposit_result = add_to_goal(st.session_state.user_id, deposit_value)
                    except ValueError as deposit_err:
                        st.error(str(deposit_err))
                    except RuntimeError as deposit_err:
                        st.error(str(deposit_err))
                    except Exception as deposit_err:
                        print(f"[goal] 存入心愿失败：{type(deposit_err).__name__}: {deposit_err}")
                        st.error("存入失败，请稍后再试")
                    else:
                        after_progress = 0.0
                        if isinstance(deposit_result, dict):
                            after_progress = float(deposit_result.get("progress") or 0)
                        st.session_state["flash"] = {
                            "level": "success",
                            "msg": f"已存入 ¥{deposit_value:.2f}，心愿进度 {after_progress * 100:.0f}%",
                        }
                        st.rerun()

with tab_ai:
    # AI Chat
    st.subheader("AI 金融导师")

    # ========== AI 月度消费复盘 ==========
    # 放在聊天历史之前：按钮 + 结果块独立渲染，rerun 后仍能看到上一次的复盘
    month_review_clicked = st.button("🧾 一键生成本月消费复盘", key="month_review_btn")
    if month_review_clicked:
        if st.session_state.ai_daily_count >= 20:
            st.warning("今日免费额度已用完，请明天再来")
        else:
            # 组装本月支出统计：Top3 商户 + 单笔最高，空数据时保持 [] / None
            month_expense_df = df_current_month[df_current_month["type"] == "支出"]
            top_merchants = []
            max_single = None
            try:
                if not month_expense_df.empty:
                    merchant_totals = (
                        month_expense_df.groupby("merchant")["amount"]
                        .sum()
                        .sort_values(ascending=False)
                        .head(3)
                    )
                    top_merchants = [
                        {"name": str(name), "amount": float(amount)}
                        for name, amount in merchant_totals.items()
                    ]
                    max_row = month_expense_df.loc[month_expense_df["amount"].idxmax()]
                    max_single = {
                        "merchant": str(max_row["merchant"]),
                        "amount": float(max_row["amount"]),
                        "date": str(max_row["date"]),
                    }
            except Exception as stats_exc:
                # 统计组装失败不该阻断复盘：退化为不带明细的复盘
                print(f"[month_review] 统计组装失败：{type(stats_exc).__name__}: {stats_exc}")
                top_merchants, max_single = [], None

            review_stats = {
                "month": current_month_str,
                "total_expense": float(total_expense),
                "total_income": float(total_income),
                "balance": float(balance),
                "budget": float(budget),
                "over_budget": bool(month_expense > budget),
                "top_merchants": top_merchants,
                "max_single": max_single,
                "save_target": float(st.session_state.save_target),
            }

            # 调用前计一次额度：失败也算一次，避免反复点按钮刷额度
            st.session_state.ai_daily_count += 1
            try:
                from ai_agent import generate_month_review, MonthReviewError
            except ImportError as import_exc:
                print(f"[month_review] 复盘模块不可用：{type(import_exc).__name__}: {import_exc}")
                st.error("复盘生成失败，请稍后再试")
            else:
                try:
                    review_text = generate_month_review(review_stats, get_deepseek_api_key())
                except MonthReviewError as review_err:
                    st.error(str(review_err))
                except ValueError as review_err:
                    st.warning(str(review_err))
                except Exception as review_err:
                    print(f"[month_review] 生成失败：{type(review_err).__name__}: {review_err}")
                    st.error("复盘生成失败，请稍后再试")
                else:
                    # 不 rerun：结果存 session_state 就地在下方渲染，rerun 后也能继续展示
                    st.session_state["month_review"] = {
                        "month": current_month_str,
                        "text": review_text,
                        "over_budget": bool(review_stats["over_budget"]),
                    }

    saved_month_review = st.session_state.get("month_review")
    if saved_month_review and saved_month_review.get("month") == current_month_str:
        st.caption(f"📅 {saved_month_review['month']} 消费复盘 · 由 AI 生成，仅供参考")
        if saved_month_review.get("over_budget"):
            st.info(saved_month_review["text"])
        else:
            st.success(saved_month_review["text"])

    if len(st.session_state.chat_history) == 0:
        st.session_state.chat_history.append({"role": "assistant", "content": "你好！我是明白账AI财务管家，可以帮你分析账单，也可以直接帮你记账，有什么想问我的吗？"})

    for msg in st.session_state.chat_history:
        st.chat_message(msg["role"]).write(msg["content"])

    # 📸 截图记账：放在聊天输入框上方，聊天本身的逻辑不变
    screenshot_file = st.file_uploader(
        "📸 上传账单截图，自动记账",
        type=["png", "jpg", "jpeg"],
        key=f"screenshot_uploader_{st.session_state.user_id}",
    )
    st.caption("支持 png/jpg，单张 ≤5MB；识别后会自动记一笔")
    if screenshot_file is not None:
        # 防重复提交：file_uploader 的值跨 rerun 保留，成功后的 st.rerun() 会让同一张图
        # 再跑一遍 → 重复记账。签名 = 用户ID + 图片内容哈希，同一张图只处理一次。
        screenshot_bytes = screenshot_file.getvalue()
        screenshot_sig = f"{st.session_state.user_id}:{hashlib.sha256(screenshot_bytes).hexdigest()}"
        if st.session_state.get("last_screenshot_sig") == screenshot_sig:
            st.info("这张截图已处理过；如需重新识别，请换一张或刷新页面")
        elif screenshot_file.size > 5 * 1024 * 1024:
            st.warning("图片超过 5MB，请压缩后再上传")
        elif st.session_state.ai_daily_count >= 20:
            # 截图识别也算一次 AI 调用：额度用完只提示，不调用、不计次
            st.warning("今日免费额度已用完，请明天再来")
        else:
            try:
                from ai_agent import BillImageError, extract_bill_from_image
            except ImportError as exc:
                # ai_agent 的识别函数未合入时优雅降级，避免整页红屏（细节只进服务端日志）
                print(f"[screenshot] 截图识别模块不可用：{type(exc).__name__}: {exc}")
                st.error("截图处理失败，请稍后再试")
            else:
                # 失败也计一次（避免反复上传刷额度）；但**不**落签名 —— 失败后允许用同一张图
                # 直接重试（演示时网络抖动不应逼用户刷新页面）。签名只在成功入库后落位。
                st.session_state.ai_daily_count += 1
                try:
                    result = extract_bill_from_image(
                        screenshot_bytes,
                        screenshot_file.type or "image/png",
                        ZHIPU_API_KEY,
                    )
                    add_bill(
                        st.session_state.user_id,
                        result["occurred_at"],
                        result["merchant"],
                        result["amount"],
                        "截图识别",
                        result["bill_type"],
                    )
                except BillImageError as exc:
                    code = getattr(exc, "code", "")
                    if code == "not_bill":
                        st.error("这张截图看起来不是账单，请换一张包含金额和商户的账单截图")
                    elif code == "blurry":
                        st.error("图片太模糊，关键信息看不清，请上传更清晰的原图")
                    elif code == "ambiguous":
                        st.error(str(exc))  # 后端给的已经是中文可操作提示
                    elif code == "no_api_key":
                        st.error("未配置 ZHIPU_API_KEY，截图识别暂不可用")
                    else:
                        # network / http / parse 及其它后端错误：细节只进服务端日志
                        print(f"[screenshot] 截图识别失败（{code}）：{type(exc).__name__}: {exc}")
                        st.error("识别服务暂时不可用，请稍后再试")
                except Exception as exc:
                    print(f"[screenshot] 截图处理异常：{type(exc).__name__}: {exc}")
                    st.error("截图处理失败，请稍后再试")
                else:
                    # 只有成功入库才落签名：同一张图后续 rerun 不会重复记账；
                    # 失败（网络/模糊/非账单…）不落签名，用户可以直接再点上传重试。
                    st.session_state["last_screenshot_sig"] = screenshot_sig
                    st.session_state["flash"] = {
                        "level": "success",
                        "msg": f"已识别您的截图，成功记入一笔{result['amount']:.2f}元{result['bill_type']}",
                    }
                    st.cache_data.clear()      # 让指标/图表/明细立刻反映新账单
                    st.session_state.page = 0  # 新账单按日期倒序，通常在第一页
                    st.rerun()

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

# 用户反馈模块
with st.expander("💬 给我们提建议"):
    feedback_content = st.text_area("请输入你的反馈内容：", height=120)
    if st.button("提交"):
        if feedback_content.strip():
            add_feedback(st.session_state.user_id, feedback_content.strip())
            st.success("感谢您的反馈！")
        else:
            st.warning("反馈内容不能为空！")
