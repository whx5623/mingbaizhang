import streamlit as st
import pandas as pd
from streamlit_echarts import st_echarts
from datetime import datetime, date, timedelta, timezone
from config import get_deepseek_api_key
try:
    from health_score import compute_health_score
except ImportError as _health_import_exc:
    # 并行开发期 health_score.py 可能尚未合入：先降级，绝不让整页红屏
    compute_health_score = None
    print(f"[health] 健康分模块不可用：{type(_health_import_exc).__name__}: {_health_import_exc}")
from finance_math import compute_irr, payment_from_fee, debt_ratio
from db import init_db, get_all_bills, get_bills_paginated, get_total_bill_count, insert_demo_bills, import_csv_to_bills, register_user, verify_user, add_bill, get_user_auth_state, add_feedback, add_user_credits, get_admin_operate_data, get_admin_funnel_data, get_login_daily_stats, login_events_available, LoginThrottled, set_goal, get_goal, add_to_goal, get_budget_details, save_budget_details, generate_smart_budget, query_bills, delete_bills, detect_subscriptions
# 微信/支付宝真实账单导入由并行方提供（db.import_platform_bill）：与其它并行模块同样先降级，
# 尚未合入时侧边栏给出友好提示，而不是整页 ImportError 红屏。
try:
    from db import import_platform_bill
except ImportError as _platform_import_exc:
    import_platform_bill = None
    print(
        f"[platform] 微信/支付宝账单导入后端不可用："
        f"{type(_platform_import_exc).__name__}: {_platform_import_exc}"
    )
# 每周省钱挑战后端由并行方提供（db.get_challenge_state / db.award_challenge_points + migrations/017）：
# 与 get_daily_expense_totals 同样处理——db.py 可能晚于 app.py 合入，这里先降级成 None，
# 卡片内按名字取模块属性（便于测试打桩），函数缺失时只显示「未启用」，绝不让整页 ImportError 红屏。
try:
    from db import get_challenge_state, award_challenge_points
except ImportError as _challenge_import_exc:
    get_challenge_state = None
    award_challenge_points = None
    print(
        f"[challenge] 省钱挑战后端不可用："
        f"{type(_challenge_import_exc).__name__}: {_challenge_import_exc}"
    )
# 消费日历热力图后端由并行方提供（见 db.get_daily_expense_totals）：
# 尚未合入时先降级成 None，页面只显示一行提示，绝不因 ImportError 红屏。
try:
    from db import get_daily_expense_totals
except ImportError as _cal_import_exc:
    get_daily_expense_totals = None
    print(
        f"[calendar] 每日支出统计不可用："
        f"{type(_cal_import_exc).__name__}: {_cal_import_exc}"
    )
import db
import requests
import uuid
import json
import math
import hashlib
import os
import tomllib

# 吃土倒计时后端由并行方提供：尚未合入时先降级成 None，页面整块静默不显示，绝不红屏
try:
    from db import forecast_broke, get_recent_expense, get_username
except ImportError as _forecast_import_exc:
    forecast_broke = None
    get_recent_expense = None
    get_username = None
    print(
        f"[forecast] 预测模块不可用："
        f"{type(_forecast_import_exc).__name__}: {_forecast_import_exc}"
    )

# 海报绘制模块（Pillow 本地出图，不消耗 AI 额度）：缺失只让海报功能降级
try:
    import poster
except ImportError as _poster_import_exc:
    poster = None
    print(
        f"[poster] 海报模块不可用："
        f"{type(_poster_import_exc).__name__}: {_poster_import_exc}"
    )


class _ChartInsightFallbackError(Exception):
    """ai_agent.ChartInsightError 尚未合入时的占位异常（保证 except 子句可用）。"""


st.set_page_config(layout="wide")

# ========= 「记住我」常量 =========
# cookie 名固定下来，前后端（登录写入 / 顶层自动登录 / 退出删除）共用同一份，
# 避免三处字符串漂移导致「退出后还能自动登录」。
REMEMBER_COOKIE_NAME = "mbz_remember"
_REMEMBER_TOKEN_DAYS = 7

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

# ========= 全局视觉主题（集中管理于 styles.py，失败不阻塞业务） =========
# 注入点必须在 st.set_page_config 之后（否则 set_page_config 会抛错），
# 且在任何业务控件渲染之前；异常只打日志，绝不让样式问题拖垮整页。
try:
    from styles import inject_styles
    inject_styles()
except Exception as exc:  # 样式失败绝不能阻塞业务
    print(f"[styles] 注入失败：{type(exc).__name__}: {exc}")
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


# ========== 「记住我」：CookieManager 单例 + 登录写入 + 顶层自动登录 ==========
# 单例原因：extra_streamlit_components 的组件 key 在**同一次脚本 run 内必须唯一**，
# 每次 rerun 都 new 一个同 key 的 CookieManager 会报重复组件错误；存进 session_state
# 后每个 session 只实例化一次（不放在模块级：模块级对象跨 session 共享，
# 而组件状态本身是 per-session 的）。
def _get_cookie_manager():
    manager = st.session_state.get("_remember_cookie_manager")
    if manager is None:
        # 延迟导入：依赖缺失/组件异常时只让「记住我」降级，绝不让整页 import 失败
        from extra_streamlit_components import CookieManager
        manager = CookieManager(key="remember_cookie_manager")
        st.session_state["_remember_cookie_manager"] = manager
    return manager


def _remember_db_func(name):
    """按名字取 db 里的记住我函数；并行开发期尚未提供时返回 None，由调用方降级处理。

    刻意走 db 模块属性而不是 `from db import ...`：db.py 由并行方提供，
    函数可能晚于 app.py 落地；模块属性取用可避免 import 失败，也方便测试打桩。
    """
    return getattr(db, name, None)


def _challenge_db_func(name):
    """按名字取 db 里的省钱挑战函数；缺失时返回 None，由卡片降级成「未启用」。

    优先走 db 模块属性（与 _remember_db_func 同理：并行开发期可缺失、也方便测试打桩），
    再退回顶部 `from db import ...` 的绑定，保证两种打桩方式都能生效。
    """
    func = getattr(db, name, None)
    if func is None:
        func = globals().get(name)
    return func


def _issue_remember_token(uid):
    """登录成功且勾选「记住我」时调用：先写库（存 sha256），成功后才写浏览器 cookie。

    任何一步失败都只降级 + 提示，**绝不影响登录本身**。
    """
    _token = uuid.uuid4().hex
    # 用 UTC 计算过期时间：db.set_remember_token 把 naive 时间当 UTC 处理，
    # 这里若用本地时间（UTC+8）会让库里的有效期比 cookie 多 8 小时，口径不一致。
    _expires_at = datetime.now(timezone.utc) + timedelta(days=_REMEMBER_TOKEN_DAYS)
    _set_remember = _remember_db_func("set_remember_token")
    if _set_remember is None:
        print("[remember] db.set_remember_token 不可用（请确认已执行 migrations/016）")
        st.warning("记住我暂不可用（请确认已执行 migrations/016）")
        return
    try:
        _set_remember(uid, _token, _expires_at)
    except Exception as _remember_exc:
        # 016 未执行时 db 层抛 RuntimeError，这里一并兜住；日志只写异常类型，不写 token
        print(f"[remember] 写入 remember token 失败：{type(_remember_exc).__name__}: {_remember_exc}")
        # 登录成功后会立刻 st.rerun()，st.warning 会被这次 rerun 吞掉；
        # 额外塞一条 flash，让用户在登录后的看板上仍能看到「记住我未生效」。
        st.warning("记住我暂不可用（请确认已执行 migrations/016）")
        st.session_state["flash"] = {"level": "warning", "msg": "记住我暂不可用（请确认已执行 migrations/016）"}
        return
    try:
        _get_cookie_manager().set(
            REMEMBER_COOKIE_NAME,
            _token,
            path="/",
            expires_at=_expires_at,
            # localhost 被浏览器视为安全上下文，secure=True 在本地也能写入；
            # 纯 http 内网部署需改为 False，否则浏览器会静默丢弃该 cookie。
            secure=True,
            same_site="lax",
        )
    except Exception as _cookie_exc:
        # 库里有 token 但浏览器没写成功：本次登录照常，下次会重新覆盖，不影响使用
        print(f"[remember] 写入 remember cookie 失败：{type(_cookie_exc).__name__}: {_cookie_exc}")
        st.warning("记住我暂不可用（浏览器未能保存登录状态）")


# 顶层自动登录：必须在「未登录分支」之前执行，否则登录页会先 st.stop() 掉。
# 首次 get_all() 在真实浏览器里可能返回 None / {}（iframe 还没把 cookie 回传），
# 这时**不能**写 auto_login_checked：组件就绪后会触发 rerun，下一轮再读；
# 只有真正拿到了 token 才标记「已尝试」，坏 token 不会每次 rerun 都查一次库。
if not st.session_state.get("user_id"):
    _remember_token = st.session_state.get("remember_token_cache")
    if _remember_token is None:
        try:
            _cookies = _get_cookie_manager().get_all() or {}
        except Exception as _cookie_exc:
            # 组件缺失 / 首次未就绪 / 浏览器事件异常 → 静默降级成普通登录
            print(f"[remember] 读取 cookie 失败：{type(_cookie_exc).__name__}: {_cookie_exc}")
            _cookies = {}
        # 只接受 dict：None / 组件占位值一律当作「没有 cookie」
        _token_in_cookie = _cookies.get(REMEMBER_COOKIE_NAME) if isinstance(_cookies, dict) else None
        if isinstance(_token_in_cookie, str) and _token_in_cookie:
            # 缓存到 session：同一 session 不再重复渲染组件读 cookie（测试也依赖这一点）
            st.session_state["remember_token_cache"] = _token_in_cookie
            _remember_token = _token_in_cookie
    if _remember_token and not st.session_state.get("auto_login_checked"):
        st.session_state["auto_login_checked"] = True
        _remember_user = None
        _lookup_remember = _remember_db_func("get_user_by_remember_token")
        if _lookup_remember is None:
            print("[remember] db.get_user_by_remember_token 不可用（请确认已执行 migrations/016）")
        else:
            try:
                _remember_user = _lookup_remember(_remember_token)
            except RuntimeError as _remember_exc:
                # 016 未执行（db 层主动抛的中文提示）：当作未登录，绝不红屏
                print(f"[remember] 自动登录不可用（016 未执行？）：{type(_remember_exc).__name__}: {_remember_exc}")
                _remember_user = None
            except Exception as _remember_exc:
                print(f"[remember] 自动登录查询异常：{type(_remember_exc).__name__}: {_remember_exc}")
                _remember_user = None
        if _remember_user:
            # 查询失败时**不删 cookie**：可能是 016 未执行或临时故障，删了会让用户莫名掉登录
            _remember_uid = _remember_user.get("user_id") if isinstance(_remember_user, dict) else None
            if _remember_uid:
                # 与手动登录成功分支完全一致：只写 user_id，紧接的 st.rerun() 会在下一次
                # run 开头的统一鉴权里补 is_admin；page / ai_daily_count / ai_last_count_date /
                # chat_history 等默认值已在本次 run 顶部初始化过，无需重复设置。
                st.session_state.user_id = _remember_uid
                st.session_state["flash"] = {"level": "success", "msg": "已自动登录"}
                st.rerun()


def logout():
    # 「记住我」双清：先清库（需要当前 user_id），再删浏览器 cookie，最后才清 session。
    # 顺序关键：delete() 会触发组件 rerun，若此时 user_id / cookie 还在就可能又自动登回来；
    # 因此先把 auto_login_checked 置 True，本轮/下一轮都不会再尝试自动登录。
    st.session_state["auto_login_checked"] = True
    _logout_uid = st.session_state.get("user_id")
    if _logout_uid is not None:
        _clear_remember = _remember_db_func("clear_remember_token")
        if _clear_remember is None:
            print("[remember] db.clear_remember_token 不可用（请确认已执行 migrations/016）")
        else:
            try:
                _clear_remember(_logout_uid)
            except Exception as _clear_exc:
                print(f"[remember] 清除 remember token 失败：{type(_clear_exc).__name__}: {_clear_exc}")
    try:
        _get_cookie_manager().delete(REMEMBER_COOKIE_NAME)
    except Exception as _delete_exc:
        # cookie 不存在时组件内部 del self.cookies[...] 会 KeyError：没有可删的，忽略即可
        print(f"[remember] 删除 remember cookie 失败：{type(_delete_exc).__name__}: {_delete_exc}")
    st.session_state.pop("remember_token_cache", None)
    st.session_state.user_id = None
    st.session_state.is_admin = False
    st.session_state.pop("last_upload_sig", None)        # 换人登录后不能沿用上一个人的文件签名
    st.session_state.pop("last_screenshot_sig", None)     # 截图签名同理：换人后同一张图应能重新识别
    st.session_state.pop("flash", None)             # 上一个人的提示（可能含商户名）不能带给下一个人
    st.session_state.pop("month_review", None)      # AI 复盘含本月消费明细，绝不能带给下一个登录的人
    st.session_state.pop("health_comment", None)    # AI 锐评同样基于本月财务数据，换人后必须失效
    st.session_state.pop("_title_level_seen", None) # 换人后不能沿用上一人的头衔等级（否则首屏可能误撒花/漏撒花）
    st.session_state["page"] = 0
    st.session_state["chat_history"] = []           # 换人登录不应看到上一个人的 AI 聊天记录
    st.cache_data.clear()

# ========== 未登录分支 ==========
if st.session_state.user_id is None:
    # 居中布局：左右两列只做留白，所有登录内容都放在 center 里。
    # 用比例列而不是固定像素宽度，宽屏/窄屏都能自适应，不会横向溢出。
    left, center, right = st.columns([1, 2, 1])
    with center:
        # 标题在卡片之上、列内居中：走 markdown 内联样式，避免 st.title 再产生一个左对齐大标题
        st.markdown(
            "<div style='text-align:center; padding: 12px 0 6px;'>"
            "<h1 style='margin:0 0 6px;'>明白账</h1>"
            "<div style='color:#6B7280; font-size:0.95rem;'>欢迎回来，继续记录每一笔</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        # key 以 card_ 开头 → styles.py 的 [class*="st-key-card_"] 自动把它变成
        # 白底 + 16px 圆角 + 柔和阴影的卡片（无需再写任何 CSS）
        with st.container(border=True, key="card_login"):
            st.markdown(
                "<h3 style='text-align:center;margin-top:0'>登录 / 注册</h3>",
                unsafe_allow_html=True,
            )
            # 用户名：库里是 VARCHAR(50)，前端先卡住长度，避免提交后被数据库截断/报错
            username = st.text_input("用户名", max_chars=50, placeholder="3-50 个字符")
            # 密码：长度规则与 db 层 bcrypt 的硬限制保持一致（8 位起，UTF-8 72 字节封顶）
            password = st.text_input("密码", type="password", help="至少 8 位；最长 72 字节（约 24 个汉字）")
            # 「记住我」只属于登录表单：注册分支不读这个值，也不会下发 remember token
            remember_me = st.checkbox("记住我（7天免登录）", key="remember_me_checkbox")
            # 输入框保持单列（卡片内不再套多列输入），窄列里也不会被挤到溢出
            st.caption("首次使用？输入用户名和密码后点「注册」即可自动登录")
            # 按钮横排：左边「登录」primary、右边「注册」secondary，顺序与原先一致
            btn_login, btn_register = st.columns(2)
            with btn_login:
                if st.button("登录", type="primary", use_container_width=True):
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
                                if remember_me:
                                    # 写库失败/组件异常只降级提示，登录继续（见 _issue_remember_token）
                                    _issue_remember_token(uid)
                                # 不在这里查管理员标记：紧接的 st.rerun() 会在下一次 run 开头的
                                # 统一鉴权里重算，此处赋值没人读，纯多一次查询
                                st.rerun()
                            else:
                                # “用户不存在”和“密码错误”共用一句提示，避免用户名枚举
                                st.error("用户名或密码错误")
            with btn_register:
                if st.button("注册", type="secondary", use_container_width=True):
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
        st_echarts(reg_option, height="300px")

        st.subheader("近 7 个自然日每日活跃用户")
        active_option = {
            "xAxis": {"type": "category", "data": active_7df["date"].tolist()},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "data": active_7df["count"].tolist()}],
            "tooltip": {"trigger": "axis"}
        }
        st_echarts(active_option, height="300px")

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
        st_echarts(login_option, height="300px")
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


# ===== AI 图表洞察：同一份 stats 命中进程内缓存，不重复调用 API =====
# 这里刻意**不用** @st.cache_data：额度必须按「是否真的发起 API 请求」计算，
# 而 st.cache_data 不暴露命中/未命中，无法区分。改用显式 dict：
#   · 命中 → 直接返回文本、不调用 AI、页面不扣额度（用户多点几次不会被扣完）；
#   · 未命中 → 调一次 AI，成功后写入缓存，由调用方 +1 额度；
#   · 失败 → 不写缓存（下次仍会重试），由调用方按“已尝试”+1 额度。
# 进程级缓存必须挂在 st.cache_resource 上：Streamlit 每次 rerun 都会重跑本文件，
# 模块级 dict 会被重新初始化为空，缓存等于没做。cache_resource 在进程内只建一次，
# 跨 rerun / 跨会话共享；重启应用即失效。上限 200 条，超出丢最早一条（FIFO 近似）。
_CHART_INSIGHT_CACHE_MAX = 200


@st.cache_resource(show_spinner=False)
def _chart_insight_cache_store() -> dict[str, str]:
    """返回进程级 stats_json -> 洞察文本 的缓存字典（单例）。"""
    return {}


def _chart_insight_cached(stats_json: str, api_key: str) -> tuple[str, bool]:
    """返回 (洞察文本, 是否命中缓存)。

    stats_json 由调用方用 json.dumps(..., sort_keys=True, ensure_ascii=False)
    归一化，同一份图表数据得到同一个 key。
    """
    store = _chart_insight_cache_store()
    if stats_json in store:
        return store[stats_json], True
    from ai_agent import generate_chart_insight

    text = generate_chart_insight(json.loads(stats_json), api_key)
    if len(store) >= _CHART_INSIGHT_CACHE_MAX:
        store.pop(next(iter(store)))
    store[stats_json] = text
    return text, False


# ===== 隐形扣费雷达：AI 取消建议的进程级缓存 =====
# 与图表洞察同理：命中缓存直接复用、不调 API、不扣额度；未命中才调一次。
# key 只取影响建议内容的字段（商户 / 月均 / 次数 / 分类），同一批订阅在
# rerun / 换会话时都能命中；失败兜底结果也写缓存，避免反复打不可用的 API。
@st.cache_resource(show_spinner=False)
def _subscription_advice_store() -> dict:
    """返回进程级缓存：{key: {"map": {merchant: advice}, "fallback": bool}}。"""
    return {}


def _subscription_advice_key(subscriptions: list[dict]) -> str:
    """把订阅关键信息归一化成稳定 key（字段顺序 / 中文都不影响哈希）。"""
    payload = [
        {
            "merchant": item.get("merchant"),
            "monthly_amount": item.get("monthly_amount"),
            "occurrences": item.get("occurrences"),
            "category": item.get("category"),
        }
        for item in subscriptions
        if isinstance(item, dict)
    ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _subscription_local_advice(subscription: dict) -> str:
    """AI 不可用时的本地兜底建议（每个商户一句，不消耗额度）。"""
    merchant = str(subscription.get("merchant") or "该商户")
    try:
        occurrences = int(subscription.get("occurrences") or 0)
    except (TypeError, ValueError):
        occurrences = 0
    try:
        monthly_amount = float(subscription.get("monthly_amount") or 0)
    except (TypeError, ValueError):
        monthly_amount = 0.0
    return (
        f"{merchant} 已连续扣 {occurrences} 个月、每月约 ¥{monthly_amount:.0f}，"
        f"建议去支付宝/微信的「自动续费」里关闭。"
    )


def _get_subscription_advice_fn():
    """按模块属性取 AI 建议函数：并行开发期缺失 / 测试打桩都能感知。"""
    try:
        import ai_agent as _ai_agent_mod
    except Exception as _ai_import_exc:  # pragma: no cover - 依赖缺失时只降级
        print(
            f"[subscription] ai_agent 不可用："
            f"{type(_ai_import_exc).__name__}: {_ai_import_exc}"
        )
        return None
    advice_fn = getattr(_ai_agent_mod, "generate_subscription_advice", None)
    return advice_fn if callable(advice_fn) else None


# Sidebar
with st.sidebar:
    # ---- 积分头衔荣誉卡片（纯本地计算；017 未执行 / 读库失败只隐藏卡片）----
    # 规则见 titles.py：0-100 记账新手 / 101-300 省钱学徒 / 301-600 理财达人 / 601+ 财务自由（预备役）
    _title_points = None
    try:
        _cs = db.get_challenge_state(st.session_state.user_id)
        _title_points = int(_cs.get("points") or 0)
    except Exception as exc:  # 017 未执行（RuntimeError）/ 读库失败 → 不显示卡片，不影响其它侧边栏
        print(f"[title] 读取挑战积分失败：{type(exc).__name__}: {exc}")
        _title_points = None

    if _title_points is not None:
        from titles import get_title
        try:
            _ti = get_title(_title_points)
        except Exception as exc:  # 库里的脏积分（负数等）只隐藏卡片，绝不让整页红屏
            print(f"[title] 计算头衔失败：{type(exc).__name__}: {exc}")
        else:
            # 注意：Streamlit 1.65 的 st.info 会把**行首 emoji** 抽成 alert 图标并从正文删掉；
            # 把 emoji 放进加粗标题内，正文才能稳定保留「🐣 记账新手」这段可断言文案。
            st.info(f"**{_ti['emoji']} {_ti['title']}**\n\n当前积分：**{_ti['points']}** 分")
            if not _ti["is_max"]:
                st.progress(_ti["progress"])
                st.caption(f"距离「{_ti['next_emoji']} {_ti['next_title']}」还差 {_ti['points_to_next']} 分")
            else:
                # 满级也画一条 100% 进度条：视觉上「走完了」，也便于 UI 测试断言 progress == 1.0
                st.progress(_ti["progress"])
                st.caption("已满级 · 财务自由预备役 👑")

            # 升级撒花：只在跨越等级门槛的那一次 rerun 触发。
            # 先把 seen 更新成当前等级，下一次 rerun 两者相等 → 不再撒；
            # 首次进入（seen is None）只记录、不撒花。
            _seen_level = st.session_state.get("_title_level_seen")
            st.session_state["_title_level_seen"] = _ti["level_index"]
            if _seen_level is not None and _ti["level_index"] > _seen_level:
                st.balloons()
                st.success(f"🎉 恭喜升级：{_ti['emoji']} {_ti['title']}！")

    # ---- 个人设定（含负债率常驻预警）----
    with st.container(border=True, key="card_settings"):
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
    with st.container(border=True, key="card_quota"):
        st.subheader("今日提问额度")
        today = date.today()
        if st.session_state.ai_last_count_date != today:
            st.session_state.ai_daily_count = 0
            st.session_state.ai_last_count_date = today
        remaining = 20 - st.session_state.ai_daily_count
        st.info(f"今日剩余提问次数：{remaining}")

    # ---- 数据导入（微信/支付宝真实账单 + 演示数据）----
    with st.container(border=True, key="card_data_import"):
        st.subheader("数据导入")
        uploaded_file = st.file_uploader(
            "上传微信/支付宝账单",
            type=["csv", "txt"],
            key=f"platform_bill_{st.session_state.user_id}",
            help="在微信/支付宝 App 中申请导出账单，下载 CSV 后直接上传；支持 GBK/GB18030 编码与说明表头",
        )
        st.caption(
            "微信：我 → 服务 → 钱包 → 账单 → 常见问题 → 下载账单；"
            "支付宝：我的 → 账单 → 开具交易流水证明"
        )
        if uploaded_file is not None:
            # 文件级去重：st.file_uploader 的值跨 rerun 保留，成功分支里的 st.rerun()
            # 会让脚本重跑并再次读到同一个文件 → 无限「导入→rerun→再导入」循环。
            # 签名 = 用户ID + 文件名 + 字节数 + 内容哈希：
            #   - 带 user_id：换人登录后不沿用上一个人的签名，同名同大小的另一份文件照常导入；
            #   - 带内容哈希：同名同大小但内容不同的文件不会被误判成「已处理过」而静默不导入。
            # 签名只在真正写库成功后落位（见 imported > 0 分支），解析/校验/导入失败都不留签名，用户可立即重试。
            upload_sig = f"{st.session_state.user_id}:{uploaded_file.name}:{uploaded_file.size}:{hashlib.sha256(uploaded_file.getvalue()).hexdigest()[:16]}"
            if st.session_state.get("last_upload_sig") == upload_sig:
                st.info(f"这份文件已处理过（「{uploaded_file.name}」已成功导入）；如需重新导入同一文件，请修改内容或改名后重新上传。")
            else:
                # 优先取 db 模块属性：并行开发期允许晚合入，测试里 patch db.import_platform_bill 也能生效；
                # 再退回顶部 try import 的绑定，两种方式任一可用即可。
                _import_platform_bill = getattr(db, "import_platform_bill", None) or import_platform_bill
                if _import_platform_bill is None:
                    st.error("账单导入功能暂不可用，请稍后再试")
                else:
                    try:
                        # 真实账单可能是 GBK/GB18030 且带说明表头，统一交给数据层解密/识别编码/解析
                        with st.spinner("正在解密并解析真实账单..."):
                            result = _import_platform_bill(
                                st.session_state.user_id,
                                uploaded_file.getvalue(),
                                uploaded_file.name,
                            )
                        imported = int(result.get("imported", 0) or 0)
                        skipped = result.get("skipped", []) or []
                        total = int(result.get("total", imported + len(skipped)) or 0)
                        platform = result.get("platform", "generic") or "generic"
                        encoding = str(result.get("encoding") or "utf-8")
                        platform_cn = {
                            "wechat": "微信",
                            "alipay": "支付宝",
                            "generic": "通用账单",
                        }.get(platform, "通用账单")

                        # 跳过明细跨 rerun 持久化：成功分支 rerun 后上传控件仍持有文件，
                        # 但本次 run 的局部变量已丢失，只能在 session_state 里留一份报告。
                        # 带上 uid，换人登录后不会看到上一个人的账单信息。
                        st.session_state["platform_import_report"] = {
                            "uid": st.session_state.user_id,
                            "platform_cn": platform_cn,
                            "encoding": encoding,
                            "imported": imported,
                            "total": total,
                            "skipped_total": len(skipped),
                            "skipped": skipped[:10],
                        }

                        if imported == 0:
                            # 一行都没写成功：数据层完全没动库，页面数据也没变 → 就地警告，不 rerun、不清缓存。
                            # 不写签名：用户可以改文件后用同一个文件名直接重传。
                            detail = "；".join(f"第 {s['row']} 行：{s['reason']}" for s in skipped[:5])
                            msg = f"未导入任何账单（共 {total} 行），原有账单未改动，可检查文件后重试。"
                            if detail:
                                msg += f" 跳过明细：{detail}"
                            st.warning(msg)
                        else:
                            # 真正写库成功之后才记签名：此时库已更新，再处理一次就会重复导入
                            st.session_state["last_upload_sig"] = upload_sig
                            st.session_state["flash"] = {
                                "level": "success",
                                "msg": f"已导入 {imported} 笔（{platform_cn} · {encoding.upper()} 编码），跳过 {len(skipped)} 笔",
                            }
                            st.cache_data.clear()
                            st.session_state.page = 0
                            st.rerun()
                    except ValueError as exc:
                        # 编码/格式不认识：数据层给的是可直接展示的中文提示。不写签名，同一文件可立即重试。
                        st.error(str(exc))
                    except Exception as exc:
                        # 其它异常细节只进服务端日志，页面给一句可操作的提示
                        print(f"[platform] 账单解析失败：{type(exc).__name__}: {exc}")
                        st.error("账单解析失败，请确认是微信/支付宝导出的 CSV")

        # 上次导入报告（含跳过明细）：成功导入后会 rerun，这里从 session_state 取出来展示
        _last_report = st.session_state.get("platform_import_report")
        if _last_report and _last_report.get("uid") == st.session_state.user_id:
            st.caption(
                f"上次导入：{_last_report['imported']}/{_last_report['total']} 笔成功"
                f"（{_last_report['platform_cn']} · {_last_report['encoding'].upper()}）"
            )
            _last_skipped = _last_report.get("skipped") or []
            if _last_skipped:
                with st.expander(f"查看跳过的 {_last_report.get('skipped_total', len(_last_skipped))} 笔"):
                    for item in _last_skipped[:10]:
                        st.caption(f"第 {item.get('row')} 行：{item.get('reason')}")

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
    # ===== 财务健康分仪表盘（放在最顶部，最醒目的位置）=====
    # 计算失败只降级成一行提示，绝不影响下方的指标卡 / 图表 / 导出
    try:
        # 本月支出按 category 汇总；迁移 012 未执行（无 category 列）时传空 dict
        if "category" in df_current_month.columns:
            _month_exp_df = df_current_month[df_current_month["type"] == "支出"]
            _month_cat = _month_exp_df["category"].fillna("其他").astype(str).str.strip()
            _month_exp_df = _month_exp_df.assign(
                _category=_month_cat.where(_month_cat != "", "其他")
            )
            category_totals = {
                str(_cat): float(_amt)
                for _cat, _amt in _month_exp_df.groupby("_category")["amount"].sum().items()
            }
        else:
            category_totals = {}

        # 心愿进度：没有目标 / 011 未执行 / 读库异常 → 一律 None，健康分照常计算
        try:
            goal_progress = (get_goal(st.session_state.user_id) or {}).get("progress")
            if goal_progress is not None:
                goal_progress = float(goal_progress)
        except Exception as goal_exc:
            print(f"[health] 读取心愿进度失败：{type(goal_exc).__name__}: {goal_exc}")
            goal_progress = None

        if compute_health_score is None:
            raise RuntimeError("health_score 模块未就绪")
        health = compute_health_score(
            month_expense=float(month_expense),
            budget=float(budget),
            month_balance=float(month_balance),
            save_target=float(st.session_state.save_target),
            goal_progress=goal_progress,
            category_totals=category_totals,
        )
        if not isinstance(health, dict) or "score" not in health:
            raise ValueError("compute_health_score 返回值缺少 score")
    except Exception as health_exc:
        print(f"[health] 健康分计算失败：{type(health_exc).__name__}: {health_exc}")
        st.info("健康分暂不可用")
    else:
        st.subheader("🏆 本月财务健康分")
        health_option = {
            "series": [{
                "type": "gauge", "min": 0, "max": 100, "startAngle": 210, "endAngle": -30,
                "progress": {"show": True, "width": 16},
                "axisLine": {"lineStyle": {"width": 16, "color": [
                    [0.6, "#e74c3c"], [0.75, "#f39c12"], [0.9, "#3498db"], [1, "#2ecc71"]]}},
                "pointer": {"width": 6, "itemStyle": {"color": "#333"}},
                "axisTick": {"distance": -20, "length": 6},
                "splitLine": {"distance": -22, "length": 16},
                "axisLabel": {"distance": -32, "fontSize": 12},
                "detail": {"valueAnimation": True, "fontSize": 40, "offsetCenter": [0, "40%"],
                           "formatter": "{value} 分"},
                "title": {"offsetCenter": [0, "68%"], "fontSize": 15},
                "data": [{"value": health["score"], "name": f'{health["grade"]} · {health["title"]}'}]
            }]
        }
        st_echarts(health_option, height="300px")

        health_score = int(health["score"])
        health_grade = str(health["grade"])
        health_title = str(health["title"])
        health_deductions = [str(item) for item in (health.get("deductions") or [])]

        st.markdown(f"### 财务健康分 **{health_score}** 分 · {health_grade} 级 · {health_title}")
        if health_deductions:
            st.caption("主要扣分项：" + " ｜ ".join(health_deductions))

        # ===== AI 锐评：按「月份 + 分数」缓存，正常 rerun 不重复调用 API =====
        comment_key = f"{current_month_str}:{health_score}"
        cached_comment = st.session_state.get("health_comment")
        if (
            isinstance(cached_comment, dict)
            and cached_comment.get("key") == comment_key
            and cached_comment.get("text")
        ):
            st.info(cached_comment["text"])
        elif st.session_state.ai_daily_count >= 20:
            st.caption("今日 AI 额度已用完，明天再看点评")
        else:
            try:
                from ai_agent import generate_health_comment, HealthCommentError
            except ImportError as import_exc:
                print(f"[health] AI 点评模块不可用：{type(import_exc).__name__}: {import_exc}")
                st.caption("AI 点评暂不可用，请稍后再试")
            else:
                with st.spinner("AI 正在锐评你的钱包…"):
                    try:
                        comment_text = generate_health_comment(
                            health_score,
                            health_grade,
                            health_title,
                            health_deductions,
                            get_deepseek_api_key(),
                        )
                    except HealthCommentError as ai_err:
                        # 业务失败（未配 key / 网络 / 解析）：不红屏、不计额度，下次 rerun 可重试
                        st.caption(f"AI 点评暂不可用：{ai_err}")
                    except Exception as ai_exc:
                        print(f"[health] AI 点评失败：{type(ai_exc).__name__}: {ai_exc}")
                        st.caption("AI 点评暂不可用，请稍后再试")
                    else:
                        st.session_state.ai_daily_count += 1
                        st.session_state["health_comment"] = {
                            "key": comment_key,
                            "text": comment_text,
                        }
                        st.info(comment_text)

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

    # ===== 吃土倒计时（近 7 天 / 本月日均 → 月底是否见底）=====
    # 后端函数由并行方提供：缺失 / 任何异常都静默不显示，绝不影响下方预算进度与账单明细。
    _forecast = None
    try:
        try:
            _recent_expense = get_recent_expense(st.session_state.user_id, 7)
        except Exception as _recent_exc:
            print(
                f"[forecast] 读取近7天支出失败："
                f"{type(_recent_exc).__name__}: {_recent_exc}"
            )
            _recent_expense = None
        # 用项目统一的 UTC+8 日报时钟，避免部署机为 UTC 时北京凌晨 00:00-08:00 差一天
        _forecast_today = getattr(db, "_report_today", None)
        _forecast = forecast_broke(
            float(month_expense),
            float(st.session_state.monthly_budget),
            recent_expense=_recent_expense,
            today=_forecast_today() if callable(_forecast_today) else None,
        )
    except Exception as _forecast_exc:
        print(f"[forecast] 预测失败：{type(_forecast_exc).__name__}: {_forecast_exc}")
        _forecast = None

    if isinstance(_forecast, dict):
        if _forecast.get("can_last") is True:
            try:
                _surplus = float(_forecast["projected_surplus"])
            except (KeyError, TypeError, ValueError):
                _surplus = 0.0
            try:
                _daily_avg = float(_forecast["daily_avg"])
            except (KeyError, TypeError, ValueError):
                _daily_avg = 0.0
            try:
                _days_left = int(_forecast["days_left_in_month"])
            except (KeyError, TypeError, ValueError):
                _days_left = 0
            st.success(f"💪 照此速度，月底还能结余 {_surplus:.2f} 元！")
            st.caption(
                f"日均 ¥{_daily_avg:.2f}"
                f"（{'近7天' if _forecast.get('basis') == '7d' else '本月'}口径）"
                f"· 剩余 {_days_left} 天"
            )
        elif _forecast.get("can_last") is False:
            _broke_date_raw = str(_forecast.get("broke_date") or "")
            try:
                _broke_dt = datetime.strptime(_broke_date_raw, "%Y-%m-%d")
            except (TypeError, ValueError):
                _broke_dt = None
            try:
                _days_to_broke = _forecast.get("days_to_broke")
                _days_to_broke = (
                    int(_days_to_broke) if _days_to_broke is not None else None
                )
            except (TypeError, ValueError):
                _days_to_broke = None
            _days_text = f"{_days_to_broke} 天" if _days_to_broke is not None else "几天"
            if _days_to_broke == 0:
                _blink_text = "🚨 今天已经吃土了，本月预算已用完！"
            elif _broke_dt is not None:
                _blink_text = (
                    f"🚨 预计 {_broke_dt.month} 月 {_broke_dt.day} 日破产，"
                    f"距离吃土还有 {_days_text}！建议少喝奶茶。"
                )
            else:
                _blink_text = (
                    f"🚨 预计 {_broke_date_raw or '本月'} 破产，"
                    f"距离吃土还有 {_days_text}！建议少喝奶茶。"
                )
            # 动画样式只注入一次：否则每次 rerun 都会多一段重复 <style>
            if not st.session_state.get("_blink_css_injected"):
                st.markdown(
                    "<style>"
                    "@keyframes mbz-blink {0%,100%{opacity:1;} 50%{opacity:.35;}}"
                    ".mbz-blink{animation:mbz-blink 1s infinite;background:#7f1d1d;"
                    "color:#fff;border-radius:10px;padding:14px;font-size:18px;"
                    "font-weight:700;}"
                    "</style>",
                    unsafe_allow_html=True,
                )
                st.session_state["_blink_css_injected"] = True
            st.markdown(
                f'<div class="mbz-blink">{_blink_text}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.info(str(_forecast.get("reason") or "暂时无法预测"))

    # ===== 本月预算进度（细分预算来自 Tab2「AI 智能预算」的采纳结果）=====
    # 未设置 / 015 未执行 / 读库异常：一律不显示该区块，页面上不会出现空标题。
    try:
        _budget_details = get_budget_details(st.session_state.user_id)
    except Exception as _budget_exc:
        # 静默降级：预算进度只是锦上添花，绝不能影响下方账单明细 / 图表 / 导出
        print(f"[budget] 读取细分预算失败：{type(_budget_exc).__name__}: {_budget_exc}")
        _budget_details = {}
    if isinstance(_budget_details, dict) and _budget_details:
        st.subheader("🎯 本月预算进度")
        # 本月各分类实际支出（category 列在迁移 012 后才存在，缺列时全部按 0 算）
        _spent_by_category = {}
        if "category" in df_current_month.columns:
            _spent_df = df_current_month[df_current_month["type"] == "支出"].copy()
            if not _spent_df.empty:
                _spent_cat = _spent_df["category"].fillna("其他").astype(str).str.strip()
                _spent_df = _spent_df.assign(
                    _category=_spent_cat.where(_spent_cat != "", "其他")
                )
                _spent_by_category = {
                    str(_cat): float(_amt)
                    for _cat, _amt in _spent_df.groupby("_category")["amount"].sum().items()
                }
        # 预算金额统一转 float；脏值（非数字 / NaN / 非正数）直接跳过，一条坏数据不拖垮整块
        _budget_items = []
        for _cat, _amount in _budget_details.items():
            try:
                _budget_value = float(_amount)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(_budget_value) or _budget_value <= 0:
                continue
            _cat_name = str(_cat).strip()
            if not _cat_name:
                continue
            _budget_items.append((_cat_name, _budget_value))
        _budget_items.sort(key=lambda _item: _item[1], reverse=True)
        for _cat_name, _budget_value in _budget_items:
            _spent = float(_spent_by_category.get(_cat_name, 0.0))
            _ratio = _spent / _budget_value
            st.progress(min(1.0, max(0.0, _ratio)))
            _progress_text = f"{_cat_name}：已花 ¥{_spent:.2f} / ¥{_budget_value:.2f}"
            if _ratio > 1:
                _progress_text += f" · ⚠️ 已超支 ¥{_spent - _budget_value:.2f}"
            st.caption(_progress_text)
            if _ratio > 1:
                # 只给一条红色文字提示，不把整块渲染成 error（会影响下方其它内容）
                st.caption(f":red[⚠️ {_cat_name} 已超出预算，建议控制该类支出]")

    # ===== 隐形扣费雷达：疑似连续包月 + 年费预估 + AI 取消建议 =====
    # 后端由并行方提供：取数失败只降级成一行 caption，绝不红屏、不影响下方账单明细。
    _subs = None
    try:
        _subs_today_fn = getattr(db, "_report_today", None)
        _subs_today = _subs_today_fn() if callable(_subs_today_fn) else None
        # 走模块属性取函数：并行开发期缺失 / AppTest 打桩都能感知
        _detect_subs_fn = getattr(db, "detect_subscriptions", None)
        if not callable(_detect_subs_fn):
            _detect_subs_fn = detect_subscriptions
        if not callable(_detect_subs_fn):
            raise RuntimeError("db.detect_subscriptions 不可用")
        _subs = _detect_subs_fn(
            st.session_state.user_id, months=3, today=_subs_today
        )
        if not isinstance(_subs, list):
            _subs = []
        else:
            # 兜住脏返回：非 dict 项直接丢弃，避免下面 .get 把整块打红
            _subs = [item for item in _subs if isinstance(item, dict)]
    except Exception as _subs_exc:
        print(
            f"[subscription] 订阅检测失败："
            f"{type(_subs_exc).__name__}: {_subs_exc}"
        )
        st.caption("隐形扣费雷达暂不可用")
        _subs = None

    if _subs is not None:
        if not _subs:
            st.success("🎉 未发现隐形订阅，干得漂亮！")
        else:
            st.subheader("📡 隐形扣费雷达")
            st.caption("近 3 个月疑似连续包月（同商户 + 相近金额 + 约每月一次）")

            _total_annual = 0.0
            for _sub in _subs:
                try:
                    _total_annual += float(_sub.get("annual_cost") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
            st.metric("预计每年将被扣费", f"¥{_total_annual:,.2f}")

            # 表格：金额统一千分位，置信度只展示「高 / 中 / 低」三档
            _sub_rows = []
            for _sub in _subs:
                try:
                    _sub_monthly = float(_sub.get("monthly_amount") or 0)
                except (TypeError, ValueError, AttributeError):
                    _sub_monthly = 0.0
                try:
                    _sub_annual = float(_sub.get("annual_cost") or 0)
                except (TypeError, ValueError, AttributeError):
                    _sub_annual = 0.0
                try:
                    _sub_occurrences = int(_sub.get("occurrences") or 0)
                except (TypeError, ValueError, AttributeError):
                    _sub_occurrences = 0
                _confidence = {"high": "高", "medium": "中"}.get(
                    str(_sub.get("confidence") or "").strip().lower(), "低"
                )
                _sub_rows.append(
                    {
                        "商户": str(_sub.get("merchant") or "未知商户"),
                        "月均": f"¥{_sub_monthly:,.2f}",
                        "已扣次数": _sub_occurrences,
                        "下次预计": str(_sub.get("next_expected_date") or "-"),
                        "年费": f"¥{_sub_annual:,.2f}",
                        "置信度": _confidence,
                    }
                )
            st.dataframe(
                pd.DataFrame(_sub_rows), hide_index=True, use_container_width=True
            )

            # ---- AI 取消建议：自动生成一次，按订阅批次做进程级缓存 ----
            _advice_key = _subscription_advice_key(_subs)
            _advice_store = _subscription_advice_store()
            _advice_entry = _advice_store.get(_advice_key)
            _advice_map = {}
            _advice_is_fallback = False
            _advice_ready = False

            if isinstance(_advice_entry, dict) and isinstance(
                _advice_entry.get("map"), dict
            ):
                # 缓存命中：不调 API、不扣额度
                _advice_map = _advice_entry["map"]
                _advice_is_fallback = bool(_advice_entry.get("fallback"))
                _advice_ready = True
            elif st.session_state.get("ai_daily_count", 0) >= 20:
                # 额度用完：不调用、不写缓存（额度恢复后同批订阅仍可生成）
                st.caption("今日 AI 额度已用完，暂不生成取消建议")
            else:
                try:
                    _advice_fn = _get_subscription_advice_fn()
                    if _advice_fn is None:
                        raise RuntimeError("ai_agent.generate_subscription_advice 不可用")
                    with st.spinner("AI 正在分析订阅…"):
                        _text_map = _advice_fn(_subs, get_deepseek_api_key())
                    if not isinstance(_text_map, dict):
                        raise ValueError("AI 返回值不是 dict")
                    _advice_map = {
                        str(_merchant): str(_text)
                        for _merchant, _text in _text_map.items()
                        if str(_text or "").strip()
                    }
                    _advice_store[_advice_key] = {
                        "map": _advice_map,
                        "fallback": False,
                    }
                    st.session_state.ai_daily_count += 1
                    _advice_is_fallback = False
                    _advice_ready = True
                except Exception as _advice_exc:
                    # SubscriptionAdviceError / 网络 / 解析 / 依赖缺失统一本地兜底
                    print(
                        f"[subscription] AI 取消建议失败："
                        f"{type(_advice_exc).__name__}: {_advice_exc}"
                    )
                    _advice_map = {
                        str(_sub.get("merchant") or "该商户"): _subscription_local_advice(_sub)
                        for _sub in _subs
                    }
                    _advice_store[_advice_key] = {
                        "map": _advice_map,
                        "fallback": True,
                    }
                    # 已经真的尝试过 API（或至少试过调用），按「已尝试」+1
                    st.session_state.ai_daily_count += 1
                    _advice_is_fallback = True
                    _advice_ready = True

            if _advice_ready:
                for _sub in _subs:
                    _merchant = str(_sub.get("merchant") or "该商户")
                    st.markdown(
                        f"**{_merchant}**："
                        f"{_advice_map.get(_merchant, _subscription_local_advice(_sub))}"
                    )
                if _advice_is_fallback:
                    st.caption("以上为本地建议（AI 暂不可用）")

    # ===== 账单明细：基于全量 df 的本地筛选 + 本地分页 =====
    # 说明：get_bills_paginated / get_total_bill_count 不再用于表格，但 import 保留
    # （导入 / 截图记帐等旧逻辑仍在用 st.session_state.page）；图表与指标始终用全量 df。
    st.subheader("账单明细")

    # 筛选分页独立 state：不复用 st.session_state.page，避免与导入/截图记帐互相干扰
    if "bill_filter_page" not in st.session_state:
        st.session_state.bill_filter_page = 0
    if "bill_page_size" not in st.session_state:
        st.session_state.bill_page_size = 10

    # ---- 筛选控件可选项（只从全量 df 取，空值/空白不入选项）----
    _bill_has_category = "category" in df.columns
    _bill_category_options = (
        sorted({str(_v).strip() for _v in df["category"].dropna().unique() if str(_v).strip()})
        if _bill_has_category
        else []
    )
    _bill_payment_options = (
        sorted({str(_v).strip() for _v in df["payment_method"].dropna().unique() if str(_v).strip()})
        if "payment_method" in df.columns
        else []
    )

    # 导入新数据后旧的多选值可能已不在新选项里：先收敛，避免 multiselect 报错
    _saved_categories = st.session_state.get("bill_categories")
    if _bill_has_category and _saved_categories:
        _valid_categories = [c for c in _saved_categories if c in _bill_category_options]
        if _valid_categories != list(_saved_categories):
            st.session_state["bill_categories"] = _valid_categories
    _saved_payments = st.session_state.get("bill_payments")
    if _saved_payments:
        _valid_payments = [p for p in _saved_payments if p in _bill_payment_options]
        if _valid_payments != list(_saved_payments):
            st.session_state["bill_payments"] = _valid_payments

    # ---- 已启用筛选条件数：按维度计数，用于自动展开 + 提示 ----
    def _bill_active_filter_count():
        _count = 0
        if str(st.session_state.get("bill_kw", "") or "").strip():
            _count += 1
        if st.session_state.get("bill_date_limit", False):
            _count += 1
        if st.session_state.get("bill_type", "全部") != "全部":
            _count += 1
        if _bill_has_category and st.session_state.get("bill_categories"):
            _count += 1
        if st.session_state.get("bill_payments"):
            _count += 1
        try:
            _min_amount = float(st.session_state.get("bill_amount_min", 0.0) or 0.0)
        except (TypeError, ValueError):
            _min_amount = 0.0
        try:
            _max_amount = float(st.session_state.get("bill_amount_max", 0.0) or 0.0)
        except (TypeError, ValueError):
            _max_amount = 0.0
        if _min_amount > 0 or _max_amount > 0:
            _count += 1
        return _count

    _bill_active_count = _bill_active_filter_count()
    if _bill_active_count:
        st.caption(f"🔍 已启用 {_bill_active_count} 个筛选条件")

    _bill_default_date_from = date(now.year, now.month, 1)
    _bill_default_date_to = now.date()

    # 默认折叠；任一筛选生效时 expanded 变为 True（state 跟踪下会重新展开）
    with st.expander(
        "🔍 高级筛选",
        expanded=(_bill_active_count > 0),
        key="bill_filter_expander",
        on_change="rerun",
    ):
        _bill_kw = st.text_input(
            "关键词（商户 / 分类）",
            key="bill_kw",
            placeholder="模糊匹配商户或分类，留空=不限",
        )

        _bill_date_limit = st.checkbox("限定日期范围", value=False, key="bill_date_limit")
        if _bill_date_limit:
            _bill_date_col1, _bill_date_col2 = st.columns(2)
            with _bill_date_col1:
                _bill_date_from = st.date_input(
                    "开始日期", value=_bill_default_date_from, key="bill_date_from"
                )
            with _bill_date_col2:
                _bill_date_to = st.date_input(
                    "结束日期", value=_bill_default_date_to, key="bill_date_to"
                )
        else:
            _bill_date_from = _bill_default_date_from
            _bill_date_to = _bill_default_date_to

        _bill_type_col, _bill_cat_col, _bill_pay_col = st.columns(3)
        with _bill_type_col:
            _bill_type = st.selectbox("类型", ["全部", "支出", "收入"], key="bill_type")
        with _bill_cat_col:
            if _bill_has_category:
                _bill_categories = st.multiselect(
                    "分类", options=_bill_category_options, key="bill_categories"
                )
            else:
                _bill_categories = []
        with _bill_pay_col:
            _bill_payments = st.multiselect(
                "支付方式", options=_bill_payment_options, key="bill_payments"
            )

        _bill_amt_min_col, _bill_amt_max_col, _bill_sort_col = st.columns(3)
        with _bill_amt_min_col:
            _bill_amount_min = st.number_input(
                "最小金额（元）",
                min_value=0.0,
                value=0.0,
                step=1.0,
                format="%.2f",
                key="bill_amount_min",
                help="只看金额 ≥ 该值的账单；0 表示不限",
            )
        with _bill_amt_max_col:
            _bill_amount_max = st.number_input(
                "最大金额（元）",
                min_value=0.0,
                value=0.0,
                step=1.0,
                format="%.2f",
                key="bill_amount_max",
                help="只看金额 ≤ 该值的账单；0 表示不限",
            )
        with _bill_sort_col:
            _bill_sort = st.selectbox(
                "排序",
                ["日期（新→旧）", "日期（旧→新）", "金额（高→低）", "金额（低→高）"],
                key="bill_sort",
            )

        if st.button("重置筛选", key="bill_filter_reset"):
            # 回调场景下按钮在同一轮已渲染完所有筛选控件，pop 后立刻 rerun 重建为默认值
            for _reset_key in [
                "bill_kw",
                "bill_date_limit",
                "bill_date_from",
                "bill_date_to",
                "bill_type",
                "bill_categories",
                "bill_payments",
                "bill_amount_min",
                "bill_amount_max",
                "bill_sort",
            ]:
                st.session_state.pop(_reset_key, None)
            st.session_state["bill_filter_page"] = 0
            st.rerun()

    # 每页条数变化 → 回到第一页（on_change 只改 state，不主动 rerun，避免死循环）
    def _reset_bill_filter_page():
        st.session_state["bill_filter_page"] = 0

    page_size = st.selectbox(
        "每页条数",
        options=[10, 20, 50],
        key="bill_page_size",
        on_change=_reset_bill_filter_page,
    )

    # ---- 过滤逻辑（全部在本地 df 上完成，不改动 df 本身）----
    filtered = df.copy()

    _bill_kw = str(_bill_kw or "").strip()
    if _bill_kw:
        _merchant_hit = (
            filtered["merchant"].fillna("").astype(str).str.contains(
                _bill_kw, case=False, regex=False
            )
        )
        if "category" in filtered.columns:
            _category_hit = (
                filtered["category"].fillna("").astype(str).str.contains(
                    _bill_kw, case=False, regex=False
                )
            )
            _keyword_mask = _merchant_hit | _category_hit
        else:
            _keyword_mask = _merchant_hit
        filtered = filtered[_keyword_mask]

    if _bill_date_limit:
        _bill_date_start = pd.to_datetime(_bill_date_from, errors="coerce")
        _bill_date_end = pd.to_datetime(_bill_date_to, errors="coerce")
        if pd.notna(_bill_date_start) and pd.notna(_bill_date_end):
            if _bill_date_start > _bill_date_end:
                _bill_date_start, _bill_date_end = _bill_date_end, _bill_date_start
            filtered = filtered[
                (filtered["date_parsed"] >= _bill_date_start)
                & (filtered["date_parsed"] < _bill_date_end + pd.Timedelta(days=1))
            ]

    if _bill_type != "全部":
        # 直接用原值比较：空类型不会被算成支出
        filtered = filtered[filtered["type"] == _bill_type]

    if _bill_has_category and _bill_categories:
        _bill_filter_cat = filtered["category"].fillna("其他").astype(str).str.strip()
        _bill_filter_cat = _bill_filter_cat.where(_bill_filter_cat != "", "其他")
        filtered = filtered[_bill_filter_cat.isin(_bill_categories)]

    if _bill_payments:
        _bill_filter_pay = filtered["payment_method"].fillna("").astype(str).str.strip()
        filtered = filtered[_bill_filter_pay.isin(_bill_payments)]

    if _bill_amount_min > 0:
        filtered = filtered[filtered["amount"] >= float(_bill_amount_min)]
    if _bill_amount_max > 0:
        filtered = filtered[filtered["amount"] <= float(_bill_amount_max)]

    if _bill_sort == "日期（新→旧）":
        filtered = filtered.sort_values("date_parsed", ascending=False, kind="stable")
    elif _bill_sort == "日期（旧→新）":
        filtered = filtered.sort_values("date_parsed", ascending=True, kind="stable")
    elif _bill_sort == "金额（高→低）":
        filtered = filtered.sort_values("amount", ascending=False, kind="stable")
    else:
        filtered = filtered.sort_values("amount", ascending=True, kind="stable")

    # ---- 汇总与展示 ----
    _filtered_expense = float(filtered.loc[filtered["type"] == "支出", "amount"].sum())
    _filtered_income = float(filtered.loc[filtered["type"] == "收入", "amount"].sum())
    st.caption(
        f"共 {len(df)} 笔 · 筛选出 {len(filtered)} 笔 · "
        f"支出合计 ¥{_filtered_expense:.2f} · 收入合计 ¥{_filtered_income:.2f}"
    )
    st.caption("图表与指标仍基于全量数据；本表格为筛选结果")

    if filtered.empty:
        st.info("没有符合条件的账单，试试放宽筛选条件")
    else:
        _bill_total_pages = max(1, math.ceil(len(filtered) / int(page_size)))
        _bill_page = int(st.session_state.get("bill_filter_page", 0) or 0)
        _bill_page = max(0, min(_bill_page, _bill_total_pages - 1))  # 超界自动夹到 [1, 页数]
        st.session_state.bill_filter_page = _bill_page
        _bill_page_start = _bill_page * int(page_size)
        _bill_page_df = filtered.iloc[_bill_page_start:_bill_page_start + int(page_size)]

        # 展示口径与改造前一致：有 category 时固定列序（category 紧跟 merchant），
        # 其余列补后；NULL/空串/纯空白归一为「其他」。date_parsed 仅用于筛选，不展示。
        if "category" in _bill_page_df.columns:
            base_cols = ["date", "merchant", "category", "amount", "payment_method", "type"]
            display_cols = [col for col in base_cols if col in _bill_page_df.columns]
            display_cols += [
                col for col in _bill_page_df.columns
                if col not in display_cols and col != "date_parsed"
            ]
            df_display = _bill_page_df[display_cols].copy()
            _cat_series = df_display["category"].fillna("其他").astype(str).str.strip()
            df_display["category"] = _cat_series.where(_cat_series != "", "其他")
            st.dataframe(df_display, use_container_width=True)
        else:
            df_display = _bill_page_df[
                [col for col in _bill_page_df.columns if col != "date_parsed"]
            ]
            st.caption("提示：执行 migrations/012 后这里会显示消费分类")
            st.dataframe(df_display, use_container_width=True)

        btn_left, btn_right = st.columns(2)
        with btn_left:
            if st.button("上一页", key="bill_filter_prev"):
                if _bill_page > 0:
                    st.session_state.bill_filter_page = _bill_page - 1
                    st.rerun()
        with btn_right:
            if st.button("下一页", key="bill_filter_next"):
                if _bill_page < _bill_total_pages - 1:
                    st.session_state.bill_filter_page = _bill_page + 1
                    st.rerun()
        st.write(f"第 {_bill_page + 1} / {_bill_total_pages} 页")

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
            # 用容器包裹，避免父级 flex/样式异常时图表塌陷；异常只降级提示，不红屏。
            try:
                with st.container():
                    st_echarts(rose_option, height="320px")
            except Exception as _rose_exc:
                print(f"[chart] 玫瑰图渲染失败：{type(_rose_exc).__name__}: {_rose_exc}")
                st.warning("玫瑰图暂时无法渲染，请刷新页面重试")

    # ===== 消费日历热力图（本月 / 近三个月）=====
    # 数据由并行方 db.get_daily_expense_totals 提供（连续零填充的每日支出合计）：
    # 函数缺失 / 取数异常 / 返回空 dict 都只降级成一行提示，绝不红屏。
    # 放在玫瑰图之后、AI 洞察之前；与上方玫瑰图不同，这里不依赖 df 的 category 列。
    st.subheader("🗓️ 消费日历热力图")
    st.caption("💡 颜色越红，代表当天花钱越猛。鼠标悬停可查看具体金额。")
    # 默认展示「近三个月」：只在首次访问时写入默认值，用户之后切到「本月」不会被重置。
    if "cal_range" not in st.session_state:
        st.session_state["cal_range"] = "近三个月"
    _cal_range = st.radio("范围", ["本月", "近三个月"], horizontal=True, key="cal_range")
    _cal_months = 1 if _cal_range == "本月" else 3

    # 用项目统一的 UTC+8 日报时钟确定「今天」，避免部署机为 UTC 时月份错位；
    # 时钟异常时退回本机 date.today()，只影响月份归属，不影响其它区块。
    _cal_today_fn = getattr(db, "_report_today", None)
    try:
        _cal_today = _cal_today_fn() if callable(_cal_today_fn) else None
    except Exception as _cal_today_exc:
        print(
            f"[calendar] 读取日报时钟失败："
            f"{type(_cal_today_exc).__name__}: {_cal_today_exc}"
        )
        _cal_today = None
    _cal_base_today = _cal_today if isinstance(_cal_today, date) else date.today()

    # 近 N 个月：以今天所在月为最后一个月往前推（升序，方便日历从上到下按时间排列）
    _cal_month_keys = []
    for _cal_back in range(_cal_months - 1, -1, -1):
        _cal_m = _cal_base_today.month - _cal_back
        _cal_y = _cal_base_today.year
        while _cal_m <= 0:
            _cal_m += 12
            _cal_y -= 1
        _cal_month_keys.append(f"{_cal_y:04d}-{_cal_m:02d}")

    # 优先取 db 模块属性：并行方稍后合入、或测试里 patch db.get_daily_expense_totals 都能生效；
    # 模块属性不存在时退回顶部 guarded import 绑定的名字（同样可能为 None）。
    _cal_lookup = getattr(db, "get_daily_expense_totals", None)
    if _cal_lookup is None:
        _cal_lookup = get_daily_expense_totals

    _cal_daily = None
    _cal_unavailable = False
    if _cal_lookup is None:
        # 后端函数尚未合入：与取数异常同样处理，只给一行提示
        _cal_unavailable = True
    else:
        try:
            _cal_daily = _cal_lookup(
                st.session_state.user_id, months=_cal_months, today=_cal_today
            )
        except Exception as _cal_exc:
            print(f"[calendar] 读取每日支出失败：{type(_cal_exc).__name__}: {_cal_exc}")
            _cal_daily = None
            _cal_unavailable = True

    if _cal_unavailable or not isinstance(_cal_daily, dict) or not _cal_daily:
        # 空 dict 视为后端未能给出连续序列（正常契约至少含零填充日期），同样降级
        st.caption("日历热力图暂不可用")
    else:
        # 归一成 [["YYYY-MM-DD", float], ...]；跳过非法键 / NaN / 负数 / 非数字值，
        # 一条脏数据不拖垮整块日历。
        _cal_points = []
        for _cal_date, _cal_value in _cal_daily.items():
            try:
                _cal_amount = float(_cal_value)
            except (TypeError, ValueError):
                continue
            _cal_date_str = str(_cal_date).strip()
            if (
                len(_cal_date_str) != 10
                or _cal_date_str[4] != "-"
                or _cal_date_str[7] != "-"
                or not math.isfinite(_cal_amount)
                or _cal_amount < 0
            ):
                continue
            _cal_points.append((_cal_date_str, _cal_amount))
        _cal_points.sort(key=lambda _item: _item[0])

        _cal_vmax = max((_value for _, _value in _cal_points), default=0.0)
        if _cal_vmax <= 0:
            # 契约里零填充的日期也会有 0 值：全 0 说明这段时间没有支出，不画空图
            st.caption("这段时间还没有支出记录，先记几笔吧")
        else:
            # streamlit-echarts 的 height 参数是字符串（默认 "300px"）：传整数时前端
            # 会拼成无单位的 style.height → canvas 高度为 0（图表整块空白），必须带 px。
            # 本月压到 240px 减少下方空白；三个月横向并排，300px 足够放下 6 行格子。
            _cal_height = "240px" if _cal_months == 1 else "300px"
            # 三个月：三个 calendar 横向并排（3 × 7 列 ≈ 460px），比纵向堆叠饱满得多，
            # 也不会出现月份标签互相叠压；本月：单个 calendar 居中、格子放大到 26px。
            _cal_lefts = ["3%", "36%", "69%"] if _cal_months > 1 else ["center"]
            _cal_cell = 26 if _cal_months == 1 else 22
            _cal_calendars = []
            _cal_series = []
            for _cal_index, _cal_month_key in enumerate(_cal_month_keys):
                _cal_calendar = {
                    "range": [_cal_month_key],
                    # 固定正方形色块：cellSize 两个数值必须相等；传 ["auto", N] 时
                    # 宽度会被日历布局拉长成细长条（上一版的问题）。
                    "cellSize": [_cal_cell, _cal_cell],
                    "yearLabel": {"show": False},
                    "dayLabel": {"nameMap": "ZH"},
                    "monthLabel": {"nameMap": "ZH", "position": "start"},
                    "itemStyle": {"color": "transparent", "borderWidth": 0},
                    "splitLine": {"show": False},
                    "left": _cal_lefts[_cal_index],
                    "top": "12%",
                }
                _cal_calendars.append(_cal_calendar)
                _cal_series.append(
                    {
                        "type": "heatmap",
                        "coordinateSystem": "calendar",
                        "calendarIndex": _cal_index,
                        "data": [
                            [_date_str, _value]
                            for _date_str, _value in _cal_points
                            if _date_str[:7] == _cal_month_key
                        ],
                        "itemStyle": {"borderRadius": 3},
                    }
                )

            # tooltip 用 ECharts 默认的 item 触发（显示「日期, 值」）：
            # st_echarts 通过 JSON 传参，JS 函数字符串不会被执行，故不写 formatter 函数。
            _cal_option = {
                "tooltip": {"trigger": "item"},
                "visualMap": {
                    "min": 0,
                    "max": _cal_vmax,
                    "orient": "horizontal",
                    "left": "center",
                    "bottom": 0,
                    "calculable": False,
                    "itemWidth": 12,
                    "itemHeight": 80,
                    "inRange": {
                        "color": [
                            "#f1f3f5",
                            "#ffe3e3",
                            "#ffb3b3",
                            "#ff6b6b",
                            "#c92a2a",
                        ]
                    },
                    "text": ["高", "低"],
                },
                "calendar": _cal_calendars,
                "series": _cal_series,
            }
            # 左图右榜：左侧日历热力图，右侧是跟随当前时间范围的花钱 Top 3。
            # 图表保留原有 st.container() + 异常降级（容器在列内，不会视觉双框）。
            _cal_left, _cal_right = st.columns([2, 1], gap="large")
            with _cal_left:
                try:
                    with st.container():
                        st_echarts(_cal_option, height=_cal_height)
                except Exception as _cal_chart_exc:
                    print(
                        f"[chart] 日历热力图渲染失败："
                        f"{type(_cal_chart_exc).__name__}: {_cal_chart_exc}"
                    )
                    st.warning("日历热力图暂时无法渲染，请刷新页面重试")
            with _cal_right:
                st.markdown("#### 🏆 花钱最猛 Top 3")
                st.caption("按当前选择的时间范围统计")
                # 用归一化后的 _cal_points（日期已校验、金额已转 float）：
                # 榜单范围随上方 radio 走（本月 → 本月 Top3；近三个月 → 近三个月 Top3）。
                _cal_top3 = sorted(
                    ((_day, _amount) for _day, _amount in _cal_points if _amount > 0),
                    key=lambda _item: _item[1],
                    reverse=True,
                )[:3]
                if not _cal_top3:
                    st.caption("这段时间还没有支出")
                else:
                    for _cal_rank, (_cal_day_str, _cal_amount) in enumerate(_cal_top3):
                        _cal_medal = ["🥇", "🥈", "🥉"][_cal_rank]
                        _cal_day = datetime.strptime(_cal_day_str, "%Y-%m-%d")
                        st.markdown(
                            f"{_cal_medal} **{_cal_day.month}月{_cal_day.day}日**："
                            f"花了 **{_cal_amount:.0f}** 元"
                        )

    # ===== AI 洞察：本月消费图表解读（玫瑰图下方、数据导出之前）=====
    # 统计口径与上方玫瑰图一致：本月 + 支出。无支出时只给引导，不显示按钮。
    _insight_exp_df = df_current_month[df_current_month["type"] == "支出"]
    _insight_total = float(_insight_exp_df["amount"].sum()) if not _insight_exp_df.empty else 0.0
    if _insight_exp_df.empty or _insight_total <= 0:
        st.caption("本月还没有支出，先记几笔再看 AI 洞察")
    else:
        _insight_tx_count = int(len(_insight_exp_df))

        # Top1 分类：优先 category；迁移 012 未执行（无 category 列）时退回 merchant。
        if "category" in _insight_exp_df.columns:
            _insight_cat = (
                _insight_exp_df["category"].fillna("其他").astype(str).str.strip()
            )
            _insight_grouped = (
                _insight_exp_df.assign(
                    _top_cat=_insight_cat.where(_insight_cat != "", "其他")
                )
                .groupby("_top_cat")["amount"]
                .sum()
            )
        else:
            _insight_grouped = _insight_exp_df.groupby("merchant")["amount"].sum()
        if _insight_grouped.empty:
            _insight_top_category = "其他"
            _insight_top_amount = 0.0
        else:
            _insight_top_category = str(_insight_grouped.idxmax())
            _insight_top_amount = float(_insight_grouped.max())

        # 最高单笔：本月支出中金额最大的一条；空 DataFrame 已在上面拦掉。
        _insight_max_row = _insight_exp_df.loc[_insight_exp_df["amount"].idxmax()]
        _insight_max_merchant_raw = _insight_max_row.get("merchant")
        _insight_max_merchant = (
            "" if pd.isna(_insight_max_merchant_raw) else str(_insight_max_merchant_raw).strip()
        )
        _insight_max_date_raw = _insight_max_row.get("date")
        _insight_max_date = (
            "" if pd.isna(_insight_max_date_raw) else str(_insight_max_date_raw).strip()
        )
        _insight_max_single = {
            "merchant": _insight_max_merchant or "未知商户",
            "amount": float(_insight_max_row["amount"]),
            "date": _insight_max_date,
        }

        # 上月：本月 1 号往前一天 = 上月最后一天，只用 datetime.date，不引入 dateutil。
        _insight_month_first = date(
            int(current_month_str[:4]), int(current_month_str[5:7]), 1
        )
        _insight_prev_last_day = _insight_month_first - timedelta(days=1)
        _insight_prev_month = f"{_insight_prev_last_day.year}-{_insight_prev_last_day.month:02d}"
        _insight_prev_total = float(
            df[
                (df["date_parsed"].dt.strftime("%Y-%m") == _insight_prev_month)
                & (df["type"] == "支出")
            ]["amount"].sum()
        )
        _insight_change_pct = (
            (_insight_total - _insight_prev_total) / _insight_prev_total * 100.0
            if _insight_prev_total > 0
            else None
        )

        _insight_stats = {
            "month": current_month_str,
            "total_expense": _insight_total,
            "tx_count": _insight_tx_count,
            "top_category": _insight_top_category,
            "top_amount": _insight_top_amount,
            "max_single": _insight_max_single,
            "prev_month_expense": _insight_prev_total,
            "change_pct": _insight_change_pct,
        }
        _insight_stats_json = json.dumps(_insight_stats, sort_keys=True, ensure_ascii=False)
        stats_key = hashlib.sha256(_insight_stats_json.encode()).hexdigest()

        if st.button("🔎 AI 洞察", key="chart_insight_btn"):
            if st.session_state.ai_daily_count >= 20:
                st.warning("今日免费额度已用完，请明天再来")
            else:
                # 函数内 import：后端缺失也能落到本地兜底，不让整页红屏。
                try:
                    from ai_agent import ChartInsightError
                except ImportError:
                    ChartInsightError = _ChartInsightFallbackError

                _insight_error = None
                _insight_text = None
                _insight_hit = False
                try:
                    with st.spinner("AI 正在读你的账单…"):
                        _insight_text, _insight_hit = _chart_insight_cached(
                            _insight_stats_json, get_deepseek_api_key()
                        )
                except ChartInsightError as exc:
                    _insight_error = exc
                    st.warning(f"AI 洞察暂不可用：{exc}")
                except Exception as exc:
                    _insight_error = exc
                    print(f"[insight] AI 洞察失败：{type(exc).__name__}: {exc}")
                    st.warning("AI 洞察暂不可用，已用本地分析代替")

                _insight_text = (
                    _insight_text.strip() if isinstance(_insight_text, str) else ""
                )
                if _insight_error is None and _insight_text:
                    st.session_state["chart_insight"] = {
                        "key": stats_key,
                        "text": _insight_text,
                        "fallback": False,
                    }
                    # 只有真正发起 API 请求才扣额度；命中缓存不扣，避免用户
                    # 反复点击把当日 20 次额度耗尽。
                    if not _insight_hit:
                        st.session_state.ai_daily_count += 1
                else:
                    if _insight_error is None:
                        # 后端违约返回空文本：同样走本地兜底，保证页面有可用内容
                        print("[insight] AI 洞察返回空文本，使用本地兜底")
                        st.warning("AI 洞察暂不可用，已用本地分析代替")
                    _insight_fallback = (
                        f"本月你在「{_insight_top_category}」上花得最多"
                        f"（¥{_insight_top_amount:.2f}）"
                    )
                    if _insight_max_single:
                        _insight_fallback += (
                            f"，单笔最高 ¥{_insight_max_single['amount']:.2f}"
                            f"（{_insight_max_single['merchant']}）"
                        )
                    if _insight_change_pct is not None:
                        _insight_fallback += (
                            f"；比上月{'多' if _insight_change_pct > 0 else '少'}花 "
                            f"{abs(_insight_change_pct):.1f}%"
                        )
                    _insight_fallback += "。控制一下冲动消费会更好。"
                    st.session_state["chart_insight"] = {
                        "key": stats_key,
                        "text": _insight_fallback,
                        "fallback": True,
                    }
                    # 失败说明确实发起过请求（异常在 API 调用之后抛出），按“已尝试”扣 1 次；
                    # 失败结果不写缓存，用户再点会重新请求。
                    st.session_state.ai_daily_count += 1

        # 渲染独立于按钮：rerun 后只要数据没变（key 相同）就继续展示；
        # 数据已变化（key 不同）则刻意不渲染旧文本，避免误导。
        _insight_saved = st.session_state.get("chart_insight")
        if (
            isinstance(_insight_saved, dict)
            and _insight_saved.get("key") == stats_key
            and _insight_saved.get("text")
        ):
            st.info(_insight_saved["text"])
            _insight_caption = "AI 洞察 · 基于本月图表数据"
            if _insight_saved.get("fallback"):
                _insight_caption += "（本地兜底）"
            st.caption(_insight_caption)

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

    # 新增：导出「账单明细」当前筛选结果（不影响上面两个全量导出按钮）
    st.download_button(
        "导出当前筛选结果（CSV）",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"bills_filtered_{current_month_str}.csv",
        mime="text/csv",
    )

with tab_tools:
    # ---- 手动记一笔 ----
    with st.container(border=True, key="card_manual"):
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
    with st.container(border=True, key="card_installment"):
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
    with st.container(border=True, key="card_goal"):
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

    # ---- AI 智能预算（放在心愿单之后）----
    with st.container(border=True, key="card_budget"):
        st.subheader("🤖 AI 智能预算")
        try:
            _budget_details = get_budget_details(st.session_state.user_id)
        except RuntimeError as _budget_exc:
            # 015 未执行：这是已知的“功能未启用”，给黄色提示，不红屏
            print(f"[budget] 读取细分预算失败：{type(_budget_exc).__name__}: {_budget_exc}")
            st.warning("智能预算功能未启用（请确认已执行 migrations/015）")
        except Exception as _budget_exc:
            print(f"[budget] 读取细分预算失败：{type(_budget_exc).__name__}: {_budget_exc}")
            st.error("智能预算暂时不可用，请稍后再试")
        else:
            # 已有细分预算：展示表格 + 合计；契约不支持清空，只提供“重新生成”
            _existing_items = {}
            if isinstance(_budget_details, dict):
                for _cat, _amount in _budget_details.items():
                    try:
                        _value = float(_amount)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(_value) or _value <= 0:
                        continue
                    _cat_name = str(_cat).strip()
                    if not _cat_name:
                        continue
                    _existing_items[_cat_name] = _value

            if _existing_items:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"类别": _cat, "预算金额": f"¥{_value:.2f}"}
                            for _cat, _value in _existing_items.items()
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(
                    f"本月总预算 {st.session_state.monthly_budget:.0f} 元，"
                    f"细分合计 {sum(_existing_items.values()):.0f} 元"
                )
                _budget_clicked = st.button("重新生成预算", key="budget_regen_btn")
            else:
                st.caption("让 AI 参考你上个月的账单，给出本月的分类预算建议")
                _budget_clicked = st.button(
                    "根据上月账单，让 AI 帮我做预算", key="budget_gen_btn"
                )

            if _budget_clicked:
                if st.session_state.ai_daily_count >= 20:
                    st.warning("今日免费额度已用完，请明天再来")
                else:
                    # 延迟导入：不在 app 启动时就拉起 ai_agent 的 Supabase 依赖
                    try:
                        from ai_agent import BudgetPlanError
                    except Exception as _ai_import_exc:
                        print(
                            f"[budget] AI 预算模块不可用：{type(_ai_import_exc).__name__}: {_ai_import_exc}"
                        )

                        class BudgetPlanError(Exception):
                            """降级占位：ai_agent 不可用时仅用于异常归口。"""

                    with st.spinner("AI 正在为你规划本月预算…"):
                        try:
                            _plan_result = generate_smart_budget(
                                st.session_state.user_id,
                                float(st.session_state.monthly_budget),
                            )
                        except ValueError as _plan_exc:
                            # 业务前置条件不满足（如上个月还没有账单）：蓝色提示，不消耗额度
                            st.info(str(_plan_exc))
                        except BudgetPlanError as _plan_exc:
                            st.error(str(_plan_exc))
                        except RuntimeError as _plan_exc:
                            st.error(str(_plan_exc))
                        except Exception as _plan_exc:
                            # 其它异常可能含实现细节，只进服务端日志，页面给通用文案
                            if type(_plan_exc).__name__ == "BudgetPlanError":
                                st.error(str(_plan_exc))
                            else:
                                print(
                                    f"[budget] AI 预算生成失败：{type(_plan_exc).__name__}: {_plan_exc}"
                                )
                                st.error("AI 预算生成失败，请稍后再试")
                        else:
                            st.session_state.ai_daily_count += 1
                            # 不 rerun：就地渲染下方方案，避免闪一下空白
                            st.session_state["budget_plan"] = _plan_result

            # 方案跨 rerun 显示：source_month 与「上个月」不一致时视为过期，直接丢弃
            _plan = st.session_state.get("budget_plan")
            _last_month_str = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if isinstance(_plan, dict):
                _plan_source_month = str(_plan.get("source_month") or "")
                if _plan_source_month and _plan_source_month != _last_month_str:
                    st.session_state.pop("budget_plan", None)
                    _plan = None

            if isinstance(_plan, dict) and isinstance(_plan.get("items"), dict) and _plan["items"]:
                _plan_rows = []
                for _cat, _amount in _plan["items"].items():
                    try:
                        _value = float(_amount)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(_value) or _value <= 0:
                        continue
                    _cat_name = str(_cat).strip()
                    if not _cat_name:
                        continue
                    _plan_rows.append({"类别": _cat_name, "预算金额": f"¥{_value:.2f}"})
                if _plan_rows:
                    st.dataframe(
                        pd.DataFrame(_plan_rows),
                        use_container_width=True,
                        hide_index=True,
                    )
                    try:
                        _plan_total = float(
                            _plan.get("total") or sum(float(v) for v in _plan["items"].values())
                        )
                    except (TypeError, ValueError):
                        _plan_total = sum(
                            float(row["预算金额"].lstrip("¥")) for row in _plan_rows
                        )
                    try:
                        _plan_total_budget = float(
                            _plan.get("total_budget") or st.session_state.monthly_budget
                        )
                    except (TypeError, ValueError):
                        _plan_total_budget = float(st.session_state.monthly_budget)
                    st.caption(
                        f"参考 {_plan.get('source_month') or '上月'} 账单 · "
                        f"合计 ¥{_plan_total:.2f} / 总预算 ¥{_plan_total_budget:.2f}"
                    )
                    if _plan.get("scaled"):
                        st.info("已按总预算等比压缩")
                    if st.button("采纳此预算", key="budget_adopt_btn"):
                        try:
                            save_budget_details(st.session_state.user_id, _plan["items"])
                        except Exception as _save_exc:
                            print(
                                f"[budget] 采纳预算失败：{type(_save_exc).__name__}: {_save_exc}"
                            )
                            st.error("保存预算失败，请稍后再试")
                        else:
                            st.session_state["flash"] = {
                                "level": "success",
                                "msg": "预算已采纳，可在财务看板查看进度",
                            }
                            st.session_state.pop("budget_plan", None)
                            st.rerun()

    # ---- 🎤 语音记账（放在 AI 智能预算之后；后端契约：ai_agent.transcribe_audio / extract_bill_from_text）----
    with st.container(border=True, key="card_voice"):
        st.subheader("🎤 语音记账")
        st.caption("按住麦克风说一句，例如：今天食堂花了 25 块（≤30 秒）")
        _voice_audio = st.audio_input(
            "录制语音", key=f"voice_input_{st.session_state.user_id}"
        )
        # 成功路径 rerun 前无法展示识别文本：把上一次结果常驻在卡片里（月份/用户变化不必清理）
        _last_voice = st.session_state.get("last_voice_text")
        if isinstance(_last_voice, dict) and _last_voice.get("text"):
            st.caption(f"上次识别：{_last_voice['text']}")

        if _voice_audio is not None:
            _voice_bytes = _voice_audio.getvalue()
            # 整段录音内容做签名：audio_input 的值跨 rerun 保留，成功后的 st.rerun() 会让同一段
            # 录音再跑一遍 —— 只按「用户ID + 内容哈希」去重，绝不重复记账、也不重复调 API。
            _voice_sig = (
                f"{st.session_state.user_id}:{hashlib.sha256(_voice_bytes).hexdigest()}"
            )
            if st.session_state.get("last_voice_sig") == _voice_sig:
                st.info("这段语音已处理过；如需重新记账，请再录一段")
            elif st.session_state.ai_daily_count >= 20:
                # 额度用完只提示：不转写、不解析、不写库，也不占用签名
                st.warning("今日免费额度已用完，请明天再来")
            else:
                # 从这里开始这段录音就算「已处理」：失败也落签名，避免同一条录音每次 rerun 都
                # 反复调用 API；用户重新录一段会得到新签名，可以立即重试。
                st.session_state["last_voice_sig"] = _voice_sig
                st.audio(_voice_bytes, format="audio/wav")  # 回放，方便用户确认录到的内容
                st.caption(
                    f"已收到录音（约 {_voice_audio.size / 16000:.1f} 秒，按 WAV 16kHz 粗略估算）"
                )

                # 额度口径（与截图记账保持一致）：too_long / too_big / empty_audio / no_api_key
                # 属于**前置失败，不扣额度**；只要真正发起了转写（network / http / empty_text /
                # 解析失败）就扣 1 次，避免同一段录音反复刷免费额度。
                _voice_quota_charged = False
                try:
                    from ai_agent import (
                        transcribe_audio,
                        extract_bill_from_text,
                        AudioTranscribeError,
                        TextBillError,
                    )
                except ImportError as _voice_import_exc:
                    # ai_agent 的语音函数未合入时优雅降级：细节只进服务端日志，不红屏
                    print(
                        f"[voice] 语音记账模块不可用："
                        f"{type(_voice_import_exc).__name__}: {_voice_import_exc}"
                    )
                    st.info("🎤 语音已收到，但转写暂不可用，建议打字或上传截图记账")
                else:
                    try:
                        _voice_text = transcribe_audio(
                            _voice_bytes, "voice.wav", ZHIPU_API_KEY
                        )
                    except AudioTranscribeError as _voice_err:
                        _voice_code = getattr(_voice_err, "code", "")
                        if _voice_code == "too_long":
                            # 时长限制属于前置校验：不记账、不扣额度
                            st.warning("录音超过 30 秒，请说短一点再试")
                        elif _voice_code == "too_big":
                            st.warning("录音文件太大，请缩短录音")
                        elif _voice_code == "empty_audio":
                            st.warning("没有录到声音，请重新录制")
                        elif _voice_code == "empty_text":
                            # 已经真正调用过转写服务，只是没返回文字：扣 1 次
                            st.session_state.ai_daily_count += 1
                            _voice_quota_charged = True
                            st.info("没听清，请再说一遍，尽量靠近麦克风")
                        elif _voice_code == "no_api_key":
                            st.error("未配置智谱 API Key，语音记账暂不可用")
                        else:
                            # network / http / 未知错误：兜底 C —— 录音已收到，但转写暂不可用；
                            # 细节只 print 到服务端，页面绝不出现 traceback
                            st.session_state.ai_daily_count += 1
                            _voice_quota_charged = True
                            print(
                                f"[voice] 转写失败（{_voice_code}）："
                                f"{type(_voice_err).__name__}: {_voice_err}"
                            )
                            st.info("🎤 语音已收到，但转写暂不可用，建议打字或上传截图记账")
                    except Exception as _voice_err:
                        # 未预期异常：页面只给通用文案；真正发起过转写就按口径扣 1 次
                        if not _voice_quota_charged:
                            st.session_state.ai_daily_count += 1
                            _voice_quota_charged = True
                        print(
                            f"[voice] 转写异常：{type(_voice_err).__name__}: {_voice_err}"
                        )
                        st.error("语音处理失败，请稍后再试")
                    else:
                        # 转写成功：额度 +1（后续解析/入库失败也不退，与截图记账口径一致）
                        st.session_state.ai_daily_count += 1
                        _voice_quota_charged = True
                        try:
                            _voice_bill = extract_bill_from_text(
                                _voice_text, get_deepseek_api_key()
                            )
                            add_bill(
                                st.session_state.user_id,
                                _voice_bill["occurred_at"],
                                _voice_bill["merchant"],
                                _voice_bill["amount"],
                                "语音记账",
                                _voice_bill["bill_type"],
                            )
                        except TextBillError as _bill_err:
                            _bill_code = getattr(_bill_err, "code", "")
                            if _bill_code in ("invalid_bill", "parse"):
                                st.info(
                                    f"识别为「{_voice_text}」，"
                                    f"但没提取到完整账单（{_bill_err}），建议手动补全"
                                )
                            elif _bill_code in ("network", "http"):
                                st.warning("账单解析服务暂时不可用，请稍后再试或手动记账")
                            else:
                                st.warning("语音已收到，但解析失败，建议手动记账")
                        except Exception as _bill_exc:
                            # 解析之后的写库失败等：AI 调用已消耗，额度不重复扣
                            print(
                                f"[voice] 语音记账失败："
                                f"{type(_bill_exc).__name__}: {_bill_exc}"
                            )
                            st.error("语音处理失败，请稍后再试")
                        else:
                            # 成功：签名已在调用前落位，这里只补识别文本（跨 rerun 常驻展示）
                            st.session_state["last_voice_text"] = {
                                "text": _voice_text,
                                "merchant": _voice_bill["merchant"],
                            }
                            st.session_state["flash"] = {
                                "level": "success",
                                "msg": (
                                    f"已通过语音为您记账 {_voice_bill['amount']:.2f} 元"
                                    f"（{_voice_bill['merchant']}）"
                                ),
                            }
                            st.cache_data.clear()      # 让指标/图表/明细立刻反映新账单
                            st.session_state.page = 0  # 新账单按日期倒序，通常在第一页
                            st.rerun()

    # ---- 🏆 我的挑战（放在语音记账之后，Tab2 最后一张卡片）----
    # AI 生成每周省钱挑战 + 完成领积分。后端：db.get_challenge_state / db.award_challenge_points
    # （migrations/017）。后端缺失 / 017 未执行时整卡降级成一行提示，绝不红屏。
    with st.container(border=True, key="card_challenge"):
        st.subheader("🏆 我的挑战")
        _challenge_uid = st.session_state.user_id
        _challenge_state = None
        _challenge_ready = True

        _challenge_state_fn = _challenge_db_func("get_challenge_state")
        if _challenge_state_fn is None:
            st.caption("省钱挑战功能未启用（请确认已执行 migrations/017）")
            _challenge_ready = False
        else:
            try:
                _challenge_state = _challenge_state_fn(_challenge_uid)
            except RuntimeError:
                # 017 未执行时数据层主动抛 RuntimeError：整卡只留一行提示
                st.caption("省钱挑战功能未启用（请确认已执行 migrations/017）")
                _challenge_ready = False
            except Exception as _challenge_read_exc:
                # 其它读库异常：细节只进服务端日志，页面给通用文案
                print(
                    f"[challenge] 读取挑战状态失败："
                    f"{type(_challenge_read_exc).__name__}: {_challenge_read_exc}"
                )
                st.caption("挑战暂不可用")
                _challenge_ready = False

        if _challenge_ready:
            if not isinstance(_challenge_state, dict):
                _challenge_state = {}
            try:
                _challenge_points_now = int(_challenge_state.get("points") or 0)
            except (TypeError, ValueError):
                _challenge_points_now = 0

            # ISO 周（周一为一周第一天）：与 db.award_challenge_points 的周次口径保持一致。
            _challenge_iso = date.today().isocalendar()
            current_week = f"{_challenge_iso.year}-W{_challenge_iso.week:02d}"

            st.metric("挑战积分", f"{_challenge_points_now} 分")
            st.caption(f"本周：{current_week}（ISO 周）")

            # 挑战跨 rerun 常驻 session；周次不匹配（换周）视为没有挑战，自动失效。
            _challenge = st.session_state.get("weekly_challenge")
            if not (
                isinstance(_challenge, dict)
                and _challenge.get("week") == current_week
                and isinstance(_challenge.get("data"), dict)
            ):
                if _challenge is not None:
                    st.session_state.pop("weekly_challenge", None)
                _challenge = None

            # awarded=False 时就地切到「已完成」，不用 rerun；成功则 rerun 后靠 session 保留。
            _challenge_show_completed = False

            # ---- 还没有挑战：生成入口 ----
            if _challenge is None:
                st.caption("让 AI 根据你本周的消费习惯，生成一个能打的小挑战")
                if st.button(
                    "⚔️ 生成本周挑战", key=f"challenge_generate_{_challenge_uid}"
                ):
                    if st.session_state.ai_daily_count >= 20:
                        st.warning("今日免费额度已用完，请明天再来")
                    else:
                        # 组装 stats：本月 + 支出，分类口径与「消费分类占比」一致；
                        # 012 未执行（无 category 列）时退回商户，和图表洞察口径一致。
                        _ch_exp_df = df_current_month[df_current_month["type"] == "支出"]
                        _ch_month_expense = float(_ch_exp_df["amount"].sum())
                        _ch_tx_count = int(len(_ch_exp_df))
                        _ch_top_category = "其他"
                        _ch_top_amount = 0.0
                        if not _ch_exp_df.empty:
                            if "category" in _ch_exp_df.columns:
                                _ch_cat = (
                                    _ch_exp_df["category"]
                                    .fillna("其他")
                                    .astype(str)
                                    .str.strip()
                                )
                                _ch_grouped = (
                                    _ch_exp_df.assign(
                                        _ch_cat=_ch_cat.where(_ch_cat != "", "其他")
                                    )
                                    .groupby("_ch_cat")["amount"]
                                    .sum()
                                )
                            else:
                                _ch_grouped = _ch_exp_df.groupby("merchant")[
                                    "amount"
                                ].sum()
                            if not _ch_grouped.empty:
                                _ch_top_category = str(_ch_grouped.idxmax())
                                _ch_top_amount = float(_ch_grouped.max())

                        _ch_budget = float(st.session_state.monthly_budget)
                        _ch_budget_used_pct = (
                            (_ch_month_expense / _ch_budget * 100.0)
                            if _ch_budget > 0
                            else 0.0
                        )
                        _challenge_stats = {
                            "week": current_week,
                            "month": current_month_str,
                            "month_expense": _ch_month_expense,
                            "budget": _ch_budget,
                            "budget_used_pct": _ch_budget_used_pct,
                            "top_category": _ch_top_category,
                            "top_amount": _ch_top_amount,
                            "tx_count": _ch_tx_count,
                        }

                        # 延迟导入：不在 app 启动时就拉起 ai_agent 的 Supabase 依赖
                        try:
                            from ai_agent import (
                                ChallengeError,
                                generate_weekly_challenge,
                            )
                        except Exception as _challenge_ai_import_exc:
                            print(
                                f"[challenge] AI 挑战模块不可用："
                                f"{type(_challenge_ai_import_exc).__name__}: "
                                f"{_challenge_ai_import_exc}"
                            )
                            generate_weekly_challenge = None

                            class ChallengeError(Exception):
                                """降级占位：ai_agent 不可用时仅用于异常归口。"""

                        # AI 契约内失败（no_api_key/network/http/parse/empty_stats）→ 本地兜底；
                        # 未预期异常 → 只提示「生成失败」，不生成兜底挑战（避免掩盖真的 bug）。
                        _challenge_ai_data = None
                        _challenge_ai_failed = False
                        _challenge_ai_error = False
                        if not callable(generate_weekly_challenge):
                            _challenge_ai_failed = True
                        else:
                            try:
                                with st.spinner("AI 正在设计挑战…"):
                                    _challenge_ai_data = generate_weekly_challenge(
                                        _challenge_stats, get_deepseek_api_key()
                                    )
                            except ChallengeError as _challenge_ai_err:
                                _challenge_ai_failed = True
                                print(
                                    f"[challenge] AI 生成失败"
                                    f"（{getattr(_challenge_ai_err, 'code', 'parse')}）："
                                    f"{type(_challenge_ai_err).__name__}"
                                )
                            except Exception as _challenge_ai_exc:
                                _challenge_ai_error = True
                                print(
                                    f"[challenge] AI 生成异常："
                                    f"{type(_challenge_ai_exc).__name__}: "
                                    f"{_challenge_ai_exc}"
                                )
                                st.error("生成失败，请稍后再试")

                        if not _challenge_ai_error:
                            # 后端违约返回不合规结构（正常不会发生）：同样按 AI 不可用走兜底
                            if not _challenge_ai_failed and not (
                                isinstance(_challenge_ai_data, dict)
                                and str(
                                    _challenge_ai_data.get("title") or ""
                                ).strip()
                            ):
                                print("[challenge] AI 返回结构不合规，使用本地兜底")
                                _challenge_ai_failed = True

                            if _challenge_ai_failed:
                                # 本地兜底挑战：top_amount * 0.3 取整，不消耗 AI 额度
                                _ch_fallback_saving = int(
                                    round(_ch_top_amount * 0.3)
                                )
                                _ch_fallback_target = max(
                                    0.0, _ch_top_amount - _ch_fallback_saving
                                )
                                _challenge = {
                                    "week": current_week,
                                    "data": {
                                        "week": current_week,
                                        "title": f"少喝 3 杯{_ch_top_category}",
                                        "detail": (
                                            f"本周把「{_ch_top_category}」的花费控制在 "
                                            f"¥{_ch_fallback_target:.0f} 以内，"
                                            "每笔先记账再下单，超了就留到下周"
                                        ),
                                        "estimated_saving": _ch_fallback_saving,
                                        "points": 10,
                                    },
                                    "completed": False,
                                }
                                st.session_state["weekly_challenge"] = _challenge
                                st.info("AI 暂不可用，已生成本地挑战")
                            else:
                                # 成功：只存 AI 结果，额度 +1（失败兜底不扣）
                                _challenge = {
                                    "week": current_week,
                                    "data": _challenge_ai_data,
                                    "completed": False,
                                }
                                st.session_state["weekly_challenge"] = _challenge
                                st.session_state.ai_daily_count += 1

            # ---- 有挑战：展示 + 完成领积分 ----
            if _challenge is not None:
                if _challenge.get("completed"):
                    _challenge_show_completed = True
                else:
                    _challenge_data = _challenge.get("data") or {}
                    _ch_title = (
                        str(_challenge_data.get("title") or "").strip()
                        or "本周省钱挑战"
                    )
                    _ch_detail = str(_challenge_data.get("detail") or "").strip()
                    try:
                        _ch_saving = float(
                            _challenge_data.get("estimated_saving") or 0
                        )
                    except (TypeError, ValueError):
                        _ch_saving = 0.0
                    try:
                        _ch_points = int(_challenge_data.get("points") or 10)
                    except (TypeError, ValueError):
                        _ch_points = 10
                    _ch_points = max(10, min(50, _ch_points))

                    # 标题来自 AI：用 subheader 纯文本渲染，绝不走 markdown，避免注入。
                    st.subheader(_ch_title)
                    if _ch_detail:
                        st.write(_ch_detail)
                    st.caption(
                        f"预计省下 ¥{_ch_saving:.0f} · 完成可得 {_ch_points} 分"
                    )

                    if st.button(
                        "✅ 我完成了", key=f"challenge_done_{_challenge_uid}"
                    ):
                        _challenge_award_fn = _challenge_db_func(
                            "award_challenge_points"
                        )
                        if _challenge_award_fn is None:
                            st.error(
                                "省钱挑战功能未启用（请确认已执行 migrations/017）"
                            )
                        else:
                            try:
                                with st.spinner("正在登记挑战完成…"):
                                    _challenge_award_res = _challenge_award_fn(
                                        _challenge_uid, current_week, _ch_points
                                    )
                            except (ValueError, RuntimeError) as _ch_award_exc:
                                # 业务/数据层错误：中文 message 可直接展示
                                st.error(str(_ch_award_exc))
                            except Exception as _ch_award_exc:
                                print(
                                    f"[challenge] 领取挑战积分失败："
                                    f"{type(_ch_award_exc).__name__}: "
                                    f"{_ch_award_exc}"
                                )
                                st.error("领取挑战积分失败，请稍后再试")
                            else:
                                if not isinstance(_challenge_award_res, dict):
                                    print(
                                        "[challenge] award_challenge_points 返回结构不合规"
                                    )
                                    st.error("领取挑战积分失败，请稍后再试")
                                else:
                                    try:
                                        _ch_award_points = int(
                                            _challenge_award_res.get("points")
                                            if _challenge_award_res.get("points")
                                            is not None
                                            else _challenge_points_now
                                        )
                                    except (TypeError, ValueError):
                                        _ch_award_points = _challenge_points_now

                                    if _challenge_award_res.get("awarded"):
                                        # 先落 completed 再 rerun：rerun 后卡片保持「已完成」
                                        _challenge["completed"] = True
                                        st.session_state["weekly_challenge"] = (
                                            _challenge
                                        )
                                        st.session_state["flash"] = {
                                            "level": "success",
                                            "msg": (
                                                f"挑战完成！+{_ch_points} 分，当前 "
                                                f"{_ch_award_points} 分"
                                            ),
                                        }
                                        st.rerun()
                                    else:
                                        # 同周重复领取：db 已拦下，这里就地切到已完成，无需 rerun
                                        st.info("本周的挑战积分已经领取过啦")
                                        _challenge["completed"] = True
                                        st.session_state["weekly_challenge"] = (
                                            _challenge
                                        )
                                        _challenge_points_now = _ch_award_points
                                        _challenge_show_completed = True

            if _challenge_show_completed:
                st.success("🎉 本周挑战已完成，下周再来！")
                st.caption(f"当前积分：{_challenge_points_now} 分")

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

    # ========== 财务诊断海报（本地 Pillow 绘制，不额外消耗 AI 额度）==========
    # 放在月度复盘之后：生成海报只读页面已有数据，不调用任何 AI 接口。
    with st.expander("🖼️ 生成我的财务诊断海报", expanded=False):
        if st.button("生成海报", key="poster_generate_btn"):
            _poster_uid = st.session_state.user_id

            # 昵称：读库失败只用「用户」，不影响海报
            try:
                _poster_nickname = (
                    (get_username(_poster_uid) or "").strip() or "用户"
                )
            except Exception as _nick_exc:
                print(
                    f"[poster] 读取昵称失败："
                    f"{type(_nick_exc).__name__}: {_nick_exc}"
                )
                _poster_nickname = "用户"

            # 本月 Top1：优先 category（迁移 012 后），没这列就退回 merchant
            _poster_top_category, _poster_top_amount = "暂无", 0.0
            _poster_category_totals = {}
            try:
                _poster_expense_df = df_current_month[df_current_month["type"] == "支出"]
                if not _poster_expense_df.empty:
                    _poster_group_col = (
                        "category"
                        if "category" in _poster_expense_df.columns
                        else "merchant"
                    )
                    _poster_groups = (
                        _poster_expense_df.groupby(_poster_group_col)["amount"]
                        .sum()
                        .sort_values(ascending=False)
                    )
                    if len(_poster_groups) > 0:
                        _poster_top_category = (
                            str(_poster_groups.index[0]).strip() or "暂无"
                        )
                        _poster_top_amount = float(_poster_groups.iloc[0])
                        _poster_category_totals = {
                            str(_name): float(_amount)
                            for _name, _amount in _poster_groups.items()
                        }
            except Exception as _top_exc:
                print(
                    f"[poster] 统计 Top1 失败："
                    f"{type(_top_exc).__name__}: {_top_exc}"
                )

            # 健康分：优先复用 Tab1 已算好的 health；拿不到就按同一口径现算一次
            try:
                _poster_health = health
            except NameError:
                _poster_health = None
            if not (isinstance(_poster_health, dict) and "score" in _poster_health):
                try:
                    if compute_health_score is None:
                        raise RuntimeError("health_score 模块未就绪")
                    try:
                        _poster_goal_progress = (
                            get_goal(_poster_uid) or {}
                        ).get("progress")
                    except Exception as _goal_exc:
                        print(
                            f"[poster] 读取心愿进度失败："
                            f"{type(_goal_exc).__name__}: {_goal_exc}"
                        )
                        _poster_goal_progress = None
                    _poster_health = compute_health_score(
                        month_expense=float(month_expense),
                        budget=float(st.session_state.monthly_budget),
                        month_balance=float(month_balance),
                        save_target=float(st.session_state.save_target),
                        goal_progress=_poster_goal_progress,
                        category_totals=_poster_category_totals,
                    )
                except Exception as _poster_health_exc:
                    print(
                        f"[poster] 健康分现算失败："
                        f"{type(_poster_health_exc).__name__}: {_poster_health_exc}"
                    )
                    _poster_health = {}
            if not isinstance(_poster_health, dict):
                _poster_health = {}
            try:
                _poster_score = int(_poster_health.get("score") or 0)
            except (TypeError, ValueError):
                _poster_score = 0
            _poster_grade = str(_poster_health.get("grade") or "-")
            _poster_title = str(_poster_health.get("title") or "")

            # 点评：优先用 Tab1 已缓存的 AI 锐评；没有就用本地兜底文案，绝不为此再调 AI
            _poster_comment = ""
            try:
                _cached_comment = st.session_state.get("health_comment") or {}
                if isinstance(_cached_comment, dict):
                    _poster_comment = str(_cached_comment.get("text") or "").strip()
            except Exception:
                _poster_comment = ""
            if not _poster_comment:
                _poster_comment = (
                    f"本月花了 {float(month_expense):.2f} 元，"
                    "奶茶少两杯，钱包会感谢你。"
                )

            try:
                _poster_app_url = get_secret("APP_URL") or ""
            except Exception:
                _poster_app_url = ""

            _poster_payload = {
                "nickname": _poster_nickname,
                "month": current_month_str,
                "total_expense": float(month_expense),
                "health_score": _poster_score,
                "health_grade": _poster_grade,
                "health_title": _poster_title,
                "top_category": _poster_top_category,
                "top_amount": _poster_top_amount,
                "comment": _poster_comment,
                "app_url": _poster_app_url,
            }

            if poster is None:
                st.error("海报功能暂不可用，请稍后再试")
            else:
                try:
                    with st.spinner("正在绘制海报…"):
                        _poster_png = poster.build_poster(_poster_payload)
                except poster.PosterError as _poster_err:
                    st.error(str(_poster_err))
                except Exception as _poster_exc:
                    print(
                        f"[poster] 生成失败："
                        f"{type(_poster_exc).__name__}: {_poster_exc}"
                    )
                    st.error("海报生成失败，请稍后再试")
                else:
                    st.session_state["poster_bytes"] = _poster_png

        _poster_bytes = st.session_state.get("poster_bytes")
        if _poster_bytes:
            st.image(_poster_bytes, caption=f"{current_month_str} 财务诊断海报")
            st.download_button(
                "保存到手机",
                data=_poster_bytes,
                file_name=f"mingbaizhang_{current_month_str}.png",
                mime="image/png",
                key="poster_download_btn",
            )

    if len(st.session_state.chat_history) == 0:
        st.session_state.chat_history.append({"role": "assistant", "content": "你好！我是明白账AI财务管家，可以帮你分析账单，也可以直接帮你记账，有什么想问我的吗？"})

    for msg in st.session_state.chat_history:
        # tool 消息是模型 ↔ 本地函数的协议数据（查询结果原文可能很长），不直接展示；
        # assistant 的 tool_calls 消息 content 为 None，也没有可展示内容 —— 一并跳过。
        if msg.get("role") == "tool":
            continue
        _msg_content = msg.get("content")
        _msg_table = msg.get("table")
        if not _msg_content and not _msg_table:
            continue
        with st.chat_message(msg["role"]):
            if _msg_content:
                st.write(_msg_content)
            # 查询结果表随 assistant 消息持久化：rerun / 切换 Tab 后依然在文字下方
            if _msg_table:
                st.dataframe(
                    pd.DataFrame(_msg_table),
                    use_container_width=True,
                    hide_index=True,
                )

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

    # ===== 聊天工具（query_bills / delete_bills）的代码级安全辅助 =====
    # 安全原则：删除**绝不**直接执行模型给的参数。
    #   1) 收到 delete_bills tool call 时，先按同条件 query_bills 预览（笔数 + 合计，含收入）；
    #   2) 把条件快照写进 st.session_state["pending_delete"]，只回复确认文案：
    #      不删库、不 append tool 消息、不再调模型（模型没法在同一轮里「顺手」删数据）；
    #   3) 下一轮用户必须亲口回「确认」，执行时只用 pending 里的快照参数；
    #      pending 为空时即使输入「确认」也只走普通聊天 —— 二次确认由代码强制，不依赖模型自觉。
    _PENDING_DELETE_YES = frozenset({"确认", "确定", "是", "删除"})
    _PENDING_DELETE_NO = frozenset({"取消", "算了", "不要", "否"})

    def _clear_pending_delete():
        """清空待确认的删除及归属标记，避免残留到下一个账号 / 下一次登录。"""
        st.session_state.pop("pending_delete", None)
        st.session_state.pop("pending_delete_owner", None)

    def _pending_confirmation_visible():
        """确认文案是否仍在聊天记录里（登出会清空 chat_history，旧 pending 必须失效）。"""
        return any(
            str(_m.get("content") or "").startswith("⚠️ 即将删除")
            for _m in st.session_state.chat_history
            if _m.get("role") == "assistant"
        )

    def _validate_delete_range(start_raw, end_raw):
        """严格校验删除区间：ISO 日期（YYYY-MM-DD）、end >= start、跨度 <= 366 天。"""
        try:
            _start_dt = datetime.strptime(str(start_raw), "%Y-%m-%d").date()
            _end_dt = datetime.strptime(str(end_raw), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None, None, "删除参数不合法：日期需为 YYYY-MM-DD 格式"
        if _end_dt < _start_dt:
            return None, None, "删除参数不合法：结束日期早于开始日期"
        if (_end_dt - _start_dt).days > 366:
            return None, None, "删除参数不合法：单次最多删除 366 天内的账单"
        return _start_dt.isoformat(), _end_dt.isoformat(), None

    def _format_query_result(bill_df):
        """查询结果 → (给模型看的短文本, 持久化到 chat_history 的表格行)。

        给模型的最多 20 行且带列名；聊天里持久化的表格最多 50 行，避免一次全量
        查询把 session_state 撑爆。空结果不写 table，页面只显示「没有查到」。
        """
        if bill_df is None or len(bill_df) == 0:
            return "没有查到符合条件的账单", None
        _spend = _income = 0.0
        try:
            _spend = float(bill_df.loc[bill_df["type"] == "支出", "amount"].sum())
            _income = float(bill_df.loc[bill_df["type"] == "收入", "amount"].sum())
        except (KeyError, TypeError, ValueError):
            pass
        _tool_text = (
            f"查询到 {len(bill_df)} 笔，支出合计 {_spend:.2f}，收入合计 {_income:.2f}\n"
            f"{bill_df.head(20).to_string(index=False)}"
        )
        return _tool_text, bill_df.head(50).to_dict("records")

    def _build_delete_confirmation(func_args):
        """delete_bills tool call → 预览 + 写 pending_delete，返回给用户看的确认文案。

        注意：本函数**不执行删除**；只有下一轮用户回「确认」才会真正调 delete_bills。
        """
        _start, _end, _range_err = _validate_delete_range(
            func_args.get("start_date"), func_args.get("end_date")
        )
        if _range_err:
            st.error(f"{_range_err}，未执行删除。")
            return f"{_range_err}，未执行删除。"
        _category = func_args.get("category")
        st.caption("🔎 正在执行查询操作…")
        try:
            with st.spinner("正在执行查询操作…"):
                _preview_df = query_bills(
                    st.session_state.user_id, _start, _end, _category, None
                )
        except ValueError as _preview_err:
            st.error(f"删除前预览失败：{_preview_err}")
            return f"删除前预览失败：{_preview_err}。未执行删除。"
        except Exception as _preview_exc:
            print(f"[chat] 删除前预览失败：{type(_preview_exc).__name__}: {_preview_exc}")
            st.error("删除前预览失败，请稍后再试")
            return "删除前预览失败，账单未改动，请稍后再试。"
        _count = 0 if _preview_df is None else len(_preview_df)
        if _count == 0:
            # 没有可删的数据：不进入待确认状态，避免用户对着「删除 0 笔」再确认一次
            _clear_pending_delete()
            return "没有查到符合条件的账单，无需删除。"
        _spend = _income = 0.0
        try:
            _spend = float(_preview_df.loc[_preview_df["type"] == "支出", "amount"].sum())
            _income = float(_preview_df.loc[_preview_df["type"] == "收入", "amount"].sum())
        except (KeyError, TypeError, ValueError):
            pass
        _total = _spend + _income
        st.session_state["pending_delete"] = {
            "start_date": _start,
            "end_date": _end,
            "category": _category,
            "keyword": None,
            "count": _count,
            "total": _total,
        }
        # 归属标记：logout 只清 chat_history、不清 pending_delete，换账号后必须让旧快照失效
        st.session_state["pending_delete_owner"] = st.session_state.user_id
        _scope = f"{_start} 至 {_end}" + (f"·{_category}" if _category else "")
        return (
            f"⚠️ 即将删除 {_scope} 的 {_count} 笔账单"
            f"（合计 ¥{_total:.2f}，含收入）。\n\n"
            "回复「确认」执行删除，回复「取消」放弃。"
        )

    user_input = st.chat_input("可以直接让我帮你记账，例如：今天喝奶茶花了15元")
    if user_input:
        st.chat_message("user").write(user_input)
        st.session_state.chat_history.append({"role": "user", "content": user_input})

        if st.session_state.get("pending_delete"):
            # ---- 待删除会话：只认用户的明确指令，绝不把输入交给模型 ----
            _pending_owner = st.session_state.get("pending_delete_owner")
            if _pending_owner is not None and (
                _pending_owner != st.session_state.user_id
                or not _pending_confirmation_visible()
            ):
                # 账号切换 / 聊天记录被清空（如登出）后：旧 pending 一律作废，绝不执行删除
                _clear_pending_delete()
                st.session_state.chat_history.append(
                    {
                        "role": "assistant",
                        "content": "待确认的删除已失效（账号切换或聊天记录已清空），未删除任何账单。",
                    }
                )
                st.rerun()
            _pending = st.session_state["pending_delete"]
            _cmd = user_input.strip()
            if _cmd in _PENDING_DELETE_YES:
                _start, _end, _range_err = _validate_delete_range(
                    _pending.get("start_date"), _pending.get("end_date")
                )
                if _range_err:
                    # 快照参数被污染/过期：报错并作废，不执行任何删除
                    _clear_pending_delete()
                    st.error(f"{_range_err}，已取消本次删除。")
                else:
                    st.caption("🗑️ 正在执行删除操作…")
                    try:
                        with st.spinner("正在执行删除操作…"):
                            delete_bills(
                                st.session_state.user_id,
                                _start,
                                _end,
                                _pending.get("category"),
                            )
                    except Exception as _del_exc:
                        print(f"[chat] 删除账单失败：{type(_del_exc).__name__}: {_del_exc}")
                        _clear_pending_delete()
                        st.error("删除失败，账单未改动，请稍后再试")
                    else:
                        st.cache_data.clear()
                        _pending_count = int(_pending.get("count") or 0)
                        _pending_total = float(_pending.get("total") or 0)
                        _deleted_msg = (
                            f"已删除 {_pending_count} 笔账单"
                            f"（合计 ¥{_pending_total:.2f}）。"
                        )
                        st.session_state.chat_history.append(
                            {"role": "assistant", "content": _deleted_msg}
                        )
                        _clear_pending_delete()
                        st.rerun()
            elif _cmd in _PENDING_DELETE_NO:
                _clear_pending_delete()
                st.session_state.chat_history.append(
                    {"role": "assistant", "content": "已取消删除，没有改动任何账单。"}
                )
                st.rerun()
            else:
                # 既不是确认也不是取消：留在待确认状态，提示后重跑；不调用模型，避免绕过确认
                st.session_state.chat_history.append(
                    {
                        "role": "assistant",
                        "content": "请回复「确认」或「取消」以继续（取消后可以重新提问）。",
                    }
                )
                st.rerun()
        else:
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
                            ai_answer = None
                            answer_table = None
                            _delete_confirmation_sent = False
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
                                elif func_name == "query_bills":
                                    # 界面提示「正在执行查询操作」：spinner + 聊天内一行 caption
                                    st.caption("🔎 正在执行查询操作…")
                                    try:
                                        with st.spinner("正在执行查询操作…"):
                                            _query_df = query_bills(
                                                st.session_state.user_id,
                                                func_args.get("start_date"),
                                                func_args.get("end_date"),
                                                func_args.get("category"),
                                                func_args.get("keyword"),
                                            )
                                    except ValueError as _query_err:
                                        # 参数非法：把中文原因交回模型解释，页面不红屏
                                        st.error(f"查询参数不合法：{_query_err}")
                                        tool_result = f"查询参数不合法：{_query_err}"
                                    except Exception as _query_exc:
                                        print(f"[chat] 查询账单失败：{type(_query_exc).__name__}: {_query_exc}")
                                        st.error("查询失败，请稍后再试")
                                        tool_result = "查询失败，请稍后再试"
                                    else:
                                        tool_result, answer_table = _format_query_result(_query_df)
                                elif func_name == "delete_bills":
                                    # 安全核心：只预览 + 写 pending_delete，绝不执行删除，
                                    # 也不 append tool 消息 / 不再调模型（用户回「确认」后才删）。
                                    ai_answer = _build_delete_confirmation(func_args)
                                    _delete_confirmation_sent = True
                                if not _delete_confirmation_sent:
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
                                    if resp2.status_code != 200:
                                        st.error(f"API请求失败，状态码：{resp2.status_code}")
                                        ai_answer = "操作已完成，但生成文字总结失败，请稍后再试。"
                                    else:
                                        res2 = resp2.json()
                                        ai_answer = res2["choices"][0]["message"]["content"]
                            else:
                                ai_answer = msg["content"]
                            if ai_answer is not None:
                                with st.chat_message("assistant"):
                                    st.write(ai_answer)
                                    if answer_table:
                                        # 本轮立刻渲染；同时写进 chat_history，rerun 后依然在文字下方
                                        st.dataframe(
                                            pd.DataFrame(answer_table),
                                            use_container_width=True,
                                            hide_index=True,
                                        )
                                _assistant_entry = {"role": "assistant", "content": ai_answer}
                                if answer_table:
                                    _assistant_entry["table"] = answer_table
                                st.session_state.chat_history.append(_assistant_entry)
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
