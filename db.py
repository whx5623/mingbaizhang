from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
import pandas as pd
import hashlib
import logging
import math
import re
import bcrypt
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, date, timedelta, timezone

engine = None
_initialized = False

def init_db():
    """懒加载：engine 只建一次、建表 DDL 只跑一次。

    app.py 每次 Streamlit rerun 都会调用 init_db()；若每次都 create_engine，
    等于每次重建连接池 —— 既浪费，也让 pool_pre_ping（应对 pooler 回收空闲连接）
    彻底落空（跨 rerun 根本没有可复用的池）。
    """
    global engine, _initialized
    if _initialized:
        return
    from config import DATABASE_URL
    # hide_parameters=True：SQLAlchemy 默认会把**绑定参数的值**拼进 StatementError /
    # 引擎日志里，而本文件多处 log.exception(..., exc_info=True)；users 相关语句里带着
    # password_hash（bcrypt 哈希）和注册/改密时的明文参数 —— 默认配置下会随异常进日志。
    # 这个开关只改"异常与日志的渲染"，不影响 SQL 执行、参数绑定和结果。
    # （注意：它挡不住 PG 服务端报错文本里自带的键值，如 Key (username)=(alice)，属缓解非彻底脱敏。）
    # pool_pre_ping=True：Supabase pooler（6543 / transaction 模式）会回收空闲连接，
    # 池里可能躺着已断开的连接；取用前先探活（SELECT 1），失效就丢弃换新，
    # 避免用户偶发拿到一条死连接、直接吃到 OperationalError。
    # connect_args options：把**每条连接**的会话时区钉成 UTC。
    #   为什么必须钉：created_at 在 008 执行前还是 naive timestamp，此时 tz-aware 参数
    #   与 naive 列比较会按**会话时区**解释列；若不钉死，"库/角色恰好是 UTC"就成了正确性前提。
    #   钉死之后，无论 pooler 把连接交到哪个后端、库默认时区是什么，应用侧口径都稳定。
    engine = create_engine(
        DATABASE_URL,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                credits INTEGER DEFAULT 0,
                -- 必须与 migrations/008_users_created_at_tz.sql 逐字同口径：
                -- timestamptz 存**绝对时刻**；naive TIMESTAMP 存的是"没有时区的墙上时间"，
                -- 含义取决于读写时会话的 TimeZone，时区一变历史值整体平移。
                -- ⚠ 这条 CREATE TABLE 只对**新库**生效：CREATE TABLE IF NOT EXISTS
                --   对已存在的表什么都不做；老库把列改成 timestamptz 必须执行 008
                --   （008 幂等、可回滚，且转换前后逐值相等）。
                --   NOT NULL 同理只约束新库；老库历史列仍是 nullable（现网 12 行全部非 NULL，
                --   没有需要补数据的行）。
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                -- 必须是 TIMESTAMPTZ，与 migrations/005_last_login.sql 逐字同口径。
                -- naive TIMESTAMP 存的是“没有时区的墙上时间”，含义取决于读写时会话的 TimeZone，
                -- 时区一变历史值整体平移；timestamptz 存绝对时刻，跨环境稳定。
                -- （这条 CREATE TABLE 只对**新库**生效；老库补列必须跑 005，
                --   005 的核对会用 data_type = 'timestamp with time zone' 拦下历史遗留的错误类型。）
                last_login TIMESTAMPTZ NULL,
                -- 管理员标记：与 migrations/006_user_admin_flag.sql 逐字同口径
                -- （BOOLEAN NOT NULL DEFAULT FALSE，默认最小权限）。
                -- 同样**只对新建库生效**：CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，
                -- 老库补这一列必须执行 migrations/006；没执行时 get_user_auth_state() 走
                -- ProgrammingError(pgcode=42703) 降级分支返回 (存在, False)（面板隐藏），不会红屏。
                is_admin BOOLEAN NOT NULL DEFAULT FALSE
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
    _initialized = True

# ---------------------------------------------------------------------------
# 账单类型归一化
# bills.type 只允许 "收入" / "支出" 两个中文值：app.py 的收支汇总全靠字符串精确比较
# （df[df["type"] == "收入"]），一旦库里混入 "expense" / " Income " / "支出 " 等脏值，
# 这笔钱会被收入侧和支出侧同时漏算 → 结余虚高 → 误判“已达成目标存款”。
# ---------------------------------------------------------------------------

INCOME_TYPE = "收入"
EXPENSE_TYPE = "支出"
FALLBACK_TYPE = EXPENSE_TYPE  # 兜底口径：宁可少算结余，也绝不虚增收入

# 金额上界：与 migrations/003 的前置校验、以及 bills.amount NUMERIC(12,2) 的上界同口径。
# NUMERIC(12,2) 能存的最大值就是 9999999999.99；超了数据库会报 numeric field overflow，
# 所以在 Python 侧就提前拒绝，给页面一句能看懂的提示，而不是一条 psycopg2 栈。
MAX_ABS_AMOUNT = 9999999999.99

# 金额定量的最小单位（两位小数）与舍入口径。
# 必须与 PostgreSQL 的 numeric round 一致：**半值远离零**（1.005 → 1.01）。
# Python 内置 round() 是银行家舍入（round(1.005, 2) == 1.0），f-string "{1.005:.2f}"
# 也是 1.00 —— 用它们做定量会和库里的值差 1 分钱（页面显示 1.00、库里 1.01）。
_CENT = Decimal("0.01")

# 注意：别名表里刻意不含 "1" / "0" / "-1" —— 这类数字编码没有公认约定
# （很多账本里 1 恰恰表示支出），一旦命中就会把支出永久改写成收入。
# 它们走“无法识别”的保守路径：strict=True 抛错，strict=False/SQL 兜底归入支出。
_INCOME_ALIASES = {"收入", "收", "income", "in", "revenue", "salary", "credit"}
_EXPENSE_ALIASES = {"支出", "支", "expense", "expenditure", "out", "spend", "debit", "cost"}


def normalize_bill_type(raw, *, strict=True):
    """把任意来源的账单类型收敛成 "收入" / "支出"。

    - 先 str() 再 strip()（str.strip() 默认就会吃掉全角空格 \\u3000），再 casefold() 比对别名表；
    - strict=True ：无法识别 / None / 空串直接抛 ValueError，让写入方的错误可见；
    - strict=False：无法识别回落为 "支出"（读取兜底用，绝不虚增收入）。
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # 模型偶尔把类型写成 1 / 0 / 1.0 / -1
        key = str(int(raw)) if float(raw).is_integer() else str(raw)
    else:
        key = str(raw).strip()
    if key in (INCOME_TYPE, EXPENSE_TYPE):  # 已经规范的值原样返回（幂等）
        return key
    folded = key.casefold()
    if folded in _INCOME_ALIASES:
        return INCOME_TYPE
    if folded in _EXPENSE_ALIASES:
        return EXPENSE_TYPE
    if strict:
        raise ValueError(f"非法的账单类型：{raw!r}")
    return FALLBACK_TYPE


def _normalize_amount(amount, bill_type):
    """金额符号收敛 + 两位小数定量，口径与数据库（NUMERIC(12,2)）完全一致。

    为什么必须先定类型、再收敛符号（顺序绝不能反）：
    CSV 里「type=收入、amount=-100」是退款/冲正。若先做 abs() 再判断类型，
    它会被翻成 +100 的收入 → 收入虚增 → 结余虚高 → 误判“已达成目标存款”，
    正是本功能要守住的红线方向。

    符号：
    - 支出：返回 abs(值)。支出写负会反向冲减月支出，同样让结余虚高；
    - 收入：**保留原符号**。负收入只会让结余偏小，属保守方向，不许翻正；
    - 0 允许（0 元账单合法，abs(0) / -0 归一成 0.0）。

    精度（为什么不能用 round()）：
    数据库列是 NUMERIC(12,2)，psycopg2 把 float 按文本发给 PG，PG 的 numeric 舍入是
    **半值远离零**：1.005 → 1.01。而 Python 的 round() 是银行家舍入（1.005 → 1.00），
    f-string "{1.005:.2f}" 同样是 1.00 —— 页面显示值和库内值会差 1 分钱。
    所以这里统一用 Decimal + ROUND_HALF_UP 定量到两位小数：
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) → float
    注意用 str(value) 而不是 Decimal(value)：后者会带出 float 的二进制尾数
    （Decimal(1.005) == 1.00499999999999989… → 定量成 1.00，反而错）。

    返回 float（不是 Decimal）：下游 pandas / app.py 全程按 float 用，
    且 psycopg2 发 float 时同样按最短 repr 转文本，与数据库定量结果一致。

    上界：abs(值) 超过 9999999999.99（NUMERIC(12,2) 的上界）→ ValueError，
    与 migrations/003 的前置校验同口径；否则数据库会抛 numeric field overflow。

    转不成数字 / nan / inf / -inf / 超上界：抛 ValueError，由调用方决定跳过或上抛。
    """
    try:
        value = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"账单金额非法：{amount!r}（不是数字）") from exc
    if not math.isfinite(value):
        raise ValueError(f"账单金额非法：{amount!r}（必须是有限数字，不允许 nan/inf）")
    if bill_type == EXPENSE_TYPE:
        value = abs(value)
    # 与 PG numeric 同口径的两位小数定量（负数同样“半值远离零”，Decimal 的
    # ROUND_HALF_UP 对负数是 -1.005 → -1.01，与 PG 的 round(-1.005, 2) 一致）。
    try:
        value = float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        # 极端大的数（如 1e300）会让 quantize 超出 Decimal 上下文精度
        raise ValueError(f"金额超出上限 {MAX_ABS_AMOUNT}") from exc
    # 定量后再判上界：9999999999.995 会被定量成 10000000000.00，同样越界
    if abs(value) > MAX_ABS_AMOUNT:
        raise ValueError(f"金额超出上限 {MAX_ABS_AMOUNT}")
    return value + 0.0  # + 0.0 把 -0.0 归一成 0.0（避免页面显示 "-0.00"），其余值不受影响


# ---------------------------------------------------------------------------
# 账单日期归一 + 校验
# ---------------------------------------------------------------------------
# 背景：bills.date 是 VARCHAR/TEXT（**不是 DATE**），而读取侧全程靠"字符串"工作：
#   · app.py 的分页：           ORDER BY date DESC
#   · 管理员活跃图：            WHERE date >= :start_text AND date < :end_text
#   · pandas 归期：             pd.to_datetime(..., errors="coerce")
# 这三处只有在值是**零填充 ISO 'YYYY-MM-DD'** 时才成立（位数固定 → 字典序 == 日期序）。
# 一旦库里混进 '2026/10/1' / '2026-1-5' / '' / 'abc'，后果都是**静默**的：
#   · 活跃图少算：实测 '2026/10/1' >= '2026-10-11' 为 False → 该行被当成"不在窗口内"，
#     不报错、不告警，图上就是凭空少一天；
#   · 分页排序错乱：字典序把 '2026-1-5' 排在 '2026-10-17' 之后；
#   · pd.to_datetime(errors="coerce") 把解析不了的行变成 NaT 直接丢掉。
# 所以写入侧必须把格式钉死（本段），数据库侧另有 007_bill_date_check.sql 的
# CHECK 约束兜底 —— 它挡的是**绕过本模块**的写入（手工 SQL、别的脚本、别的服务）。
# 历史数据不在这里迁移（不改存储类型、不改老值），涉不涉及由 007 的前置校验决定。
# ---------------------------------------------------------------------------

# 与 migrations/007_bill_date_check.sql 的 CHECK 正则**同口径**：
#   Python 侧  _ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
#   SQL 侧     date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
# 两边写法不同但**承认同一组字符**（4 位 ASCII 数字 - 2 位 - 2 位，且位数固定、零填充）。
# SQL 侧刻意写 [0-9] 而不是 \d：PostgreSQL 的 \d 是"数字类"而 Python 的 \d（不加 re.ASCII）
# 是"所有 Unicode 十进制数字"，写 [0-9] 让两边的字符集一眼就能对上，也顺带让
# 007 里的形状核对不必再处理反斜杠转义。
# 两个刻意的 Python 写法：
#   ① re.ASCII：Python 的 \d 默认匹配**所有 Unicode 十进制数字**（'٢٠٢٦-١٠-٠١' 也会命中），
#      不加这个标志，"4 位数字"的判断就成了"4 位任意语言的数字"，与 SQL 侧口径漂移。
#   ② fullmatch 而不是 re.match(r"^...$")：Python 的 "$" 允许串尾多出一个 "\n"
#      （re.match(r"^\d{4}-\d{2}-\d{2}$", "2026-10-01\n") 会命中），PostgreSQL 的 ~ 不会。
#      同一个值在两边判定相反，就是"库里合规、代码判非法"的幽灵数据。
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _is_missing_value(raw) -> bool:
    """None / 空串 / 纯空白 / NaN(NaT) → "没给值"。

    单独抽成一个函数，是为了让**校验**和**给用户的原因文案**共用同一个判据：
    否则会出现"校验按空值拒、文案却说'格式非法：nan'"这种自相矛盾的提示
    （CSV 里日期列缺失时，pandas 给的是 float('nan') 而不是 None / ""）。
    """
    if raw is None:
        return True
    if isinstance(raw, str):
        return not raw.strip()  # str.strip() 默认连全角空格 \u3000 一起吃掉
    try:
        return bool(pd.isna(raw))  # float('nan') / pd.NaT / pd.NA
    except (TypeError, ValueError):  # 不可判定的容器/自定义对象 → 当作"有值"
        return False


def _normalize_bill_date(raw) -> str:
    """把账单日期收敛成严格的 ISO 'YYYY-MM-DD' 字符串；任何非法输入一律 ValueError。

    为什么"钉死格式"而不是"尽力解析成 ISO"（这是本函数最重要的设计取舍）：
      bills.date 是字符串列，读取侧三处（分页 ORDER BY、活跃图字符串窗口、
      pd.to_datetime）都只认零填充 ISO。如果这里做"顺手帮你转一下"
      （'2026/10/1' → '2026-10-01'），写入侧就会长期存在两种格式，
      而"能不能转"这件事只在**写入那一刻**成立 —— 读取侧永远要再赌一次。
      所以：**要么是严格 ISO，要么直接拒绝**，让错误在写入处暴露。

    校验分三步（顺序固定，报错文案只有两种）：
      ① None / 空串 / 纯空白 / NaN → ValueError("账单日期不能为空")
      ② str(raw).strip() 后必须整串匹配 ^\\d{4}-\\d{2}-\\d{2}$（正则是**廉价**的一步，
         先做它，避免把任意脏串喂给 strptime）；
      ③ datetime.strptime(s, "%Y-%m-%d") 校验**真实日历日**：'2026-02-30'、'2026-13-01'
         这类"格式对、日子不存在"的值必须被拒（它们能通过 ②，会让活跃图凭空多一个
         永远不会出现的日期）。失败与 ② 给**同一条**文案 —— 对用户来说
         "2026-02-30 不是合法日期"和"2026/10/1 格式不对"是同一件事：日期写错了。

    返回值：**strip 后的原字符串**（不擅自改格式：能走到这里的已经是标准 ISO）。
    非法时抛 ValueError（中文文案），由调用方决定上抛还是跳过该行。
    """
    if _is_missing_value(raw):
        raise ValueError("账单日期不能为空")
    text = str(raw).strip()
    if not _ISO_DATE_PATTERN.fullmatch(text):
        raise ValueError(f"账单日期格式必须是 YYYY-MM-DD：{raw!r}")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        # 格式对但日子不存在（2026-02-30 / 2026-13-01 / 2025-02-29）
        raise ValueError(f"账单日期格式必须是 YYYY-MM-DD：{raw!r}") from exc
    return text


def _describe_date_error(raw_date) -> str:
    """把日期校验失败翻译成一句中文原因（给 CSV 导入的 skipped 明细用）。

    与 _normalize_bill_date 的判据一一对应，只是把"错在哪一步"说清楚：
      · 没给值            → 账单日期不能为空
      · 格式不对          → 日期格式非法：'2026/10/1'
      · 格式对但日子不存在 → 日期不存在（不是真实日历日）：'2026-02-30'
    （不能用 str(ValueError)：那会把两条不同成因都糊成一句，运维看不出是
      "用户写了 2026/10/1" 还是"二月三十号"。）
    """
    if _is_missing_value(raw_date):
        return "账单日期不能为空"
    text = str(raw_date).strip()
    if not _ISO_DATE_PATTERN.fullmatch(text):
        return f"日期格式非法：{raw_date!r}"
    return f"日期不存在（不是真实日历日）：{raw_date!r}"


def _normalize_type_column(df):
    """读取兜底：把 DataFrame 的 type 列整列归一化，NaN / 空值按 "支出" 处理且不抛异常。"""
    if not isinstance(df, pd.DataFrame) or df.empty or "type" not in df.columns:
        return df
    try:
        df = df.copy()
        df["type"] = df["type"].map(lambda v: normalize_bill_type(v, strict=False))
    except Exception as exc:  # 兜底逻辑本身不能把页面搞崩，异常时退回原始数据
        print(f"[db] type 列归一化失败，返回原始数据：{exc}")
    return df


def _normalize_amount_column(df):
    """读取兜底：把 DataFrame 的 amount 列整列转成 float，且绝不抛异常。

    为什么必须转：migrations/003 把 bills.amount 改成了 NUMERIC(12,2)（数据库侧保精度），
    而 pd.read_sql 读 NUMERIC 得到的是 Decimal（object dtype）。app.py 里两处会因此直接崩：
      · percent = (month_expense / budget) * 100   → Decimal / float 抛 TypeError
      · st_echarts 里 {"value": round(row["amount"], 2)} → Decimal 无法被 JSON 序列化
    所以：数据库存 NUMERIC 保精度，Python 侧统一用 float 供页面/图表使用。

    口径：
    - 空 DataFrame / 没有 amount 列 / 整列无法判断：原样返回；
    - errors="coerce"：转不成数字的留 NaN。**不填 0** —— 填 0 等于凭空造一笔 0 元账单，
      会悄悄改变结余，比页面上显示 NaN 危险得多；NaN 就交给页面自己决定怎么展示。
    """
    if not isinstance(df, pd.DataFrame) or df.empty or "amount" not in df.columns:
        return df
    try:
        df = df.copy()
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        # 个别 pandas/SQLAlchemy 组合下 to_numeric 对 Decimal 仍会停在 object dtype，
        # 这里再兜一次（Decimal('12.34') → '12.34' → 12.34；None/坏值 coerce 成 NaN）。
        if df["amount"].dtype == object:
            df["amount"] = pd.to_numeric(df["amount"].astype(str), errors="coerce")
    except Exception as exc:  # 兜底逻辑本身不能把页面搞崩，异常时退回（最多退到 object dtype）
        print(f"[db] amount 列转 float 失败，返回未完全转换的数据：{exc}")
    return df


# ---------------------------------------------------------------------------
# 密码哈希（bcrypt）
# 历史问题：users.password_hash 里存的是**明文**，登录时用 SQL 做明文等值比较
# （WHERE password_hash = :ph）。一次拖库 = 全量密码泄露，且用户往往跨站复用密码。
# 现在：库里只存 bcrypt 哈希（$2b$12$ + 22 字符 salt + 31 字符 digest = 60 字符），
# 明文只在内存里出现一瞬，比较改由 bcrypt.checkpw 做恒定时间的哈希比对。
# ---------------------------------------------------------------------------

BCRYPT_ROUNDS = 12  # 2^12 次迭代；单次校验约 200~300ms，是“挡暴力破解”和“用户能忍”的折中

# 密码最小长度：**下沉到 db 层**。app.py 的 UI 校验保留（它只是提前拦一次、给页面
# 一句 warning），但真正的口径在这里 —— 任何绕过 UI 的调用路径（脚本、REPL、
# 将来的 API、别的服务）都必须受同一条规则约束。
MIN_PASSWORD_LEN = 8

# 与 migrations/004_password_bcrypt.sql 里的 CHECK 约束保持**逐字同口径**：
#     \$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}
# 前缀四种都认：pgcrypto 生成 $2a$、Python bcrypt 生成 $2b$、老系统有 $2y$、
# pyca bcrypt 5.0.0 实测也能校验 $2x$；两位 cost + 53 个 [./A-Za-z0-9] 字符
# （22 salt + 31 digest），合计恰好 60 字符。
# 两个刻意的写法：
#   ① 用 re.fullmatch 而不是 "^...$"：Python 的 "$" 允许串尾多出一个 "\n"
#      （re.match(r"...$", h + "\n") 会命中），而 PostgreSQL 的 ~ 不会 ——
#      同一个值在两边判定相反，会造成“库里合规、代码判非法”的幽灵账号。
#   ② 字符类写 [0-9] 而不是 \d：SQL 侧没有 \d 的统一写法，两边必须承认同一组字符。
_BCRYPT_PATTERN = re.compile(r"\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}")

# verify_user 在“用户名不存在”分支上用来抹平时序差异的假哈希（惰性生成，仅存内存）。
_TIMING_DUMMY_HASH = None


def is_bcrypt_hash(value) -> bool:
    r"""判断一个值是否已经是合法的 bcrypt 哈希字符串（用于迁移幂等 / fail-closed 判定）。

    用 fullmatch：整串必须恰好 60 字符，**末尾多一个换行也算不合格**
    （与 004 的 SQL 正则 `^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$` 同口径）。
    """
    if not isinstance(value, str):
        return False
    return bool(_BCRYPT_PATTERN.fullmatch(value))


def hash_password(password: str) -> str:
    """把明文密码哈希成 bcrypt 字符串（每次调用盐随机，同一密码结果不同）。

    - password 必须是 str：库里历史数据是文本，传 bytes/int 一律拒绝，避免“看起来写了其实写错了”；
    - 空串 / 纯空白 → ValueError：注册一个空密码等于把账号白送；
    - 字符数 < MIN_PASSWORD_LEN(8) → ValueError：与 app.py 的提示同口径，但这里是**硬约束**，
      绕过 UI 的调用路径同样拦得住；
    - UTF-8 编码后 > 72 字节 → ValueError：这是 bcrypt 的**硬限制**（超出部分会被丢弃）。
      绝不能静默截断 —— 用户以为设了长密码，实际只有前 72 字节生效，且后面的差异
      会让 verify 莫名失败。中文 1 字 3 字节，72 字节约 24 个汉字。
    - 超过 72 字节也不要“先 sha256 再 bcrypt”，那会引入新的兼容口径，本次不做。

    检查顺序固定为：类型 → 判空 → 最小长度 → 字节上限。
    （先判空再判长度：纯空白串给“密码不能为空”，比给“至少 8 位”更贴近用户实际做错的事。）
    """
    if not isinstance(password, str):
        raise TypeError(f"密码必须是 str，收到 {type(password).__name__}")
    if not password.strip():
        raise ValueError("密码不能为空")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise ValueError("密码长度不能超过 72 字节（约 24 个汉字）")
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    """校验明文密码是否匹配库里的哈希。**失败一律返回 False，绝不抛异常给上层。**

    关键取舍（fail closed）：stored_hash 不是 bcrypt 格式（例如迁移还没跑的遗留明文、
    或者被写入的空串/NULL 占位）时，直接拒绝登录并打一条中文告警。
    绝不能退化成“明文等值比较”兜底 —— 那样只要还有人没迁移，明文比对这条攻击面
    就永远活着；宁可让老用户暂时登不上，由运维把它们跑完 004。
    """
    if not is_bcrypt_hash(stored_hash):
        print("[db] 检测到非 bcrypt 密码，拒绝登录；请先执行 migrations/004_password_bcrypt.sql")
        return False
    try:
        if not isinstance(password, str):
            return False
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception as exc:  # bcrypt 对超长/畸形输入会抛 ValueError，一律当“校验失败”
        print(f"[db] 密码校验异常，按校验失败处理：{exc}")
        return False


_USERNAME_UNIQUE_CONSTRAINT = "users_username_key"
DUPLICATE_USERNAME_MESSAGE = "该用户名已被占用，请换一个或直接登录"


def _is_duplicate_username_error(exc) -> bool:
    """判断一个 IntegrityError 是不是 users.username 唯一约束冲突。

    判据优先用 **约束名**（psycopg2 的 exc.orig.diag.constraint_name）：错误文本会随
    PostgreSQL 版本、区域语言变化（"duplicate key value violates unique constraint"），
    拿文本当判据迟早会失效。拿不到 diag 时才退回文本里找约束名。
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name is not None:
        return name == _USERNAME_UNIQUE_CONSTRAINT
    return _USERNAME_UNIQUE_CONSTRAINT in str(getattr(exc, "orig", exc))


def _touch_last_login(user_id):
    """登录成功后把 users.last_login 写成数据库当前时间（now()）。

    **本函数永不抛异常**：last_login 只服务于后台漏斗（运营指标），
    绝不能因为它把“密码正确”的登录变成“登录失败”—— 一个报表字段不值得挡住用户。

    几个刻意的写法：
    - 独立连接 + 独立提交：bcrypt 校验（约 200~300ms、纯 CPU）已经在调用方做完，
      这里只发一条 UPDATE。**不要把 bcrypt 计算夹进这个事务**，否则一次慢哈希
      （或 UPDATE 的锁等待）就会让 users 行的写锁被意外长时间持有。
    - 用数据库的 now() 而不是 Python 的 datetime.now()：now() 返回 timestamptz（绝对时刻），
      写进 last_login（TIMESTAMPTZ）不需要任何换算；以数据库为唯一时钟，
      应用机的时区/时钟漂移进不来（本机 UTC+8、库 TimeZone=UTC，写 naive 本地时间就会错 8 小时）。
    - 表名**不带** `public.` 前缀：与本文件其余 SQL（`SELECT ... FROM users`、
      `UPDATE users SET credits = ...`、`INSERT INTO users ...`）统一成一种写法，
      统一靠 search_path 找到 public.users。混用 `public.users` 与 `users` 的代价是：
      一旦某个会话/角色把 search_path 改了，只有写 `users` 的那几句会打到别的 schema 上，
      而这种差异在测试库里永远不会暴露。
    - 异常分两类，都只降级成告警、不影响登录：
        · ProgrammingError：几乎只有一种成因 —— migrations/005_last_login.sql 还没执行，
          列不存在，UPDATE 里的 last_login 直接 UndefinedColumn；
        · 其余 SQLAlchemyError / OSError：库暂时不可写、连接超时、权限不足等（含驱动层 OSError）。
      用 logging.warning(..., exc_info=True) 而不是 print：运维能拿到 logger 名和完整栈，
      也方便以后接日志系统（这里不调 basicConfig，交给应用/进程自己配置 handler）。
    """
    global engine
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("UPDATE users SET last_login = now() WHERE id = :uid"),
                {"uid": user_id},
            )
            rowcount = result.rowcount
            conn.commit()
        if rowcount == 0:
            # 连上了、写成功，但一行都没命中：id 不存在（用户被删了 / session 里的 id 过期）。
            # 不算错误，但要留痕，否则“漏斗里这个人怎么没有登录记录”查不出来。
            logging.getLogger(__name__).warning(
                "[db] last_login 未更新：users.id=%r 不存在（rowcount=0），本次登录未记录", user_id
            )
    except ProgrammingError:
        # 最常见的真实成因：005 还没执行。这条告警就是给运维的提示，别让用户看到。
        logging.getLogger(__name__).warning(
            "[db] last_login 写入失败：可能是 migrations/005_last_login.sql 尚未执行"
            "（users.last_login 列不存在，UndefinedColumn）。登录本身不受影响。",
            exc_info=True,
        )
    except (SQLAlchemyError, OSError):
        logging.getLogger(__name__).warning(
            "[db] last_login 更新失败（库不可写 / 连接异常），不影响登录", exc_info=True
        )


# ---------------------------------------------------------------------------
# 登录事件与失败限流（migrations/009_login_events.sql）
#
# 为什么需要一张事件表：users.last_login 只是「最后一次登录」的覆盖式快照 ——
#   它算不出每日登录趋势/留存曲线，也回答不了「最近有没有人在暴力试密码」。
#   009 建 login_events(user_id, username, event, occurred_at) 记下每次尝试。
#
# 限流口径：同一用户名在 LOGIN_WINDOW_MINUTES 分钟内失败达 LOGIN_MAX_FAILURES 次，
#   且这段时间里没有成功登录 → 暂时拒绝继续尝试。
#   按**用户名**计数而不是客户端 IP：Streamlit 侧拿不到可信 IP，硬凑一个反而
#   给人「我们有 IP 级防护」的错觉；按用户名计数会让攻击者能锁定某个账号（DoS），
#   但配合成功后清零与窗口自然过期，代价可接受，且不给用户枚举提供额外信息。
#
# 两张表都可能"还没建"（009 未执行）：所有读写都 fail-open（限流不生效、埋点静默），
#   绝不能让一张统计表把登录流程弄挂。
# ---------------------------------------------------------------------------

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_MINUTES = 15
LOGIN_EVENT_SUCCESS = "success"
LOGIN_EVENT_FAILURE = "failure"

_WARNED_LOGIN_TABLE: set[str] = set()


class LoginThrottled(Exception):
    """登录尝试过于频繁。app.py 捕获它给用户一句中文提示（不是程序故障）。"""


def _is_undefined_table(exc) -> bool:
    """42P01 = undefined_table（009 未执行时最典型的报错）。"""
    return _pg_sqlstate(exc) == "42P01"


def _warn_login_table_once(message, exc=None):
    if "missing" in _WARNED_LOGIN_TABLE:
        return
    _WARNED_LOGIN_TABLE.add("missing")
    logging.getLogger(__name__).warning(message, exc_info=exc is not None)


def record_login_event(username, user_id, event):
    """记录一次登录**成功**事件。**永不抛异常**：埋点失败不能把登录搞挂。

    失败事件请走 record_login_failure()（它把「插入 + 计数」放在同一事务里）。
    """
    if event not in (LOGIN_EVENT_SUCCESS, LOGIN_EVENT_FAILURE):
        raise ValueError(f"非法的登录事件类型：{event!r}")
    un = (username or "").strip() or None
    try:
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO login_events (user_id, username, event) VALUES (:uid, :un, :ev)"),
                {"uid": user_id, "un": un, "ev": event},
            )
            conn.commit()
    except ProgrammingError as exc:
        if _is_undefined_table(exc):
            _warn_login_table_once(
                "[db] 登录事件写入失败：migrations/009_login_events.sql 尚未执行"
                "（login_events 表不存在）。登录本身不受影响。",
                exc,
            )
        else:
            logging.getLogger(__name__).warning("[db] 登录事件写入失败（ProgrammingError）", exc_info=True)
    except (SQLAlchemyError, OSError):
        logging.getLogger(__name__).warning("[db] 登录事件写入失败（库不可写 / 连接异常）", exc_info=True)


# 限流计数 SQL：只数「最后一次成功登录之后」的失败（按 id 判定先后，不用 occurred_at ——
# now() 是事务开始时刻，并发可同微秒、时钟回拨会让先后错乱；id 单调无歧义）。
_LOGIN_FAILURES_SINCE_SUCCESS_SQL = """
    WITH recent AS (
        SELECT id, event
        FROM login_events
        WHERE username = :un
          AND occurred_at >= now() - make_interval(mins => :mins)
    ), last_ok AS (
        SELECT coalesce(max(id), -1) AS last_success_id
        FROM recent WHERE event = :ev_success
    )
    SELECT count(*) FILTER (
               WHERE event = :ev_failure AND id > (SELECT last_success_id FROM last_ok)
           ) AS failures_since_success
    FROM recent
"""


def _login_event_params(**extra):
    """事件取值走常量参数，避免 SQL 里出现 'success'/'failure' 字面量与常量各写一遍。"""
    params = {"ev_success": LOGIN_EVENT_SUCCESS, "ev_failure": LOGIN_EVENT_FAILURE}
    params.update(extra)
    return params


def record_login_failure(username, user_id) -> bool:
    """原子登记一次失败，并返回「登记之后是否已达限流阈值」。

    为什么必须原子：若先 count 再 insert（两条连接），N 个并发请求会同时看到 4 次失败
    而全部放行 —— 单窗口实际尝试次数会变成 5 + 并发度。这里用
    `pg_advisory_xact_lock(hashtext(username))` 把同一用户名的「插入 + 计数」串行化，
    锁随事务结束自动释放（xact 级），不会泄漏到连接池里的后续会话。

    表未建 / 库异常 → 返回 False（fail-open），与 login_is_throttled 一致。
    """
    un = (username or "").strip()
    if not un:
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:un))"), {"un": un})
            conn.execute(
                text("INSERT INTO login_events (user_id, username, event) VALUES (:uid, :un, :ev)"),
                {"uid": user_id, "un": un, "ev": LOGIN_EVENT_FAILURE},
            )
            row = conn.execute(
                text(_LOGIN_FAILURES_SINCE_SUCCESS_SQL),
                _login_event_params(un=un, mins=LOGIN_WINDOW_MINUTES),
            ).fetchone()
        return int(row[0] or 0) >= LOGIN_MAX_FAILURES
    except ProgrammingError as exc:
        if _is_undefined_table(exc):
            _warn_login_table_once(
                "[db] 登录失败事件未记录：migrations/009_login_events.sql 尚未执行"
                "（login_events 表不存在）。按“不限流”处理。",
                exc,
            )
            return False
        logging.getLogger(__name__).warning("[db] 登记登录失败事件失败", exc_info=True)
        return False
    except (SQLAlchemyError, OSError):
        logging.getLogger(__name__).warning("[db] 登记登录失败事件失败（库不可用）", exc_info=True)
        return False


def login_is_throttled(username) -> bool:
    """最近 LOGIN_WINDOW_MINUTES 分钟内「最后一次成功登录之后」的失败数是否已达上限。

    表未建 / 库不可用时一律返回 False（fail-open）：限流是防滥用，
    不该因为一张统计表没建就把所有用户挡在门外。
    """
    un = (username or "").strip()
    if not un:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(_LOGIN_FAILURES_SINCE_SUCCESS_SQL),
                _login_event_params(un=un, mins=LOGIN_WINDOW_MINUTES),
            ).fetchone()
    except ProgrammingError as exc:
        if _is_undefined_table(exc):
            _warn_login_table_once(
                "[db] 登录限流未生效：migrations/009_login_events.sql 尚未执行"
                "（login_events 表不存在）。按“不限制”处理。",
                exc,
            )
            return False
        logging.getLogger(__name__).warning("[db] 登录限流查询失败", exc_info=True)
        return False
    except (SQLAlchemyError, OSError):
        logging.getLogger(__name__).warning("[db] 登录限流查询失败（库不可用）", exc_info=True)
        return False

    # 只统计「最后一次成功登录之后」的失败：成功一次就重新计数，
    # 否则用户被自己历史上的失败长期拖累，限流会变成账号锁定。
    return int(row[0] or 0) >= LOGIN_MAX_FAILURES


def login_events_available() -> bool:
    """login_events 表是否存在（009 是否执行）。管理面板据此显式提示「限流/趋势未启用」。"""
    global engine
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT to_regclass('public.login_events') IS NOT NULL")).scalar() is True
    except (SQLAlchemyError, OSError):
        return False


def verify_user(username, password):
    """校验登录。password 是**明文**；通过返回 user id，否则 None。

    - 只按 username 取 id + password_hash，**比较交给 bcrypt**（SQL 里不再出现密码）；
    - “用户不存在”和“密码错误”都返回 None，由调用方给同一句提示，避免用户名枚举；
    - 用户名不存在时也照样跑一次 bcrypt 校验（对一个惰性生成的假哈希），让两种失败分支
      耗时接近，减少“响应快 = 用户不存在”的时序侧信道。假哈希只在本进程内存里，
      不对应任何真实账号与密码。
    - **校验通过后写 last_login（= now()）**，这是后台漏斗「7 天登录用户/留存率」的
      唯一数据来源。写入发生在 bcrypt 校验**之后**、并在独立连接里提交，
      失败也不影响登录（见 _touch_last_login）。
    - 本函数里没有任何把 Python 时间写进 last_login 的路径：时间一律由数据库的 now()
      （timestamptz）产生，写入 TIMESTAMPTZ 列免换算。
    - **限流**：进入本函数先查 login_is_throttled()，命中抛 LoginThrottled（app.py 展示中文文案）。
      放在 bcrypt 之前，命中时不再消耗一次慢哈希。
    - **登录事件**：成功/失败都写一条 login_events（用户名不存在也记 failure，避免用限流状态
      反推账号是否存在）。009 未执行时埋点静默降级、限流不生效，登录照常。
    """
    global engine, _TIMING_DUMMY_HASH
    username = (username or "").strip()
    # 库里不可能有 >50 的用户名（users.username 是 VARCHAR(50)）：提前拒绝，
    # 既省一次查询，也避免把超长串写进 login_events（否则「超长名」会被截断后记到别人名下）。
    if not username or len(username) > 50:
        return None
    # 限流放在 bcrypt 之前：命中时直接拒绝，省掉一次 200~300ms 的慢哈希
    # （也顺带削弱暴力尝试的成本优势）。命中时抛 LoginThrottled，由 app.py 给中文提示。
    if login_is_throttled(username):
        raise LoginThrottled(f"登录尝试过于频繁，请 {LOGIN_WINDOW_MINUTES} 分钟后再试")

    stmt = text("SELECT id, password_hash FROM users WHERE username = :un")
    with engine.connect() as conn:
        row = conn.execute(stmt, {"un": username}).fetchone()
    if row is None:
        if _TIMING_DUMMY_HASH is None:
            _TIMING_DUMMY_HASH = hash_password("timing-dummy-not-a-real-credential")
        verify_password(password, _TIMING_DUMMY_HASH)
        # 不存在的用户名也记失败：否则攻击者可以用「有没有被限流」反推账号是否存在。
        # 失败事件走原子登记（插入 + 计数在同一个事务里，串行化同一用户名的并发尝试）。
        record_login_failure(username, None)
        return None
    if not verify_password(password, row[1]):
        record_login_failure(username, row[0])
        return None
    user_id = row[0]
    # 成功登录：埋点 + 写 last_login（两者都内部吞异常，不影响本次登录）
    record_login_event(username, user_id, LOGIN_EVENT_SUCCESS)
    _touch_last_login(user_id)
    return user_id

def _bill_insert_statement(write_category, write_mood):
    """按「012 / 019 是否已执行」选出四种 INSERT 之一（列名全是字面量）。

    为什么要写死四种组合：category 与 mood 分属两个迁移，线上完全可能出现
    「012 已执行、019 未执行」（或反过来）的中间态。只要把**不存在的列**写进
    INSERT，整笔账单就会失败 —— 而分类和情绪都只是附加信息，绝不能拖垮记账。
    调用方只有在 write_* 为 True 时才绑对应的参数，用户数据一律走绑定参数。
    """
    if write_category and write_mood:
        return text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category, mood)
            VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat, :mood)
        """)
    if write_category:
        return text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category)
            VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat)
        """)
    if write_mood:
        return text("""
            INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, mood)
            VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :mood)
        """)
    return text("""
        INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
        VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
    """)


def add_bill(user_id, date, merchant, amount, payment_method, type, category=None, mood=None):
    global engine
    # 参数名 date 是历史遗留（遮蔽了 datetime.date），本函数里不再用 date 类型，只当值用。
    #
    # 三项校验全部放在**开连接之前**：非法入参连库都不碰。
    # ① 日期：必须是严格 ISO 'YYYY-MM-DD'（bills.date 是 VARCHAR，靠 ISO 字典序排序，
    #    格式必须钉死；见 _normalize_bill_date 的长注释和 007 的 CHECK 兜底）。
    #    这是 AI 记账 path 的主要风险面：func_args.get("date", ...) 取到什么就是什么。
    # ② 类型：乱归一律抛 ValueError（app.py 已有 except Exception 兜底提示）。
    # ③ 金额：同一套 _normalize_amount 收敛符号+两位小数，与 CSV 导入路径口径一致。
    # 三者都是 ValueError 向上抛，由 app.py 的 except 显示成中文文案。
    bill_date = _normalize_bill_date(date)
    bill_type = normalize_bill_type(type, strict=True)
    amt = _normalize_amount(amount, bill_type)
    # 分类在开连接之前算好。auto_categorize 内部已经吞掉 AI/导入异常并回落 "其他"，
    # 所以分类失败绝不会让这笔账记不上（这是本功能的硬约束）。
    # category 显式传了就尊重调用方（app.py 目前不传，保持位置参数兼容）。
    category_value = category or auto_categorize(merchant, "", api_key=_deepseek_key())
    # 情绪同样在开连接之前收敛：normalize_mood 永不抛异常，AI 把整段描述塞进 mood
    # 也只会变成 ""（= 未标注），不会让这笔账记不上（与分类同一个硬约束）。
    # 注意 mood 是**只能追加**的 keyword 参数：老调用方仍可按位置传 category。
    mood_value = normalize_mood(mood)
    # 列存在性探测放在开写连接之前：避免在一条已检出的连接上再借第二条连接做元数据查询。
    write_category = _bills_category_supported()
    write_mood = _bills_mood_supported()
    with engine.connect() as conn:
        # 012 / 019 的四种组合由 _bill_insert_statement 统一挑选：缺哪列就少写哪列。
        # 降级只发生在这里，告警由两个 _bills_*_supported() 负责（每个进程一次）。
        stmt = _bill_insert_statement(write_category, write_mood)
        params = {
            "uid": user_id,
            "dt": bill_date,
            "mer": merchant,
            "amt": amt,
            "pm": payment_method,
            "tp": bill_type,
        }
        if write_category:
            params["cat"] = category_value
        if write_mood:
            # 空串在读取侧与 NULL 等价，但库里保持 NULL 更干净：
            # 「没标注」和「标注了一个空情绪」在数据上不该是两种东西。
            params["mood"] = mood_value or None
        conn.execute(stmt, params)
        conn.commit()

def get_all_bills(user_id):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid")
    df = pd.read_sql(sql, engine, params={"uid": user_id})
    # 顺序无所谓，但四列都必须收口：type 归一化（字符串口径）+ amount 转 float（数值口径）
    # + category 空值收敛 + mood 收敛（后两者列不存在时原样返回，绝不凭空造列）。
    return _normalize_category_column(
        _normalize_mood_column(_normalize_type_column(_normalize_amount_column(df)))
    )

def get_bills_paginated(user_id, limit, offset):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid ORDER BY date DESC LIMIT :lim OFFSET :off")
    df = pd.read_sql(sql, engine, params={"uid": user_id, "lim": limit, "off": offset})
    return _normalize_category_column(
        _normalize_mood_column(_normalize_type_column(_normalize_amount_column(df)))
    )

def get_total_bill_count(user_id):
    global engine
    with engine.connect() as conn:
        stmt = text("SELECT COUNT(id) FROM bills WHERE user_id = :uid")
        return conn.execute(stmt, {"uid": user_id}).scalar() or 0

def insert_demo_bills(user_id):
    global engine
    demo_data = [
        # mood 是**确定性**写死的（不按商户猜、不调 AI）：演示数据的意义是"点一下就能看到
        # 效果"，同一份数据每次加载出的气象站必须完全一样。收入行标 None —— 情绪分析只
        # 看支出，给收入编一个情绪只会污染口径。
        {"date":"2026-10-01","merchant":"食堂","amount":12.5,"payment_method":"校园卡","type":"支出","mood":"开心"},
        {"date":"2026-10-02","merchant":"蜜雪冰城","amount":8.0,"payment_method":"微信","type":"支出","mood":"疲惫"},
        {"date":"2026-10-03","merchant":"淘宝网购","amount":129.0,"payment_method":"支付宝","type":"支出","mood":"激动"},
        {"date":"2026-10-04","merchant":"兼职","amount":400.0,"payment_method":"银行卡","type":"收入","mood":None},
        {"date":"2026-10-05","merchant":"白条分期","amount":99.0,"payment_method":"白条","type":"支出","mood":"焦虑"},
        {"date":"2026-10-06","merchant":"食堂","amount":15.0,"payment_method":"校园卡","type":"支出","mood":"开心"},
        {"date":"2026-10-07","merchant":"瑞幸咖啡","amount":14.0,"payment_method":"微信","type":"支出","mood":"疲惫"},
        {"date":"2026-10-08","merchant":"拼多多","amount":45.5,"payment_method":"支付宝","type":"支出","mood":"激动"},
        {"date":"2026-10-09","merchant":"家教兼职","amount":300.0,"payment_method":"微信","type":"收入","mood":None},
        {"date":"2026-10-10","merchant":"超市","amount":56.2,"payment_method":"支付宝","type":"支出","mood":"开心"},
    ]
    write_category = _bills_category_supported()
    write_mood = _bills_mood_supported()
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM bills WHERE user_id = :uid"), {"uid": user_id})
        # 012 / 019 的四种组合由同一个函数挑选：缺哪列就少写哪列（降级，记账优先）。
        stmt = _bill_insert_statement(write_category, write_mood)
        for row in demo_data:
            # 演示数据里本来就是中文字面量，归一化是幂等的（防止以后有人改数据源）
            bill_type = normalize_bill_type(row["type"], strict=True)
            # 演示数据只走规则（不传 api_key）：结果确定、不依赖外部 AI 是否可用；
            # 规则命不中的（兼职 / 白条分期）落 "其他"，仍保证在 BILL_CATEGORIES 内。
            category_value = auto_categorize(row["merchant"], "")
            params = {
                "uid": user_id,
                # 日期也过同一个校验：本表数据全部是 ISO（幂等、结果不变），
                # 接入的目的是把"以后有人改 demo_data"钉在和 add_bill / CSV 完全相同的
                # 口径上 —— 否则"示例数据"就成了唯一能绕过格式校验的后门。
                # 就算将来写错了，异常也会让 with 块回滚，DELETE 不会留下半成品。
                "dt": _normalize_bill_date(row["date"]),
                "mer": row["merchant"],
                # 走同一个金额口径：这里数据本就是正的，abs 不会改变结果
                "amt": _normalize_amount(row["amount"], bill_type),
                "pm": row["payment_method"],
                "tp": bill_type
            }
            if write_category:
                params["cat"] = category_value
            if write_mood:
                # 与 add_bill 同一口径：认不出/收入行都写 NULL（= 未标注）
                params["mood"] = normalize_mood(row.get("mood")) or None
            conn.execute(stmt, params)
        conn.commit()
    return True

def _excel_row_number(idx, position):
    """把 DataFrame 索引换算成“用户在 Excel / CSV 里看到的行号”。

    表头占第 1 行，所以 DataFrame 索引 0 对应 Excel 第 2 行 → idx + 2。
    索引不是整数（有人 set_index 过 / 多级索引）时退回按出现顺序算的行号，
    保证跳过明细里永远有个能查的行号，且绝不抛异常。
    """
    try:
        return int(idx) + 2
    except (TypeError, ValueError):
        return position + 2


def _describe_amount_error(raw_amount):
    """把金额的 ValueError 翻译成一句中文原因（不是数字 / nan-inf / 超上界）。

    _normalize_amount() 只会因为三种原因抛 ValueError：
      ① float() 都转不动 → 不是数字；② 转得动但不是有限数 → nan/inf；
      ③ 有限数但绝对值超过 NUMERIC(12,2) 上界 9999999999.99。
    这里用同样的判定复述一遍，页面就能拿到准确的短句（否则超上界会被误报成 nan/inf）。
    """
    try:
        value = float(raw_amount)
    except (TypeError, ValueError):
        return f"金额不是数字：{raw_amount!r}"
    if not math.isfinite(value):
        return f"金额非法 nan-inf：{raw_amount!r}"
    try:
        rounded = Decimal(str(abs(value))).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return f"金额超出上限 {MAX_ABS_AMOUNT}：{raw_amount!r}"
    if rounded > Decimal(str(MAX_ABS_AMOUNT)):
        return f"金额超出上限 {MAX_ABS_AMOUNT}：{raw_amount!r}"
    return f"金额非法：{raw_amount!r}"


def import_csv_to_bills(user_id, df):
    """把 CSV（DataFrame）整表导入 bills，返回结构化结果，**不抛异常给调用方**。

    返回：
        {"imported": 实际写入的行数,
         "skipped":  [{"row": Excel 行号, "reason": "中文短句"}, ...],
         "total":    CSV 总行数}

    行为（与改版前保持一致，只是把信息从 print 提到返回值）：
    - 先整表校验、再删旧写新：坏行只跳过自己，不影响其它行；
    - 日期列过 _normalize_bill_date：空值 / 非 ISO 格式 / 不存在的日子 → 跳过该行，
      原因写进 skipped（"账单日期不能为空" / "日期格式非法：'2026/10/1'" /
      "日期不存在（不是真实日历日）：'2026-02-30'"）。**不整批失败**：
      CSV 是用户手写的，一行写错不该让另外 99 行也导不进来；
    - 有有效行时才 DELETE 旧账单 + 批量插入；
    - 一条有效行都没有时**完全不碰库**（imported=0，库里原账单原样保留）；
    - 服务端 print 告警保留（便于排查），但页面要用信息一律走返回值。
    """
    result = {"imported": 0, "skipped": [], "total": 0}

    if not isinstance(df, pd.DataFrame):
        # 程序性错误不往上抛：给页面一个能直接说的结果，原因写清楚
        print(f"[import_csv_to_bills] 入参不是 DataFrame（{type(df).__name__}），本次不写库")
        return result

    result["total"] = int(len(df))
    if result["total"] == 0:
        print("[import_csv_to_bills] 传入的 DataFrame 为空，本次不写库")
        return result

    # 先整表校验、再删旧写新：校验不通过时不碰库，避免“清空了旧账单却一条也没导进去”。
    rows = []
    for position, (idx, r) in enumerate(df.iterrows()):
        row_no = _excel_row_number(idx, position)  # 用户在 Excel 里看到的行号
        # 日期先校验（最便宜、也最容易出错的一列）：
        # **坏日期只跳过这一行，不整批失败** —— CSV 是用户手写的，一行写错不该让
        # 另外 99 行导不进来，也不该让"导入"这个动作变成全有或全无。
        # 原因文案按成因分开（空值 / 格式非法 / 日子不存在），见 _describe_date_error。
        raw_date = r.get("date")
        try:
            bill_date = _normalize_bill_date(raw_date)
        except ValueError:
            reason = _describe_date_error(raw_date)
            result["skipped"].append({"row": row_no, "reason": reason})
            print(f"[import_csv_to_bills] 第 {row_no} 行 {reason}，已跳过该行")
            continue
        # 顺序很重要：先定类型，再按类型收敛符号。
        # 反过来（先 abs 再定类型）会把「收入 -100」翻成 +100 的收入 → 结余虚高。
        try:
            bill_type = normalize_bill_type(r.get("type"), strict=True)
        except ValueError:
            reason = f"账单类型非法：{r.get('type')!r}"
            result["skipped"].append({"row": row_no, "reason": reason})
            print(f"[import_csv_to_bills] 第 {row_no} 行 {reason}，已跳过该行")
            continue
        try:
            amt = _normalize_amount(r.get("amount"), bill_type)
        except ValueError:
            reason = _describe_amount_error(r.get("amount"))
            result["skipped"].append({"row": row_no, "reason": reason})
            print(f"[import_csv_to_bills] 第 {row_no} 行 {reason}，已跳过该行")
            continue
        # 分类：CSV 有 category 列且值在 BILL_CATEGORIES 内就用它（先 strip），
        # 否则按商户/描述走规则自动打标。这里**不传 api_key**：批量导入可能有几百行，
        # 逐行调 AI 既慢又贵，规则未命中直接落 "其他"（仍可后续人工改）。
        raw_category = r.get("category") if "category" in df.columns else None
        if isinstance(raw_category, str) and raw_category.strip() in BILL_CATEGORIES:
            category_value = raw_category.strip()
        else:
            category_value = auto_categorize(r.get("merchant"), "")
        rows.append({
            "uid": user_id,
            "dt": bill_date,
            "mer": r.get("merchant"),
            "amt": amt,
            "pm": r.get("payment_method"),
            "tp": bill_type,
            "cat": category_value
        })

    if not rows:
        # 一条有效行都没有：直接返回，保留库里原有账单，不提交任何变更
        print("[import_csv_to_bills] 没有可导入的有效行，本次不写库（库里原账单保持不变）")
        return result

    write_category = _bills_category_supported()
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM bills WHERE user_id = :uid"), {"uid": user_id})
            if write_category:
                stmt = text("""
                    INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category)
                    VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat)
                """)
            else:
                # 012 未执行：去掉 category 键再插入（降级，导入优先）
                stmt = text("""
                    INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                    VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
                """)
                rows = [{k: v for k, v in row.items() if k != "cat"} for row in rows]
            for row in rows:
                conn.execute(stmt, row)
            conn.commit()
    except Exception as exc:
        # 写库失败属于程序性/环境性错误（连接断了、约束冲突等）：同一个事务里
        # DELETE + INSERT 要么全成要么全不成（异常时 with 退出会回滚，旧账单不会被删）。
        # 这种情况不能假装“导入成功”，必须让调用方（app.py 的 except Exception）看到。
        print(f"[import_csv_to_bills] 写库失败，本次导入已回滚（旧账单未被删除）：{exc}")
        raise

    result["imported"] = len(rows)
    return result

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

# ---------------------------------------------------------------------------
# 管理员鉴权（唯一入口）
# ---------------------------------------------------------------------------
# 背景：app.py 的管理面板代码一直都在，但 st.session_state.is_admin 从来只有 False，
# 面板等于死代码。把"谁是管理员"落到 users.is_admin（migrations/006_user_admin_flag.sql），
# 由下面这个函数回答 —— 页面不再自己猜，也不再把管理员身份写死在代码/配置里。
#
# 为什么把"用户是否存在"和"是否管理员"合并成**一次查询**（get_user_auth_state）：
#   改版前 app.py 每轮 run 要问两次（两次各是 db 里的一个函数）：
#       第 1 次：SELECT COUNT(id) FROM users WHERE id = :uid     -- 只回答"在不在"
#       第 2 次：SELECT COALESCE(is_admin, FALSE) ...            -- 只回答"是不是管理员"
#   两个问题各开一个连接、各发一条 SQL，而且**中间存在 TOCTOU 窗口**：第 1 次说"存在"、
#   第 2 次执行前用户被删掉（或反过来），页面就会拿着一个已不存在的 user_id 继续渲染，
#   或者把一个刚被撤销管理员的人当成管理员过一个 rerun。
#   （那两个旧函数在本批次已按 reviewer 要求删除：全仓 0 调用点。全仓 grep 过
#     app.py / db_init.py / seed_knowledge.py / migrations，无人引用。）
#   合并成一条 `SELECT id, COALESCE(is_admin, FALSE) FROM users WHERE id = :uid` 之后：
#     · 一次往返（省掉一次连接建立/归还 + 一次网络 RT）；
#     · 两个答案来自**同一个快照/同一行**，不可能互相矛盾（行在 == 用户存在）；
#     · app.py 只需要一次 try/except。
# ---------------------------------------------------------------------------

# 鉴权查询降级时的告警去重：**按 pgcode 分别记一次**（不是"全局只报一次"）。
#   · 42703（undefined_column = 006 没执行）：用 log.warning，**每个 pgcode 只报一次**。
#     app.py 每轮 rerun 都会问一次鉴权，一次交互可能触发好几轮；不去重就会被同一条
#     warning 刷屏（每次点击 +N 行），运维反而看不到真正的信号。
#   · 其它 pgcode（42501 权限不足 / 42P01 表不见 / 语法类等）：用 log.error 且**不抑制**，
#     每次都要报 —— 那不是一个"等运维跑迁移"的已知状态，而是**代码/环境 bug**，
#     抑制它等于把 bug 藏起来。
# ⚠ 这里刻意是 set 而不是一个 bool：旧实现用一个布尔同时管 42703 和"其它"，
#   于是只要先出现过一次"其它"（比如权限抖动），后来真正的"缺列"就再也不会告警，
#   运维永远看不到"请执行 006"。按 pgcode 分桶之后，两种告警互不影响。
_ADMIN_WARNED_CODES: set[str] = set()

# 缺列（006 未执行）的 SQLSTATE。抽成常量，免得下面的判据与告警文案各写一份字符串。
_PGCODE_UNDEFINED_COLUMN = "42703"


def _pg_sqlstate(exc, default=None):
    """从 SQLAlchemy 的 ProgrammingError 里挖出 SQLSTATE（兼容 psycopg2 / psycopg3）。

    为什么要"兼容"而不是直接 `exc.orig.pgcode`：
      · psycopg2（本项目当前驱动，SQLAlchemy 2.1 下的 psycopg2 2.9.13）把 SQLSTATE
        暴露成 **exc.orig.pgcode**；
      · psycopg3 没有 pgcode 属性，叫 **exc.orig.sqlstate**；
      · 两边都有 exc.orig.diag.sqlstate（DBAPI 标准位置），但它可能不存在（非 psycopg 驱动）。
    三级回退再落到 default：拿到 None 时调用方会走"未知 pgcode"分支（log.error、不抑制），
    绝不会因为取不到 code 就把真故障静默成"无权限"。
    用 getattr 串起来（而不是 try/except 或 isinstance 分支）是因为这些属性都可能是
    None 或干脆不存在，getattr(..., None) + or 链最省事，也不会因为某个中间层是 None 而抛。
    """
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "pgcode", None)                       # psycopg2
        or getattr(orig, "sqlstate", None)                  # psycopg3
        or getattr(getattr(orig, "diag", None), "sqlstate", None)  # DBAPI diag
        or default
    )


def get_user_auth_state(user_id) -> tuple[bool, bool]:
    """一次查询回答两个问题：返回 ``(exists, is_admin)``，两个都是**真 bool**。

    契约（app.py 依赖它，改签名/改语义前先看 app.py 的调用点）：
    - 返回 `(bool, bool)`：不返回 None、不返回 1/0、不返回 numpy.bool_ ——
      调用方可能写 `is True` 或把它塞进 JSON，非 bool 在那里会静默出错。
    - 用户不存在（id 不存在 / 为 None / 会话里的 id 已过期被删）→ ``(False, False)``。
      "不存在"必须是 False，**不能**退化成 True：页面靠它决定是否 logout。
    - migrations/006 未执行（is_admin 列不存在）→ **优雅降级**：
      退回 `SELECT id FROM users WHERE id = :uid`，返回 ``(存在?, False)``，
      并打一条告警（用模块级 _ADMIN_WARNED_CODES 按 pgcode 分桶：缺列那种只报一次，
      其它 pgcode 每次都报 —— 见下面两档说明）。
      降级方向是刻意的：鉴权失败只能落成"无权限"，绝不能落成"有权限"，
      但"用户在不在"这件事仍然要答对（否则会把正常用户踢下线）。
      告警按 pgcode 分两档（判据见 _pg_sqlstate，兼容 psycopg2 的 .pgcode 与
      psycopg3 的 .sqlstate/.diag.sqlstate）：
        · ``42703``（undefined_column）→ ``log.warning``，文案指向"请执行 006"，
          **同一个 pgcode 只报一次**（否则每轮 rerun 刷屏）；
        · 其它 pgcode / 取不到 pgcode → ``log.error``，**每次都报、绝不抑制**。
          那不是一个"等运维跑迁移"的已知状态，而是 bug/环境故障（权限、表不见、
          SQL 拼错）；一句"请执行 006"只会把运维引到错路上，而抑制它等于把 bug 藏起来。
      **告警一律打在降级查询之后**：降级查询本身也失败时异常直接上抛，
      日志里就不会留下一条"已降级成功"的误导记录（旧顺序是先报降级、后失败）。
    - 其余 `SQLAlchemyError` / `OSError` / 未知异常：**一律向上抛，不吞**。
      这是与"旧版薄封装（已删）"最重要的语义变化：旧版把所有异常都吞成 False，
      于是"连不上库"和"这个用户不是管理员"在页面上长得一模一样 ——
      库抖一下，全体用户都被当成"无权限"（更糟的是如果被当成"不存在"，
      就直接把人踢下线）。连不上库是环境故障，必须让调用方看见（app.py 外层
      有 try/except，会停止渲染并提示，而不是静默降权）。
    - 只读、单条语句、不写库、**不做缓存**：管理员一被撤销，下一次调用立刻生效
      （app.py 每次 rerun 都会重新问一次）。

    SQL 为什么这么写：
    - `SELECT id, COALESCE(is_admin, FALSE) FROM users WHERE id = :uid`
      · 取 id 是为了让"有没有这一行"和"is_admin 是什么"来自**同一次执行**，
        避免再发一条 COUNT 查询（也就是本轮要消灭的 TOCTOU）；
      · COALESCE 是第二层保险：006 已把该列建成 NOT NULL，理论上不会有 NULL；
        但鉴权路径上多一层兜底成本为零，收益是"绝不让三值逻辑漏进调用方"
        （NULL → None，`if None:` 恰好是 False，可这种"恰好正确"经不起重构）。
    - 表名**不带** `public.` 前缀：与本文件其余 SQL 统一（靠 search_path 找 public.users）。
    """
    global engine
    log = logging.getLogger(__name__)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, COALESCE(is_admin, FALSE) FROM users WHERE id = :uid"),
                {"uid": user_id},
            ).fetchone()
    except ProgrammingError as exc:
        # 两条分支都要"降级"（否则用户会被当不存在而被踢下线），差别只在**怎么报**。
        #
        # ⚠ 顺序很重要：**先**跑降级查询，**再**打告警。
        #   旧顺序是先 log.warning("已降级…") 再去执行降级查询；万一降级查询也失败
        #   （权限不足 / 表不见 / 连接断了），异常虽然照样上抛，但日志里已经多了一条
        #   "已降级成功"的记录 —— 排障的人会被这条误导（以为问题只在 is_admin 上）。
        #   现在降级查询失败就直接上抛，一条告警都不留：日志与实际行为一致。
        # 这里**故意不捕获**降级查询的异常：它也失败说明库真的有问题，交给调用方。
        with engine.connect() as conn:
            fallback = conn.execute(
                text("SELECT id FROM users WHERE id = :uid"), {"uid": user_id}
            ).fetchone()

        pgcode = _pg_sqlstate(exc)  # 兼容 psycopg2(.pgcode) / psycopg3(.sqlstate)
        if pgcode == _PGCODE_UNDEFINED_COLUMN:  # 42703 undefined_column = 006 没执行
            # 按 pgcode 分桶，**每个 code 只报一次**：这是"等运维跑迁移"的已知状态，
            # 但 app.py 每轮 rerun 都会问一次，不去重就会刷屏。
            if _PGCODE_UNDEFINED_COLUMN not in _ADMIN_WARNED_CODES:
                _ADMIN_WARNED_CODES.add(_PGCODE_UNDEFINED_COLUMN)
                log.warning(
                    "[db] 鉴权查询降级：users.is_admin 列不存在（pgcode=42703），"
                    "可能未执行 migrations/006_user_admin_flag.sql。"
                    "本次退回“只查用户是否存在”，管理员一律按 False 处理"
                    "（管理面板对所有人隐藏）。此告警只打一次，避免每轮 rerun 刷屏；"
                    "请执行 006 让管理面板恢复。",
                    exc_info=True,
                )
        else:
            # 其它 pgcode = **bug / 环境故障**，不是"没跑迁移"：每次都要报（log.error）。
            # 用 log.error 而不是 warning：warning 在多数日志配置里会被忽略，
            # 而这条恰恰是"必须有人来看"的那一类。
            log.error(
                "[db] 鉴权查询失败（ProgrammingError，pgcode=%r，不是缺列 42703），"
                "本次退回“只查用户是否存在”，管理员按 False 处理。"
                "请检查鉴权 SQL、数据库对象权限与连接状态。**此告警不抑制，每次都会报**。",
                pgcode,
                exc_info=True,
            )
        return (bool(fallback is not None), False)

    # 注意用 `row is not None` 而不是 `if row:`：Row 的真值来自长度，
    # 一行 `(0, False)` 虽然非空却是"合法但全 False"的答案，别让真值判断混进来。
    return (bool(row is not None), bool(row[1]) if row is not None else False)


# 备注（本批次已删除两个死符号，别再写回来）：
#   · 「只查用户是否存在」的薄封装：全仓 0 调用点。app.py 每轮 rerun 用的是
#     get_user_auth_state()（一次查询同时给出"存在"与"是否管理员"，没有 TOCTOU），
#     单独的存在性查询没有用户；留着只会让下一个人多写一条 COUNT。
#   · 「只返回 is_admin」的薄封装：同样是 0 调用点，而且它是个**陷阱** ——
#     名字读起来像"不抛异常的布尔判断"，实际会从 get_user_auth_state 把异常抛出去
#     （连不上库 ≠ 没有权限）。同一个问题有两个语义不同的入口，迟早有人用错那个。
#   需要权限一律调用 get_user_auth_state(user_id)[1]，需要存在性用 [0]。


# 后台图表的时间窗口：**含今天在内共 7 个自然日**（UTC）。
# 定义成常量而不是散落的魔法数字，是为了让下面 SQL 的 >= / < 与 docstring 永远同步：
#   days=6 → [今天-6 00:00, 明天 00:00) = 7 个自然日（x 轴最多 7 个点，和标题“过去 7 天”对齐）
#   days=7 → [今天-7 00:00, 明天 00:00) = 8 个自然日（图会比标题多一天）
ADMIN_CHART_DAYS = 7


def _admin_chart_frame(df):
    """把后台图表的查询结果收敛成固定列名 ``["date", "count"]`` 的 DataFrame。

    为什么必须显式收口（不是洁癖，是两条真实的崩法）：
    - 0 行时列名不能丢：app.py 直接 `reg_7df["count"].sum()` / `reg_7df["date"].tolist()`，
      列名没保住就是 KeyError → 管理面板红屏。而**活跃图必然是空结果**（库里 now bills 0 行）。
    - `date` 统一输出 `'YYYY-MM-DD'` 字符串：调用方只拿它当 echarts 的 x 轴 category 标签，
      而 `datetime.date` 对象**不是 JSON 可序列化的**（st_echarts 会把 options 原样序列化
      给前端组件），传 date 对象会在面板可达的那一刻才炸 —— 正是这一轮要消灭的那类坑。
      这里用 .map 而不是 pd.to_datetime：bills.date 是库里原样的 VARCHAR，非 ISO 的脏值
      不该因为"画个图"就抛异常（转换成日期是另一件事）。
    - `count` 统一 int64：PG 的 COUNT() 已经是 bigint，这里只是保证空结果时也是数值列，
      `.sum()` 得到 0 而不是 object 拼接。
    - 空表的 `date` 用 `dtype=object`，**不用** `dtype="str"`：`"str"` 是**pandas 相关的**
      推断结果（pandas 2.x 里 `text` 列 map 出来是 object；pandas 3.x 默认 str dtype，
      实测本 venv pandas 3.0.6 下 `Series(['x']).map(str).dtype == str`），而 requirements
      没锁 pandas 版本 —— 用 `"str"` 会让"空表"这条路径的含义随环境变。
      所以两条路径都显式钉成 `object`：空表这里 `pd.Series(dtype=object)`，非空那侧在
      `.map(...)` 之后补 `.astype(object)`。这样无论 pandas 2 / 3，`df.dtypes` 都是
      `{date: object, count: int64}`，调用方 `.tolist()` / JSON 序列化行为不随环境漂移。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(
            {"date": pd.Series(dtype=object), "count": pd.Series(dtype="int64")}
        )
    out = df.loc[:, ["date", "count"]].reset_index(drop=True)
    out["date"] = out["date"].map(
        lambda v: v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    ).astype(object)  # 与空表路径的 dtype=object 对齐（pandas 3 的 .map(str) 会给 str dtype）
    out["count"] = out["count"].astype("int64")
    return out


def _fill_utc_days(df, days=ADMIN_CHART_DAYS):
    """把图表数据补零到固定的 N 个 UTC 自然日。

    为什么需要：没有样板的日期不会出现在结果里，折线图会少点、点距不均 ——
    "过去 7 天只有 2 个点"很容易被读成"每天都差不多"。补零后 x 轴恒定 N 个日期，
    缺数据的日期就是 0（对趋势图而言这是正确语义，不是伪造数据）。
    """
    df = _admin_chart_frame(df)
    today_utc = datetime.now(timezone.utc).date()
    labels = [(today_utc - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    counts = dict(zip(df["date"].astype(str).tolist(), df["count"].tolist()))
    return pd.DataFrame(
        {
            "date": pd.Series(labels, dtype=object),
            "count": pd.Series([int(counts.get(d, 0)) for d in labels], dtype="int64"),
        }
    )


def get_admin_operate_data():
    """后台面板两张折线图 + 赞助用户数，返回 ``(reg_df, active_df, sponsor_count)``。

    返回契约（app.py 按这个解包，见 `reg_7df, active_7df, sponsor_user_count = ...`）：
      - reg_df    : DataFrame，列固定为 ``["date", "count"]``，按 UTC 自然日的注册数；
      - active_df : DataFrame，列固定为 ``["date", "count"]``，按 UTC 自然日的活跃用户数
                    （活跃 = 当天有账单的**去重**用户数）；
      - sponsor_count : int，credits > 0 的用户数（模拟赞助）。
      两个 DataFrame 都保证：列名固定、date 是 'YYYY-MM-DD' 字符串、0 行时仍然可用。

    ⚠ 时间口径与 get_admin_funnel_data() **故意不同**，两处不要"对齐"成同一个数：
      · 本函数 = **UTC 自然日窗口**，含今天在内共 ADMIN_CHART_DAYS(7) 个自然日：
            [今天(UTC) 00:00 - 6 天, 明天(UTC) 00:00)
        按"天"分桶是为了画趋势线（x 轴最多 7 个点）。
      · get_admin_funnel_data() 的「7 天登录用户」= **滚动 7×24 小时**：
            last_login >= now() - interval '7 days'
        按"最近 168 小时"算，是为了回答"最近一周还有多少人回来过"。
      同一时刻两者最多差一天，这是刻意的：一个是"日历周"，一个是"滑动周"。
      要改口径就两处一起改、并同步这两段 docstring；**不要**只把这里的 ADMIN_CHART_DAYS
      调成 7 之外的数去"凑"漏斗的数字 —— 那会让"过去 7 天"的标题和实际柱数对不上。

    为什么边界是 **tz-aware UTC datetime**、分桶要 `(created_at AT TIME ZONE 'UTC')::date`：
      · users.created_at 现在是 **timestamptz**（migrations/008）：列里存的是绝对时刻，
        读出来按会话时区渲染。既然列是绝对时刻，参数也必须给绝对时刻 ——
        用 `datetime(..., tzinfo=timezone.utc)` 构造边界，比较的是两个瞬间的先后，
        与应用机时区（本机 UTC+8）、库的会话 TimeZone 都无关；
      · 分桶 `(created_at AT TIME ZONE 'UTC')::date`：先把 timestamptz 折回 UTC 墙钟
        （得到 naive），再截成日期。**这一步不经过会话 TimeZone**，所以同一行在
        UTC+8 / UTC-5 的会话里永远落进同一个 UTC 自然日；
      · "今天"必须取**UTC 的今天**：`datetime.now(timezone.utc).date()`。
        不能用本机的 `date.today()`：本机是 UTC+8，UTC 16:00 之后本机已经是"明天"，
        窗口会整体错一天，而库里所有时间都是 UTC；
      · 左闭右开 `>= :start AND < :end`，不是 BETWEEN：右开让"明天 00:00:00.000"那一瞬间
        落进下一天；边界是常量、函数不包列 —— 说明：users.created_at 上**目前没有索引**
        （users 只有 pkey / username unique / idx_users_last_login），现在是顺序扫描；
        这样写是为了将来真给它建普通 B-tree 时也能直接用上，不需要表达式索引；
      · 窗口仍是**含今天在内共 ADMIN_CHART_DAYS(7) 个 UTC 自然日**：
            [今天(UTC) 00:00 - 6 天, 明天(UTC) 00:00)

    为什么活跃数用**字符串比较**而不是 DATE(date)：
      bills.date 是 VARCHAR/TEXT（'YYYY-MM-DD'，由 app.py 的 `date.strftime('%Y-%m-%d')`
      和 CSV 导入路径写入）。旧代码写的是 `DATE(date) BETWEEN :start AND :end`。**实测（本库 PG 17）**：
        · `DATE(text)` / `DATE(varchar)` **不会**报 "function date(...) does not exist" ——
          PG 会把 `date(x)` 解析成 I/O 转换 cast（等价 `x::date`），所以旧写法其实跑得通；
        · 它真正的雷有两个：
          ① 逐行解析字符串：只要**有一行**脏值（'abc'、空串），整条查询就抛
             `invalid input syntax for type date`，管理面板整体红屏 —— 数据相关的雷，
             平时不响，等某个 CSV 导进来才响；
          ② 函数包在列上：date 列上**目前没有索引**（bills 只有 bills_pkey 和
             idx_bills_user_id），所以现在本来就是顺序扫描；但将来给 date 建普通 B-tree
             索引时，`DATE(date)` 用不上它，只有额外建**表达式索引**才行。
      ISO 'YYYY-MM-DD' 的字符串字典序 == 日期序（位数固定、零填充），实测
      `'2026-10-16' >= '2026-10-11' AND '2026-10-16' < '2026-10-18'` 为真、
      与日期比较同结果，所以 `date >= :start_text AND date < :end_text` 既正确、
      零 cast、将来建的普通索引也能直接用。
      ⚠ 代价是脏值变成**静默**的：非 ISO 行（如 '2026/10/1'）不报错、也不落进窗口
      （实测 `'2026/10/1' >= '2026-10-11'` 为 False，即被当成"不在窗口内"）。
      ⚠ **事实订正（别被"只有 ISO 来源"这种说法骗了）**：写入侧**当前不校验 date 格式**。
      app.py 里 AI function calling 的 `func_args.get("date", ...)` 取到什么就入库什么，
      CSV 导入路径的 `date` 也是如此 —— 都是**原样入库**，没有格式校验、没有归一；
      真正被 strftime 格式化过的只有一条路径。所以"bills.date 里只可能是 ISO"**不成立**，
      它会被人（模型/CSV）写成 '2026/10/1'、'10/01/2026'、'2026-10-1' 这类值。
      本条查询的正确性因此依赖一个**外部约定**：`bills.date` 上的字符串比较==日期比较，
      只有在值确实是零填充 ISO（'YYYY-MM-DD'）时才成立（字典序 == 日期序）。
      这个约定数据库**没有**替你保证；非 ISO 值只会在活跃图里**静默**不入窗，不报警。
      以后若要导非 ISO 数据，请在导入时归一，或给 date 列加 CHECK 约束 ——
      **不要**指望这条查询替你报警。（给写入侧补格式校验是另一件事，不在本条注释范围内。）

    注册数为什么按 UTC 折日（`(created_at AT TIME ZONE 'UTC')::date`），而不是 `created_at::date`：
      · 对 timestamptz 直接用 `created_at::date` 是 **timestamptz→date**，那一步按
        **会话 TimeZone** 折日 —— 同一个时刻在 UTC+8 会话里可能已经算"明天"，
        图上的日期桶会随环境漂移；
      · 先 `AT TIME ZONE 'UTC'` 把绝对时刻折回 UTC 墙钟（naive），再 `::date` 是
        naive 截断（与会话时区无关）。它与 WHERE 里的 tz-aware UTC 边界是**同一个基准**，
        筛选与分桶同源，边界附近的行不会偏一天；
      · 这也依赖 migrations/008：老库的列还是 naive timestamp。**过渡态要求会话时区是 UTC**
        —— 这一点已由 init_db 的 `connect_args={"options": "-c timezone=UTC"}` 在应用侧钉死，
        不再依赖"库 / 角色恰好是 UTC"；008 执行后则连推理都不需要，列本身就是绝对时刻。
    """
    global engine
    today_utc = datetime.now(timezone.utc).date()
    # 窗口边界：**tz-aware UTC** 的「今天-N 天零点」与「明天零点」。
    # 显式带 tzinfo=timezone.utc：users.created_at 是 timestamptz（绝对时刻），
    # 参数也必须是绝对时刻 —— 比较的是两个瞬间，与应用机时区、会话 TimeZone 无关。
    # 不要写 datetime(...)（naive）：naive 参数和 timestamptz 列比较时会按会话时区
    # 解释，窗口就随环境漂移了（本库会话 TimeZone=UTC 只是恰好对）。
    start_dt = datetime(
        today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc
    ) - timedelta(days=ADMIN_CHART_DAYS - 1)
    end_dt = datetime(
        today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    # 活跃图用字符串边界：ISO 'YYYY-MM-DD' 字典序 == 日期序，避免对 VARCHAR 列做 cast
    start_text = start_dt.strftime("%Y-%m-%d")
    end_text = end_dt.strftime("%Y-%m-%d")

    with engine.connect() as conn:
        # 注册数：按 **UTC 自然日**分桶。
        # · WHERE 用 tz-aware UTC 常量边界（函数不包列）——users.created_at 目前**没有索引**
        #   （只有 pkey/username unique/idx_users_last_login），现在就是顺序扫描；
        #   这样写只是为将来建普通 B-tree 时不至于用不上；
        # · 分桶先 `AT TIME ZONE 'UTC'` 折回 UTC 墙钟、再 `::date` 截断，与会话时区无关，
        #   与 WHERE 是同一个基准（这是本轮的核心口径，别改回 created_at::date）；
        # · GROUP BY 1 / ORDER BY 1 直接引用 SELECT 的第一个表达式，避免重复写转日期表达式。
        reg_sql = text("""
            SELECT (created_at AT TIME ZONE 'UTC')::date AS date, COUNT(*) AS count
            FROM users
            WHERE created_at >= :start AND created_at < :end
            GROUP BY 1
            ORDER BY 1
        """)
        reg_df = pd.read_sql(reg_sql, conn, params={"start": start_dt, "end": end_dt})

        # 活跃数：bills.date 是 VARCHAR，**只用字符串比较**（不 cast、不 DATE()）。
        # date 列原样输出（'YYYY-MM-DD' 字符串），调用方只拿它当 x 轴标签。
        active_sql = text("""
            SELECT date AS date, COUNT(DISTINCT user_id) AS count
            FROM bills
            WHERE date >= :start_text AND date < :end_text
            GROUP BY date
            ORDER BY date
        """)
        active_df = pd.read_sql(
            active_sql, conn, params={"start_text": start_text, "end_text": end_text}
        )

        sponsor_sql = text("SELECT COUNT(DISTINCT id) FROM users WHERE credits > 0")
        sponsor_count = conn.execute(sponsor_sql).scalar() or 0

    return _fill_utc_days(reg_df), _fill_utc_days(active_df), int(sponsor_count)


def get_login_daily_stats(days=ADMIN_CHART_DAYS):
    """最近 N 个 UTC 自然日的**每日登录用户数**（distinct user_id），来自 login_events。

    与「近 7 天登录用户」的口径区别：后者是滚动 168 小时的去重总数（回答"有多少人回来过"），
    这里按 UTC 自然日分桶画趋势（回答"每天有多少人登录"）。
    去重键用 COALESCE(user_id, username)：009 的外键是 ON DELETE SET NULL，
    删号后历史事件仍在（不会让过去的趋势柱凭空回缩）。
    009 未执行（表不存在）时返回带列名的空 DataFrame，页面不会红屏。
    """
    global engine
    today_utc = datetime.now(timezone.utc).date()
    start_dt = datetime(
        today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc
    ) - timedelta(days=days - 1)
    end_dt = datetime(
        today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    try:
        with engine.connect() as conn:
            # 先探测表是否存在：pd.read_sql 会把 DBAPI 异常包成 pandas.errors.DatabaseError，
            # 不是 SQLAlchemy 的 ProgrammingError —— 直接查会漏掉 except ProgrammingError，
            # 让「009 没执行」变成整块面板降级。这里先探测，缺表就干净地返回空 DataFrame。
            if conn.execute(text("SELECT to_regclass('public.login_events')")).scalar() is None:
                _warn_login_table_once(
                    "[db] 登录趋势不可用：migrations/009_login_events.sql 尚未执行"
                    "（login_events 表不存在）。管理面板按空数据显示。"
                )
                return _fill_utc_days(pd.DataFrame(), days)
            df = pd.read_sql(
                text("""
                    SELECT (occurred_at AT TIME ZONE 'UTC')::date AS date,
                           COUNT(DISTINCT COALESCE(user_id::text, 'name:' || coalesce(username, ''))) AS count
                    FROM login_events
                    WHERE event = :ev_success
                      AND occurred_at >= :start
                      AND occurred_at < :end
                    GROUP BY 1
                    ORDER BY 1
                """),
                conn,
                params=_login_event_params(start=start_dt, end=end_dt),
            )
    except ProgrammingError as exc:
        if _is_undefined_table(exc):
            _warn_login_table_once(
                "[db] 登录趋势不可用：migrations/009_login_events.sql 尚未执行"
                "（login_events 表不存在）。管理面板按空数据显示。",
                exc,
            )
            return _fill_utc_days(pd.DataFrame(), days)
        logging.getLogger(__name__).error("[db] 登录趋势查询失败（非缺表）", exc_info=True)
        return _fill_utc_days(pd.DataFrame(), days)
    except (SQLAlchemyError, OSError, ValueError) as exc:
        # pd.read_sql 会把 DBAPI 异常包成 pandas.errors.DatabaseError（不是 SQLAlchemyError），
        # 它在 pandas 里继承自 ValueError，所以这里一并覆盖；用 error 级别，便于和"真的没数据"区分。
        logging.getLogger(__name__).error("[db] 登录趋势查询异常，按空数据处理", exc_info=True)
        return _fill_utc_days(pd.DataFrame(), days)
    return _fill_utc_days(df, days)


def get_admin_funnel_data():
    global engine
    with engine.connect() as conn:
        total_reg_sql = text("SELECT COUNT(id) FROM users")
        total_reg = conn.execute(total_reg_sql).scalar() or 0

        bill_user_sql = text("SELECT COUNT(DISTINCT user_id) FROM bills")
        bill_user_count = conn.execute(bill_user_sql).scalar() or 0

        # 时间窗口整段下沉到数据库，不用 Python 算日期、也不再传 :start 参数。
        #   · 窗口 = 精确的过去 7×24 小时（滚动窗口），不是“7 个自然日”；
        #   · now() 返回 timestamptz，last_login 也是 TIMESTAMPTZ，比较是两个**绝对时刻**
        #     的先后 —— 用数据库时钟，与应用机时区（本机 UTC+8）和库的会话 TimeZone
        #     （UTC）都无关，应用机时区漂移不会把窗口算歪。
        #   · 不要写 DATE(last_login)：把函数包在列上会让 idx_users_last_login 失效
        #     （B-tree 用不上 DATE(col) 这种表达式，除非另建表达式索引），而这条语句
        #     是后台面板每次刷新都要跑的；而且 DATE() 的结果依赖会话 TimeZone，
        #     同一行在不同时区下会落进“不同的那一天”。
        #   · now() 是 STABLE 函数，规划器会把它当常量求值，右侧不影响索引使用。
        #   · NULL（老用户 / 迁移后还没登录过）不满足 >=，天然被排除，正是我们要的口径。
        login_7d_sql = text(
            "SELECT COUNT(DISTINCT id) FROM users WHERE last_login >= now() - interval '7 days'"
        )
        login_7d_user = conn.execute(login_7d_sql).scalar() or 0

        ai_user_sql = text("SELECT COUNT(DISTINCT user_id) FROM chat_history")
        ai_user_count = conn.execute(ai_user_sql).scalar() or 0
    return total_reg, bill_user_count, login_7d_user, ai_user_count


def register_user(username, password):
    """注册用户。password 是**明文**，内部哈希后入库。

    错误契约（app.py 依赖它）：
    - 用户名已存在 → ValueError("该用户名已被占用，请换一个或直接登录")。
      **必须是 ValueError + 面向用户的中文文案**：app.py 的注册分支会
      `except ValueError as e: st.error(str(e))` 把文案直接展示给用户；
      其它异常则只进服务端日志、页面上给“系统繁忙”。
    - 密码不合法（空/纯空白/少于 8 位/超过 72 字节）→ ValueError **向上抛**，
      同样由 app.py 的 `except ValueError` 展示成一句可读的中文提示。
    - 其它 IntegrityError（外键、权限、连接等）→ 原样上抛，由 app.py 的
      `except Exception` 兜成通用提示 + 服务端日志。绝不吞成 None。
    """
    global engine
    from sqlalchemy.exc import IntegrityError
    ph = hash_password(password)  # 先校验再连库：不合法的密码连事务都不用开
    with engine.connect() as conn:
        try:
            # 注册即视为一次登录，last_login = now()（数据库时钟，timestamptz）。
            # 不要改成 datetime.now()：naive 值会按会话时区被解释，应用机 UTC+8 / 库 UTC 直接差 8 小时。
            stmt = text("INSERT INTO users (username, password_hash, last_login) VALUES (:username, :password_hash, now()) RETURNING id;")
            result = conn.execute(stmt, {"username": username, "password_hash": ph})
            row = result.fetchone()
            conn.commit()
            new_id = row[0] if row else None
        except IntegrityError as exc:
            conn.rollback()
            if _is_duplicate_username_error(exc):
                raise ValueError(DUPLICATE_USERNAME_MESSAGE) from exc
            raise
    # 注册成功 = 一次登录（last_login 已在 INSERT 里写了 now()），登录事件也补一条，
    # 否则「每日登录用户」曲线会把新注册用户漏掉。埋点失败不影响注册。
    if new_id:
        record_login_event(username, new_id, LOGIN_EVENT_SUCCESS)
    return new_id


# ---------------------------------------------------------------------------
# 心愿存钱罐（migrations/011_savings_goals.sql）
# ---------------------------------------------------------------------------
# 一张表、一个人一个目标：savings_goals 上有 UNIQUE (user_id)，
# set_goal() 靠它做 INSERT ... ON CONFLICT (user_id) DO UPDATE 的 upsert
# （有则改目标、无则新建），并**刻意不写 created_at** —— 创建时间不该因为
# 用户改了一次目标金额就被重置（"这个心愿我坚持了多久"是产品要展示的信息）。
#
# 金额口径：数据库列是 NUMERIC(12,2)，这里与 bills.amount 用**同一套**定量规则
# （Decimal + ROUND_HALF_UP 到两位小数、上界 MAX_ABS_AMOUNT）。理由与
# _normalize_amount 的长注释相同：Python 的 round() 是银行家舍入，
# 会和 PG 的 numeric 舍入差 1 分钱；不定量则可能 numeric field overflow。
# 读取侧仍然统一转 float：app.py 的进度条/图表全程按 float 用
# （理由与 _normalize_amount_column 相同 —— Decimal 除以 float 会 TypeError、
# 也无法被 JSON 序列化）。
#
# ⚠ 表不存在（011 还没执行）：42P01 一律转成 RuntimeError + 中文提示，
#   **绝不**静默返回 None / 假数据 —— 那会让页面显示"还没有心愿目标"，
#   用户以为自己没设过，实际是功能没启用、数据可能就在库里。
#   app.py 捕获 RuntimeError 后显示提示，不会红屏。
# ---------------------------------------------------------------------------

GOAL_NAME_MAX_LEN = 50            # 与 savings_goals_name_len CHECK 同口径（btrim 后 1~50 字符）
GOAL_TABLE_MISSING_MESSAGE = "心愿存钱罐功能未启用：请先执行 migrations/011_savings_goals.sql"


def _raise_goal_table_missing(exc):
    """把 42P01 统一翻译成 RuntimeError（带中文可操作提示），保留原始异常链。"""
    raise RuntimeError(GOAL_TABLE_MISSING_MESSAGE) from exc


def _normalize_goal_name(goal_name) -> str:
    """心愿名称：strip 后必须是 1~50 个字符。

    与 011 的 CHECK 同口径（char_length(btrim(goal_name)) BETWEEN 1 AND 50）：
    数据库里存的是 strip 后的值，所以这里也先 strip 再判长度 ——
    否则"名字是 50 个空格 + 1 个字"会先通过 Python 的长度检查、再被 CHECK 拒绝，
    错误就会以 psycopg2 栈的形式漏到页面上。
    """
    if not isinstance(goal_name, str):
        raise ValueError("心愿名称不能为空")
    text = goal_name.strip()
    if not text:
        raise ValueError("心愿名称不能为空")
    if len(text) > GOAL_NAME_MAX_LEN:
        raise ValueError(f"心愿名称最多 {GOAL_NAME_MAX_LEN} 个字符")
    return text


def _normalize_goal_amount(value, field: str) -> float:
    """金额校验 + 两位小数定量，**任何非法输入都抛 ValueError（中文文案）**。

    field 用于拼报错文案（"目标金额" / "存入金额"），两个调用点的规则完全一样：
    - 不是数字 / nan / inf → ValueError；
    - 定量到两位小数后必须 > 0（0.001 会被定量成 0.00，因此同样被拒）；
    - 不得超过 NUMERIC(12,2) 的上界 MAX_ABS_AMOUNT（与 migrations/003 同口径）。
    顺序是"先定量、再判正、最后判上界"：9999999999.995 会定量成 10000000000.00，
    必须在定量之后才拦得住。
    """
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}非法：{value!r}（不是数字）") from exc
    if not math.isfinite(num):
        raise ValueError(f"{field}非法：{value!r}（必须是有限数字，不允许 nan/inf）")
    try:
        num = float(Decimal(str(num)).quantize(_CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}超出上限 {MAX_ABS_AMOUNT}") from exc
    if num <= 0:
        raise ValueError(f"{field}必须大于 0")
    if num > MAX_ABS_AMOUNT:
        raise ValueError(f"{field}超出上限 {MAX_ABS_AMOUNT}")
    return num


def _normalize_goal_date(target_date):
    """目标日期：None / "YYYY-MM-DD" / datetime.date → datetime.date 或 None。

    口径与 bills.date 的 _normalize_bill_date 一致（严格 ISO、校验真实日历日），
    区别只是这里的列是**真正的 DATE**、且允许"没定 deadline"（None）：
    - None（以及空串/纯空白 —— 页面上的空输入框）→ None；
    - 字符串必须整串匹配 ^\\d{4}-\\d{2}-\\d{2}$ 且是真实日历日，否则 ValueError；
    - datetime.date 原样返回；datetime.datetime 是 date 的子类，先拦下来取 .date()，
      否则 psycopg2 会把"带时分秒的值"写进 DATE 列（PG 会静默截断，口径就模糊了）。
    非 ISO 写法（"2026/10/1"、"2026-1-5"、"2026-02-30"）一律拒绝，**不做自动转换**：
    顺手转一下会让写入侧长期存在两种格式，而"能不能转"只在写入那一刻成立。
    """
    if target_date is None:
        return None
    if isinstance(target_date, datetime):
        return target_date.date()
    if isinstance(target_date, date):
        return target_date
    if isinstance(target_date, str):
        text = target_date.strip()
        if not text:
            return None  # 空输入框 = 没定日期，与传 None 等价
        if not _ISO_DATE_PATTERN.fullmatch(text):
            raise ValueError(f"目标日期格式必须是 YYYY-MM-DD：{target_date!r}")
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            # 格式对但日子不存在（2026-02-30 / 2026-13-01 / 2025-02-29）
            raise ValueError(f"目标日期格式必须是 YYYY-MM-DD：{target_date!r}") from exc
        return parsed.date()
    raise ValueError(f"目标日期格式必须是 YYYY-MM-DD：{target_date!r}")


def _goal_row_to_dict(row) -> dict:
    """把 savings_goals 的一行转成给页面用的 dict。

    - 金额转 float（与项目其余读取一致，见 _normalize_amount_column 的注释）；
    - target_date 输出 "YYYY-MM-DD" 字符串（页面/JSON 直接可用），没有则 None；
    - created_at 保持数据库返回的 tz-aware datetime（timestamptz 原样，不擅自格式化）；
    - progress = min(1, current/target)，**封顶 1**：超目标的进度条不该画到 150%；
      读到的目标金额理论上恒 > 0（CHECK 保证），这里仍兜一个 target <= 0 的分支，
      避免脏数据/绕过 CHECK 的极端情况下 ZeroDivisionError 把页面弄崩；
    - remaining = max(0, target - current)：存满之后剩余应显示 0，而不是负数；
    - reached = current >= target：已完成（用于页面换成"已达成"样式）。
    """
    target = float(row.target_amount)
    current = float(row.current_amount)
    progress = min(1.0, current / target) if target > 0 else 1.0
    deadline = row.target_date
    return {
        "id": int(row.id),
        "user_id": int(row.user_id),
        "goal_name": row.goal_name,
        "target_amount": target,
        "current_amount": current,
        "target_date": deadline.strftime("%Y-%m-%d") if deadline is not None else None,
        "created_at": row.created_at,
        "progress": progress,
        "remaining": max(0.0, target - current),
        "reached": current >= target,
    }


def set_goal(user_id, goal_name, target_amount, target_date=None) -> dict:
    """新建 / 更新心愿目标（一人一个），返回 get_goal() 的同一结构。

    - 入参校验全部在**开连接之前**完成：非法入参连库都不碰；
    - 幂等 upsert：`INSERT ... ON CONFLICT (user_id) DO UPDATE`，
      已存在时只更新 goal_name / target_amount / target_date，
      **不更新 created_at、也不重置 current_amount** ——
      用户改目标金额不应该把已经攒下的钱清零；
    - 返回值统一走 get_goal()，避免"写入口"和"读入口"各算一套字段（口径漂移）；
    - 表不存在（011 未执行）→ RuntimeError（中文提示），不静默返回假数据。
    """
    global engine
    goal = _normalize_goal_name(goal_name)
    target = _normalize_goal_amount(target_amount, "目标金额")
    deadline = _normalize_goal_date(target_date)
    with engine.connect() as conn:
        try:
            # RETURNING * 只为把这条语句收尾成"确认写成功"（结果本身不直接用；
            # 下面的返回值统一由 get_goal() 生成，保证读写口径完全一致）。
            # 参数全部走绑定：金额/日期都不是 f-string 拼进 SQL 的。
            conn.execute(
                text("""
                    INSERT INTO savings_goals (user_id, goal_name, target_amount, target_date)
                    VALUES (:uid, :name, :target, :deadline)
                    ON CONFLICT (user_id) DO UPDATE
                       SET goal_name     = EXCLUDED.goal_name,
                           target_amount = EXCLUDED.target_amount,
                           target_date   = EXCLUDED.target_date
                    RETURNING *
                """),
                {"uid": user_id, "name": goal, "target": target, "deadline": deadline},
            ).fetchone()
            conn.commit()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_undefined_table(exc):
                _raise_goal_table_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise
    result = get_goal(user_id)
    if result is None:
        # 写成功了却读不到：极少见（并发删号 / 连接串指向别的库）。不返回 None，
        # 否则调用方会拿到一个与签名不符的返回值，页面随后就 AttributeError。
        raise RuntimeError("心愿目标保存后未能读回，请重试")
    return result


def get_goal(user_id) -> dict | None:
    """读取某个用户的心愿目标；没有则返回 **None**（这是"还没有目标"，不是错误）。

    只在 user_id 上取单条（UNIQUE 保证最多一条）。表不存在（011 未执行）时抛
    RuntimeError 而不是 None —— None 会被页面理解成"用户还没设置过"，
    但真实原因是功能没启用；这两种状态给用户的下一步动作完全不同。
    """
    global engine
    with engine.connect() as conn:
        try:
            row = conn.execute(
                text("""
                    SELECT id, user_id, goal_name, target_amount, current_amount,
                           target_date, created_at
                    FROM savings_goals
                    WHERE user_id = :uid
                """),
                {"uid": user_id},
            ).fetchone()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_undefined_table(exc):
                _raise_goal_table_missing(exc)
            raise
    if row is None:
        return None
    return _goal_row_to_dict(row)


def add_to_goal(user_id, amount) -> dict:
    """往心愿存钱罐里存一笔，返回 {"deposited": 实际增加额, **get_goal(...)}。

    封顶语义：`LEAST(target_amount, current_amount + :amt)` ——
    存满之后继续存**不会**让 current_amount 超过目标（已达成的心愿不该显示 150%），
    所以"实际增加额"可能小于传入的 amount（已存满时是 0）。

    一个 LEAST 本身挡不住的边界：set_goal 允许把目标金额下调到**低于已存金额**
    （已存 5000、目标改成 1000）。此时 current > target，再跑
    LEAST(target, current + amt) 会把已存金额**往下调**（5000 → 1000）——
    用户一存钱反而少了 4000 的记录。所以先判"是否已存满"：
    已满就不执行 UPDATE、deposited 记 0。这与 current == target 时 LEAST 的结果完全一致
    （LEAST(target, target + amt) 就是 target，本来就是无写入），只是额外挡住了
    current > target 时的倒退。

    为什么要先 SELECT ... FOR UPDATE 再 UPDATE：
      只发一条 UPDATE 拿不到"存之前是多少"，算不出 deposited；而先读后写之间若没有行锁，
      两个并发请求会各自按旧值算 deposited（都显示 +500，实际只加了 500）。
      行锁一直持到 commit，读到的旧值和随后写入的新值一定是同一个事务视图。
      另外"没有目标"就在这里用同一个查询判掉（ValueError，中文提示），
      不靠 UPDATE 的 rowcount —— rowcount=0 既可能是"没目标"也可能是"目标被并发删了"。

    金额校验在开连接之前：amount 必须 > 0 且有限（"存 0 元"没有意义，
    "存负数"会被误当成取钱，而本功能不支持取钱），走 btrim/定量同一套规则。
    """
    global engine
    amt = _normalize_goal_amount(amount, "存入金额")
    with engine.connect() as conn:
        try:
            # FOR UPDATE：锁住这一行，直到 commit 才释放；没有目标时返回 None。
            row = conn.execute(
                text("""
                    SELECT current_amount, target_amount
                    FROM savings_goals
                    WHERE user_id = :uid
                    FOR UPDATE
                """),
                {"uid": user_id},
            ).fetchone()
            if row is None:
                raise ValueError("还没有心愿目标，请先创建一个")
            old_current = row.current_amount  # Decimal（NUMERIC 列），保留精度用于算 deposited
            if old_current >= row.target_amount:
                # 已存满（含"目标被下调到低于已存金额"）：不写库。
                # rollback 只用于释放 FOR UPDATE 行锁；这里没有任何写入。
                conn.rollback()
                new_current = old_current
                already_full = True
            else:
                new_current = conn.execute(
                    text("""
                        UPDATE savings_goals
                           SET current_amount = LEAST(target_amount, current_amount + :amt)
                         WHERE user_id = :uid
                        RETURNING current_amount
                    """),
                    {"uid": user_id, "amt": amt},
                ).scalar()
                conn.commit()
                already_full = False
        except ProgrammingError as exc:
            conn.rollback()
            if _is_undefined_table(exc):
                _raise_goal_table_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise
    # deposited 用 Decimal 相减后再定量：float 相减可能出现 499.99999999999994，
    # 而"这次实际存进去多少钱"要能原样显示给用户（两位小数）。
    if already_full:
        deposited = 0.0
    else:
        deposited = float(
            (Decimal(str(new_current)) - Decimal(str(old_current))).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
        )
    goal = get_goal(user_id)
    if goal is None:
        # 提交之后、读取之前目标被删掉（并发删号）。宁可报错，也不返回缺字段的 dict。
        raise ValueError("心愿目标不存在（可能已被删除），请刷新后重试")
    return {"deposited": deposited, **goal}


# ---------------------------------------------------------------------------
# 账单分类（bills.category，迁移 012）
# ---------------------------------------------------------------------------
# 设计口径：
#   · 分类是**尽力而为**的附加信息，不是记账的前置条件。规则未命中、没配 key、
#     AI 超时/报错/返回垃圾 —— 一律回落 "其他"，add_bill 照常成功。
#     绝不因为分类失败让用户丢一笔账（这是本功能的硬约束）。
#   · 规则优先、AI 兜底：常见商户（食堂、蜜雪、淘宝、水电费…）用关键词表就能覆盖，
#     零延迟、零成本、结果稳定可复现；只有规则完全没命中的长尾才值得花一次 API。
#   · 写入侧兼容「012 未执行」：_bills_category_supported() 探测列是否存在，
#     不存在时 INSERT 自动去掉 category，只告警一次。
#   · 读取侧不凭空造列：列存在才把 NULL/空串归一成 "其他"；列不存在原样返回，
#     由前端自己判断要不要展示分类。
# ---------------------------------------------------------------------------

# 分类枚举：与 migrations/012_bills_category.sql 的 VARCHAR(50) 以及前端下拉同口径。
# 顺序也是前端展示顺序（"其他"永远垫底）。
BILL_CATEGORIES = ["餐饮", "奶茶咖啡", "购物", "生活费", "交通", "娱乐", "学习", "医疗", "其他"]

# 兜底分类：规则/AI 都给不出结果时用它；也是 DB 列的 DEFAULT，两处必须一致。
FALLBACK_CATEGORY = "其他"

# 关键词规则：(关键词列表, 分类)，**顺序即优先级**。
# 为什么奶茶咖啡排在餐饮前面：蜜雪/瑞幸这类商户名里常带"咖啡/茶"，但它们是独立品类，
# 若先匹配餐饮的宽泛词就容易串味；先命中先返回，所以顺序本身就是业务口径。
CATEGORY_RULES = [
    (["蜜雪", "瑞幸", "星巴克", "库迪", "奶茶", "咖啡", "茶百道", "喜茶", "霸王茶姬"], "奶茶咖啡"),
    (["食堂", "外卖", "美团", "饿了么", "餐厅", "饭店", "快餐", "麦当劳", "肯德基", "面馆", "米饭"], "餐饮"),
    (["京东", "淘宝", "天猫", "拼多多", "唯品会", "超市", "便利店", "商场"], "购物"),
    (["水电", "电费", "水费", "网费", "话费", "房租", "物业", "住宿"], "生活费"),
    (["地铁", "公交", "打车", "滴滴", "高铁", "火车", "机票", "共享单车"], "交通"),
    (["电影", "游戏", "会员", "KTV", "演出", "门票"], "娱乐"),
    (["书", "教材", "文具", "打印", "考试", "课程"], "学习"),
    (["药", "医院", "挂号", "体检", "诊所"], "医疗"),
]

# ---------------------------------------------------------------------------
# AI 分类结果的进程内缓存
# ---------------------------------------------------------------------------
# 为什么要缓存：规则未命中的长尾商户（"楼下小卖部"之类）每次记账都会打一次
# DeepSeek；同一商户反复出现时，网络延迟和 API 成本都白花，而且一次超时就会让
# 同一笔商户的分类结果在两次记账之间摇摆。缓存只存**成功且合法**的分类：
#   · 失败 / 超时 / 回落 "其他" 一律不缓存 —— 否则一次网络抖动就会把某个商户
#     永久钉死成"其他"，之后即使 AI 恢复了也再也不会重试；
#   · 缓存是**进程级**的（普通 dict，不落库、不跨进程）：重启/热重载即失效，
#     多个 worker 各有一份；这是刻意的取舍 —— 分类只是可选增强，不值得为它
#     引入 Redis/数据库表这种新的故障点和运维成本。
# key 用「商户|描述」的小写折叠形式：strip + casefold 让 " 瑞幸咖啡 " 与
# "瑞幸咖啡"、"Starbucks" 与 "starbucks" 共用一条缓存；描述参与 key 是因为
# 同一商户在不同描述下可能被分到不同类。
_AI_CATEGORY_CACHE: dict[str, str] = {}

# 容量上限：512 条 ×（key 几十字符 + value 几个字符）≈ 几十 KB，对学生账单的
# 商户规模足够；超过上限时丢**最早插入**的一条（dict 自 3.7 起保插入序），
# 不做真正的 LRU（命中不刷新顺序）。取舍：真正的 LRU 要 OrderedDict.move_to_end
# 或额外时间戳，收益只是让热点商户晚一点被淘汰；这里选"实现最简单、内存上限硬、
# 行为可预测"，清空整表则会让下一波请求同时打 AI（惊群），所以只淘汰一条。
_AI_CATEGORY_CACHE_MAX = 512

# 命中/未命中计数：不参与业务逻辑，只为观测缓存是否真的生效（测试也靠它断言）。
# 注意：读取/累加这两个计数本身绝不能影响分类，所以下面用 try/except 兜住。
_AI_CATEGORY_CACHE_HITS = 0
_AI_CATEGORY_CACHE_MISSES = 0

# bills.category 是否存在的进程级缓存：None = 还没探测过；True/False = 探测结果。
# 只缓存**探测成功**的结果：探测本身失败（库暂时不可用）时不缓存，
# 否则一次网络抖动就会让整个进程永久降级、执行 012 后也必须重启才能恢复。
_BILLS_CATEGORY_SUPPORT = None

# DEEPSEEK_API_KEY 的懒加载缓存：{"loaded": bool, "value": str|None}。
# 不在模块顶层读 secrets：db.py 会被脚本/测试直接 import，没装 streamlit 或没有
# secrets 文件时也不该在 import 阶段就失败。读取失败缓存成 None（本进程不再重试）。
_DEEPSEEK_KEY_CACHE = {"loaded": False, "value": None}


def _deepseek_key():
    """懒加载并缓存 DEEPSEEK_API_KEY；没配置 / 读取失败都返回 None。

    分类是可选增强，读不到 key 只代表"不用 AI 兜底"，不是错误。
    """
    cache = _DEEPSEEK_KEY_CACHE
    if not cache["loaded"]:
        cache["loaded"] = True
        try:
            from config import get_secret
            raw = get_secret("DEEPSEEK_API_KEY")
        except Exception:
            logging.getLogger(__name__).warning(
                "[db] 读取 DEEPSEEK_API_KEY 失败，AI 分类不可用（记账不受影响）", exc_info=True
            )
            raw = None
        cache["value"] = raw.strip() if isinstance(raw, str) and raw.strip() else None
    return cache["value"]


def _bills_category_supported():
    """探测 bills.category 是否存在（迁移 012 是否已执行），结果进程内缓存。

    False 时调用方必须走"不带 category 的 INSERT"：012 未执行时硬塞 category
    会让整笔账单写入失败 —— 分类是可选信息，绝不能拖垮记账主路径。
    这里只告警一次（缓存 False 之后的调用直接返回，不再刷日志）。
    """
    global _BILLS_CATEGORY_SUPPORT
    if _BILLS_CATEGORY_SUPPORT is not None:
        return _BILLS_CATEGORY_SUPPORT
    if engine is None:
        # 正常入口 app.py 会先 init_db()；这里兜底，避免脚本里直接调 add_bill 时
        # 把"引擎没建"误判成"列不存在"并永久缓存。
        try:
            init_db()
        except Exception:
            logging.getLogger(__name__).warning(
                "[db] 探测 bills.category 前初始化数据库失败，本次按「无 category 列」处理", exc_info=True
            )
            return False
    try:
        with engine.connect() as conn:
            exists = bool(conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'bills'
                      AND column_name = 'category'
                )
            """)).scalar())
    except Exception:
        # 探测失败不等于列不存在：不写缓存，下次再探（写库本身也会失败并照常抛出）。
        logging.getLogger(__name__).warning(
            "[db] 探测 bills.category 失败，本次按「无 category 列」写入（不带分类）", exc_info=True
        )
        return False
    _BILLS_CATEGORY_SUPPORT = exists
    if not exists:
        logging.getLogger(__name__).warning(
            "[db] bills.category 不存在（migrations/012_bills_category.sql 未执行）："
            "账单将不带分类写入，请执行 012 迁移后重启应用"
        )
    return exists


def _normalize_category_value(value):
    """把单个分类值收敛成非空字符串：None / NaN / 空串 / 纯空白 → "其他"。

    不校验是否在 BILL_CATEGORIES 内：读取侧只负责让前端拿到的分类列没有空值，
    未知值原样保留（可能是执行 012 之前人工写入的标签，悄悄改写会掩盖数据问题）。
    """
    if value is None:
        return FALLBACK_CATEGORY
    try:
        if pd.isna(value):
            return FALLBACK_CATEGORY
    except (TypeError, ValueError):
        # pd.isna 对 list / dict 之类返回数组，真值判断会抛错；这类值交给 str()。
        pass
    text = str(value).strip()
    return text or FALLBACK_CATEGORY


def _normalize_category_column(df):
    """读取兜底：category 列的 NULL/空串归一成 "其他"；**没有该列时不造列**。

    与 _normalize_type_column 同样"绝不抛异常"：归一化失败就返回原始数据，
    页面最多显示空分类，不会因为兜底逻辑崩掉。
    """
    if not isinstance(df, pd.DataFrame) or df.empty or "category" not in df.columns:
        return df
    try:
        df = df.copy()
        df["category"] = df["category"].map(_normalize_category_value)
    except Exception as exc:  # 兜底逻辑本身不能把页面搞崩，异常时退回原始数据
        print(f"[db] category 列归一化失败，返回原始数据：{exc}")
    return df


def _ai_category_cache_key(merchant_text, description_text):
    """拼缓存 key：商户 + 描述，各自 strip + casefold，用 '|' 分隔。

    两个字段都用 '|' 之外的字符做分隔符本身不保证无碰撞（商户里真带 '|' 时
    "a|b"+"c" 与 "a"+"b|c" 会撞）；但碰撞的代价只是多一次缓存命中（分类可能
    不完美），而分类本来就是尽力而为，不值得为它引入转义/元组 key 的复杂度。
    """
    return f"{merchant_text.strip().casefold()}|{description_text.strip().casefold()}"


def _remember_ai_category(cache_key, category):
    """把一次成功的 AI 分类写进进程内缓存；**缓存自身出任何问题都只降级**。

    · 只接受已在 BILL_CATEGORIES 内的分类（调用方已校验，这里再兜一层）；
    · 容量上限 _AI_CATEGORY_CACHE_MAX：满了丢最早插入的一条（dict 保序）；
    · 不记 key/描述到日志（可能含用户隐私），只记异常类型。
    """
    if category not in BILL_CATEGORIES:
        return
    try:
        max_size = int(_AI_CATEGORY_CACHE_MAX)
        if max_size <= 0:
            return
        while len(_AI_CATEGORY_CACHE) >= max_size:
            _AI_CATEGORY_CACHE.pop(next(iter(_AI_CATEGORY_CACHE)))
        _AI_CATEGORY_CACHE[cache_key] = category
    except Exception:
        logging.getLogger(__name__).warning(
            "[db] 写入 AI 分类缓存失败（忽略，不影响记账）", exc_info=True
        )


def clear_ai_category_cache():
    """清空 AI 分类缓存，并把命中/未命中计数归零（测试 / 运维用）。

    缓存是**进程级**的：只存在于当前 Python 进程的内存字典里，重启、热重载、
    切换 worker 都会失效，也**不会**写进数据库。多进程部署时各进程互不共享，
    清理只影响当前进程 —— 这是"分类只是可选增强"的刻意取舍，不引入外部缓存。
    """
    global _AI_CATEGORY_CACHE_HITS, _AI_CATEGORY_CACHE_MISSES
    _AI_CATEGORY_CACHE.clear()
    _AI_CATEGORY_CACHE_HITS = 0
    _AI_CATEGORY_CACHE_MISSES = 0


def auto_categorize(merchant, description="", api_key=None) -> str:
    """给一笔账单推断分类，返回 BILL_CATEGORIES 中的一个（失败一律 "其他"）。

    优先级：
      ① 关键词规则（CATEGORY_RULES 顺序，命中即返回）：零延迟、零成本、可复现；
      ② 规则未命中且 api_key 非空 → 先查进程内缓存（命中直接返回，不打网络）；
      ③ 缓存未命中才懒加载 ai_agent.classify_category 问一次 AI，成功结果写回缓存；
      ④ 其余所有情况（无 key / 空输入 / AI 异常 / 返回值不在枚举内）→ "其他"。

    缓存只存**成功且合法**的分类：失败/回落「其他」的结果不缓存，避免一次网络
    抖动把商户永久钉死成「其他」。缓存是进程级的（重启即失效），详见
    _AI_CATEGORY_CACHE 上方的注释与 clear_ai_category_cache()。

    为什么 from ai_agent import classify_category 放在函数内部：
    db.py 与 ai_agent.py 在 `import db` 时不能互相顶层 import（循环依赖，
    ai_agent 顶层还会立刻 create_client 初始化 Supabase）。放到函数里既避开
    循环依赖，也让"没配 AI 的部署"完全不加载 ai_agent。

    这个函数**永不抛异常**：调用方（add_bill / CSV 导入 / 示例数据）都把它当
    可选信息，分类失败只能降级，不能影响记账。
    """
    global _AI_CATEGORY_CACHE_HITS, _AI_CATEGORY_CACHE_MISSES
    merchant_text = "" if merchant is None else str(merchant).strip()
    description_text = "" if description is None else str(description).strip()
    if not merchant_text and not description_text:
        return FALLBACK_CATEGORY

    haystack = f"{merchant_text} {description_text}".casefold()
    for keywords, category in CATEGORY_RULES:
        for keyword in keywords:
            if keyword.casefold() in haystack:
                return category

    if not api_key or not str(api_key).strip():
        return FALLBACK_CATEGORY

    # 命中缓存：同一「商户|描述」不再打 AI。缓存只可能存成功分类，所以这里
    # 直接返回即可；计数只为观测，绝不影响返回值。
    cache_key = _ai_category_cache_key(merchant_text, description_text)
    try:
        cached = _AI_CATEGORY_CACHE.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        _AI_CATEGORY_CACHE_HITS += 1
        return cached
    _AI_CATEGORY_CACHE_MISSES += 1

    try:
        from ai_agent import classify_category
        category = classify_category(merchant_text, description_text, str(api_key), BILL_CATEGORIES)
    except Exception as exc:
        # 只记异常类型与商户片段：日志里绝不出现 api_key，也不把整段描述倒进日志。
        logging.getLogger(__name__).warning(
            "[db] AI 分类失败，回落「%s」（商户=%r，异常=%s）",
            FALLBACK_CATEGORY, merchant_text[:50], type(exc).__name__,
        )
        return FALLBACK_CATEGORY

    if category in BILL_CATEGORIES:
        # 只缓存成功结果；失败/「其他」不缓存，下次仍会重试 AI。
        _remember_ai_category(cache_key, category)
        return category
    logging.getLogger(__name__).warning(
        "[db] AI 返回分类 %r 不在 BILL_CATEGORIES 内，回落「%s」（商户=%r）",
        category, FALLBACK_CATEGORY, merchant_text[:50],
    )
    return FALLBACK_CATEGORY


# ---------------------------------------------------------------------------
# 消费情绪（bills.mood，迁移 019）
# ---------------------------------------------------------------------------
# 设计口径（与分类同构，但「可选」的意味更强）：
#   · 情绪是用户/AI 顺手给的附加信息，不是记账的前置条件。019 未执行、值非法、
#     列不存在 —— 一律降级成「未标注」，绝不因此让一笔账记不上。
#   · 「未标注」不是一种情绪：NULL / 空串 / 非法值都收敛成 ""，不进 moods 分组，
#     只进 total_expense 与 baseline_avg。否则它会被当成第六种情绪参与排名，
#     均值参照线也会被它污染。
#   · 写入侧兼容「019 未执行」：_bills_mood_supported() 探测列是否存在，不存在时
#     INSERT 自动去掉 mood（见 add_bill / _bill_insert_statement）。
#   · 读取侧不凭空造列：只有列存在时才把列值收敛成合法情绪或空串。
MOOD_OPTIONS = ("开心", "Emo", "焦虑", "疲惫", "激动")

MOOD_EMOJI = {"开心": "😊", "Emo": "😢", "焦虑": "😠", "疲惫": "😴", "激动": "🤩"}

# 未标注的**展示文案**：只给页面用，绝不写进库（库里如实是真 NULL）。
MOOD_UNKNOWN = "未标注"

# 统计窗口的默认值与上限：24 个月足够覆盖"年度回顾"，也避免一个误传的月份数
# 把整张 bills 扫成一次全表聚合。
MOOD_MONTHS_DEFAULT = 3
MOOD_MONTHS_MAX = 24

# high_risk 的判定阈值：同一情绪的平均单笔 >= 全窗口均值的 1.5 倍，且至少 2 笔。
# 为什么要 count>=2：一笔大额不构成"情绪模式"，否则每次都会随机标红一个情绪。
MOOD_HIGH_RISK_RATIO = 1.5
MOOD_HIGH_RISK_MIN_COUNT = 2

# bills.mood 是否存在的进程级缓存：None = 还没探测过；True/False = 探测结果。
# 与 _BILLS_CATEGORY_SUPPORT 同一套取舍：只缓存**探测成功**的结果，探测失败
# （库暂时不可用）不缓存，否则一次抖动就会让整个进程永久降级。
_BILLS_MOOD_SUPPORT = None

# get_mood_spending 的告警只打一次：页面每轮 rerun 都会调用它，逐次 print 会刷屏。
_MOOD_WARNED = False


def normalize_mood(value) -> str:
    """把任意传入值收敛成 MOOD_OPTIONS 里的原值；认不出就返回 ""（= 未标注）。

    为什么这样"外宽内严"：
      · 外宽：AI / 前端可能给 "😊开心"、"开心 "、"emo"、"EMO"、甚至把 emoji 夹在
        中间。先 strip，再去掉 emoji / 标点 / 空白等非字母数字字符，最后做**整串**
        比较，于是 "😊 开心" 与 "开心" 等价，大小写只对 Emo 不敏感。
      · 内严：中文情绪必须整串精确匹配，"不开心" / "穷开心" 不会被误判成 "开心"；
        非法值一律 ""，由调用方按「未标注」处理。
      · **绝不抛异常**：它跑在记账主路径上（mood 可能来自 AI 幻觉），任何脏值
        最多变成"没有标注"，不能让一笔账记不上。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        # NaN / NaT / pd.NA 都按"没给值"处理；容器等不可判定值退回 str() 再比对。
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        try:
            value = str(value)
        except Exception:
            return ""
    cleaned = "".join(ch for ch in value.strip() if ch.isalnum())
    if not cleaned:
        return ""
    # Emo 是唯一大小写不敏感的值（用户/AI 常写成 emo/EMO）；中文 casefold 无副作用。
    if cleaned.casefold() == "emo":
        return "Emo"
    if cleaned in MOOD_OPTIONS:
        return cleaned
    return ""


def _bills_mood_supported():
    """探测 bills.mood 是否存在（迁移 019 是否已执行），结果进程内缓存。

    False 时调用方必须走"不带 mood 的 INSERT"：019 未执行时硬塞 mood 会让
    整笔账单写入失败 —— 情绪是可选信息，绝不能拖垮记账主路径。
    这里只告警一次（缓存 False 之后的调用直接返回，不再刷日志）。
    """
    global _BILLS_MOOD_SUPPORT
    if _BILLS_MOOD_SUPPORT is not None:
        return _BILLS_MOOD_SUPPORT
    if engine is None:
        # 正常入口 app.py 会先 init_db()；这里兜底，避免脚本里直接调 add_bill 时
        # 把"引擎没建"误判成"列不存在"并永久缓存。
        try:
            init_db()
        except Exception:
            logging.getLogger(__name__).warning(
                "[db] 探测 bills.mood 前初始化数据库失败，本次按「无 mood 列」处理", exc_info=True
            )
            return False
    try:
        with engine.connect() as conn:
            exists = bool(conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'bills'
                      AND column_name = 'mood'
                )
            """)).scalar())
    except Exception:
        # 探测失败不等于列不存在：不写缓存，下次再探（写库本身也会失败并照常抛出）。
        logging.getLogger(__name__).warning(
            "[db] 探测 bills.mood 失败，本次按「无 mood 列」写入（不带情绪）", exc_info=True
        )
        return False
    _BILLS_MOOD_SUPPORT = exists
    if not exists:
        logging.getLogger(__name__).warning(
            "[db] bills.mood 不存在（migrations/019_bills_mood.sql 未执行）："
            "账单将不带情绪写入，请执行 019 迁移后重启应用"
        )
    return exists


def _normalize_mood_column(df):
    """读取兜底：mood 列收敛成 MOOD_OPTIONS 原值或 ""；**没有该列时不造列**。

    与 _normalize_category_column 同样"绝不抛异常"：归一化失败就返回原始数据，
    页面最多把某个情绪显示成未标注，不会因为兜底逻辑崩掉。
    NaN → ""、首尾空白去掉、非法值 → "" 都由 normalize_mood 统一负责。
    """
    if not isinstance(df, pd.DataFrame) or df.empty or "mood" not in df.columns:
        return df
    try:
        df = df.copy()
        df["mood"] = df["mood"].map(normalize_mood)
    except Exception as exc:  # 兜底逻辑本身不能把页面搞崩，异常时退回原始数据
        print(f"[db] mood 列归一化失败，返回原始数据：{exc}")
    return df


def _normalize_mood_months(months) -> int:
    """统计月数收敛到 1~24：非数字 / <=0 → 默认 3，>24 → 24。

    与其它模块"非法即 ValueError"不同：情绪气象站是只读展示，月份来自页面控件，
    传错/传空只该退回默认窗口，不该把整块页面打成异常（页面不能红屏）。
    """
    try:
        value = int(months)
    except (TypeError, ValueError, OverflowError):
        return MOOD_MONTHS_DEFAULT
    if value <= 0:
        return MOOD_MONTHS_DEFAULT
    return min(value, MOOD_MONTHS_MAX)


def _warn_mood_once(message: str) -> None:
    """[mood] 告警只打一次（进程内）：页面每轮 rerun 都会调统计函数，不能刷屏。"""
    global _MOOD_WARNED
    if _MOOD_WARNED:
        return
    _MOOD_WARNED = True
    print(f"[mood] {message}")


def _empty_mood_spending(window_months: int, start: str, end: str) -> dict:
    """降级时返回的完整空结构：键/类型与正常返回一致，调用方无需写分支。"""
    return {
        "months": window_months,
        "start": start,
        "end": end,
        "has_data": False,
        "total_expense": 0.0,
        "tagged_expense": 0.0,
        "tagged_count": 0,
        "baseline_avg": 0.0,
        "moods": [],
        "high_risk": None,
        "untagged": {"total": 0.0, "count": 0, "avg": 0.0},
    }


def get_mood_spending(user_id, months=3, today=None) -> dict:
    """统计某用户近 months 个月的「情绪 × 支出」，供情绪消费气象站使用。

    统计窗口（左闭右闭）
    --------------------
    · 终点 = today（默认 date.today()）；起点 = 终点所在月往前推 months-1 个月的
      1 号。例：today=2026-10-20、months=3 → 2026-08-01 ~ 2026-10-20。
    · months<=0 / 非数字 → 3；months>24 → 24（只读展示，不为控件值把页面打成异常）。
    · bills.date 是 VARCHAR 且 007 的 CHECK 保证 ISO，所以**按字符串比较**：
      ISO 字典序与日历序一致，也避免 ``::date`` 在脏数据上抛异常。
    · 只统计 ``type = '支出'``，收入不进任何数字。

    口径
    ----
    · total_expense  = 窗口内全部支出（含未标注）；
    · tagged_expense = 带合法情绪标签的支出合计；tagged_count = 对应笔数；
    · baseline_avg   = **全部**支出的单笔均值（含未标注）= total / 全部笔数；
      不是"已标注部分的均值" —— 参照线要回答"我平时一笔花多少"，未标注账单
      同样是平时的消费，剔掉会让参照线虚高；
    · 每种情绪：avg = total/count、share = total/tagged_expense、
      avg_ratio = avg/baseline_avg；任何除零一律 0.0；
    · high_risk = count>=2 且 avg >= baseline_avg*1.5 的情绪里 avg_ratio 最大的
      一个，没有则 None；同分按 MOOD_OPTIONS 顺序取靠前者，保证同一份数据
      每次输出完全一致。

    降级（本函数**绝不向上抛**）
    ----------------------------
    · 019 未执行（bills.mood 不存在）→ 空结构 + 一句告警（print 一次）；
    · 查询本身异常（连接断了、权限变了……）→ 同样空结构 + 告警。
      情绪气象站是可选功能，页面渲染不该为它红屏；返回值仍是完整结构。
    · user_id 非法仍抛 ValueError（与其它查询函数一致）：那是调用方的程序性
      错误，不是环境降级，静默返回空数据会把 bug 藏起来。
    """
    uid = _normalize_budget_user_id(user_id)
    window_months = _normalize_mood_months(months)
    end_day = _normalize_forecast_today(today)

    # 与 get_daily_expense_totals / get_latte_factor 同一套「月份序号」算法：
    # 跨年由 floor 除 / 模自然处理，不写 year == 1 分支。
    month_index = (end_day.year * 12 + end_day.month - 1) - (window_months - 1)
    start_day = date(month_index // 12, month_index % 12 + 1, 1)
    start_text = start_day.isoformat()
    end_text = end_day.isoformat()
    empty = _empty_mood_spending(window_months, start_text, end_text)

    if not _bills_mood_supported():
        _warn_mood_once(
            "bills.mood 不可用（migrations/019_bills_mood.sql 未执行或数据库不可用），"
            "情绪消费气象站按「暂无数据」展示"
        )
        return empty

    try:
        if engine is None:
            init_db()
        with engine.connect() as conn:
            # 一条 SQL 按 mood 分组：NULL / 空串 / 非法值都落进同一个"未标注"桶，
            # 由 Python 侧 normalize_mood 统一判定，SQL 里不写 IN (...) 枚举。
            rows = conn.execute(
                text("""
                    SELECT b.mood AS mood,
                           SUM(b.amount) AS total,
                           COUNT(*) AS cnt
                    FROM public.bills AS b
                    WHERE b.user_id = :uid
                      AND b.type = :expense
                      AND b.date >= :start
                      AND b.date <= :end
                    GROUP BY b.mood
                """),
                {
                    "uid": uid,
                    "expense": EXPENSE_TYPE,
                    "start": start_text,
                    "end": end_text,
                },
            ).fetchall()
    except Exception as exc:
        # 只记异常类型：SQL 文本/参数值不进日志（与 hide_parameters 的口径一致）。
        _warn_mood_once(f"查询情绪消费失败，按「暂无数据」展示：{type(exc).__name__}")
        return empty

    total_decimal = Decimal("0")
    tagged_decimal = Decimal("0")
    untagged_decimal = Decimal("0")
    total_count = 0
    tagged_count = 0
    untagged_count = 0
    per_mood: dict[str, dict] = {}
    for row in rows:
        count = int(row.cnt or 0)
        if count <= 0:
            continue
        amount = _as_decimal(row.total)
        total_decimal += amount
        total_count += count
        mood = normalize_mood(row.mood)
        if not mood:
            # NULL / 空串 / 库里的历史脏值都算未标注：只进总额与参照线，不参与排名。
            untagged_decimal += amount
            untagged_count += count
            continue
        bucket = per_mood.setdefault(mood, {"total": Decimal("0"), "count": 0})
        bucket["total"] += amount
        bucket["count"] += count
        tagged_decimal += amount
        tagged_count += count

    total_expense = _money_to_float(total_decimal)
    tagged_expense = _money_to_float(tagged_decimal)
    # 全部支出为 0 时不能做除法：参照线给 0.0，下游据此把 avg_ratio 也置 0。
    baseline_avg = (
        _money_to_float(total_decimal / Decimal(total_count)) if total_count else 0.0
    )
    untagged = {
        "total": _money_to_float(untagged_decimal),
        "count": untagged_count,
        "avg": (
            _money_to_float(untagged_decimal / Decimal(untagged_count))
            if untagged_count
            else 0.0
        ),
    }

    # 先按 MOOD_OPTIONS 顺序生成（同额时顺序稳定），再按总额降序排序。
    moods = []
    by_mood: dict[str, dict] = {}
    for mood in MOOD_OPTIONS:
        bucket = per_mood.get(mood)
        if not bucket or bucket["count"] <= 0:
            continue
        count = int(bucket["count"])
        total = _money_to_float(bucket["total"])
        avg = _money_to_float(bucket["total"] / Decimal(count))
        item = {
            "mood": mood,
            "emoji": MOOD_EMOJI.get(mood, ""),
            "total": total,
            "count": count,
            "avg": avg,
            "share": round(total / tagged_expense, 4) if tagged_expense > 0 else 0.0,
            "avg_ratio": round(avg / baseline_avg, 2) if baseline_avg > 0 else 0.0,
            "high_risk": False,
        }
        moods.append(item)
        by_mood[mood] = item
    moods.sort(key=lambda item: (-item["total"], MOOD_OPTIONS.index(item["mood"])))

    # 高危情绪：笔数够、且客单价明显高于全体均值。同分时按 MOOD_OPTIONS 顺序取靠前者。
    high_risk = None
    if baseline_avg > 0:
        threshold = baseline_avg * MOOD_HIGH_RISK_RATIO
        for mood in MOOD_OPTIONS:
            item = by_mood.get(mood)
            if item is None or item["count"] < MOOD_HIGH_RISK_MIN_COUNT:
                continue
            if item["avg"] < threshold:
                continue
            if high_risk is None or item["avg_ratio"] > high_risk["avg_ratio"]:
                high_risk = item
    if high_risk is not None:
        # 同一个 dict 对象也在 moods 里，标记一次两处都生效（页面按列表渲染）。
        high_risk["high_risk"] = True

    return {
        "months": window_months,
        "start": start_text,
        "end": end_text,
        "has_data": tagged_count > 0,
        "total_expense": total_expense,
        "tagged_expense": tagged_expense,
        "tagged_count": tagged_count,
        "baseline_avg": baseline_avg,
        "moods": moods,
        "high_risk": high_risk,
        "untagged": untagged,
    }


# ---------------------------------------------------------------------------
# 日报 / 通知（migrations/014_user_notifications.sql）
# ---------------------------------------------------------------------------
# 这组函数只服务于「把日报发出去」这件事，不参与记账/登录主流程：
#   · report_user_stats 只读 users / bills，014 没执行也能用（不会因为通知表缺失而失败）；
#   · record_notification / notification_already_sent 依赖 014 的 user_notifications，
#     表不存在时**明确抛 RuntimeError**（中文提示先执行 014），不静默返回假数据 ——
#     「没发过」和「功能没启用」是两种完全不同的状态，调用方必须能区分。
#   · 幂等口径：同一 (user_id, sent_date, channel) 只有一行；重复写走
#     ON CONFLICT DO UPDATE，不会新增行，也不会把 created_at 重置成"第一次发送"。
# ---------------------------------------------------------------------------

NOTIFICATION_STATUS_SENT = "sent"
NOTIFICATION_STATUS_FAILED = "failed"
NOTIFICATION_STATUS_SKIPPED = "skipped"
NOTIFICATION_STATUSES = frozenset(
    {NOTIFICATION_STATUS_SENT, NOTIFICATION_STATUS_FAILED, NOTIFICATION_STATUS_SKIPPED}
)

DEFAULT_NOTIFICATION_CHANNEL = "webhook"
NOTIFICATION_CHANNEL_MAX_LEN = 32  # 与 014 的 user_notifications_channel_len 同口径

NOTIFICATION_TABLE_MISSING_MESSAGE = (
    "日报通知记录功能未启用：请先执行 migrations/014_user_notifications.sql"
)

# 日报的「今天」按北京时间（UTC+8）自然日计算。
# 为什么不用 date.today()：应用机可能部署在 UTC 或其他时区，本机日期会随环境漂移；
# 为什么不用库会话的 current_date：init_db 把会话时区钉成了 UTC，而本功能面向中国用户，
# 「今天」应是用户所在地的日历日。中国自 1991 年起不实行夏令时，固定 +8 没有歧义；
# bills.date 本身是 DATE/ISO 日历日，不携带时区，所以这里只决定"拿哪一天去比"。
_REPORT_TZ = timezone(timedelta(hours=8))

# 月份严格格式：YYYY-MM，月份 01~12。用 fullmatch，'2026-1'、'2026-13'、'26-10' 一律拒绝。
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$", re.ASCII)

# month_budget_source 的固定口径标记。本函数**不读任何预算表**，见 docstring 说明。
REPORT_BUDGET_SOURCE = "budget"


def _parse_report_month(month: str) -> tuple[date, date]:
    """把 "YYYY-MM" 解析成 [当月 1 日, 下月 1 日) 的左闭右开边界。

    严格校验：只接受 4 位年份 + 2 位月份（01~12）。非法输入抛 ValueError（中文文案），
    在**开数据库连接之前**就拒绝，避免拿一个拼错的月份去扫表。
    """
    if not isinstance(month, str):
        raise ValueError(f"月份格式必须是 YYYY-MM（例如 2026-10）：{month!r}")
    text = month.strip()
    if not _MONTH_PATTERN.fullmatch(text):
        raise ValueError(f"月份格式必须是 YYYY-MM（例如 2026-10）：{month!r}")
    year, mon = int(text[:4]), int(text[5:7])
    try:
        start = date(year, mon, 1)
        end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    except ValueError as exc:
        # 理论上年份 0000 / 9999-12 才会走到这里（正则已挡住月份越界）。
        raise ValueError(f"月份超出可计算范围：{month!r}") from exc
    return start, end


def _report_today() -> date:
    """日报口径的「今天」：北京时间（UTC+8）的日历日。"""
    return datetime.now(_REPORT_TZ).date()


def _as_decimal(value) -> Decimal:
    """把 DB 返回的 numeric / None 收敛成 Decimal；金额运算一律用 Decimal，避免二进制浮点误差。"""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money_to_float(value) -> float:
    """金额转 float，并定量到两位小数（与 NUMERIC(12,2) / _CENT 同口径）。"""
    return float(_as_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _normalize_sent_date(sent_date) -> date:
    """通知归属日：datetime.date / "YYYY-MM-DD" → datetime.date；其余抛 ValueError。

    datetime.datetime 是 date 的子类，必须先拦下来取 .date()，否则会带着时分秒
    绑定到 DATE 列（PG 会静默截断，口径就模糊了）。None 不允许：sent_date 是 NOT NULL。
    """
    if isinstance(sent_date, datetime):
        return sent_date.date()
    if isinstance(sent_date, date):
        return sent_date
    if isinstance(sent_date, str):
        text = sent_date.strip()
        if _ISO_DATE_PATTERN.fullmatch(text):
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                pass
        raise ValueError(f"发送日期格式必须是 YYYY-MM-DD：{sent_date!r}")
    raise ValueError(f"发送日期格式必须是 YYYY-MM-DD：{sent_date!r}")


def _normalize_notification_channel(channel) -> str:
    """渠道：strip 后 1~32 个字符（与 014 的 CHECK 同口径），非法输入抛 ValueError。"""
    if not isinstance(channel, str):
        raise ValueError(f"通知渠道必须是字符串：{channel!r}")
    text = channel.strip()
    if not 1 <= len(text) <= NOTIFICATION_CHANNEL_MAX_LEN:
        raise ValueError(
            f"通知渠道长度必须是 1~{NOTIFICATION_CHANNEL_MAX_LEN} 个字符：{channel!r}"
        )
    return text


def _normalize_notification_status(status) -> str:
    """状态只允许 sent / failed / skipped（与 014 的 CHECK 同口径）。"""
    if not isinstance(status, str) or status.strip() not in NOTIFICATION_STATUSES:
        raise ValueError(
            f"通知状态非法：{status!r}（只允许 {' / '.join(sorted(NOTIFICATION_STATUSES))}）"
        )
    return status.strip()


def _normalize_notification_user_id(user_id) -> int:
    """user_id 必须是正整数；非法输入在现场就抛 ValueError，而不是等 FK 抛 IntegrityError。

    只接受 int / 十进制字符串：float / Decimal 即使值是 1.0 也拒绝 ——
    ``int(1.9)`` 会静默截断成 1，把"写错用户"伪装成"写对用户"，比直接报错更危险。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    try:
        uid = int(user_id.strip()) if isinstance(user_id, str) else int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}") from exc
    if uid <= 0:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    return uid


def _raise_notification_table_missing(exc):
    """把 42P01（user_notifications 不存在）翻译成带中文可操作提示的 RuntimeError。"""
    raise RuntimeError(NOTIFICATION_TABLE_MISSING_MESSAGE) from exc


def _notification_row_to_dict(row) -> dict:
    """把 user_notifications 的一行转成给调用方用的 dict。

    · sent_date 保持 datetime.date（不是字符串），与"日期用 datetime.date"的约定一致；
    · created_at 保留数据库返回的 tz-aware datetime（timestamptz 原样，不擅自格式化）；
    · id / user_id 转 int；金额字段本表没有，无需处理。
    """
    mapping = row._mapping
    return {
        "id": int(mapping["id"]),
        "user_id": int(mapping["user_id"]),
        "sent_date": mapping["sent_date"],
        "channel": mapping["channel"],
        "status": mapping["status"],
        "message": mapping["message"],
        "error": mapping["error"],
        "created_at": mapping["created_at"],
    }


def report_user_stats(month: str) -> list[dict]:
    """按「YYYY-MM」汇总每个用户的本月收支与今日支出，供日报脚本使用。

    参数
    ----
    month : str
        形如 "2026-10" 的月份；**严格校验**（必须 4 位年 + 2 位月，月份 01~12），
        否则抛 ValueError。校验发生在开连接之前。

    返回
    ----
    list[dict]，**每个用户一行**（包括本月没有任何账单的用户），按 user_id 升序：
        {
          "user_id": int,
          "username": str,
          "is_admin": bool,
          "month_expense": float,      # 本月支出
          "month_income": float,       # 本月收入
          "month_balance": float,      # = month_income - month_expense
          "today_expense": float,      # 今天（北京时间自然日）的支出
          "today_overspend": bool,
          "month_budget_source": "budget",   # 口径标记，见下
        }

    口径（务必与调用方对齐）
    ------------------------
    · 本月支出/收入：``bills.date::date`` 落在 [当月 1 日, 下月 1 日) 的行，
      且 ``bills.type`` 分别等于常量 EXPENSE_TYPE("支出") / INCOME_TYPE("收入")。
      类型没有在 SQL 里做模糊匹配：001/003/007 已把 type 归一并用 CHECK 限定为这两个值，
      这里复用常量而不是在 SQL 里写魔法字符串。
    · month_balance = month_income - month_expense（用 Decimal 相减后再转 float，
      避免 0.1 + 0.2 这类二进制浮点尾差）。
    · today_expense：只统计**今天且属于本月**的支出；如果请求的是历史月份，今天不在
      该月窗口内，today_expense = 0（函数不会把"今天"的消费算进别的月份）。
    · today_overspend：``today_expense > 0`` **且**
      ``today_expense > 本月日均支出`` 时为 True。本月日均支出 =
      month_expense / 已过天数（当月请求取 today.day；历史月份取整月天数；
      未来月份已过 0 天，此时恒为 False，不做除以 0）。
      ⚠ 这是「今天花得比**自己本月日均**多」的相对判断，**不是**「超预算」：
        本函数只读 users / bills，库里没有 budget 表，也没有用户的预算值。
        字段名沿用 today_overspend 是为了和日报模板/调用方兼容，注释以此为准。
    · month_budget_source：固定返回 "budget"，只是给调用方的口径标记（表示"预算口径"），
      **不代表**本函数真的读到了预算；实际阈值是上面的本月日均支出。若以后接入真实预算，
      应在此处返回预算来源并同步改 today_overspend 的算法。

    时区
    ----
    「今天」= 北京时间（UTC+8）自然日，由 _report_today() 计算；
    ``bills.date`` 是不带时区的日历日（TEXT/DATE），比较时统一 ``::date``。
    本函数不受数据库会话 TimeZone 影响（DATE 本身没有时区）。

    性能
    ----
    单条 SQL：bounds CTE + 按 user 聚合的 CTE + ``users LEFT JOIN``，没有任何 N+1。
    ⚠ ``bills.date`` 目前是 TEXT（007 用 CHECK 保证 ISO 格式）。``date::date`` 依赖
      「所有值都是合法 ISO 日期」这一前提；如果绕过 007 写入脏值（'2026/10/1'），
      整条查询会因 invalid input syntax 失败。写入侧请继续走 add_bill / CSV 归一。
    """
    global engine
    month_start, month_end = _parse_report_month(month)
    today = _report_today()

    if engine is None:
        init_db()

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                WITH bounds AS (
                    SELECT CAST(:month_start AS date) AS month_start,
                           CAST(:month_end   AS date) AS month_end,
                           CAST(:today       AS date) AS today
                ),
                per_user AS (
                    SELECT b.user_id,
                           COALESCE(SUM(b.amount) FILTER (WHERE b.type = :expense), 0)
                               AS month_expense,
                           COALESCE(SUM(b.amount) FILTER (WHERE b.type = :income), 0)
                               AS month_income,
                           COALESCE(SUM(b.amount) FILTER (
                               WHERE b.type = :expense
                                 AND b.date::date = bounds.today
                           ), 0) AS today_expense
                    FROM public.bills b
                    CROSS JOIN bounds
                    WHERE b.date::date >= bounds.month_start
                      AND b.date::date <  bounds.month_end
                    GROUP BY b.user_id
                )
                SELECT u.id            AS user_id,
                       u.username      AS username,
                       u.is_admin      AS is_admin,
                       COALESCE(p.month_expense, 0) AS month_expense,
                       COALESCE(p.month_income,  0) AS month_income,
                       COALESCE(p.today_expense, 0) AS today_expense
                FROM public.users u
                LEFT JOIN per_user p ON p.user_id = u.id
                ORDER BY u.id
            """),
            {
                "month_start": month_start,
                "month_end": month_end,
                "today": today,
                "expense": EXPENSE_TYPE,
                "income": INCOME_TYPE,
            },
        ).fetchall()

    # 已过天数：当月按 today.day；历史月份按整月；未来月份 0（不做除法）。
    if month_start <= today < month_end:
        elapsed_days = today.day
    elif today >= month_end:
        elapsed_days = (month_end - month_start).days
    else:
        elapsed_days = 0

    stats = []
    for row in rows:
        month_expense_dec = _as_decimal(row.month_expense)
        month_income_dec = _as_decimal(row.month_income)
        today_expense_dec = _as_decimal(row.today_expense)
        month_balance_dec = month_income_dec - month_expense_dec

        if elapsed_days > 0:
            daily_avg = month_expense_dec / Decimal(elapsed_days)
            today_overspend = today_expense_dec > 0 and today_expense_dec > daily_avg
        else:
            today_overspend = False

        stats.append(
            {
                "user_id": int(row.user_id),
                "username": row.username,
                "is_admin": bool(row.is_admin),
                "month_expense": _money_to_float(month_expense_dec),
                "month_income": _money_to_float(month_income_dec),
                "month_balance": _money_to_float(month_balance_dec),
                "today_expense": _money_to_float(today_expense_dec),
                "today_overspend": bool(today_overspend),
                "month_budget_source": REPORT_BUDGET_SOURCE,
            }
        )
    return stats


def record_notification(user_id, sent_date, channel, status, message=None, error=None) -> dict:
    """写入/更新一条通知发送记录（同一 user+日期+渠道**只有一行**）。

    幂等语义：``INSERT ... ON CONFLICT (user_id, sent_date, channel) DO UPDATE``，
    冲突时只更新 status / message / error / created_at=now()，**不新增行**；
    因此同一天先发 failed、后发成功再写 sent，得到的仍是同一行（status 变 sent）。
    created_at 记录的是「最后一次写入」的时刻，不是第一次发送的时刻 ——
    需要"首次发送时间"应另加列，本表刻意不承担审计全量历史。
    ⚠ 因为冲突时**无条件**覆盖 status，把一条已经 sent 的行再写成 skipped
      （例如"今天已发过，补一条 skipped 留痕"）会让 notification_already_sent
      变回 False；调用方不要在 sent 之后用 skipped 覆盖它，否则同日第三次运行
      会误判"还没发"并重复推送。failed 覆盖 sent 同理，重试语义必须由调用方显式决定。

    参数校验（全部在开连接之前）：
      · status 必须在 {'sent','failed','skipped'} 内，否则 ValueError；
      · sent_date 接受 datetime.date 或 "YYYY-MM-DD"，其余 ValueError；
      · channel strip 后 1~32 字符，否则 ValueError；
      · user_id 必须是正整数。
    message / error 可为 None；非字符串会转成 str 后入库。

    返回：RETURNING * 的 dict（id、user_id、sent_date(date)、channel、status、
    message、error、created_at(tz-aware datetime)）。

    表不存在（014 未执行）→ RuntimeError（中文提示），不静默吞掉；
    其他数据库异常照常向上抛，由调用方决定是否重试。
    """
    global engine
    uid = _normalize_notification_user_id(user_id)
    day = _normalize_sent_date(sent_date)
    ch = _normalize_notification_channel(channel)
    st = _normalize_notification_status(status)
    msg = None if message is None else str(message)
    err = None if error is None else str(error)

    if engine is None:
        init_db()

    with engine.connect() as conn:
        try:
            row = conn.execute(
                text("""
                    INSERT INTO public.user_notifications
                        (user_id, sent_date, channel, status, message, error)
                    VALUES
                        (:uid, :sent_date, :channel, :status, :message, :error)
                    ON CONFLICT (user_id, sent_date, channel) DO UPDATE
                       SET status     = EXCLUDED.status,
                           message    = EXCLUDED.message,
                           error      = EXCLUDED.error,
                           created_at = now()
                    RETURNING *
                """),
                {
                    "uid": uid,
                    "sent_date": day,
                    "channel": ch,
                    "status": st,
                    "message": msg,
                    "error": err,
                },
            ).fetchone()
            conn.commit()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_undefined_table(exc):
                _raise_notification_table_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise

    if row is None:
        # 写成功却读不回：极少见（并发删号 / 连接串指向别的库）。不返回 None，
        # 否则调用方会拿到一个与签名不符的返回值。
        raise RuntimeError("通知记录写入后未能读回，请重试")
    return _notification_row_to_dict(row)


def notification_already_sent(user_id, sent_date, channel="webhook") -> bool:
    """该用户在该日、该渠道是否已有 **status='sent'** 的记录。

    只看 sent：failed / skipped 不算"已发送"，允许下一次运行重试；
    同一行被 upsert 成 failed 后本函数会返回 False（按 status 判定，不是按行存在）。
    表不存在（014 未执行）→ RuntimeError（中文提示），而不是 False ——
    False 会被调用方理解成"还没发，可以发"，导致一个没启用的功能被当成正常状态。
    """
    global engine
    uid = _normalize_notification_user_id(user_id)
    day = _normalize_sent_date(sent_date)
    ch = _normalize_notification_channel(channel)

    if engine is None:
        init_db()

    with engine.connect() as conn:
        try:
            exists = conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.user_notifications
                        WHERE user_id   = :uid
                          AND sent_date = :sent_date
                          AND channel   = :channel
                          AND status    = :sent
                    )
                """),
                {
                    "uid": uid,
                    "sent_date": day,
                    "channel": ch,
                    "sent": NOTIFICATION_STATUS_SENT,
                },
            ).scalar()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_undefined_table(exc):
                _raise_notification_table_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise
    return bool(exists)


# ---------------------------------------------------------------------------
# 智能预算（users.budget_details，迁移 015）
# ---------------------------------------------------------------------------
# 这组函数服务于「让 AI 按上月真实消费结构分配本月总预算」：
#   · get_budget_details / save_budget_details 读写 users.budget_details（JSONB 小对象，
#     {分类: 金额}）。列不存在（015 未执行）→ **RuntimeError + 中文提示**，
#     绝不静默返回 {} / 假装保存成功 —— 「功能没启用」与「用户没设置过」必须能区分；
#   · get_month_category_totals 只读 bills，**不依赖 015**：015 没执行也能统计分类支出，
#     它是 generate_smart_budget 的输入；
#   · generate_smart_budget 懒加载 ai_agent.generate_budget_plan（函数内 import，
#     避免 db ↔ ai_agent 的循环依赖）。AI 失败时：BudgetPlanError 原样上抛（保留类型，
#     前端能按类型显示它自己的中文文案），其它异常包装成 RuntimeError（中文，
#     **不带 API key、不带原始请求内容**），页面只看到一句可读提示。
#
# 口径：
#   · budget_details 的 key 是分类名，value 是**金额**（float，两位小数）；
#   · 写入侧**不校验各项之和 == 总预算**：总和由调用方 / AI 层约束（用户可能只想
#     先分配一部分），这里只校验每个值本身合法；
#   · NULL = 未设置（不是 {}）；015 的 CHECK 保证非 NULL 时一定是 JSON object；
#   · 金额统一用 Decimal 定量到两位小数（与 003 的 NUMERIC(12,2)、_CENT 同口径），
#     避免二进制浮点把 0.1 + 0.2 写成 0.30000000000000004 再落库。
# ---------------------------------------------------------------------------

BUDGET_DETAILS_MISSING_MESSAGE = (
    "智能预算功能未启用：请先执行 migrations/015_users_budget_details.sql"
)

# 与 015 的列 / 约束语义配套的应用层校验口径。
BUDGET_KEY_MAX_LEN = 20        # 分类名 strip 后 1~20 个字符
BUDGET_MAX_CATEGORIES = 20     # 一个用户最多 20 个预算分类
BUDGET_DETAILS_MAX_AMOUNT = MAX_ABS_AMOUNT  # 9999999999.99，与 NUMERIC(12,2) 上界同口径

BUDGET_EMPTY_BILLS_MESSAGE = "上个月还没有账单，先记几笔再让 AI 做预算吧"
BUDGET_EMPTY_PLAN_MESSAGE = "AI 没有给出有效的预算方案，请重试"


def _normalize_budget_user_id(user_id) -> int:
    """user_id 必须是正整数；非法输入在**开连接之前**就抛 ValueError。

    规则与 _normalize_notification_user_id 相同（与具体表无关），这里独立实现
    而不是复用：015 的读写不该依赖 014 的函数命名，将来删改 014 也不会牵连预算。
    只接受 int / 十进制字符串：float / Decimal 即使值是 1.0 也拒绝 ——
    ``int(1.9)`` 会静默截断成 1，把 "写错用户" 伪装成 "写对用户" 比直接报错更危险。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    try:
        uid = int(user_id.strip()) if isinstance(user_id, str) else int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}") from exc
    if uid <= 0:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    return uid


def _is_budget_storage_missing(exc) -> bool:
    """42703 = undefined_column（budget_details 不存在）、42P01 = undefined_table。

    42P01 只会在 users 表整个不见了（库 / 连接串指错）时出现；两者都翻译成同一条
    "先执行 015" 的提示，避免把 psycopg2 栈丢到页面上。
    """
    return _pg_sqlstate(exc) in ("42703", "42P01")


def _raise_budget_column_missing(exc):
    """把 42703 / 42P01 统一翻译成 RuntimeError（带中文可操作提示），保留原始异常链。"""
    raise RuntimeError(BUDGET_DETAILS_MISSING_MESSAGE) from exc


def _normalize_budget_amount(category: str, value) -> float:
    """单个分类的预算金额：有限、> 0、≤ 9999999999.99；返回两位小数的 float。

    顺序是 "先定量、再判正、最后判上界"（与 _normalize_goal_amount 一致）：
    0.004 会被定量成 0.00 → 判正时被拒；9999999999.995 会定量成 10000000000.00
    → 必须在上界判断之前定量才拦得住。
    """
    if isinstance(value, bool):
        raise ValueError(f"分类「{category}」的预算金额非法：{value!r}（不是数字）")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"分类「{category}」的预算金额非法：{value!r}（不是数字）"
        ) from exc
    if not math.isfinite(num):
        raise ValueError(
            f"分类「{category}」的预算金额非法：{value!r}（必须是有限数字，不允许 nan/inf）"
        )
    try:
        num = float(Decimal(str(num)).quantize(_CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"分类「{category}」的预算金额超出上限 {BUDGET_DETAILS_MAX_AMOUNT}"
        ) from exc
    if num <= 0:
        raise ValueError(f"分类「{category}」的预算金额必须大于 0")
    if num > BUDGET_DETAILS_MAX_AMOUNT:
        raise ValueError(f"分类「{category}」的预算金额超出上限 {BUDGET_DETAILS_MAX_AMOUNT}")
    return num


def _normalize_budget_details(details) -> dict[str, float]:
    """校验并归一化 save_budget_details 的入参，**全部在开连接之前完成**。

    规则：
      · 必须是 dict 且非空（"清空预算" 是显式删除操作，不是传 {}）；
      · key 必须是字符串，strip 后 1~20 个字符（中文 / 字母均可）；
      · value 必须是有限数、> 0、≤ 9999999999.99，定量到两位小数；
      · 分类数 ≤ 20；
      · **不校验总和**：总和由调用方 / AI 层约束。

    strip 后重名（"餐饮" 与 " 餐饮 "）直接报错，而不是让后写覆盖前写：
    那样用户会得到 "少了一个分类" 却毫无提示的结果。
    """
    if not isinstance(details, dict):
        raise ValueError("预算明细必须是字典（{分类: 金额}）")
    if not details:
        raise ValueError("预算明细不能为空")
    if len(details) > BUDGET_MAX_CATEGORIES:
        raise ValueError(f"预算分类最多 {BUDGET_MAX_CATEGORIES} 个")
    normalized: dict[str, float] = {}
    for raw_key, raw_value in details.items():
        if not isinstance(raw_key, str):
            raise ValueError(f"预算分类名必须是字符串：{raw_key!r}")
        key = raw_key.strip()
        if not 1 <= len(key) <= BUDGET_KEY_MAX_LEN:
            raise ValueError(
                f"预算分类名必须是 1~{BUDGET_KEY_MAX_LEN} 个字符：{raw_key!r}"
            )
        if key in normalized:
            raise ValueError(f"预算分类名重复（strip 后都是「{key}」）：{raw_key!r}")
        normalized[key] = _normalize_budget_amount(key, raw_value)
    return normalized


def _normalize_budget_total(total_budget) -> float:
    """总预算：有限、> 0；定量到两位小数后仍须 > 0（0.001 会被定量成 0.00 而被拒）。"""
    if isinstance(total_budget, bool):
        raise ValueError(f"总预算非法：{total_budget!r}（不是数字）")
    try:
        num = float(total_budget)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"总预算非法：{total_budget!r}（不是数字）") from exc
    if not math.isfinite(num):
        raise ValueError(f"总预算非法：{total_budget!r}（必须是有限数字，不允许 nan/inf）")
    try:
        num = float(Decimal(str(num)).quantize(_CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"总预算超出可计算范围：{total_budget!r}") from exc
    if num <= 0:
        raise ValueError(f"总预算必须大于 0：{total_budget!r}")
    return num


def _previous_month(today: date | None = None) -> str:
    """返回上一个自然月的 "YYYY-MM"（UTC+8 口径，与 _report_today 同一时钟）。

    用 date 计算而不是减 30 天：3 月 1 日减 30 天会落到 2 月，而各月天数不固定；
    12 月还要跨年。"本月 1 日 - 1 天" 永远是上个月最后一天，没有闰年 / 月长分支。
    例：2026-10-18 → "2026-09"；2026-01-05 → "2025-12"。
    """
    current = today or _report_today()
    first_of_month = current.replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


def _normalize_ai_budget_plan(raw_plan) -> dict[str, float]:
    """把 AI 返回的 {分类: 金额} 收敛成合法预算：未知分类丢弃、非法值丢弃、定量两位小数。

    · key 不在 BILL_CATEGORIES 内 → 丢弃 + warning（AI 偶尔会自造 "房租" 之类，
      页面下拉里没有这个分类，存进去也无法展示 / 编辑）；
    · value 不是有限数 / <= 0 → 丢弃 + warning；
    · 非字典（AI 回了字符串 / 数组）→ 返回 {}，由调用方统一报 "没有有效方案"；
    · 同一 key 重复（strip 后）→ 金额合并（AI 的 JSON 理论上不会有重复键，
      但这里不假设它守规矩）。
    """
    log = logging.getLogger(__name__)
    if not isinstance(raw_plan, dict):
        log.warning(
            "[db] AI 预算方案不是 dict（%s），按空方案处理", type(raw_plan).__name__
        )
        return {}
    allowed = set(BILL_CATEGORIES)
    items: dict[str, float] = {}
    for raw_key, raw_value in raw_plan.items():
        if not isinstance(raw_key, str):
            log.warning("[db] AI 预算方案含非字符串分类名，已丢弃：%r", raw_key)
            continue
        key = raw_key.strip()
        if key not in allowed:
            log.warning(
                "[db] AI 预算方案含未知分类 %r（不在 BILL_CATEGORIES 内），已丢弃", raw_key
            )
            continue
        if isinstance(raw_value, bool):
            log.warning("[db] AI 预算方案分类 %r 的金额不是数字，已丢弃：%r", key, raw_value)
            continue
        try:
            num = float(raw_value)
        except (TypeError, ValueError):
            log.warning("[db] AI 预算方案分类 %r 的金额不是数字，已丢弃：%r", key, raw_value)
            continue
        if not math.isfinite(num):
            log.warning(
                "[db] AI 预算方案分类 %r 的金额不是有限数，已丢弃：%r", key, raw_value
            )
            continue
        try:
            num = float(Decimal(str(num)).quantize(_CENT, rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            log.warning(
                "[db] AI 预算方案分类 %r 的金额超出可处理范围，已丢弃：%r", key, raw_value
            )
            continue
        if num <= 0:
            log.warning("[db] AI 预算方案分类 %r 的金额不是正数，已丢弃：%r", key, raw_value)
            continue
        items[key] = items.get(key, 0.0) + num
    return items


def _scale_budget_items(items: dict[str, float], total_budget: float) -> dict[str, float]:
    """把一组预算按比例缩放到 total_budget（两位小数，最后一项吸收舍入误差）。

    为什么用 "整数分" 算：浮点乘比例再 round 容易出现 599.9999999999999 /
    1200.0000000000002 —— 页面显示两位小数没问题，但 sum 断言 / 对账会失败。
    全部换算成 "分" 做整数运算后，总和**精确**等于 int(total_budget * 100)。

    为什么最后一项吸收误差：逐项 HALF_UP 的舍入误差最多累计 n * 0.5 分；
    让最后一项 = 总分 - 已分配，总和必定精确。极端小的总预算下逐项 HALF_UP
    可能把前 n-1 项加过头（最后一项变负），这里从金额最大的几项往回扣，
    保证不出现负数；扣不回来（总预算小到无法分配）宁可报错也不返回错的总和。
    """
    keys = list(items)
    raw_total_cents = sum(Decimal(str(items[k])) * 100 for k in keys)
    total_cents = int(
        (Decimal(str(total_budget)) * 100).to_integral_value(rounding=ROUND_HALF_UP)
    )
    cents: list[int] = []
    for key in keys[:-1]:
        share = Decimal(str(items[key])) * 100 / raw_total_cents * total_cents
        cents.append(int(share.to_integral_value(rounding=ROUND_HALF_UP)))
    cents.append(total_cents - sum(cents))

    deficit = -cents[-1] if cents[-1] < 0 else 0
    if cents[-1] < 0:
        cents[-1] = 0
    # 从最大项开始扣，避免把某个分类扣成负数；前 n-1 项之和恰好等于
    # total_cents + deficit，所以 deficit 一定能扣完。
    for idx in sorted(range(len(cents) - 1), key=lambda i: cents[i], reverse=True):
        if deficit <= 0:
            break
        take = min(deficit, cents[idx])
        cents[idx] -= take
        deficit -= take
    if deficit > 0:
        raise RuntimeError("预算缩放失败：总预算过小，无法按比例分配到每个分类")
    return {key: float(Decimal(c) / 100) for key, c in zip(keys, cents)}

def get_budget_details(user_id) -> dict:
    """读取某个用户的预算明细；未设置返回 {}。

    契约：
      · 列值是 SQL NULL → `{}`（"还没设置过"，不是错误）；
      · 行不存在（id 不存在 / 会话里的 id 已过期被删）→ ValueError("用户不存在")；
      · budget_details 列不存在（015 未执行，42703）或 users 表不存在（42P01）
        → RuntimeError（中文提示先执行 015），**不返回 {}** —— 否则页面会把
        "功能没启用" 显示成 "还没设置预算"，用户以为自己的数据丢了；
      · 返回值是 `{分类: float}`：金额统一转 float 并定量两位小数（_money_to_float），
        与页面 / JSON 的口径一致（psycopg2 的 Decimal 不能直接 JSON 序列化）；
      · 只读、单条语句、不写库、**不做缓存**：015 是否执行在进程生命周期内可能变化
        （运维中途跑迁移），缓存 False 会让功能一直不可用。

    脏数据（非 JSON object / 非数字金额）只 warning 并跳过，不让一条坏行把页面弄崩；
    "跳过" 是安全的降级方向：最坏情况是页面少显示一个分类，而不是整块功能报错。
    """
    uid = _normalize_budget_user_id(user_id)
    if engine is None:
        init_db()
    with engine.connect() as conn:
        try:
            row = conn.execute(
                text("SELECT budget_details FROM public.users WHERE id = :uid"),
                {"uid": uid},
            ).fetchone()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_budget_storage_missing(exc):
                _raise_budget_column_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise

    if row is None:
        raise ValueError("用户不存在")

    raw = row[0]
    if raw is None:
        return {}
    if isinstance(raw, str):
        # 防御性分支：psycopg2 会把 jsonb 直接反序列化成 dict；换成不注册 json
        # 适配器的驱动（或某些连接池配置）时可能拿到原始 JSON 文本。
        import json

        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            logging.getLogger(__name__).warning(
                "[db] budget_details 不是合法 JSON，按未设置处理（user_id=%r）", uid
            )
            return {}
    if not isinstance(raw, dict):
        logging.getLogger(__name__).warning(
            "[db] budget_details 不是 JSON object（%s），按未设置处理（user_id=%r）",
            type(raw).__name__,
            uid,
        )
        return {}

    details: dict[str, float] = {}
    for key, value in raw.items():
        if value is None or isinstance(value, bool):
            logging.getLogger(__name__).warning(
                "[db] budget_details 里的金额非法，已跳过：key=%r value=%r", key, value
            )
            continue
        try:
            details[str(key)] = _money_to_float(value)
        except (TypeError, ValueError, InvalidOperation):
            logging.getLogger(__name__).warning(
                "[db] budget_details 里的金额无法转成数字，已跳过：key=%r value=%r",
                key,
                value,
            )
    return details


def save_budget_details(user_id, details: dict) -> dict:
    """整体覆盖保存预算明细，返回 get_budget_details() 的同一结构。

    - 入参校验（dict / 非空 / key 长度 / value 范围 / 分类数）全部在**开连接之前**
      完成：非法输入连库都不碰；
    - `UPDATE ... SET budget_details = CAST(:d AS jsonb)`：整对象覆盖，不做部分合并
      （页面提交的是完整表单，合并语义会让 "删掉一个分类" 永远删不掉）。
      JSON 文本由 json.dumps(ensure_ascii=False) 生成后**绑定成参数**，不拼 SQL；
      这里用 CAST(:d AS jsonb) 而不是 :d::jsonb：SQLAlchemy 的 text() 不把
      `:d::jsonb` 识别成绑定参数（实测会原样发给 PostgreSQL 报 syntax error），
      CAST 与 :: 语义完全等价；
    - rowcount=0 → 用户不存在 → ValueError("用户不存在")（不静默成功）；
    - 列不存在（015 未执行）→ RuntimeError（中文提示），不假装保存成功；
    - 返回值统一走 get_budget_details()：写入口 / 读入口只有一套字段与精度口径，
      也顺带确认 "写进去的就是能读出来的"。
    """
    uid = _normalize_budget_user_id(user_id)
    normalized = _normalize_budget_details(details)
    import json

    payload = json.dumps(
        normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if engine is None:
        init_db()
    with engine.connect() as conn:
        try:
            result = conn.execute(
                text(
                    "UPDATE public.users SET budget_details = CAST(:d AS jsonb) WHERE id = :uid"
                ),
                {"uid": uid, "d": payload},
            )
            rowcount = result.rowcount
            conn.commit()
        except ProgrammingError as exc:
            conn.rollback()
            if _is_budget_storage_missing(exc):
                _raise_budget_column_missing(exc)
            raise
        except SQLAlchemyError:
            conn.rollback()
            raise

    if rowcount == 0:
        raise ValueError("用户不存在")
    return get_budget_details(uid)


def get_month_category_totals(user_id, month: str) -> dict[str, float]:
    """统计某用户某个月（"YYYY-MM"）按分类的**支出**总额，返回 {分类: float}。

    口径：
      · month 严格 "YYYY-MM"（复用 _parse_report_month：'2026-1'、'2026-13'、
        '26-10' 一律 ValueError），且**在开连接之前**校验；
      · bills.date 落在该自然月（左闭右开 [1 日, 下月 1 日)）、type = '支出'；
      · 按 category 汇总，category 为 NULL / 空白 → 归 "其他"（与 FALLBACK_CATEGORY 一致）；
      · 无数据返回 {}（"这个月没有支出账"，不是错误）；
      · 金额从 NUMERIC 转 float 并定量两位小数（_money_to_float），与页面口径一致；
      · **不依赖 015**：只读 bills，015 没执行也能用；
      · 所有参数走绑定，month 不会拼进 SQL。

    ⚠ bills.date 是 VARCHAR（历史遗留），这里按 `b.date::date` 比较；007 已用 CHECK
      保证库里都是 ISO 日期。若历史脏数据导致 cast 失败，异常原样上抛，由调用方决定
      怎么提示（不静默跳过整月数据 —— 那会让预算基于残缺的统计生成）。
    """
    uid = _normalize_budget_user_id(user_id)
    start, end = _parse_report_month(month)  # 严格校验 "YYYY-MM"
    if engine is None:
        init_db()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT COALESCE(NULLIF(btrim(b.category), ''), :fallback) AS category,
                       SUM(b.amount) AS total
                FROM public.bills AS b
                WHERE b.user_id = :uid
                  AND b.type = :expense
                  AND b.date::date >= :start
                  AND b.date::date < :end
                GROUP BY 1
                ORDER BY 1
            """),
            {
                "uid": uid,
                "expense": EXPENSE_TYPE,
                "start": start,
                "end": end,
                "fallback": FALLBACK_CATEGORY,
            },
        ).fetchall()
    return {row.category: _money_to_float(row.total) for row in rows}


# ===========================================================================
# 月度消费回忆录 · 锚点提取（纯函数：吃 DataFrame，不碰数据库）
# ---------------------------------------------------------------------------
# Tab1 已经有全量账单 DataFrame（load_all_data），回忆录只需要本月最值得写的
# 几笔，所以这里不新增 SQL，只做「从账单里挑故事」：
#   · happiest    : 娱乐 / 购物里金额最大的一笔（「最开心的一笔消费」）
#   · painful     : 本月金额最大的单笔支出（「最心疼的一笔」）
#   · top_merchant: 本月出现次数最多的商户（同次数比累计金额，再比名称）
#   · net         : 本月收入 - 支出
# 返回结构逐字固定：app.py 直接展示，ai_agent.generate_monthly_memoir 直接当 stats 用。
# ===========================================================================

MEMOIR_HAPPY_CATEGORIES = ("娱乐", "购物")


def build_memoir_anchors(df, month: str) -> dict:
    """从账单 DataFrame 里提取本月 4 个「回忆录锚点」。纯函数，不读库、不写库。

    参数
    ----
    df    : 至少包含 date / merchant / amount / type 列的 DataFrame
            （app.py 的 load_all_data 返回；有 category 更好，没有就按"其他"处理）。
    month : "YYYY-MM"（严格校验，复用 _parse_report_month）。

    返回（逐字固定）
    ----------------
      month / month_label / has_expense
      expense / income / net / bill_count
      happiest     : {"merchant", "amount", "date", "category"} | None
      painful      : {"merchant", "amount", "date", "category"} | None
      top_merchant : {"merchant", "count", "total"} | None

    口径：
      · 金额一律两位小数（_money_to_float），日期一律 "YYYY-MM-DD" 字符串
        （JSON 可序列化，直接能进 session_state / 喂给 AI）；
      · 空表 / 没有本月账单 / 没有支出 → has_expense=False，三个锚点都是 None，
        金额是 0.0，绝不抛错（"这个月没记账"不是错误）；
      · 金额非数字的行（历史脏数据）直接跳过，不让整段回忆录挂掉；
      · 并列时取 DataFrame 里先出现的那一行（账单默认按日期倒序，结果稳定）。
    """
    if not isinstance(month, str):
        raise ValueError(f"月份格式必须是 YYYY-MM（例如 2026-10）：{month!r}")
    _parse_report_month(month)  # 严格校验格式，非法在动 DataFrame 之前就抛 ValueError
    month_label = f"{month[:4]}年{int(month[5:7])}月"

    empty = {
        "month": month,
        "month_label": month_label,
        "has_expense": False,
        "expense": 0.0,
        "income": 0.0,
        "net": 0.0,
        "bill_count": 0,
        "happiest": None,
        "painful": None,
        "top_merchant": None,
    }
    if df is None or getattr(df, "empty", True):
        return empty

    work = df.copy()
    # 日期：上层已经解析好 date_parsed 就复用，没有就从 date 现解析（两种都缺 → 空结果）。
    if "date_parsed" in work.columns:
        dates = pd.to_datetime(work["date_parsed"], errors="coerce")
    elif "date" in work.columns:
        dates = pd.to_datetime(work["date"], errors="coerce")
    else:
        return empty
    work = work.assign(_date=dates)
    work = work[work["_date"].dt.strftime("%Y-%m") == month]
    if work.empty:
        return empty

    work = work.assign(_amount=pd.to_numeric(work.get("amount"), errors="coerce"))
    work = work.dropna(subset=["_date", "_amount"]).copy()
    if work.empty:
        return empty

    work["_type"] = work["type"].astype(str).str.strip() if "type" in work.columns else ""
    if "merchant" in work.columns:
        work["_merchant"] = (
            work["merchant"]
            .astype(str)
            .str.strip()
            .replace({"": "未知商户", "nan": "未知商户", "None": "未知商户"})
        )
    else:
        work["_merchant"] = "未知商户"
    if "category" in work.columns:
        work["_category"] = (
            work["category"]
            .fillna(FALLBACK_CATEGORY)
            .astype(str)
            .str.strip()
            .replace({"": FALLBACK_CATEGORY, "nan": FALLBACK_CATEGORY})
        )
    else:
        work["_category"] = FALLBACK_CATEGORY

    expense = work[work["_type"] == EXPENSE_TYPE]
    income = work[work["_type"] == INCOME_TYPE]

    def _pick(frame):
        """取金额最大的一行；空表返回 None（并列时 idxmax 取先出现的那行）。"""
        if frame.empty:
            return None
        row = frame.loc[frame["_amount"].idxmax()]
        return {
            "merchant": str(row["_merchant"]),
            "amount": _money_to_float(row["_amount"]),
            "date": row["_date"].strftime("%Y-%m-%d"),
            "category": str(row["_category"]),
        }

    happiest = _pick(expense[expense["_category"].isin(MEMOIR_HAPPY_CATEGORIES)])
    painful = _pick(expense)

    top_merchant = None
    if not expense.empty:
        grouped = (
            expense.groupby("_merchant")
            .agg(count=("_amount", "size"), total=("_amount", "sum"))
            .reset_index()
            .sort_values(
                ["count", "total", "_merchant"], ascending=[False, False, True]
            )
        )
        row = grouped.iloc[0]
        top_merchant = {
            "merchant": str(row["_merchant"]),
            "count": int(row["count"]),
            "total": _money_to_float(row["total"]),
        }

    expense_total = _money_to_float(expense["_amount"].sum()) if not expense.empty else 0.0
    income_total = _money_to_float(income["_amount"].sum()) if not income.empty else 0.0

    return {
        "month": month,
        "month_label": month_label,
        "has_expense": not expense.empty,
        "expense": expense_total,
        "income": income_total,
        "net": _money_to_float(income_total - expense_total),
        "bill_count": int(len(work)),
        "happiest": happiest,
        "painful": painful,
        "top_merchant": top_merchant,
    }


def generate_smart_budget(user_id, total_budget, month: str | None = None) -> dict:
    """让 AI 按某个月的真实分类支出，给出一个总预算 = total_budget 的分配方案。

    流程：
      1. total_budget 必须是有限数且 > 0（在开连接 / 调 AI 之前校验）；
      2. month=None → 上个月（UTC+8 口径，用 date 计算，例：2026-10-18 → "2026-09"）；
         显式传 month 时严格校验 "YYYY-MM"；
      3. 读 get_month_category_totals(user_id, month)；没有账单 → ValueError
         （中文提示先记账，不浪费一次 AI 调用）；
      4. stats = {"month", "total", "category_totals"} 传给 AI；
      5. 懒加载 ai_agent.generate_budget_plan（函数内 import，避免循环依赖）：
         `generate_budget_plan(stats, float(total_budget), _deepseek_key())`；
      6. 校验 AI 返回：分类必须在 BILL_CATEGORIES 内（不在的丢弃 + warning）、
         金额 > 0；没有有效项 → RuntimeError；总和 > total_budget → 按比例缩放
         到 total_budget（两位小数，最后一项吸收误差），并标记 scaled=True；
      7. 异常分层：ai_agent.BudgetPlanError 原样上抛（前端按类型显示自己的中文文案）；
         其它异常包装成 RuntimeError("AI 预算生成失败：...")，**不含 API key**；
      8. 返回 {"source_month", "total_budget", "items", "total", "scaled"}。

    ⚠ 本函数**不写库**：只生成方案，用户确认后由调用方 save_budget_details 保存。
    ⚠ items 与 total 经过定量：缩放时总和严格等于 int(total_budget * 100) / 100。
    """
    uid = _normalize_budget_user_id(user_id)
    total = _normalize_budget_total(total_budget)

    if month is None:
        month = _previous_month()
    else:
        month = month.strip() if isinstance(month, str) else month
        _parse_report_month(month)  # 只借它的严格校验；边界值在 totals 里重新算

    totals = get_month_category_totals(uid, month)
    if not totals:
        raise ValueError(BUDGET_EMPTY_BILLS_MESSAGE)

    stats = {
        "month": month,
        "total": _money_to_float(sum(totals.values())),
        "category_totals": totals,
    }

    # 懒加载：函数内 import，避免 ai_agent 将来在模块顶层 import db 形成循环依赖。
    try:
        import ai_agent
        from ai_agent import generate_budget_plan
    except ImportError as exc:
        raise RuntimeError(
            "智能预算功能未启用：ai_agent.generate_budget_plan 尚未就绪，请稍后重试"
        ) from exc

    # BudgetPlanError 可能尚未定义（并行开发中）：用 getattr 取，取不到就只包装
    # "非预期异常"。绝不把 `except ai_agent.BudgetPlanError` 写成硬引用 ——
    # 属性不存在时 except 子句求值本身会抛 AttributeError，把原异常换成另一个错。
    budget_plan_error = getattr(ai_agent, "BudgetPlanError", None)

    key = _deepseek_key()  # None = 没配置；由 generate_budget_plan 抛它自己的中文错误
    try:
        raw_plan = generate_budget_plan(stats, total, key)
    except Exception as exc:  # noqa: BLE001 - 分层：可识别异常原样抛，其它统一包装
        if budget_plan_error is not None and isinstance(exc, budget_plan_error):
            raise
        # 只暴露异常**类名**，不暴露 str(exc)：requests 的异常文本 / 响应体里
        # 可能带着请求 URL、请求头甚至 key，绝不能进页面或日志。
        raise RuntimeError(
            f"AI 预算生成失败：{type(exc).__name__}，请稍后重试"
        ) from exc

    items = _normalize_ai_budget_plan(raw_plan)
    if not items:
        raise RuntimeError(BUDGET_EMPTY_PLAN_MESSAGE)

    raw_total = _money_to_float(sum(items.values()))
    scaled = raw_total > total
    if scaled:
        items = _scale_budget_items(items, total)
    total_result = _money_to_float(sum(items.values()))

    return {
        "source_month": month,
        "total_budget": total,
        "items": items,
        "total": total_result,
        "scaled": scaled,
    }


# ---------------------------------------------------------------------------
# 登录「记住我」（users.remember_token / token_expires_at，迁移 016）
# ---------------------------------------------------------------------------
# 工作方式：
#   · 用户勾选「记住我」并登录成功后，调用方生成一个 UUID4 原文写进 Cookie，
#     再调用 set_remember_token(user_id, token, now + 7 天)；
#   · 下次打开页面时，调用方把 Cookie 原文交给 get_user_by_remember_token(token)：
#     返回 {"user_id", "username", "is_admin"} 表示会话有效，否则 None；
#   · 退出登录时调用 clear_remember_token(user_id) 作废旧 token。
#
# 安全口径（与 016 文件头的说明一致）：
#   · **库里只存 sha256(token) 的 64 位小写 hex，Cookie 里存 UUID 原文**。
#     拖库 / 备份泄露 / SQL 注入读到 remember_token 不能直接登录：sha256 不可逆，
#     而 token 是 UUID4（122 位随机），穷举与彩虹表都不可行；
#   · 刻意不用 bcrypt：token 是高熵随机串，不是人选的密码；记住我校验发生在
#     **登录恢复路径**（每个会话至少一次）上，bcrypt 的 ~200ms 会直接拖慢首屏。不加盐是安全的
#     （随机 token 不存在"相同密码批量撞库"的问题）。
#   · 过期判定用数据库的 now()（timestamptz 绝对时刻），不用应用机时钟：
#     本机是 UTC+8、库是 UTC，拿 Python 的 naive 时间与库里 timestamptz 比较
#     会整体错 8 小时；用 now() 还能屏蔽应用机时钟漂移（"7 天"不会变成 6 天 16 小时）。
#   · 日志只写 user_id / 异常类型，**绝不写 token、也不写 hash**。
#
# 错误契约（app.py 依赖它，改语义前先看调用点）：
#   · 参数非法（token 非字符串/空/长度越界、过期时间格式错或已过去）
#     → ValueError（中文文案），且**在开连接之前**就拒绝，非法入参连库都不碰；
#   · 016 未执行（42703 undefined_column）→ RuntimeError，提示先执行 016。
#     get 也**不能**静默返回 None —— 那会让页面把"功能没启用"误判成
#     "Cookie 失效"，用户反复重新登录也永远好不了；
#   · set 时用户不存在（rowcount=0）→ ValueError("用户不存在")；
#   · clear 幂等：用户不存在 / 本来就没有 token 都不报错。
# ---------------------------------------------------------------------------

REMEMBER_TOKEN_MISSING_MESSAGE = (
    "记住我功能未启用：请先执行 migrations/016_users_remember_token.sql"
)

# 与 016 的 CHECK / 索引同口径的边界。UUID 原文 36 字符、uuid4().hex 32 字符都在区间内；
# 上界 128 是"明显不是我们签发的 token"的廉价拒绝线，避免把超长串送进索引查询。
REMEMBER_TOKEN_MIN_LEN = 8
REMEMBER_TOKEN_MAX_LEN = 128


def _sha256_hex(value: str) -> str:
    """sha256 的小写十六进制（恰好 64 字符），与 016 的 CHECK / 索引逐字同口径。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_remember_token(token) -> str:
    """写入侧：校验并返回用于哈希的 token（strip 后）。

    为什么 strip：Cookie 经传输/框架解码后可能带首尾空白，而 token 本身不含空白；
    写入与读取必须用**同一个** strip 口径，否则会出现"存进去却永远查不到"。
    这里只校验长度，不打印 token 本身 —— 错误文案里连片段都不带。
    """
    if not isinstance(token, str):
        raise ValueError(f"记住我 token 必须是字符串：{type(token).__name__}")
    normalized = token.strip()
    if not normalized:
        raise ValueError("记住我 token 不能为空")
    if not (REMEMBER_TOKEN_MIN_LEN <= len(normalized) <= REMEMBER_TOKEN_MAX_LEN):
        raise ValueError(
            f"记住我 token 长度必须在 {REMEMBER_TOKEN_MIN_LEN}~{REMEMBER_TOKEN_MAX_LEN} 个字符之间"
        )
    return normalized


def _remember_token_digest_or_none(token):
    """读取侧：非法 token 直接返回 None（**不抛**，脏 Cookie 不能把页面搞崩）。

    非 str / 空 / 过短 / 过长都按"没有这个 token"处理，连一次数据库查询都不发。
    """
    if not isinstance(token, str):
        return None
    normalized = token.strip()
    if not (REMEMBER_TOKEN_MIN_LEN <= len(normalized) <= REMEMBER_TOKEN_MAX_LEN):
        return None
    return _sha256_hex(normalized)


def _normalize_remember_expiry(expires_at) -> datetime:
    """把过期时间收敛成 **aware UTC datetime**，并保证严格晚于当前 UTC 时刻。

    接受：
      · datetime：naive 视为 UTC（与 008 起 users.created_at 的"绝对时刻"口径一致），
        aware 一律 astimezone(UTC)；
      · ISO 8601 字符串（如 "2026-10-25T12:00:00+00:00"、"...Z"、含空格的形式）。
    拒绝：date / int / None / 空串 / 解析不了 / 已过去的时间（全部 ValueError）。

    「必须晚于现在」刻意放在这里而不是数据库：过期时间在过去等于一写入就失效，
    属于调用方 bug（比如把 seconds 和 days 搞混），必须当场暴露，而不是静默存一条废 token。
    """
    if isinstance(expires_at, datetime):
        value = expires_at
    elif isinstance(expires_at, str):
        text = expires_at.strip()
        if not text:
            raise ValueError("记住我过期时间不能为空")
        # Python 3.11+ 的 fromisoformat 已接受 "Z"，这里仍显式归一，
        # 兼容只认 "+00:00" 的旧解释器，避免"本地行、线上不行"。
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"记住我过期时间格式非法：{expires_at!r}"
                "（应为 ISO 8601，如 2026-10-25T12:00:00+00:00）"
            ) from exc
    else:
        raise ValueError(
            f"记住我过期时间非法：{expires_at!r}（应为 datetime 或 ISO 8601 字符串）"
        )

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # naive 视为 UTC
    else:
        value = value.astimezone(timezone.utc)
    if value <= datetime.now(timezone.utc):
        raise ValueError("记住我过期时间必须晚于当前 UTC 时间")
    return value


def set_remember_token(user_id, token: str, expires_at) -> None:
    """为用户写入「记住我」token（存 sha256 哈希，不存原文）。

    参数：
      · token：Cookie 里的原文（UUID 等随机串），strip 后长度 8~128，否则 ValueError；
      · expires_at：datetime（naive 视为 UTC）或 ISO 字符串，必须晚于当前 UTC 时间。
    行为：
      · 同一用户只保留一个 token：UPDATE 直接覆盖旧值（旧 Cookie 立即失效）；
      · 用户不存在 → ValueError("用户不存在")（rowcount=0，不静默成功）；
      · 016 未执行（42703）→ RuntimeError（中文提示先执行 016）；
      · 其余数据库异常原样上抛，由调用方按"系统繁忙"处理。
    """
    global engine
    # 所有校验都在开连接之前：非法参数不会占用连接、不会开事务。
    normalized = _normalize_remember_token(token)
    expiry = _normalize_remember_expiry(expires_at)
    digest = _sha256_hex(normalized)

    if engine is None:
        init_db()
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "UPDATE users SET remember_token = :h, token_expires_at = :e "
                    "WHERE id = :uid"
                ),
                {"h": digest, "e": expiry, "uid": user_id},
            )
            rowcount = result.rowcount
            conn.commit()
    except ProgrammingError as exc:
        if _pg_sqlstate(exc) == _PGCODE_UNDEFINED_COLUMN:  # 42703 = 016 未执行
            raise RuntimeError(REMEMBER_TOKEN_MISSING_MESSAGE) from exc
        raise
    if rowcount == 0:
        # 连上了、写成功，但一行都没命中：用户不存在（id 错 / 账号已删）。
        # 必须报错：调用方不能对"记住了一个不存在的用户"当成功。
        raise ValueError("用户不存在")


def get_user_by_remember_token(token: str) -> dict | None:
    """用 Cookie 里的 token 原文查「记住我」会话；有效返回用户信息，否则 None。

    返回 ``{"user_id", "username", "is_admin"}``：
      · is_admin 是**真 bool**（app.py 会直接塞进 session_state / 参与判断）；
      · **不返回 remember_token / token_expires_at / password_hash** —— 页面上
        没有任何理由拿到哈希，少一个字段就少一条泄露路径。

    过期处理：
      · 判定用 SQL 的 `token_expires_at <= now()`（数据库时钟，timestamptz 绝对时刻）；
      · 过期行会顺手清空该用户的 token（best-effort）：清理失败只打 warning，
        仍然返回 None —— 过期 token 绝不能因为"清理没跑成"又变成有效。
    错误契约：
      · 非法 token（非 str / 空 / 长度越界）→ 直接 None，不抛、不查库；
      · 没有匹配行 → None（token 错 / 已被覆盖 / 已被 clear）；
      · 016 未执行（42703）→ RuntimeError（前端据此提示），**不静默 None**；
      · 其余数据库异常原样上抛（"连不上库"不能被伪装成"Cookie 失效"，
        否则一次数据库抖动会把所有记住我用户静默踢下线）。
    """
    global engine
    digest = _remember_token_digest_or_none(token)
    if digest is None:
        return None

    if engine is None:
        init_db()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, username, is_admin, token_expires_at, "
                    "       (token_expires_at IS NULL OR token_expires_at <= now()) AS is_expired "
                    "FROM users WHERE remember_token = :h"
                ),
                {"h": digest},
            ).fetchone()
    except ProgrammingError as exc:
        if _pg_sqlstate(exc) == _PGCODE_UNDEFINED_COLUMN:  # 42703 = 016 未执行
            raise RuntimeError(REMEMBER_TOKEN_MISSING_MESSAGE) from exc
        raise

    if row is None:
        return None

    if bool(row.is_expired):
        # best-effort 清理：失败只告警，不影响"过期 = 未登录"的结论。
        # 注意日志里只有 user_id，没有 token / hash。
        try:
            clear_remember_token(row.id)
        except Exception:  # noqa: BLE001 - 清理是附带动作，绝不能改变登录判定
            logging.getLogger(__name__).warning(
                "[db] 清理过期「记住我」token 失败（users.id=%r），本次仍按未登录处理",
                row.id,
                exc_info=True,
            )
        return None

    return {
        "user_id": row.id,
        "username": row.username,
        "is_admin": bool(row.is_admin),
    }


def clear_remember_token(user_id) -> None:
    """作废用户的「记住我」token（两列同时置 NULL）。

    幂等：用户不存在、本来就没有 token、重复调用都不报错（UPDATE 影响 0 行是正常结果）。
    016 未执行（42703）→ RuntimeError：与 set/get 保持同一条错误契约，
    避免"以为已经清掉了，其实列都不存在"的静默状态。
    """
    global engine
    if engine is None:
        init_db()
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE users SET remember_token = NULL, token_expires_at = NULL "
                    "WHERE id = :uid"
                ),
                {"uid": user_id},
            )
            conn.commit()
    except ProgrammingError as exc:
        if _pg_sqlstate(exc) == _PGCODE_UNDEFINED_COLUMN:  # 42703 = 016 未执行
            raise RuntimeError(REMEMBER_TOKEN_MISSING_MESSAGE) from exc
        raise


# ---------------------------------------------------------------------------
# 「吃土倒计时」预测 + 近 N 天支出 / 用户名查询（纯函数 + 只读查询）
# ---------------------------------------------------------------------------
# 本段只**追加**：不改动上面任何既有函数、常量与取数口径。
# 分工：
#   · forecast_broke(...)      纯函数，不碰 DB、不读全局状态：输出只由入参决定，
#                              同一组入参永远得到同一结果，可直接单测；
#   · get_recent_expense(...)  只读 bills（不依赖 012 的 category、也不依赖 015）；
#   · get_username(...)        只读 users，找不到就抛 ValueError（调用方兜文案）。
# 依赖的既有件（全部在文件上方已定义，这里不重新实现）：
#   EXPENSE_TYPE / _ISO_DATE_PATTERN / _money_to_float（Decimal + ROUND_HALF_UP 定量）
#   / _normalize_budget_user_id（正整数校验，与具体表无关）。

FORECAST_REASON_NO_BUDGET = "未设置本月预算，无法预测"
FORECAST_RECENT_DAYS_MIN = 1
FORECAST_RECENT_DAYS_MAX = 31


def _normalize_forecast_amount(value, label: str) -> float:
    """把 month_expense / budget / recent_expense 收敛成「有限、≥ 0」的 float。

    拒绝顺序（报错文案统一带字段中文名，调用方不用再猜是哪个参数错了）：
      ① bool —— True/False 是 int 子类，float(True)==1.0 会把"开关"静默当成金额；
      ② 不能转 float（None / 乱串 / 容器）；
      ③ nan / inf —— 会顺着 daily_avg 把 projected 变成 nan，页面显示 "nan 元"；
      ④ 负数 —— 负预算 / 负支出没有业务含义，且会让 remaining 的判断整体失真。
    注意：**0 是合法值**（未设预算用 budget=0 表达；本月没花钱 month_expense=0），
    所以这里只挡 < 0。全部校验都在计算任何字段之前完成。
    """
    if isinstance(value, bool):
        raise ValueError(f"{label}非法：{value!r}（不是数字）")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}非法：{value!r}（不是数字）") from exc
    if not math.isfinite(num):
        raise ValueError(f"{label}非法：{value!r}（必须是有限数字，不允许 nan/inf）")
    if num < 0:
        raise ValueError(f"{label}不能为负数：{value!r}")
    return num


def _normalize_forecast_today(today) -> date:
    """today 收敛成 datetime.date：None → 今天；ISO 'YYYY-MM-DD' 或 date 原样。

    datetime 是 date 的子类，必须先取 .date()：否则今天带时分秒时
    ``today + timedelta(...)`` 与 ``(broke_date - today).days`` 会按"时间差不足 24h
    就少一天"截断，days_to_broke 少 1 —— 而调用方只关心日历日。
    非法字符串（'2026/10/18'、'2026-02-30'）与非法类型都抛同一条中文 ValueError。
    """
    if today is None:
        return date.today()
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    if isinstance(today, str):
        text = today.strip()
        if _ISO_DATE_PATTERN.fullmatch(text):
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                pass  # 格式对但日子不存在（2026-02-30）→ 落到下面统一报错
    raise ValueError(f"today 必须是 datetime.date 或 YYYY-MM-DD 字符串：{today!r}")


def _normalize_forecast_days(days, label: str) -> int:
    """天数窗口必须是 1~31 的 int（bool / float / 字符串一律拒绝，不静默转换）。

    不静默转换的理由：``int(7.9)`` 会截断成 7，把"写错窗口"伪装成"写对窗口"；
    上界 31 与"本月"同量级 —— 再长就不是"近 N 天"，而是历史统计；
    recent_days=0 则会让 recent_expense / 0 直接 ZeroDivisionError。
    """
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError(f"{label}必须是 1~{FORECAST_RECENT_DAYS_MAX} 的整数：{days!r}")
    if not FORECAST_RECENT_DAYS_MIN <= days <= FORECAST_RECENT_DAYS_MAX:
        raise ValueError(f"{label}必须是 1~{FORECAST_RECENT_DAYS_MAX} 的整数：{days!r}")
    return days


def _forecast_month_end(today: date) -> date:
    """当月的最后一天。用「下月 1 日 - 1 天」算，天然覆盖 2 月 / 闰年 / 大小月。

    刻意不 import calendar：本文件只用 date / timedelta 就够，少一个模块级依赖，
    也避免"闰年判断写错成 year % 4 == 0"这类经典坑（2028-02 是闰年、2100-02 不是）。
    9999-12 会构造 date(10000, 1, 1) 失败 —— 极端边界直接让 ValueError 上抛。
    """
    if today.month == 12:
        first_of_next = date(today.year + 1, 1, 1)
    else:
        first_of_next = date(today.year, today.month + 1, 1)
    return first_of_next - timedelta(days=1)


def forecast_broke(month_expense, budget, today=None,
                   recent_expense=None, recent_days=7) -> dict:
    """预测本月是否会在月底前「吃土」（超支 / 花光）：纯函数，不碰 DB。

    参数
    ----
    month_expense : 本月截至今天的**已支出**总额（≥ 0 的有限数）。
    budget        : 本月总预算（≥ 0 的有限数；0 表示"未设置"，见 reason）。
    today         : datetime.date / "YYYY-MM-DD"；None → date.today()。
    recent_expense: 近 recent_days 天的支出总额；None 或 0 → 退回"本月日均"。
    recent_days   : 近 N 天窗口，1~31 的 int（只在 recent_expense > 0 时参与计算）。

    口径（与产品确认过的「吃土倒计时」一致）
    ------------------------------------
    1. days_left_in_month = 当月天数 - today.day：**不含今天**（今天已过，
       月末当天为 0）。所以 projected = month_expense + daily_avg * days_left
       不会把今天重复算一次；
    2. daily_avg：recent_expense > 0 → recent_expense / recent_days（basis="7d"，
       反映"最近真的在花"）；否则 month_expense / today.day（basis="month"，
       今天也算在分母里 —— "已花"本来就包含今天）；
    3. budget <= 0：不预测（can_last=None + 中文 reason），但数值字段照算，
       页面仍可展示"按当前速度本月将花多少"；
    4. remaining = budget - month_expense <= 0：已经吃土 —— broke_date=今天、
       days_to_broke=0、can_last=False；
    5. 否则按 daily_avg 算还能撑几天：days_until = remaining / daily_avg，
       破裂日 = today + floor(days_until)：
         · 破裂日**晚于**当月最后一天 → can_last=True、broke_date=None（能撑到月底）；
         · 否则 can_last=False、broke_date / days_to_broke 都是真实值；
       daily_avg <= 0（没花钱、也预测不出速度）→ can_last=True、broke_date=None；
    6. 金额字段全部定量到两位小数（_money_to_float），且 projected 用**已定量的
       daily_avg** 计算：这样 "month_expense + daily_avg * days_left_in_month ==
       projected_month_expense" 在返回字典里严格成立，前端再算也不会差一分钱；
    7. broke_date 返回 **"YYYY-MM-DD" 字符串**（不是 date），与页面展示口径一致；
       month 返回 "YYYY-MM"。

    返回字段（逐字固定，前端按 key 取，顺序无关）：
        can_last / broke_date / days_to_broke / days_left_in_month /
        daily_avg / projected_month_expense / projected_surplus /
        basis / month / reason

    非法输入（负数、nan/inf、recent_days 越界、today 格式错）一律 ValueError，
    且在计算任何字段之前抛出 —— 调用方只会拿到一条中文提示，不会拿到半个结果。
    """
    spent = _normalize_forecast_amount(month_expense, "本月支出 month_expense")
    budget_value = _normalize_forecast_amount(budget, "本月预算 budget")
    recent = (
        None
        if recent_expense is None
        else _normalize_forecast_amount(recent_expense, "近 N 天支出 recent_expense")
    )
    days = _normalize_forecast_days(recent_days, "recent_days")
    current = _normalize_forecast_today(today)

    month_end = _forecast_month_end(current)
    days_left = month_end.day - current.day  # 不含今天；月末当天为 0

    if recent is not None and recent > 0:
        basis = "7d"
        daily_avg = _money_to_float(recent / days)
    else:
        # recent_expense 为 None 或 0：本月日均兜底。今天已过，分母含今天。
        basis = "month"
        daily_avg = _money_to_float(spent / current.day)

    can_last = None
    broke_date = None
    days_to_broke = None
    reason = None

    if budget_value <= 0:
        # 未设预算：能不能撑到月底无法定义，但"按当前速度月底会花多少"仍可算。
        reason = FORECAST_REASON_NO_BUDGET
    else:
        remaining = budget_value - spent
        if remaining <= 0:
            # 已经超支 / 用光：今天就是"吃土日"（days_to_broke=0）。
            can_last = False
            broke_date = current
            days_to_broke = 0
        elif daily_avg <= 0:
            # 预算有余额、但没有任何消费可以推出速度 → 预测不出具体日期。
            can_last = True
        else:
            days_until = remaining / daily_avg
            candidate = current + timedelta(days=math.floor(days_until))
            if candidate > month_end:
                # 破裂日落到下个月：本月能撑到月底，不给出具体日期（避免误导）。
                can_last = True
            else:
                can_last = False
                broke_date = candidate
                days_to_broke = (candidate - current).days

    projected = _money_to_float(spent + daily_avg * days_left)
    surplus = _money_to_float(budget_value - projected)

    return {
        "can_last": can_last,
        "broke_date": (
            broke_date.strftime("%Y-%m-%d") if broke_date is not None else None
        ),
        "days_to_broke": days_to_broke,
        "days_left_in_month": days_left,
        "daily_avg": daily_avg,
        "projected_month_expense": projected,
        "projected_surplus": surplus,
        "basis": basis,
        "month": current.strftime("%Y-%m"),
        "reason": reason,
    }


def get_recent_expense(user_id, days=7, today=None) -> float:
    """近 N 天（含今天）的**支出**总额；无数据返回 0.0。

    口径
    ----
    · 窗口是 [today-(days-1), today] 的**闭区间**：days=7 时正好 7 个日历日
      （今天 + 过去 6 天）。起点用 timedelta(days=days-1)，多减一天就会把
      "近 7 天"算成 8 天，与页面标签不符；
    · 只看 type = '支出'（收入不计）；
    · bills.date 是 VARCHAR（历史遗留），按 ``b.date::date`` 比较；007 的 CHECK
      保证库里都是 ISO 日期。cast 失败的脏数据让异常原样上抛，**不静默漏算**
      —— 少算支出会让"吃土倒计时"显得比实际乐观；
    · **不依赖 012 的 category、也不依赖 015 的 budget_details**：只碰 bills；
    · 参数全部绑定（:uid / :expense / :start / :end）；days 只在 Python 侧参与
      timedelta 计算，绝不被拼进 SQL；
    · 金额从 NUMERIC 转 float 并定量两位小数（_money_to_float）；无行 → 0.0。

    user_id 校验复用 _normalize_budget_user_id：规则（正整数、不接受 bool/float）
    与具体表无关，没必要再抄第三份；非法入参在**开连接之前**就 ValueError。
    """
    uid = _normalize_budget_user_id(user_id)
    window = _normalize_forecast_days(days, "days")
    end = _normalize_forecast_today(today)
    start = end - timedelta(days=window - 1)
    if engine is None:
        init_db()
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(SUM(b.amount), 0) AS total
                FROM public.bills AS b
                WHERE b.user_id = :uid
                  AND b.type = :expense
                  AND b.date::date >= :start
                  AND b.date::date <= :end
            """),
            {"uid": uid, "expense": EXPENSE_TYPE, "start": start, "end": end},
        ).fetchone()
    return _money_to_float(row.total) if row is not None else 0.0


def get_username(user_id) -> str:
    """按 users.id 取用户名；用户不存在 → ValueError("用户不存在")。

    用途：把 user_id 显示成名字（报表 / 预测卡片）时的唯一入口。
    · 只 SELECT username，不碰 password_hash / remember_token 等敏感列；
    · user_id 先过 _normalize_budget_user_id（与具体表无关的正整数校验），
      非法入参在**开连接之前**就 ValueError；
    · 无行 = 用户真的不存在（被删号 / 传错 id）：抛 ValueError 由调用方决定
      是提示还是回退，**绝不返回 None** 让下游把 None 拼成字符串 "None" 显示给用户。
    """
    uid = _normalize_budget_user_id(user_id)
    if engine is None:
        init_db()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT username FROM users WHERE id = :uid"), {"uid": uid}
        ).fetchone()
    if row is None:
        raise ValueError("用户不存在")
    return row.username


def get_daily_expense_totals(user_id, months: int = 3, today=None) -> dict[str, float]:
    """按天汇总某用户**支出**，返回 {"YYYY-MM-DD": float}，**零填充**（供日历热力图）。

    区间口径（闭区间，左闭右闭）
    --------------------------
    · 起点 = 今天所在月往前推 months-1 个月的**1 号**；终点 = 今天（含）。
      例：today=2026-10-18、months=3 → 2026-08-01 ~ 2026-10-18（共 79 个键）；
          months=1 → 2026-10-01 ~ 2026-10-18（= today.day 个键）。
    · **零填充**：区间内每一天都有键，没消费的那天是 0.0 —— 热力图靠"键存在且值为 0"
      区分"这天没花钱"与"这天没数据"，缺键会让日历出现空洞。键数恒等于
      (today - start).days + 1，按日期升序（dict 插入序即升序）。
    · 只看 type = '支出'，收入不计入；bills.date 是 VARCHAR（历史遗留），按
      ``b.date::date`` 比较。007 的 CHECK 保证库里都是 ISO 日期；cast 失败的脏数据
      让异常原样上抛，**不静默漏算** —— 少算会让花钱的日子在热力图上变成空白。

    参数校验（都在**开连接之前**完成，非法入参不浪费一次连接）
    --------------------------------------------------------
    · user_id 复用 _normalize_budget_user_id（正整数，拒绝 bool / float）；
    · months 必须是 1~12 的 int（bool / float / 字符串一律拒绝，不静默转换：
      ``int(2.9)`` 截断成 2 会把"写错区间"伪装成"写对区间"）；
    · today 复用 _normalize_forecast_today（date / datetime / "YYYY-MM-DD"，
      默认 date.today()）。

    实现口径
    --------
    · **单条 SQL + Python 零填充**：库里只 GROUP BY 一次，日期序列由 Python 生成
      （不在 SQL 里 generate_series：少一层方言依赖，也方便纯逻辑单测）；
    · 金额经 _money_to_float 定量到两位小数；SUM 为 NULL 的日期按 0.0 处理；
    · 用户不存在 / 该区间没记过账 → 返回**全 0 字典**而不是报错：新注册用户也可能
      刚打开日历，返回 {} 或抛错都会逼调用方写分支；
    · 参数全部绑定（:uid / :expense / :start / :end），months 只参与 Python 侧
      date 运算，绝不被拼进 SQL；本函数不写日志，从根源上不泄漏账单明细。
    """
    uid = _normalize_budget_user_id(user_id)
    if isinstance(months, bool) or not isinstance(months, int):
        raise ValueError(f"months 必须是 1~12 的整数：{months!r}")
    if not 1 <= months <= 12:
        raise ValueError(f"months 必须是 1~12 的整数：{months!r}")
    end = _normalize_forecast_today(today)

    # 往前推 months-1 个月：先算「月份序号」（= 年 * 12 + 月 - 1）再减。
    # 借 Python 对负数的 // 与 % 是 floor 语义，跨年（2026-01 往前 2 个月 → 2025-11）
    # 自动落到上一年，不必再写一堆 year == 1 的分支。9999-12 之后的越界日期让
    # date() 自己抛 ValueError，不额外造边界逻辑。
    month_index = (end.year * 12 + end.month - 1) - (months - 1)
    start = date(month_index // 12, month_index % 12 + 1, 1)

    if engine is None:
        init_db()
    with engine.connect() as conn:
        # 顺序由 Python 零填充决定，SQL 不需要 ORDER BY。
        rows = conn.execute(
            text("""
                SELECT b.date::date AS day, SUM(b.amount) AS total
                FROM public.bills AS b
                WHERE b.user_id = :uid
                  AND b.type = :expense
                  AND b.date::date >= :start
                  AND b.date::date <= :end
                GROUP BY 1
            """),
            {"uid": uid, "expense": EXPENSE_TYPE, "start": start, "end": end},
        ).fetchall()

    # amount 全为 NULL 的那天 SUM 也是 NULL：按 0.0 处理，别让 _money_to_float(None) 抛。
    totals = {
        row.day: _money_to_float(row.total) for row in rows if row.total is not None
    }
    result: dict[str, float] = {}
    day = start
    while day <= end:
        result[day.isoformat()] = totals.get(day, 0.0)
        day += timedelta(days=1)
    return result


# ===========================================================================
# 自然语言查账 / 删账（AI tool 后端）
# ---------------------------------------------------------------------------
# 这两个函数是 ai_agent.py 里 query_bills / delete_bills 两个 tool 的落库实现，
# 安全边界**全部在这里**，不依赖 AI 层（模型可能幻觉、可能被提示词注入诱导）：
#   · 每条 SQL 都带 `user_id = :uid`，且 uid 只来自调用方传入、经
#     _normalize_budget_user_id 校验的正整数 —— **不存在「不带 user_id 的删除」**；
#   · 日期区间、分类、关键词一律走绑定参数，绝不字符串拼接，注入面为零；
#   · 删除语句只写 bills 一张表，DELETE 语义上不可能波及 users / savings_goals；
#   · 区间上限 366 天：既防「一次删/查全库」，也防误传 1970~2999 的宽区间拖垮库；
#   · 日志只记 user_id / 区间 / 命中行数，绝不落商户、关键词明细（隐私）。
# 注意：按约定这两个函数**不接受 bill_type**，删除会连同区间内的收入一起删；
# 前端确认文案必须说明「含收入」。
# ===========================================================================

BILL_QUERY_MAX_SPAN_DAYS = 366   # 闭区间两端相差最多 366 天（含端点即最多 367 个自然日）
BILL_FILTER_TEXT_MAX_LEN = 50    # 分类 / 关键词 strip 后的长度上限


def _normalize_bill_query_range(start_date, end_date) -> tuple[str, str]:
    """校验查询/删除的日期区间，返回 (start, end) 两个严格 ISO 字符串。

    全部校验都在**开连接之前**完成：非法入参连库都不碰。
      · 两端各走 _normalize_bill_date（严格 ``^\\d{4}-\\d{2}-\\d{2}$`` + 真实日历日）；
      · end >= start；
      · 跨度（end - start 的天数）<= 366，超过即 ValueError「查询区间过大」。
    用 `date()` 而不是字符串比较算跨度：字符串只能比字典序，算不出天数。
    """
    start = _normalize_bill_date(start_date)
    end = _normalize_bill_date(end_date)
    start_day = datetime.strptime(start, "%Y-%m-%d").date()
    end_day = datetime.strptime(end, "%Y-%m-%d").date()
    if end_day < start_day:
        raise ValueError(f"结束日期不能早于开始日期：{start} ~ {end}")
    if (end_day - start_day).days > BILL_QUERY_MAX_SPAN_DAYS:
        raise ValueError(
            f"查询区间过大：最多 {BILL_QUERY_MAX_SPAN_DAYS} 天（当前 {start} ~ {end}）"
        )
    return start, end


def _normalize_bill_filter_text(value, field_name: str) -> str | None:
    """分类 / 关键词：None → None（不限）；否则 strip 后 1~50 个字符，非法即 ValueError。

    只接受 str：int / list 之类不做 str() 隐式转换 —— 把「传错类型」悄悄变成
    「查了一个叫 123 的分类」会让调用方以为查询成功，实际永远 0 行。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name}必须是 1~{BILL_FILTER_TEXT_MAX_LEN} 个字符的字符串：{value!r}")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= BILL_FILTER_TEXT_MAX_LEN:
        raise ValueError(f"{field_name}必须是 1~{BILL_FILTER_TEXT_MAX_LEN} 个字符：{value!r}")
    return cleaned


def _normalize_bill_query_user_id(user_id) -> int:
    """AI 查账/删账的 user_id 校验：直接复用预算模块的正整数口径（拒绝 bool/float）。"""
    return _normalize_budget_user_id(user_id)


def query_bills(user_id, start_date, end_date, category=None, keyword=None) -> pd.DataFrame:
    """按日期区间（可选分类 / 关键词）查询某个用户的账单明细。

    返回 DataFrame，列与 get_all_bills 一致（``SELECT *`` + 同三列归一化：
    type / amount / category）；按 ``date DESC, id DESC`` 排序。
    **空结果返回带列名的空 DataFrame**（而不是 None 或空表壳）：AI 层随后要按
    `df["amount"].sum()` 之类做汇总，None 会直接炸，空表壳会 KeyError。

    SQL 全参数绑定；user_id 限定在 WHERE 第一项，跨用户查询不可能发生。
    """
    uid = _normalize_bill_query_user_id(user_id)
    start, end = _normalize_bill_query_range(start_date, end_date)
    cat = _normalize_bill_filter_text(category, "分类")
    kw = _normalize_bill_filter_text(keyword, "关键词")

    if engine is None:
        init_db()

    # 只拼接**固定的 SQL 片段**（是否加子句由参数是否为 None 决定），值全部绑定。
    where_sql = [
        "SELECT * FROM bills",
        "WHERE user_id = :uid",
        "  AND date::date BETWEEN :start AND :end",
    ]
    params = {"uid": uid, "start": start, "end": end}
    if cat is not None:
        where_sql.append("  AND category = :cat")
        params["cat"] = cat
    if kw is not None:
        # ILIKE 的通配符由 Python 侧包成 %kw%；kw 本身仍是绑定参数，不存在注入。
        where_sql.append("  AND (merchant ILIKE :kw OR category ILIKE :kw)")
        params["kw"] = f"%{kw}%"
    # date 是 TEXT（ISO 零填充）→ 字典序 == 时间序；同一天再按 id 倒序保证稳定。
    where_sql.append("ORDER BY date DESC, id DESC")
    stmt = text("\n".join(where_sql))

    with engine.connect() as conn:
        df = pd.read_sql(stmt, conn, params=params)
        if df.empty and not len(df.columns):
            # 空结果时 pd.read_sql 通常仍带列名；这里兜的是「驱动/版本把列信息丢了」
            # 的极端情况 —— 用同表 0 行查询取一次列定义，保证 columns 非空。
            columns = list(conn.execute(text("SELECT * FROM bills WHERE 1 = 0")).keys())
            df = pd.DataFrame(columns=columns)

    # 与 get_all_bills 完全同口径：type 字符串口径 + amount float + category 空值→「其他」。
    df = _normalize_category_column(_normalize_type_column(_normalize_amount_column(df)))

    # 只记区间与命中行数，不记 category / keyword（keyword 可能是商户名，属隐私）。
    logging.getLogger(__name__).info(
        "[db] query_bills user_id=%s range=%s..%s rows=%s", uid, start, end, len(df)
    )
    return df


def delete_bills(user_id, start_date, end_date, category=None) -> int:
    """删除某个用户在日期区间内（可选分类）的账单，返回实际删除行数。

    ⚠ 危险操作，但**: 本函数不做任何确认** —— 二次确认属于交互层（ai_agent 的
    tool 描述 + 系统提示词要求模型先复述条件、等用户回复「确认」）。这里只保证
    「就算被调用，也只可能删到该用户、该区间、该分类」。
    ⚠ 不接受 bill_type：区间内**收入与支出一起删**（前端确认文案必须写明「含收入」）。

    SQL 全参数绑定；`user_id` 是 WHERE 的首个条件，且 DELETE 只作用于 bills 表
    （绝不触及 users / savings_goals / chat_history）。没有 category 时删整段区间。
    """
    uid = _normalize_bill_query_user_id(user_id)
    start, end = _normalize_bill_query_range(start_date, end_date)
    cat = _normalize_bill_filter_text(category, "分类")

    if engine is None:
        init_db()

    delete_sql = [
        "DELETE FROM bills",
        "WHERE user_id = :uid",
        "  AND date::date BETWEEN :start AND :end",
    ]
    params = {"uid": uid, "start": start, "end": end}
    if cat is not None:
        delete_sql.append("  AND category = :cat")
        params["cat"] = cat
    stmt = text("\n".join(delete_sql))

    with engine.connect() as conn:
        result = conn.execute(stmt, params)
        conn.commit()  # DELETE 必须显式提交，否则连接归还时事务回滚、rowcount 是假象
        deleted = int(result.rowcount or 0)

    # 只记 user_id / 区间 / 删除行数；不记商户、分类、关键词。
    logging.getLogger(__name__).info(
        "[db] delete_bills user_id=%s range=%s..%s deleted=%s", uid, start, end, deleted
    )
    return deleted


# ===========================================================================
# 周期订阅检测（疑似连续包月）
# ---------------------------------------------------------------------------
# 只读 public.bills 一张表，WHERE 首项就是 user_id —— 跨用户取数在语义上不可能。
# 取数只有一条 SQL，参数全部绑定；months / today 只参与 Python 侧的日期计算，
# 绝不被拼进 SQL。
#
# 为什么是"链式聚类"而不是简单按商户 GROUP BY：
#   同一商户可能只是"偶尔买了一次"，也可能是"每月固定扣一次"。只有**相邻两次**
#   扣费同时满足「间隔 ≈ 一个月」与「金额基本不变」，才可能是同一条订阅链；
#   一旦某次不满足（金额跳变 / 隔了半个月 / 隔了三个月），链就在那里断开，前后
#   各自成链。这样"25 元 → 60 元 → 25 元"不会被硬凑成三次订阅，而"25、25、25
#   但中间隔了 3 个月"也不会被误判 —— 宁可漏报，也绝不把日常消费报成订阅。
#
# 误报控制（写死在算法里的三道闸，参数集中在下面几个常量）：
#   ① 间隔闸：相邻两笔必须落在 23~37 天（覆盖 28~31 天的月扣、以及跨月的 30±7）；
#   ② 金额闸：|差额| <= max(2.0, 12% × 两次金额均值)（容忍会员提价 / 小额优惠券，
#      但 25→60 这种翻倍一定断开）；
#   ③ 候选闸：链长 >= 2 才算候选；链长 > 6 的高频消费直接排除；分类为「餐饮」且
#      商户名不含订阅关键词（会员/VIP/包月/订阅/月卡/连续包月/续费/自动续费）的
#      直接排除 —— 食堂 / 奶茶这类周期性吃饭是最典型的误报来源。
# ===========================================================================

SUBSCRIPTION_KEYWORDS = ("会员", "VIP", "包月", "订阅", "月卡", "连续包月", "续费", "自动续费")
SUBSCRIPTION_KEYWORDS_FOLDED = tuple(keyword.casefold() for keyword in SUBSCRIPTION_KEYWORDS)
SUBSCRIPTION_MIN_INTERVAL_DAYS = 23
SUBSCRIPTION_MAX_INTERVAL_DAYS = 37
SUBSCRIPTION_MAX_CHAIN_OCCURRENCES = 6
SUBSCRIPTION_AMOUNT_TOLERANCE_MIN = 2.0
SUBSCRIPTION_AMOUNT_TOLERANCE_RATIO = 0.12
SUBSCRIPTION_HIGH_CONFIDENCE_MIN_OCCURRENCES = 3
SUBSCRIPTION_HIGH_CONFIDENCE_INTERVAL_MIN = 27.0
SUBSCRIPTION_HIGH_CONFIDENCE_INTERVAL_MAX = 33.0
SUBSCRIPTION_MEAL_CATEGORY = "餐饮"
# 金额下限：几元的小额固定扣费（共享单车次卡、自动售货等）通常不是订阅，
# 加一道下限把 9.9 元以下的噪声挡掉；用户要求 10 元起。
SUBSCRIPTION_MIN_AMOUNT = 10.0
# 固定月度但明确不是"订阅会员"的支出：房租/话费/定投/保险。
# 这类支出同样是"每月相近金额"，不加排除会被误报成隐形扣费。
SUBSCRIPTION_EXCLUDED_KEYWORDS = ("房租", "话费", "定投", "保险")
SUBSCRIPTION_EXCLUDED_KEYWORDS_FOLDED = tuple(
    keyword.casefold() for keyword in SUBSCRIPTION_EXCLUDED_KEYWORDS
)


def _subscription_is_excluded(merchant: str) -> bool:
    """商户名命中房租/话费/定投/保险等「固定但非订阅」关键词时排除。

    用 casefold 包含匹配：``话费充值``、``房租-合租`` 这类带后缀的写法也能拦住。
    """
    folded = merchant.casefold()
    return any(keyword in folded for keyword in SUBSCRIPTION_EXCLUDED_KEYWORDS_FOLDED)


def _subscription_merchant_key(merchant) -> str | None:
    """分组键：商户去首尾空格 + casefold；None / 非字符串 / 空串 / 纯空白 → None。

    casefold 而不是 lower：``VIP`` / ``Vip`` / ``vip`` 视为同一商户，避免同一个
    连续包月因为显示大小写不同被拆成两条链、双双不够 2 次而漏报。
    """
    if not isinstance(merchant, str):
        return None
    key = merchant.strip().casefold()
    return key or None


def _subscription_has_keyword(merchant: str) -> bool:
    """商户名是否带订阅关键词（大小写不敏感），用于「餐饮」豁免与 high 置信度。"""
    folded = merchant.casefold()
    return any(keyword in folded for keyword in SUBSCRIPTION_KEYWORDS_FOLDED)


def _subscription_amount_matches(previous: float, current: float) -> bool:
    """相邻两笔金额是否"基本不变"：|差| <= max(2.0 元, 12% × 两次均值)。

    先算浮动阈值再比较：小额订阅（15 元）靠 2 元兜底，大额订阅（100 元）靠 12%
    兜底，不会因为"比例太小"把 1 元的手续费差异判成断链。
    """
    tolerance = max(
        SUBSCRIPTION_AMOUNT_TOLERANCE_MIN,
        SUBSCRIPTION_AMOUNT_TOLERANCE_RATIO * ((previous + current) / 2),
    )
    return abs(previous - current) <= tolerance


def _subscription_amount_or_none(value):
    """DB 金额 -> 两位小数 float；NULL / bool / 非法数字 / <= 0 → None（跳过该笔）。

    `_money_to_float` 对 None 会返回 0.0，因此必须先挡 None；0 / 负数不可能是
    "订阅扣费"，参与链运算只会污染金额均值（0 元链还会算出 0 元年费）。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = _money_to_float(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount <= 0:
        return None
    return amount


def _round_half_up(value: float, digits: int) -> float:
    """十进制四舍五入（内置 round 是银行家舍入：30.5 会被舍成 30）。

    下一期扣费日靠它算：30.5 天的平均间隔必须进位成 31 天，否则预期日期会
    系统性偏早一天；同时用 Decimal 转换避免 2.675 → 2.67 这类二进制浮点误差。
    """
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def _subscription_cluster_chains(events) -> list[list[tuple]]:
    """把同一商户的按日期升序事件切成订阅链；只返回长度 >= 2 的链。

    events: [(date, 商户原文, 金额 float, 分类), ...]，日期升序（SQL 已排序）。
    相邻两次**同时**满足间隔 23~37 天且金额基本不变才续链，否则从当前这笔重启
    一条链。纯逻辑、不碰库、不写日志，便于单独验证边界。
    """
    chains: list[list[tuple]] = []
    current: list[tuple] = []
    for event in events:
        if current:
            previous = current[-1]
            gap = (event[0] - previous[0]).days
            if (
                SUBSCRIPTION_MIN_INTERVAL_DAYS <= gap <= SUBSCRIPTION_MAX_INTERVAL_DAYS
                and _subscription_amount_matches(previous[2], event[2])
            ):
                current.append(event)
                continue
            chains.append(current)
        current = [event]
    if current:
        chains.append(current)
    return [chain for chain in chains if len(chain) >= 2]


def detect_subscriptions(user_id, months: int = 3, today=None) -> list[dict]:
    """检测某用户近 months 个月的**疑似连续包月订阅**，返回按年花费降序的列表。

    区间口径与 get_daily_expense_totals 完全一致（左闭右闭）：
      起点 = 今天所在月往前推 months-1 个月的 1 号；终点 = 今天（含）。
      例：today=2026-10-18、months=3 → 2026-08-01 ~ 2026-10-18。
    只看 ``type = '支出'``；收入（工资/红包/退款）不计入，少报优于误报。

    参数校验（都在**开连接之前**完成，非法入参不浪费一次连接）：
      · user_id 复用 _normalize_budget_user_id（正整数，拒绝 bool / float）；
      · months 必须是 1~12 的 int（bool / float / 字符串一律拒绝，不静默截断）；
      · today 复用 _normalize_forecast_today（date / datetime / "YYYY-MM-DD"）。

    检测规则（细节见上方 _subscription_cluster_chains / 金额闸 / 关键词）：
      1. 按「商户去首尾空格 + casefold」分组，组内按日期升序做链式聚类；
      2. 相邻两次间隔 23~37 天且金额差 <= max(2.0, 12% × 均值) → 同一条链；
      3. 链长 >= 2 才进入候选；链长 > 6 视为高频消费，排除；
      4. 分类为「餐饮」且商户名不含订阅关键词 → 排除（食堂/奶茶是最大误报源）；
      5. 同一商户有多条候选链时**只取最长的一条**（同长取更早出现的那条），
         再对这条链套用 3、4 两条排除规则 —— 即"取最长链"优先于"排除"，
         避免把一个月度订阅和高频消费混在一起算出一条假链。

    输出（每条）：
      merchant 原样商户名（取链内最后一次的写法）、category（空→"其他"）、
      monthly_amount（链内金额均值，两位小数）、occurrences、first_date、last_date、
      avg_interval_days（一位小数）、annual_cost = 月均 × 12、
      next_expected_date = 最后一次 + 四舍五入后的平均间隔、
      confidence = high（商户名含订阅关键词，或 >=3 次且平均间隔 27~33 天）/ medium。
      按 annual_cost 降序、occurrences 降序（同值按商户名升序，保证结果可复现）。

    边界与异常：
      · 用户不存在 / 区间内没记过账 / 没有一条达标 → 返回 ``[]``（**不是 None**）；
      · 单笔金额为 NULL 或 0/负数、商户名为空的脏行会被跳过，不会让整次检测失败；
      · 参数非法 → ValueError。连接/DB 错误按原样上抛（**不**伪装成"没有订阅"，
        否则真出故障时页面会显示"你很省钱"，比报错更危险）。
    日志只记 user_id / months / 区间 / 候选条数，**绝不**记商户名或金额明细。
    """
    uid = _normalize_budget_user_id(user_id)
    if (
        isinstance(months, bool)
        or not isinstance(months, int)
        or not 1 <= months <= 12
    ):
        raise ValueError(f"months 必须是 1~12 的整数：{months!r}")
    end = _normalize_forecast_today(today)

    # 与 get_daily_expense_totals 同一套"月份序号"算法：跨年时由 floor 除法自然回落。
    month_index = (end.year * 12 + end.month - 1) - (months - 1)
    start = date(month_index // 12, month_index % 12 + 1, 1)

    if engine is None:
        init_db()
    with engine.connect() as conn:
        # 单条 SQL：排序交给库（date::date 后按真实日期，同日再按 id 保证稳定）。
        rows = conn.execute(
            text("""
                SELECT b.date::date AS day,
                       b.merchant AS merchant,
                       b.amount AS amount,
                       b.category AS category
                FROM public.bills AS b
                WHERE b.user_id = :uid
                  AND b.type = :expense
                  AND b.date::date >= :start
                  AND b.date::date <= :end
                ORDER BY b.date::date ASC, b.id ASC
            """),
            {"uid": uid, "expense": EXPENSE_TYPE, "start": start, "end": end},
        ).fetchall()

    # 分组在 Python 侧做：SQL 只能按"原样商户名"分组，做不到 trim + casefold，
    # 而这两个归一化正是避免同一订阅被拆链的关键。
    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        merchant_key = _subscription_merchant_key(row.merchant)
        if merchant_key is None:
            continue
        amount = _subscription_amount_or_none(row.amount)
        if amount is None:
            continue
        # date 列历史上是 TEXT（可空）：NULL 脏行直接跳过，别让后面的日期相减炸掉
        # 整次检测；datetime 先取 .date()，保证链内做减法的是纯日历日。
        day = row.day.date() if isinstance(row.day, datetime) else row.day
        if not isinstance(day, date):
            continue
        grouped.setdefault(merchant_key, []).append(
            (day, row.merchant, amount, _normalize_category_value(row.category))
        )

    subscriptions: list[dict] = []
    for events in grouped.values():
        chains = _subscription_cluster_chains(events)
        if not chains:
            continue
        chain = max(chains, key=len)  # max 返回第一个最大值 → 同长取更早的链
        if len(chain) > SUBSCRIPTION_MAX_CHAIN_OCCURRENCES:
            continue
        merchant = chain[-1][1]
        category = chain[-1][3]
        if category == SUBSCRIPTION_MEAL_CATEGORY and not _subscription_has_keyword(merchant):
            continue  # 周期性吃饭不是订阅：餐饮类必须带订阅关键词才放行

        occurrences = len(chain)
        amounts = [event[2] for event in chain]
        monthly_amount = _money_to_float(sum(amounts) / occurrences)
        # 金额下限：低于 10 元的小额固定扣费不算"隐形订阅"，避免噪声刷屏。
        if monthly_amount < SUBSCRIPTION_MIN_AMOUNT:
            continue
        # 房租/话费/定投/保险：月度固定但明确不是订阅，必须排除。
        if _subscription_is_excluded(merchant):
            continue
        first_day = chain[0][0]
        last_day = chain[-1][0]
        avg_interval = _round_half_up(
            (last_day - first_day).days / (occurrences - 1), 1
        )
        # 预期下次扣费日：平均间隔四舍五入成整数天（30.5 → 31）。
        next_expected = last_day + timedelta(days=int(_round_half_up(avg_interval, 0)))

        # high 置信度的两条独立证据：商户名自带"会员/续费"等字样，或扣费节奏
        # 稳定在 27~33 天且至少观察满 3 次；其余一律 medium，不吹成"确定"。
        high_confidence = _subscription_has_keyword(merchant) or (
            occurrences >= SUBSCRIPTION_HIGH_CONFIDENCE_MIN_OCCURRENCES
            and SUBSCRIPTION_HIGH_CONFIDENCE_INTERVAL_MIN
            <= avg_interval
            <= SUBSCRIPTION_HIGH_CONFIDENCE_INTERVAL_MAX
        )
        subscriptions.append(
            {
                "merchant": merchant,
                "category": category,
                "monthly_amount": monthly_amount,
                "occurrences": occurrences,
                "first_date": first_day.isoformat(),
                "last_date": last_day.isoformat(),
                "avg_interval_days": float(avg_interval),
                "annual_cost": _money_to_float(monthly_amount * 12),
                "next_expected_date": next_expected.isoformat(),
                "confidence": "high" if high_confidence else "medium",
            }
        )

    # 年花费降序、次数降序；再按商户名升序兜底，避免同值条目顺序随数据插入变化。
    subscriptions.sort(
        key=lambda item: (
            -item["annual_cost"],
            -item["occurrences"],
            item["merchant"].casefold(),
        )
    )
    # 只记用户、区间、条数；商户名与金额一律不进日志。
    logging.getLogger(__name__).info(
        "[db] detect_subscriptions user_id=%s months=%s range=%s..%s candidates=%s",
        uid,
        months,
        start,
        end,
        len(subscriptions),
    )
    return subscriptions


# ===========================================================================
# 省钱挑战积分（challenge points）
# ---------------------------------------------------------------------------
# 只读写 public.users 的 challenge_points / last_challenge_week 两列
# （列结构见 migrations/017_users_challenge_points.sql）。本段只**追加**：
# 不改动上面任何既有函数、常量和取数口径，也不在 init_db() 里补列
# （迁移统一走 017，避免"应用启动顺手改 schema"与"迁移脚本"两条路径打架）。
#
# 防刷分（本模块的核心，改之前先读 017 文件头的「为什么用条件 UPDATE」）：
#   · 发放只有**一条**条件 UPDATE：判定（本周是否已领）与写入（加分 + 记周）
#     在同一个行锁里完成，不存在"先查后写"的 TOCTOU 窗口；
#   · 同一用户并发两次请求：后到者等行锁，锁释放后 PostgreSQL 用最新行版本
#     重新检查 WHERE，发现本周已领 → 0 行 → awarded=False（并发也不会多加分）；
#   · 周由应用按 ISO 周计算（'YYYY-Www'），跨周自然放行；同周永远只发一次。
#
# 错误契约（app.py 依赖它，改语义前先看调用点）：
#   · 参数非法（user_id 非正整数、week 形状/周号非法、points 非 int 或越界）
#     → ValueError，且**在开连接之前**就拒绝（非法入参连库都不碰）；
#   · 017 未执行（42703 undefined_column / 42P01 undefined_table）
#     → RuntimeError（中文提示先执行 017）。get **不能**静默返回 0 分 ——
#       那会把"功能没启用"显示成"你还没开始挑战"，用户永远等不到开始；
#   · 用户不存在 → ValueError("用户不存在")（get 与 award 同一条文案）；
#   · 其余数据库异常原样上抛（"连不上库"不能被伪装成"0 分"）。
# 日志只写 user_id / week / 本次发放分值 / awarded，不含任何隐私数据。
# ---------------------------------------------------------------------------

CHALLENGE_MISSING_MESSAGE = (
    "省钱挑战功能未启用：请先执行 migrations/017_users_challenge_points.sql"
)

# ISO 周的形状：4 位年 + 大写 W + 2 位周号（如 2026-W42）。
# 用 fullmatch（而不是 search）保证整串匹配：'2026-W42x' / ' 2026-W42 ' 之外的
# 变体一律拒绝（strip 之后才匹配，所以首尾空白仍被容忍，与 token 的处理一致）。
# ⚠ 光有形状不够：'2026-W99' 也满足这个正则，下面还会用 date.fromisocalendar
#   校验"这一周是否真实存在"（周号 01~53，且该年确实有这一周），
#   以免把不存在的周写进 last_challenge_week、把用户锁在"永远领不到"的状态。
_CHALLENGE_WEEK_PATTERN = re.compile(r"^\d{4}-W\d{2}$")

# 单次发放边界：与 017 文件头、页面提示同口径。1~100 足够覆盖日常打卡/周任务，
# 又能在数据库 INTEGER 与前端显示上保持"一眼可读"；上界同时挡住"错误地按分转元"
# 这类数量级 bug（把 10 分写成 1000 分）。
CHALLENGE_POINTS_MIN = 1
CHALLENGE_POINTS_MAX = 100


def _normalize_challenge_user_id(user_id) -> int:
    """user_id 必须是正整数；非法输入在**开连接之前**抛 ValueError。

    独立实现（不复用 _normalize_budget_user_id）：017 的读写不该依赖 015 的函数，
    将来删改预算模块不会牵连挑战积分。规则与 014/015 完全一致：
    只接受 int / 十进制字符串；float / Decimal 即使值是 1.0 也拒绝 ——
    ``int(1.9)`` 会静默截断成 1，把"写错用户"伪装成"写对用户"比直接报错更危险。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    try:
        uid = int(user_id.strip()) if isinstance(user_id, str) else int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}") from exc
    if uid <= 0:
        raise ValueError(f"user_id 必须是正整数：{user_id!r}")
    return uid


def _normalize_challenge_week(week) -> str:
    """ISO 周校验：必须是 'YYYY-Www' 且该周真实存在；否则 ValueError。

    · 只 strip、不改写大小写：'2026-w42' 属于调用方口径错误，静默改成大写会让
      "同一周"的判定依赖隐式归一化，不如直接拒绝（库里存的一律是大写 W）；
    · '2026-42'（缺 W）、'2026-W4'（少一位）、'2026-W99'（无此周）全部拒绝；
    · date.fromisocalendar 同时校验年份/周号范围与该年是否真有第 53 周，
      比手写 1 <= n <= 53 更严格（例如平年没有 W53）。
    """
    if not isinstance(week, str):
        raise ValueError(f"week 必须是 'YYYY-Www' 形式的字符串：{week!r}")
    value = week.strip()
    if not _CHALLENGE_WEEK_PATTERN.fullmatch(value):
        raise ValueError(f"week 必须是 'YYYY-Www' 形式（如 2026-W42）：{week!r}")
    year, week_number = int(value[:4]), int(value[6:])
    try:
        date.fromisocalendar(year, week_number, 1)
    except ValueError as exc:
        raise ValueError(
            f"week 不是真实存在的 ISO 周（周号 01~53）：{week!r}"
        ) from exc
    return value


def _normalize_challenge_points(points) -> int:
    """本次发放分值：必须是 1~100 的 int；bool / float / 字符串一律 ValueError。

    为什么显式拒绝 bool：Python 里 True 是 int 的子类（True == 1），
    不挡掉就会把 True 静默当成 1 分发放，掩盖调用方的类型错误；
    为什么拒绝 float / 字符串：1.5 会被截断成 1、'10' 会被隐式转成 10，
    两者都是"看起来成功了，实际发的不是调用方以为的数"。
    """
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError(
            f"points 必须是 {CHALLENGE_POINTS_MIN}~{CHALLENGE_POINTS_MAX} 的整数：{points!r}"
        )
    if not CHALLENGE_POINTS_MIN <= points <= CHALLENGE_POINTS_MAX:
        raise ValueError(
            f"points 必须是 {CHALLENGE_POINTS_MIN}~{CHALLENGE_POINTS_MAX} 的整数：{points!r}"
        )
    return points


def get_challenge_state(user_id) -> dict:
    """读取用户的挑战积分状态：``{"points": int, "last_challenge_week": str|None}``。

    契约（页面依赖它，改语义前先看调用点）：
      · ``points`` 是**真 int**（DB 是 INTEGER NOT NULL DEFAULT 0，这里再 int()
        一次，避免驱动把数值包成 Decimal 之类；调用方会直接做加减与显示）；
      · ``last_challenge_week`` 原样返回字符串或 None（None = 从没领过）；
      · 用户不存在 → ValueError("用户不存在")，**不返回 0 分** ——
        否则页面会把"用户被删 / 会话失效"显示成"你还没开始挑战"；
      · 017 未执行（42703 缺列 / 42P01 缺表）→ RuntimeError（提示先执行 017），
        与 award_challenge_points 保持同一条错误契约，**不静默 0 分**；
      · 只读、单条语句、不写库、不做缓存：award 之后下一次读取立刻看到新值。

    SQL 用 ``WHERE id = :uid`` 走 users 主键，不建索引（见 017 文件头）。
    """
    uid = _normalize_challenge_user_id(user_id)

    if engine is None:
        init_db()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT challenge_points, last_challenge_week "
                    "FROM users WHERE id = :uid"
                ),
                {"uid": uid},
            ).fetchone()
    except ProgrammingError as exc:
        if _pg_sqlstate(exc) in ("42703", "42P01"):  # 017 未执行（缺列 / 缺表）
            raise RuntimeError(CHALLENGE_MISSING_MESSAGE) from exc
        raise

    if row is None:
        raise ValueError("用户不存在")

    return {
        # NOT NULL DEFAULT 0 保证非 NULL；`or 0` 只防御驱动返回 None 的极端情况，
        # 且不会把合法的 0 分改错（0 or 0 == 0）。
        "points": int(row.challenge_points or 0),
        "last_challenge_week": row.last_challenge_week,
    }


def award_challenge_points(user_id, week: str, points: int) -> dict:
    """为某用户发放**本周**挑战积分；同周只发一次（幂等，防刷新 / 并发刷分）。

    返回 ``{"awarded": bool, "points": int, "week": str}``：
      · 发放成功    → awarded=True、points=**发放后的新总分**；
      · 本周已领过  → awarded=False、points=当前总分（不报错，页面显示"已领取"）；
      · 用户不存在  → ValueError("用户不存在")；
      · 017 未执行  → RuntimeError（提示先执行 017）。

    参数（全部在**开连接之前**校验，非法入参不占用连接、不开事务）：
      · user_id：正整数（bool / float / 非数字串拒绝，见 _normalize_challenge_user_id）；
      · week：'YYYY-Www' 且真实存在的 ISO 周（见 _normalize_challenge_week）；
      · points：1~100 的 int（bool / float / 字符串拒绝，见 _normalize_challenge_points）。

    实现要点（**不要**改成"先 SELECT 再 UPDATE"）：
      · 单条条件 UPDATE + RETURNING：判定与写入在同一个行锁里完成，
        并发同周请求只有一条会命中，另一条得到 0 行 → awarded=False；
      · ``last_challenge_week IS NULL OR last_challenge_week <> :w`` 才更新：
        三值逻辑下 ``NULL <> :w`` 求值为 NULL（不满足），必须显式放行 NULL；
      · 0 行影响时再做一次只读 SELECT，区分"本周已领"与"用户不存在" ——
        这次 SELECT 发生在 UPDATE **之后**，不可能造成重复发放；
      · 显式 commit：否则连接归还时事务回滚，RETURNING 出来的值只是假象；
        异常路径由 with 块关闭连接自动回滚（加分与记周是一行、同时生效/回滚）。
    """
    uid = _normalize_challenge_user_id(user_id)
    normalized_week = _normalize_challenge_week(week)
    normalized_points = _normalize_challenge_points(points)

    if engine is None:
        init_db()
    try:
        with engine.connect() as conn:
            awarded_row = conn.execute(
                text(
                    "UPDATE users "
                    "SET challenge_points = challenge_points + :p, "
                    "    last_challenge_week = :w "
                    "WHERE id = :uid "
                    "  AND (last_challenge_week IS NULL OR last_challenge_week <> :w) "
                    "RETURNING challenge_points"
                ),
                {"p": normalized_points, "w": normalized_week, "uid": uid},
            ).fetchone()
            if awarded_row is not None:
                total = int(awarded_row[0])
                awarded = True
                conn.commit()
            else:
                # 0 行只可能是"本周已领"或"用户不存在"。只读区分，不写库、不提交。
                current = conn.execute(
                    text("SELECT challenge_points FROM users WHERE id = :uid"),
                    {"uid": uid},
                ).fetchone()
                if current is None:
                    raise ValueError("用户不存在")
                total = int(current[0] or 0)
                awarded = False
    except ProgrammingError as exc:
        if _pg_sqlstate(exc) in ("42703", "42P01"):  # 017 未执行（缺列 / 缺表）
            raise RuntimeError(CHALLENGE_MISSING_MESSAGE) from exc
        raise

    logging.getLogger(__name__).info(
        "[db] award_challenge_points user_id=%s week=%s points=%s awarded=%s",
        uid,
        normalized_week,
        normalized_points,
        awarded,
    )
    return {"awarded": awarded, "points": total, "week": normalized_week}


# ===========================================================================
# 平台账单导入（微信支付 / 支付宝 / 通用 CSV）
# ===========================================================================
# 背景：用户手里的原始账单不是本项目的 5 列 CSV，而是平台自己导出的文件：
#   · 微信支付：「我 → 服务 → 钱包 → 账单 → 常见问题 → 下载账单」得到 CSV，
#     前面十几行是账户 / 时间范围 / 导出时间等说明，中间才是 11 列表头；
#   · 支付宝：「账单 → 开具交易流水证明 → 用于个人对账」得到 CSV，前 4~6 行说明；
#   · 通用：本项目 app.py 一直在用的 date,merchant,amount,payment_method,type。
# 这两类文件对用户来说"就是账单"，但结构并不统一：
#   · 编码：微信/支付宝老版本导出是 GBK/GB18030，新版本是 UTF-8(BOM)；
#   · 前导说明行、分隔行、末尾汇总行都不是数据；
#   · 列名全角/半角括号、首尾空格、BOM 都可能不同。
# 所以本段做三件事：① 猜编码并解码；② 定位表头、识别平台；③ 逐行清洗成
# bills 需要的 6 个字段（date/merchant/amount/payment_method/type/category）。
# 设计红线（与 import_csv_to_bills 一致）：
#   · 脏行只跳过自己，带**文件原始行号**与中文原因，绝不让整份账单失败；
#   · 金额/日期/类型全部复用既有归一化函数，口径与手工记账、CSV 导入完全一致；
#   · 分类只走规则（auto_categorize 不传 api_key）：批量导入几百行逐行问 AI
#     又慢又贵，规则未命中落 "其他"，仍可在页面上人工改。
# ---------------------------------------------------------------------------

PLATFORM_BILL_ENCODINGS = ("utf-8-sig", "gb18030", "gbk", "big5")

PLATFORM_ENCODING_ERROR = (
    "无法识别文件编码（已尝试 UTF-8 / GB18030 / GBK / Big5）："
    "请上传微信/支付宝导出的 CSV 文件"
)

PLATFORM_FORMAT_ERROR = "无法识别的账单格式：请上传微信/支付宝导出的 CSV 文件"

# 一个"像账单"的文本至少要命中其中一个标记。用途只有一个：当同一串字节能被
# 多种编码解开时（Big5 的中文字节恰好也是合法的 GB18030 序列），挑出真正的那一种。
# 这个判据**不参与**平台识别，只参与"选编码"，误命中不会改变平台判断。
_PLATFORM_TEXT_MARKERS = (
    "微信支付", "支付宝", "交易时间", "交易创建时间", "交易对方",
    "金额(元)", "金额（元）", "收/支", "商品名称",
)

# 表头里可能出现的列名（已归一：去空白、全角括号转半角、casefold）。
# 说明行即使带逗号，也很少同时出现 2 个以上这样的列名 → 用"≥2 个"当表头候选，
# 而不是一上来就要求完整签名（有些平台的导出会少列，完整签名会认不出）。
_PLATFORM_HEADER_KEY_COLUMNS = frozenset({
    "交易时间", "交易创建时间", "付款时间", "交易对方", "金额(元)", "金额",
    "收/支", "收支", "商品", "商品名称", "支付方式", "交易单号", "商户单号",
    "交易号", "商家订单号", "交易类型", "当前状态", "交易状态", "资金状态",
    "date", "merchant", "amount", "payment_method", "type",
})

# 表头之后仍可能出现的结构行/汇总行。这类行**静默跳过**，不写 skipped ——
# 用户关心的是"哪笔账没导进来"，而不是"文件末尾那行总计也跳过了"。
# 这也是 V3 里 skipped 恰好等于脏数据行数的原因。
_PLATFORM_STRUCTURE_ROW_MARKERS = (
    "起始时间", "终止时间", "导出时间", "账单明细", "交易记录明细",
)
_PLATFORM_TOTAL_ROW_PATTERN = re.compile(r"^共\s*\d+\s*笔", re.ASCII)

# 金额：可选正负号 + 十进制；清洗掉 ¥/￥/千分位逗号/空格/尾缀"元"后再整串匹配。
_PLATFORM_AMOUNT_PATTERN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.ASCII)

# 日期：三种平台常见写法，允许后面跟时间（空格或 T 分隔）。
_PLATFORM_DATE_PATTERNS = (
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", re.ASCII),
    re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:[ T].*)?", re.ASCII),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日?(?:\s*.*)?", re.ASCII),
)

# 平台 → 日期列别名（按优先级）。微信的日期列叫"交易时间"，支付宝叫"交易创建
# 时间"；通用 CSV 叫 date。后面的别名是宽容兜底（少数导出版本会改名）。
_PLATFORM_DATE_ALIASES = {
    "wechat": ("交易时间", "交易创建时间", "付款时间", "date"),
    "alipay": ("交易创建时间", "交易时间", "付款时间", "date"),
    "generic": ("date", "交易时间", "交易创建时间"),
}

# 其余列的平台无关别名（表头归一后的大小写/括号差异已在匹配时吃掉）。
_PLATFORM_COLUMN_ALIASES = {
    "merchant": ("交易对方", "merchant", "交易对方名称", "对方"),
    "product": ("商品", "商品名称", "product"),
    "amount": ("金额(元)", "金额", "amount"),
    "direction": ("收/支", "收支", "type", "direction"),
    "payment": ("支付方式", "payment_method", "付款方式"),
}


def _platform_format_error(filename=""):
    """拼格式错误文案：带上文件名（有的话），核心句保持不变，便于页面直接展示。"""
    if filename:
        return f"{PLATFORM_FORMAT_ERROR}（文件：{filename}）"
    return PLATFORM_FORMAT_ERROR


def _normalize_platform_header_name(value):
    """把表头/列名收敛成可比较的形式：去 BOM、去所有空白、全角括号转半角、casefold。

    为什么去**所有**空白：微信导出的列名可能写成 "金额 (元)"、" 交易对方"；
    支付宝写成 "金额（元）"。不归一，同一份文件换台机器/换次版本就认不出表头。
    casefold 是给通用 CSV 的英文列名（DATE / Date / date）用的。
    """
    text = "" if value is None else str(value)
    text = text.replace("\ufeff", "").strip()
    text = text.replace("（", "(").replace("）", ")")
    # 中文逗号只作列名分隔符出现，按半角统一（只用于表头，不影响数据值）
    text = text.replace("，", ",")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


def _platform_header_signature(names):
    """按平台导出文件的完整表头签名识别平台；认不出返回 None。

    签名来自真实导出文件：
      微信：交易时间, 交易类型, 交易对方, 商品, 收/支, 金额(元), 支付方式, ...
      支付宝：交易号, ..., 交易创建时间, ..., 交易对方, 商品名称, 金额（元）, 收/支, ...
      通用：date, merchant, amount, payment_method, type
    金额列允许写成 "金额"（少数导出版本不带单位），全角括号已被归一成半角。
    """
    name_set = {name for name in names if name}
    if {"date", "merchant", "amount", "payment_method", "type"} <= name_set:
        return "generic"
    has_amount = bool(name_set & {"金额(元)", "金额"})
    if has_amount and {"交易时间", "交易对方", "收/支"} <= name_set:
        return "wechat"
    if has_amount and {"交易创建时间", "交易对方", "收/支"} <= name_set:
        return "alipay"
    return None


def _platform_header_key_count(names):
    """表头候选打分：一行里出现多少个"账单关键列名"。

    说明行（"起始时间：[...] 终止时间：[...]"）通常一个都不命中；
    真正表头至少命中 4 个。用 >= 2 当候选，是为了兜住"平台导出少了某几列"
    的变体 —— 完整签名认不出时还能靠文本里的平台名 + 部分列名继续解析。
    """
    seen = set()
    for name in names:
        if name and name in _PLATFORM_HEADER_KEY_COLUMNS:
            seen.add(name)
    return len(seen)


def _platform_from_header_or_text(text, names):
    """完整签名认不出时，用文件里的平台名 / 部分列名推断平台。"""
    name_set = {name for name in names if name}
    has_wechat = "微信支付" in text
    has_alipay = "支付宝" in text
    if has_wechat and not has_alipay:
        return "wechat"
    if has_alipay and not has_wechat:
        return "alipay"
    # 两个平台名都出现（例如备注里提到对方平台）时，用列名区分：
    # 支付宝独有"交易创建时间 / 交易号 / 商品名称"，微信独有"交易时间 / 商品"。
    if name_set & {"交易创建时间", "交易号", "商家订单号", "商品名称", "付款时间"}:
        return "alipay"
    if name_set & {"交易时间", "交易类型", "支付方式", "商品", "交易单号", "商户单号"}:
        return "wechat"
    return None


def _looks_like_platform_bill_text(text):
    """粗判一段文本像不像账单（只用于多编码都能解码时挑哪一种）。"""
    return any(marker in text for marker in _PLATFORM_TEXT_MARKERS)


def _decode_platform_bill(file_bytes):
    """把上传的字节解码成文本，返回 (encoding, text)。

    顺序是 utf-8-sig → gb18030 → gbk → big5（gb18030 是 gbk 的超集，必须排在
    gbk 前面，否则 GB18030 里的 4 字节汉字会被 gbk 判成非法）。但"能解码"不等于
    "解对了"：Big5 的中文字节往往也是合法的 GB18030 序列。所以这里先收集所有能
    解码成功的候选，优先选**文本看起来像账单**的那一个（含平台名/关键列名）。
    全部解不出来 → ValueError（中文文案）。

    返回的 encoding 是给用户核对导出编码用的：如果 GB18030 解出来的内容同样能被
    GBK 无损解开（说明内容本来就落在 GBK 子集里），回报更具体的 "gbk"；两者文本
    逐字相同，解析结果不受影响（V2 的 GBK 账单断言"encoding=gbk"正依赖这一点）。
    """
    if isinstance(file_bytes, bytearray):
        file_bytes = bytes(file_bytes)
    if not isinstance(file_bytes, bytes):
        raise ValueError(f"账单文件必须是 bytes（二进制内容）：{type(file_bytes).__name__}")
    if not file_bytes:
        raise ValueError("账单文件为空：请重新从微信/支付宝导出后再上传")

    decoded = []
    for encoding in PLATFORM_BILL_ENCODINGS:
        try:
            decoded.append((encoding, file_bytes.decode(encoding)))
        except (UnicodeDecodeError, LookupError):
            continue
    if not decoded:
        raise ValueError(PLATFORM_ENCODING_ERROR)

    encoding, text = next(
        ((enc, content) for enc, content in decoded if _looks_like_platform_bill_text(content)),
        decoded[0],
    )
    text = text.lstrip("\ufeff")
    if encoding == "gb18030":
        try:
            if file_bytes.decode("gbk") == text:
                encoding = "gbk"
        except UnicodeDecodeError:
            pass
    return encoding, text


def _iter_platform_csv_rows(text):
    """逐行产出 (原始行号, 字段列表)，行号是**文件里的物理行号**（1-based）。

    为什么不用 pandas.read_csv(skiprows=N)：说明行数并不固定（微信 14~18 行、
    支付宝 4~6 行，不同年份/版本不一样），写死 skiprows 必定错位；而且平台文件
    里字段可能带逗号/引号，必须先按 CSV 规则切分，再逐行找表头。
    csv.reader.line_num 给的是物理行号，带引号的多行字段也能对得上。
    """
    import csv
    import io
    import re
    # 就地 import：csv / io / re 只有平台账单解析才用得到，放在函数里可以保持本次改动
    # 完全"只追加"，不动文件头的既有 import 区。

    # 平台账单偶尔把金额写成 ￥1,018.50 且**不加引号**：csv.reader 会把这个逗号当成
    # 字段分隔符，导致「金额=1.0、支付方式=018.50」这类错位（实测微信导出会遇到）。
    # 解析前去掉「数字,三位数字」之间的千分位逗号：引号内普通逗号的左右不是数字，
    # 不会被误伤；日期用 - /，也不会命中。
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)

    reader = csv.reader(io.StringIO(text, newline=""))
    previous_line = 0
    try:
        for fields in reader:
            start_line = previous_line + 1
            previous_line = reader.line_num
            yield start_line, fields
    except csv.Error as exc:
        # 解出来的文本不是合法 CSV（含 NUL、引号不闭合等）：按"格式不认识"处理
        raise ValueError(f"{PLATFORM_FORMAT_ERROR}（文件不是合法的 CSV）") from exc


def _platform_cell(cells, position):
    """取一行里某一列的值；列不存在/越界一律当空串（不抛 IndexError）。"""
    if position is None:
        return ""
    if 0 <= position < len(cells):
        return cells[position]
    return ""


def _clip_platform_text(value, limit):
    """去掉空白 / NaN / NUL 并按列宽截断（merchant VARCHAR(100)、payment_method VARCHAR(50)）。

    截断而不是报错：平台账单的商品名/商户名可能超过 100 字符，整批因此回滚的代价
    远大于丢掉尾部几个字；分类与金额都不受影响。
    """
    if _is_missing_value(value):
        return ""
    text = str(value).replace("\x00", "").strip()
    return text[:limit]


def _parse_platform_bill_date(raw):
    """把平台日期收敛成严格 ISO 'YYYY-MM-DD'，并校验真实日历日。

    支持：2026-10-01 12:30:00 / 2026/10/01 / 2026年10月1日（可带时间）。
    非法（含 2026-02-30、2026-13-01 这种"格式对、日子不存在"）→ ValueError。
    返回 ISO 串而不是 datetime：bills.date 是字符串列，读取侧靠 ISO 字典序排序。
    """
    if _is_missing_value(raw):
        raise ValueError("账单日期不能为空")
    text = str(raw).strip().replace("\u3000", " ")
    for pattern in _PLATFORM_DATE_PATTERNS:
        matched = pattern.fullmatch(text)
        if not matched:
            continue
        year, month, day = (int(part) for part in matched.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError as exc:
            raise ValueError("日期不合法") from exc
    raise ValueError("日期不合法")


def _parse_platform_bill_amount(raw):
    """把平台金额字符串转成带符号的 float（不做 >0 / 上界判断，交给调用方）。

    清洗：¥ / ￥ / $ / 千分位逗号 / 全角空格 / 首尾空白 / 尾缀"元"。
    保留符号：收/支列缺失时，负号是唯一能判断"支出"的信号。
    """
    if _is_missing_value(raw):
        raise ValueError("账单金额不能为空")
    text = str(raw).strip()
    for token in ("¥", "￥", "$", "＄", ",", "，", "\u00a0", "\u3000", " "):
        text = text.replace(token, "")
    if text.endswith("元"):
        text = text[:-1].strip()
    if not _PLATFORM_AMOUNT_PATTERN.fullmatch(text):
        raise ValueError(f"账单金额非法：{raw!r}")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"账单金额非法：{raw!r}")
    return value


def _resolve_platform_columns(platform, names):
    """把表头列名映射成 canonical 列 → 列下标；找不到的置 None。"""
    index = {}
    for position, name in enumerate(names):
        if name and name not in index:
            index[name] = position

    def pick(aliases):
        for alias in aliases:
            key = _normalize_platform_header_name(alias)
            if key in index:
                return index[key]
        return None

    date_aliases = _PLATFORM_DATE_ALIASES.get(platform, _PLATFORM_DATE_ALIASES["generic"])
    return {
        "date": pick(date_aliases),
        "merchant": pick(_PLATFORM_COLUMN_ALIASES["merchant"]),
        "product": pick(_PLATFORM_COLUMN_ALIASES["product"]),
        "amount": pick(_PLATFORM_COLUMN_ALIASES["amount"]),
        "direction": pick(_PLATFORM_COLUMN_ALIASES["direction"]),
        "payment": pick(_PLATFORM_COLUMN_ALIASES["payment"]),
    }


def _platform_missing_columns(columns):
    """返回缺失的关键列中文名（日期/金额/交易对方或商品），空列表 = 能解析。"""
    missing = []
    if columns.get("date") is None:
        missing.append("日期")
    if columns.get("amount") is None:
        missing.append("金额")
    if columns.get("merchant") is None and columns.get("product") is None:
        missing.append("交易对方/商品")
    return missing


def _parse_platform_row(cells, platform, columns):
    """清洗一行数据，返回 6 字段 dict；任何脏数据抛 ValueError（中文原因）。

    行号/平台/列映射都与"数据内容"无关，所以由 parse_platform_bill 负责；
    本函数只面对"这一行的值对不对"，方便单测逐条构造。
    """
    try:
        bill_date = _parse_platform_bill_date(_platform_cell(cells, columns.get("date")))
    except ValueError as exc:
        raise ValueError("日期不合法") from exc

    # 商户：优先"交易对方"，空则退回"商品 / 商品名称"；两边都空就不能算一笔账。
    merchant = _clip_platform_text(_platform_cell(cells, columns.get("merchant")), 100)
    if not merchant:
        merchant = _clip_platform_text(_platform_cell(cells, columns.get("product")), 100)
    if not merchant:
        raise ValueError("缺少商户")

    payment = _clip_platform_text(_platform_cell(cells, columns.get("payment")), 50)
    if not payment:
        # 支付宝导出没有"支付方式"列：资金来源本来就是这个平台
        payment = "支付宝" if platform == "alipay" else "未知"

    raw_amount = _platform_cell(cells, columns.get("amount"))
    try:
        signed_amount = _parse_platform_bill_amount(raw_amount)
    except ValueError as exc:
        raise ValueError("金额不合法") from exc

    direction_position = columns.get("direction")
    if direction_position is not None:
        # 收/支 列存在：只认 收入 / 支出（复用既有别名表），
        # "不计收支 / 中性交易 / 空 / 其他"一律跳过 —— 它们不是真正的收支。
        try:
            bill_type = normalize_bill_type(
                _platform_cell(cells, direction_position), strict=True
            )
        except ValueError as exc:
            raise ValueError("不计收支") from exc
        if signed_amount <= 0:
            raise ValueError("金额必须大于 0")
        amount_value = signed_amount
    else:
        # 收/支 列缺失（银行流水式文件）：负号 → 支出；显式正号 → 收入；
        # 无符号正数无法判断方向 → 跳过。宁可少导，绝不把支出猜成收入（会虚增结余）。
        if signed_amount < 0:
            bill_type = EXPENSE_TYPE
            amount_value = abs(signed_amount)
        elif str(raw_amount).strip().startswith("+"):
            bill_type = INCOME_TYPE
            amount_value = signed_amount
        else:
            raise ValueError("无法判断收支（缺少收/支列）")

    try:
        # 与 add_bill / import_csv_to_bills 同一套口径：符号收敛 + 两位小数 + 上界校验
        # （上界就是 MAX_ABS_AMOUNT = 9999999999.99，与 NUMERIC(12,2) 一致）。
        amount_value = _normalize_amount(amount_value, bill_type)
    except ValueError as exc:
        raise ValueError("金额超出上限") from exc

    # 分类只走规则：api_key=None → 不逐行调 AI。几百行的批量导入靠网络会极慢，
    # 规则未命中落 "其他"，页面仍可人工改。
    category = auto_categorize(merchant, "", api_key=None)

    return {
        "date": bill_date,
        "merchant": merchant,
        "amount": amount_value,
        "payment_method": payment,
        "type": bill_type,
        "category": category,
    }


def parse_platform_bill(file_bytes: bytes, filename: str = "") -> dict:
    """解析微信 / 支付宝 / 通用 CSV 账单，**只读不写库**，逐行容错。

    参数：
      · file_bytes：上传文件的原始字节（str 会直接 ValueError）
      · filename：仅用于错误/日志文案，不参与解析

    返回：
        {"platform": "wechat" | "alipay" | "generic",
         "encoding": "utf-8-sig" / "gb18030" / "gbk" / "big5",
         "rows": [{"date": "YYYY-MM-DD", "merchant": str, "amount": float,
                   "payment_method": str, "type": "支出" | "收入",
                   "category": str, "_src_row": 文件原始行号}, ...],
         "skipped": [{"row": 文件原始行号, "reason": "中文原因"}, ...],
         "total": len(rows)}

    解析流程：
      ① 编码自适应：utf-8-sig → gb18030 → gbk → big5（gb18030 在 gbk 前，它是超集）；
         全部失败 → ValueError（"无法识别文件编码…"）。
      ② 平台识别：优先完整表头签名（微信 11 列 / 支付宝 16 列 / 通用 5 列），
         其次用文件里的平台名 + 部分列名；都认不出 → ValueError（"无法识别的账单格式…"）。
      ③ 定位表头：逐行找**包含关键列名**的那一行，之前的说明行全部跳过；
         支付宝 4~6 行、微信 16 行左右都覆盖，不依赖固定 skiprows。
      ④ 表头之后：空行 / `---` / `=` 分隔行 / "总计 / 共 N 笔 / 起始时间 / 终止时间 /
         导出时间 / 账单明细"等说明汇总行**静默跳过**（不进 skipped，因为用户只关心
         哪笔账没导进来）；只有"有日期、但字段不合法"的行才进 skipped。
      ⑤ 逐行清洗：
         · 日期：交易时间(微信) / 交易创建时间(支付宝)，支持 2026-10-01 12:30:00、
           2026/10/01、2026年10月1日；严格校验真实日历日；
         · 商户：交易对方，空则用商品/商品名称；仍为空 → skipped「缺少商户」；
         · 金额：金额(元)/金额（元），去掉 ¥/￥/,/空格，必须 > 0，上界复用
           MAX_ABS_AMOUNT（9999999999.99）；
         · 收支：收/支 列 支出/收入；"不计收支/中性交易/空" → skipped「不计收支」；
           收/支 列整列缺失时才用金额符号（- → 支出，+ → 收入，无符号 → 跳过）；
         · 支付方式：微信取"支付方式"，支付宝无此列 → "支付宝"，空 → "未知"；
         · 分类：auto_categorize(merchant, "", api_key=None)，**只走规则**。
           这里刻意不逐行调 AI：几百行的批量导入逐行请求 DeepSeek 会慢到不可用，
           且规则表已覆盖常见商户；未命中落 "其他"，不阻塞导入。
      ⑥ 行号一律用**文件原始物理行号**（1-based），用户能直接对照 Excel/文本编辑器。

    设计取舍：
      · 每行独立 try：坏行只跳过自己，绝不整份失败；
      · 所有金额/日期/类型都复用 add_bill / import_csv_to_bills 的归一化函数，
        保证"手工记账 / 通用 CSV / 平台账单"三条入口口径完全一致；
      · 不做去重、不碰库：同一份文件解析两次得到相同结果（可单测）。
    """
    encoding, text = _decode_platform_bill(file_bytes)
    records = list(_iter_platform_csv_rows(text))

    header_position = None
    header_names = None
    header_signature = None
    partial_header = None

    for position, (_line_no, fields) in enumerate(records):
        names = [_normalize_platform_header_name(field) for field in fields]
        signature = _platform_header_signature(names)
        if signature is not None:
            header_position = position
            header_names, header_signature = names, signature
            break
        if partial_header is None and _platform_header_key_count(names) >= 2:
            partial_header = (position, names)

    platform = header_signature
    if header_position is None and partial_header is not None:
        position, names = partial_header
        guessed = _platform_from_header_or_text(text, names)
        if guessed is not None:
            header_position = position
            header_names, platform = names, guessed

    if header_position is None or header_names is None or platform is None:
        raise ValueError(_platform_format_error(filename))

    columns = _resolve_platform_columns(platform, header_names)
    missing = _platform_missing_columns(columns)
    if missing:
        raise ValueError(
            f"{_platform_format_error(filename)}（缺少关键列：{'、'.join(missing)}）"
        )

    date_position = columns["date"]
    rows = []
    skipped = []
    for line_no, fields in records[header_position + 1:]:
        cells = ["" if field is None else str(field).strip() for field in fields]
        non_empty = [cell for cell in cells if cell]
        if not non_empty:
            continue
        first = non_empty[0]
        joined = "".join(non_empty)
        # 分隔行 / 汇总行 / 说明行：先按形状挡掉，避免它们被误算成"日期不合法"
        if first.startswith("---") or first.startswith("=") or first.startswith("—"):
            continue
        if first.startswith("总计") or first.startswith("合计"):
            continue
        if _PLATFORM_TOTAL_ROW_PATTERN.match(first):
            continue
        if any(marker in joined for marker in _PLATFORM_STRUCTURE_ROW_MARKERS):
            continue
        # 重复表头（有些文件每页重复一次表头）
        if sum(
            1 for cell in cells
            if _normalize_platform_header_name(cell) in _PLATFORM_HEADER_KEY_COLUMNS
        ) >= 2:
            continue
        if _is_missing_value(_platform_cell(cells, date_position)):
            # 没有日期：空行 / 单列汇总行 / 账户信息（非空单元格 < 3）静默跳过；
            # 但一行明显有数据（>= 3 个非空单元格）却没日期时，仍按脏数据报告，
            # 否则用户永远不知道这几笔账没导进来。
            if len(non_empty) < 3:
                continue
            skipped.append({"row": line_no, "reason": "日期不合法"})
            continue

        try:
            parsed_row = _parse_platform_row(cells, platform, columns)
        except ValueError as exc:
            skipped.append({"row": line_no, "reason": str(exc)})
            continue
        except Exception:
            # 逐行容错兜底：任何意料之外的解析异常都不该让整份账单失败
            logging.getLogger(__name__).warning(
                "[parse_platform_bill] 第 %s 行解析失败（已跳过该行）", line_no, exc_info=True
            )
            skipped.append({"row": line_no, "reason": "行数据无法解析"})
            continue

        parsed_row["_src_row"] = line_no
        rows.append(parsed_row)

    return {
        "platform": platform,
        "encoding": encoding,
        "rows": rows,
        "skipped": skipped,
        "total": len(rows),
    }


def import_platform_bill(user_id, file_bytes: bytes, filename: str = "") -> dict:
    """把平台账单（微信/支付宝/通用 CSV）**追加**导入 bills，逐行容错。

    返回：
        {"platform": ..., "encoding": ...,
         "imported": 实际写入行数,
         "skipped": [{"row": 原始行号, "reason": 中文短句}, ...],
         "total": parse 成功的行数（= parse_platform_bill 的 total）}

    关键行为（与 import_csv_to_bills 的差异，务必先读完）：
    - **追加，不替换**：本函数不会 DELETE 用户已有账单。import_csv_to_bills 的语义是
      "整本账单替换"（内部先 DELETE FROM bills WHERE user_id=...），而平台账单是
      **按自然月分段导出**的：用户这个月导微信、下个月导支付宝，替换语义会把上一份
      账单直接清空。所以这里刻意复用它的**逐行归一化口径**（同一批 _normalize_* /
      auto_categorize / _bills_category_supported），但写库改成纯 INSERT。
    - **不去重**：同一份文件导入两次就真的写两遍；去重由前端负责（app.py 用
      user_id + 文件名 + 大小 + 内容哈希做上传签名）。本函数保持纯粹，便于单测。
    - **不逐行调 AI**：分类只走 auto_categorize 的规则（api_key=None），几百行也
      不会因为网络变慢；规则未命中落 "其他"。
    - **逐行容错**：parse 阶段已剔除的脏行原样合并进 skipped（行号是文件原始行号）；
      写库前再过一遍既有归一化函数做防御校验，单行再有意外只跳过该行。
    - **时区口径**：日期只取文件里的日历日，不做时区换算；平台导出的是用户本地
      时间，跨零点前后的记录以平台文件为准（与 bills.date 是"日历日字符串"一致）。
    - user_id 非法 → ValueError（_normalize_bill_query_user_id）；用户不存在 →
      ValueError("用户不存在")（与 get_challenge_state 同口径）；写库失败 →
      原样上抛（环境问题不能假装成功）。
    """
    uid = _normalize_bill_query_user_id(user_id)
    parsed = parse_platform_bill(file_bytes, filename)
    rows = parsed["rows"]
    skipped = [dict(item) for item in parsed["skipped"]]
    result = {
        "platform": parsed["platform"],
        "encoding": parsed["encoding"],
        "imported": 0,
        "skipped": skipped,
        "total": parsed["total"],
    }
    if engine is None:
        init_db()
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM users WHERE id = :uid"), {"uid": uid}
        ).scalar()
    if exists is None:
        # 与 get_challenge_state / award_challenge_points 同一错误契约：
        # 不能靠外键报错让页面看到 psycopg2 栈，也不能静默写成"0 行成功"。
        raise ValueError("用户不存在")

    if not rows:
        return result

    prepared = []
    for position, row in enumerate(rows):
        src_row = row.get("_src_row") or position + 1
        try:
            bill_date = _normalize_bill_date(row.get("date"))
        except ValueError:
            skipped.append({"row": src_row, "reason": "日期不合法"})
            continue
        try:
            bill_type = normalize_bill_type(row.get("type"), strict=True)
        except ValueError:
            skipped.append({"row": src_row, "reason": "收支类型非法"})
            continue
        try:
            amount = _normalize_amount(row.get("amount"), bill_type)
        except ValueError:
            skipped.append({"row": src_row, "reason": "金额不合法"})
            continue
        merchant = _clip_platform_text(row.get("merchant"), 100)
        if not merchant:
            skipped.append({"row": src_row, "reason": "缺少商户"})
            continue
        payment = _clip_platform_text(row.get("payment_method"), 50) or "未知"
        category = row.get("category")
        if not (isinstance(category, str) and category.strip() in BILL_CATEGORIES):
            category = auto_categorize(merchant, "", api_key=None)
        prepared.append({
            "dt": bill_date,
            "mer": merchant,
            "amt": amount,
            "pm": payment,
            "tp": bill_type,
            "cat": category.strip() if isinstance(category, str) else FALLBACK_CATEGORY,
        })

    if not prepared:
        return result

    write_category = _bills_category_supported()
    try:
        with engine.connect() as conn:
            if write_category:
                stmt = text("""
                    INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category)
                    VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat)
                """)
                params = [{"uid": uid, **item} for item in prepared]
            else:
                # 012 未执行：去掉 category 再写（降级，导入优先）
                stmt = text("""
                    INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                    VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
                """)
                params = [
                    {
                        "uid": uid,
                        "dt": item["dt"],
                        "mer": item["mer"],
                        "amt": item["amt"],
                        "pm": item["pm"],
                        "tp": item["tp"],
                    }
                    for item in prepared
                ]
            # 追加语义：纯 INSERT，不 DELETE 既有账单
            conn.execute(stmt, params)
            conn.commit()
    except Exception as exc:
        print(
            f"[import_platform_bill] 写库失败，本次导入已整体回滚（原有账单未被改动）：{exc}"
        )
        raise

    result["imported"] = len(prepared)
    return result


# ===========================================================================
# 负债识别与还款预测（只读；口径是「历史月均外推」的**估算**）
# ---------------------------------------------------------------------------
# 输入只有「关键词 + 月份窗口」，输出一份还款压力预测。设计边界：
#   · **只读**：一条 SELECT，不写库、不建表、不改任何既有函数口径；
#   · **关键词可配**：LIABILITY_KEYWORDS 是模块级常量，命中 merchant 或
#     payment_method 任一列即算负债（花呗/白条可能写在商户名，也可能只写在
#     支付方式里）；关键词只作为**绑定参数**（"%花呗%"）传入，SQL 片段里不含
#     任何用户可控字符串，注入面为零；
#   · **估算不是账单**：monthly_avg 是"过去 N 个自然月里，有匹配的月份的
#     平均还款额"，forecast 按这个均值外推。它不代表真实账单金额 ——
#     一次性大额、分期手续费、提前还款、这个月还没出账都算不进去，页面必须
#     标"估算"；
#   · **无匹配不抛错**：命中 0 行 → 全 0 / 空列表（has_liability=False），
#     调用方不需要写分支；参数非法才 ValueError（且都在开连接之前）；
#   · SQLAlchemy text() + 全部参数绑定，单条 SQL 取数，分组交给 Python（避免
#     N+1，也避免把"自然月"口径绑死到某个数据库方言的 date_trunc 上）。
# ===========================================================================

LIABILITY_KEYWORDS = ("花呗", "白条", "信用卡", "分期", "借呗", "月付")

LIABILITY_MONTHS_MIN = 1
LIABILITY_MONTHS_MAX = 12
LIABILITY_MATCHED_MERCHANTS_MAX = 10
LIABILITY_UNKNOWN_MERCHANT = "未知商户"


def _normalize_liability_months(value, label: str) -> int:
    """months_ahead / lookback_months：必须是 1~12 的 int。

    bool 是 int 子类（True 会被当成 1、False 当成 0）；float / 字符串一律拒绝，
    不做 `int(2.9)` 这种静默截断 —— 把"写错窗口"伪装成"写对窗口"比直接报错更危险。
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not LIABILITY_MONTHS_MIN <= value <= LIABILITY_MONTHS_MAX
    ):
        raise ValueError(f"{label}必须是 1~{LIABILITY_MONTHS_MAX} 的整数：{value!r}")
    return value


def _liability_match_clause() -> str:
    """生成「商户或支付方式命中任一关键词」的 SQL 片段。

    列名（merchant / payment_method）与占位符名（:liability_kw_0 …）都是本函数的
    常量，关键词只以绑定参数（"%花呗%"）下传，不存在 f-string 注入面；同一关键词
    在 merchant / payment_method 两处**复用同一个占位符**，参数个数恒为
    len(LIABILITY_KEYWORDS)，不随用户数据变化。COALESCE 把 NULL 归一成空串，
    让 NULL 列参与 ILIKE 时得到确定的 False 而不是 SQL 三值逻辑的 NULL。
    """
    patterns = [
        f"(COALESCE(b.merchant, '') ILIKE :liability_kw_{i}"
        f" OR COALESCE(b.payment_method, '') ILIKE :liability_kw_{i})"
        for i in range(len(LIABILITY_KEYWORDS))
    ]
    return "(" + " OR ".join(patterns) + ")"


def _liability_month_key(day: date) -> str:
    """自然月键 "YYYY-MM"（零填充 → 字典序 == 时间序）。"""
    return f"{day.year:04d}-{day.month:02d}"


def _liability_month_shift(day: date, months: int) -> date:
    """把 day 所在月平移 months 个月，返回那一个月 1 号。

    与 get_daily_expense_totals / detect_subscriptions 同一套「月份序号」算法：
    序号 = year * 12 + month - 1，负数由 Python 的 floor 除 / 模自然处理，跨年
    （2026-01 往后 1 个月 → 2026-02，往前 1 个月 → 2025-12）不必写分支。
    """
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def forecast_repayments(user_id, months_ahead: int = 3, lookback_months: int = 6,
                        today=None) -> dict:
    """按历史还款记录估算未来 N 个月的还款额（**估算**，不是真实账单金额）。

    口径（与产品确认过的「还款压力预警」一致）
    ----------------------------------------
    1. 窗口 = 今天所在月往前推 lookback_months-1 个月的 1 号 ~ 今天（闭区间）。
       例：today=2026-10-20、lookback_months=6 → 2026-05-01 ~ 2026-10-20；
    2. 只统计 ``type = '支出'``，且 ``merchant`` 或 ``payment_method`` 命中
       LIABILITY_KEYWORDS 任一关键词（ILIKE，大小写不敏感；全角/半角不做归一，
       关键词按业务约定就是中文全角，见常量定义）；
    3. monthly_avg = 有匹配的**月份**的平均值：分母是"出现过匹配行的自然月数"，
       **不是** lookback_months —— 没还款的月份不摊薄均值（否则一个只还过两次款的
       用户会被算成一半金额）。月份是否计入只看有没有匹配行，不看该月金额是否为 0；
    4. forecast[k] = 今天所在月往后第 k+1 个月的还款额 = monthly_avg（两位小数），
       month 是 "YYYY-MM"；**不预测本月剩余天数**（本月还没出账，强行拆日会误导）；
    5. next_month_amount = forecast[0]；total_forecast = 各月金额之和（再定量两位）；
    6. matched_merchants 按商户（strip 后，空→"未知商户"）汇总 count / total，
       按 total 降序、同额按商户名升序（保证结果可复现），最多 10 条；
    7. has_liability = matched_count > 0。无匹配 → 其余字段全 0 / 空列表，**不抛异常**。

    参数校验（全部在**开连接之前**完成；非法入参不浪费一次数据库连接）
    ------------------------------------------------------------
    · user_id 复用 _normalize_bill_query_user_id（正整数，拒绝 bool / float）；
    · months_ahead / lookback_months 必须是 1~12 的 int（bool / float / 字符串拒绝）；
    · today 复用 _normalize_forecast_today（date / datetime / "YYYY-MM-DD"）。

    返回字段（逐字固定，前端按 key 取，顺序无关）
    ------------------------------------------
        has_liability / matched_count / lookback_total / monthly_avg / forecast /
        next_month_amount / total_forecast / matched_merchants

    实现口径
    --------
    · **单条 SQL**：WHERE 里 user_id 是首项，关键词全参数绑定；不在 SQL 里分组，
      月度桶在 Python 侧用 Decimal 累加（避免 float 累加误差），最后由
      _money_to_float 统一定量两位小数（ROUND_HALF_UP，与页面口径一致）；
    · amount 为 NULL 的脏行按 0 计，不影响 matched_count（SQL 确实命中了这一行）；
    · date 列是 TEXT，按 ``b.date::date`` 比较；cast 失败的脏数据让异常原样上抛，
      **不静默漏算**（少算会让预警金额偏低，比报错更危险）；
    · 日志只记 user_id / 窗口 / months_ahead / matched_count，**绝不**记商户名
      或金额明细（商户名可能带个人消费信息）。
    """
    uid = _normalize_bill_query_user_id(user_id)
    ahead = _normalize_liability_months(months_ahead, "months_ahead")
    lookback = _normalize_liability_months(lookback_months, "lookback_months")
    end = _normalize_forecast_today(today)

    # 窗口起点：往前推 lookback-1 个月的 1 号（含今天所在月，共 lookback 个自然月）。
    month_index = (end.year * 12 + end.month - 1) - (lookback - 1)
    start = date(month_index // 12, month_index % 12 + 1, 1)

    if engine is None:
        init_db()

    # 关键词全部走绑定参数；参数名是固定常量，SQL 片段里没有任何用户输入。
    params = {
        "uid": uid,
        "expense": EXPENSE_TYPE,
        "start": start,
        "end": end,
    }
    for index, keyword in enumerate(LIABILITY_KEYWORDS):
        params[f"liability_kw_{index}"] = f"%{keyword}%"

    stmt = text("\n".join([
        "SELECT b.date::date AS day,",
        "       COALESCE(b.merchant, '') AS merchant,",
        "       COALESCE(b.payment_method, '') AS payment_method,",
        "       b.amount AS amount",
        "FROM public.bills AS b",
        "WHERE b.user_id = :uid",
        "  AND b.type = :expense",
        "  AND b.date::date >= :start",
        "  AND b.date::date <= :end",
        f"  AND {_liability_match_clause()}",
        "ORDER BY b.date::date ASC, b.id ASC",
    ]))
    with engine.connect() as conn:
        rows = conn.execute(stmt, params).fetchall()

    matched_count = 0
    lookback_total = Decimal("0")
    monthly_totals: dict[str, Decimal] = {}
    merchant_totals: dict[str, dict] = {}

    for row in rows:
        # date::date 已由库完成 cast；这里只做类型防御（datetime 是 date 子类）。
        day = row.day.date() if isinstance(row.day, datetime) else row.day
        if not isinstance(day, date):
            continue
        matched_count += 1

        amount = _as_decimal(row.amount)  # NULL 按 0 计（_as_decimal 已处理 None）
        lookback_total += amount

        month_key = _liability_month_key(day)
        monthly_totals[month_key] = monthly_totals.get(month_key, Decimal("0")) + amount

        merchant = str(row.merchant or "").strip() or LIABILITY_UNKNOWN_MERCHANT
        bucket = merchant_totals.setdefault(merchant, {"count": 0, "total": Decimal("0")})
        bucket["count"] += 1
        bucket["total"] += amount

    # 月均分母 = 有匹配的月份数（不摊薄没还款的月份）；没有任何匹配 → 0。
    if monthly_totals:
        monthly_avg = _money_to_float(lookback_total / len(monthly_totals))
    else:
        monthly_avg = 0.0

    # 未来 months_ahead 个自然月（从今天所在月的下一个月开始数），每月金额同均值。
    forecast = []
    for offset in range(1, ahead + 1):
        month_start = _liability_month_shift(end, offset)
        forecast.append({
            "month": _liability_month_key(month_start),
            "amount": monthly_avg,
        })

    next_month_amount = forecast[0]["amount"]
    total_forecast = _money_to_float(sum(item["amount"] for item in forecast))

    matched_merchants = [
        {
            "merchant": merchant,
            "count": bucket["count"],
            "total": _money_to_float(bucket["total"]),
        }
        for merchant, bucket in merchant_totals.items()
    ]
    # 主键 total 降序；同额再按商户名升序，保证同一份数据每次输出完全一致。
    matched_merchants.sort(key=lambda item: (-item["total"], item["merchant"]))
    matched_merchants = matched_merchants[:LIABILITY_MATCHED_MERCHANTS_MAX]

    logging.getLogger(__name__).info(
        "[db] forecast_repayments user_id=%s range=%s..%s months_ahead=%s matched=%s",
        uid, start, end, ahead, matched_count,
    )

    return {
        "has_liability": matched_count > 0,
        "matched_count": matched_count,
        "lookback_total": _money_to_float(lookback_total),
        "monthly_avg": monthly_avg,
        "forecast": forecast,
        "next_month_amount": next_month_amount,
        "total_forecast": total_forecast,
        "matched_merchants": matched_merchants,
    }


# ===========================================================================
# 拿铁因子（非必要消费统计）+ 复利定投模拟
# ---------------------------------------------------------------------------
# 本段只**追加**：不改动上面任何既有函数、常量与取数口径。
# 分工：
#   · get_latte_factor(...)       只读 bills（依赖 012 的 category 列）；
#                                  参数校验全部在开连接之前完成；
#   · project_latte_compound(...) 纯函数，不碰 DB、不读全局状态。
# 依赖的既有件（全部在文件上方已定义，这里不重新实现）：
#   _normalize_bill_query_user_id / _normalize_forecast_today / _normalize_category_value
#   / _as_decimal / _money_to_float / EXPENSE_TYPE / _CENT。
# ===========================================================================

# 非必要消费分类（与 BILL_CATEGORIES 里的三个值逐字一致；改这里即可改口径）。
NON_ESSENTIAL_CATEGORIES = ("奶茶咖啡", "娱乐", "购物")
LATTE_MONTHS_MIN = 1
LATTE_MONTHS_MAX = 12
LATTE_YEARS_MIN = 1
LATTE_YEARS_MAX = 60
LATTE_DAYS_PER_MONTH = 30  # 「每天 X 元」折算成月定投额：X × 30（产品口径，不摊 30.44）


def _normalize_latte_months(months) -> int:
    """统计窗口月数必须是 1~12 的 int（bool / float / 字符串一律拒绝，不静默截断）。

    不静默转换的理由与 get_daily_expense_totals 同口径：``int(2.9)`` 截断成 2
    会把"写错区间"伪装成"写对区间"；months=0 则会让窗口起点晚于今天。
    """
    if isinstance(months, bool) or not isinstance(months, int):
        raise ValueError(f"months 必须是 1~{LATTE_MONTHS_MAX} 的整数：{months!r}")
    if not LATTE_MONTHS_MIN <= months <= LATTE_MONTHS_MAX:
        raise ValueError(f"months 必须是 1~{LATTE_MONTHS_MAX} 的整数：{months!r}")
    return months


def _latte_category_clause(params: dict) -> str:
    """把 NON_ESSENTIAL_CATEGORIES 展开成逐个 OR 的 SQL 片段，值全部走绑定参数。

    为什么不用字符串拼 IN：值必须参数绑定，注入面为零；为什么不用 expanding
    bindparam：那要在模块顶部 import bindparam，而本段按约定只追加、不动既有 import。
    返回的片段只由**常量元组的下标**生成（:latte_cat_0/1/2），不含任何调用方输入；
    同时把三个分类值写进 params，调用方直接拿去 conn.execute(stmt, params)。
    """
    clauses = []
    for index, category in enumerate(NON_ESSENTIAL_CATEGORIES):
        key = f"latte_cat_{index}"
        params[key] = category
        clauses.append(f"b.category = :{key}")
    return "(" + " OR ".join(clauses) + ")"


def get_latte_factor(user_id, months: int = 3, today=None) -> dict:
    """统计某用户近 months 个月的「拿铁因子」：**估算**每天花在非必需品上的平均金额。

    「拿铁因子」= 那些每天不起眼、长期却会滚成大钱的非必要小额消费（一杯奶茶、
    一次冲动购物、一场电影……）。本函数不做归因、不做预测，只回答一个问题：
    "在统计窗口里，我平均每天有多少钱花在了 NON_ESSENTIAL_CATEGORIES 上"。

    窗口口径（闭区间，左闭右闭）
    --------------------------
    · 起点 = 今天所在月往前推 months-1 个月的 1 号；终点 = 今天（含）。
      例：today=2026-10-20、months=3 → 2026-08-01 ~ 2026-10-20；
          months=1 → 2026-10-01 ~ 2026-10-20。
    · 只统计 ``type = '支出'``，收入不计；``category`` 只认
      NON_ESSENTIAL_CATEGORIES 三个值（NULL / "餐饮" / "其他" 全部不计）。
    · days = 窗口内的**自然日天数（含今天）**，用 (end - start).days + 1 算；
      daily_avg = total / days —— 分母是"窗口过了几天"，不是"记了几天账"，
      没记账的日子一样摊薄均值，这符合"日均花在非必需品上多少钱"的直觉。

    参数校验（都在**开连接之前**完成，非法入参不浪费一次连接）
    --------------------------------------------------------
    · user_id 复用 _normalize_bill_query_user_id（正整数，拒绝 bool / float）；
    · months 必须是 1~12 的 int（bool / float / 字符串拒绝，不静默截断）；
    · today 复用 _normalize_forecast_today（date / datetime / "YYYY-MM-DD"）。

    返回（逐字固定，前端按 key 取，顺序无关）
    ----------------------------------------
        daily_avg  float  total / days，两位小数
        total      float  命中金额合计，两位小数
        days       int    窗口自然日天数（含今天）
        months     int    回显校验后的统计月数
        breakdown  list   [{"category": str, "amount": float}]，金额降序；
                          同额按分类名升序，保证同一份数据每次输出完全一致

    边界与异常
    ----------
    · 用户不存在 / 窗口内没有命中 → daily_avg=total=0.0、breakdown=[]，
      **不抛异常**（"这个月没喝奶茶"是正常状态，不是错误）；
    · 参数非法 → ValueError；连接 / DB 错误原样上抛（**不**伪装成"没花钱"：
      真出故障时页面显示"你很省钱"比报错更危险）；
    · 金额累加用 Decimal，最后统一 _money_to_float 定量两位小数（ROUND_HALF_UP）；
      breakdown 各分类与 total 同源相加，展示值不会出现"分项之和比 total 多 1 分"；
    · category 列依赖 migrations/012：012 未执行时 SQL 会报 undefined_column，
      异常原样上抛（本函数区分不了"没分类"与"没迁移"，不静默返回 0）；
    · 日志只记 user_id / 区间 / 命中分类数，**绝不**记金额明细。
    """
    uid = _normalize_bill_query_user_id(user_id)
    window_months = _normalize_latte_months(months)
    end = _normalize_forecast_today(today)

    # 与 get_daily_expense_totals / detect_subscriptions 同一套「月份序号」算法：
    # 跨年由 floor 除 / 模自然处理，不写 year == 1 分支。
    month_index = (end.year * 12 + end.month - 1) - (window_months - 1)
    start = date(month_index // 12, month_index % 12 + 1, 1)
    days = (end - start).days + 1  # 闭区间自然日：2026-08-01 ~ 10-20 → 81 天

    if engine is None:
        init_db()

    params = {
        "uid": uid,
        "expense": EXPENSE_TYPE,
        "start": start,
        "end": end,
    }
    stmt = text("\n".join([
        "SELECT b.category AS category, SUM(b.amount) AS total",
        "FROM public.bills AS b",
        "WHERE b.user_id = :uid",
        "  AND b.type = :expense",
        "  AND b.date::date >= :start",
        "  AND b.date::date <= :end",
        f"  AND {_latte_category_clause(params)}",
        "GROUP BY b.category",
    ]))
    with engine.connect() as conn:
        rows = conn.execute(stmt, params).fetchall()

    category_totals: dict[str, Decimal] = {}
    for row in rows:
        category = _normalize_category_value(row.category)
        category_totals[category] = (
            category_totals.get(category, Decimal("0")) + _as_decimal(row.total)
        )

    total_decimal = sum(category_totals.values(), Decimal("0"))
    breakdown = [
        {"category": category, "amount": _money_to_float(amount)}
        for category, amount in category_totals.items()
    ]
    # 金额降序；同额按分类名升序 —— 保证同一份数据每次输出完全一致。
    breakdown.sort(key=lambda item: (-item["amount"], item["category"]))

    logging.getLogger(__name__).info(
        "[db] get_latte_factor user_id=%s range=%s..%s months=%s days=%s categories=%s",
        uid, start, end, window_months, days, len(breakdown),
    )

    return {
        "daily_avg": _money_to_float(total_decimal / Decimal(days)),
        "total": _money_to_float(total_decimal),
        "days": days,
        "months": window_months,
        "breakdown": breakdown,
    }


def _normalize_latte_daily_amount(daily_amount) -> float:
    """daily_amount 收敛成「有限、≥ 0」的 float；bool / 非数字 / nan / inf / 负数 → ValueError。

    0 是合法值（"不喝奶茶"也要能算出一条全 0 的对照线），所以只挡 < 0。
    bool 必须单独挡：True/False 是 int 子类，float(True) == 1.0 会把开关静默当成金额。
    """
    if isinstance(daily_amount, bool):
        raise ValueError(f"daily_amount 非法：{daily_amount!r}（不是数字）")
    try:
        amount = float(daily_amount)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"daily_amount 非法：{daily_amount!r}（不是数字）") from exc
    if not math.isfinite(amount):
        raise ValueError(f"daily_amount 非法：{daily_amount!r}（必须是有限数字，不允许 nan/inf）")
    if amount < 0:
        raise ValueError(f"daily_amount 不能为负数：{daily_amount!r}")
    return amount


def _normalize_latte_rate(annual_rate) -> float:
    """annual_rate 必须是 0~1（含两端）的有限数：0.05 = 5%，0 = 不计息。

    上界 1（100%）不是为了鼓励，而是给"写错小数位"留出明确边界：
    传 5 会被拒（应该是 0.05），而不是被当成 500% 算出一个离谱数字。
    """
    if isinstance(annual_rate, bool):
        raise ValueError(f"annual_rate 非法：{annual_rate!r}（不是数字）")
    try:
        rate = float(annual_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"annual_rate 非法：{annual_rate!r}（不是数字）") from exc
    if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
        raise ValueError(f"annual_rate 必须是 0~1 之间的有限数字：{annual_rate!r}")
    return rate


def _normalize_latte_years(years) -> list[int]:
    """years 收敛成「去重、升序」的 1~60 int 列表；非法 / 空 → ValueError。

    每一项都必须是真正的 int：3.0 会被静默截断成 3，把"写错类型"伪装成"写对"；
    bool 是 int 子类，True 会被当成 1 年，同样拒绝。字符串按字符迭代会把 "13"
    拆成 1 和 3，所以也直接拒绝。空列表没有可投影的年限，按调用方 bug 报错。
    """
    if isinstance(years, (str, bytes)) or years is None:
        raise ValueError(f"years 必须是 1~{LATTE_YEARS_MAX} 的整数序列：{years!r}")
    try:
        items = list(years)
    except TypeError as exc:
        raise ValueError(
            f"years 必须是 1~{LATTE_YEARS_MAX} 的整数序列：{years!r}"
        ) from exc

    cleaned: set[int] = set()
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(
                f"years 每一项必须是 1~{LATTE_YEARS_MAX} 的整数：{item!r}"
            )
        if not LATTE_YEARS_MIN <= item <= LATTE_YEARS_MAX:
            raise ValueError(
                f"years 每一项必须是 1~{LATTE_YEARS_MAX} 的整数：{item!r}"
            )
        cleaned.add(item)
    if not cleaned:
        raise ValueError("years 不能为空：至少给一个 1~60 的年限")
    return sorted(cleaned)


def project_latte_compound(daily_amount, annual_rate: float = 0.05,
                           years=(1, 3, 5, 10)) -> list[dict]:
    """把「每天的非必要消费」按月定投做复利模拟：纯函数，不碰 DB、不读全局状态。

    「拿铁因子」的杀伤力不在当天，而在"这笔钱如果拿去定投，N 年后能变成多少"。
    本函数给出**估算**（不是承诺收益），同一组入参永远得到同一结果，可直接单测。

    模型（按月定投，月末投入）
    ------------------------
    1. monthly = daily_amount × 30（30 天/月是产品口径，不做 30.44 均摊）；
    2. 月利率 r_m = (1 + annual_rate) ** (1/12) - 1 —— 用**有效年利率**折月，
       所以 r_m 复利 12 次后恰好回到 (1 + annual_rate)，不会把 5% 年化算成 5% 月化；
    3. 期数 N = years × 12；
    4. future_value = monthly × (((1 + r_m) ** N - 1) / r_m)（普通年金，月末投入）；
       r_m == 0（annual_rate == 0）时退化为 monthly × N，避免除零；
    5. principal = monthly × N；interest = future_value - principal。

    参数校验
    --------
    · daily_amount：有限且 ≥ 0 的数（bool / nan / inf / 负数 → ValueError；0 合法）；
    · annual_rate：0~1（含两端）的有限数（bool → ValueError）；
    · years：1~60 的 int 序列（bool / float / 字符串 / 空 → ValueError），
      自动**去重 + 升序**，输出顺序与入参顺序无关。
    · 极端超大 daily_amount 让结果溢出 float → ValueError（不返回 inf）。

    返回（每项，金额全部两位小数，ROUND_HALF_UP）
    -------------------------------------------
        {"years": int, "monthly": float, "principal": float,
         "future_value": float, "interest": float}
    interest 由**已定量的** future_value - principal 得到，所以展示值自洽
    （future_value - principal == interest，不会差 1 分钱）。
    """
    daily = _normalize_latte_daily_amount(daily_amount)
    rate = _normalize_latte_rate(annual_rate)
    horizons = _normalize_latte_years(years)

    monthly = daily * LATTE_DAYS_PER_MONTH
    monthly_rate = (1.0 + rate) ** (1.0 / 12.0) - 1.0

    results: list[dict] = []
    for horizon in horizons:
        periods = horizon * 12
        if monthly_rate == 0:
            future_value = monthly * periods
        else:
            future_value = monthly * (((1.0 + monthly_rate) ** periods - 1) / monthly_rate)
        principal = monthly * periods
        if not (math.isfinite(future_value) and math.isfinite(principal)):
            raise ValueError(
                f"复利结果超出浮点范围，请检查 daily_amount：{daily_amount!r}"
            )
        fv = _money_to_float(future_value)
        p = _money_to_float(principal)
        results.append({
            "years": horizon,
            "monthly": _money_to_float(monthly),
            "principal": p,
            "future_value": fv,
            "interest": _money_to_float(Decimal(str(fv)) - Decimal(str(p))),
        })
    return results


# ===========================================================================
# 财务沙盘推演（未来 N 个月余额走势）
# ---------------------------------------------------------------------------
# 本段只**追加**：不改动上面任何既有函数、常量与取数口径。
# 分工：
#   · simulate_sandbox(...)  纯函数：不碰 DB、不读全局状态、不读本月已花金额，
#                            只做「每月净变化」的加减推演（可直接单测）；
# 依赖的既有件（全部在文件上方已定义，这里不重新实现）：
#   _normalize_forecast_today（today 收敛成 date）/ _money_to_float（两位小数）
#   / _liability_month_shift（月份序号平移）/ _liability_month_key（"YYYY-MM"）。
# ===========================================================================

SANDBOX_MONTHS_MIN = 1
SANDBOX_MONTHS_MAX = 24


def _normalize_sandbox_amount(value, label: str, *, allow_negative: bool = False) -> float:
    """把沙盘的四个金额收敛成「有限」的 float；基线 / 额外 / 削减还要求 ≥ 0。

    与 _normalize_forecast_amount 的唯一区别：initial_balance 允许为负
    （负值表示**已经**透支，这是真实状态而不是非法输入），其余三个金额
    （基线月支出 / 额外支出 / 削减支出）仍必须 ≥ 0 —— 负的"基线支出"会让
    净变化方向整体反过来，属于调用方 bug，不静默接受。
    拒绝顺序：① bool（True == 1.0 会把"开关"静默当成金额）；
             ② 数字串 / bytes（不静默 float("100")，类型错就是错）；
             ③ 不能转 float（None / 容器）；
             ④ nan / inf（会污染整条余额曲线）；
             ⑤ 不允许负时传负数。
    """
    if isinstance(value, bool):
        raise ValueError(f"{label}非法：{value!r}（不是数字）")
    if isinstance(value, (str, bytes)):
        # 与 _normalize_forecast_amount 的差别：沙盘是**金额参数**而非表单回填，
        # "100" 这种数字串一旦被 float() 接受，就把"类型写错"伪装成"写对"。
        raise ValueError(f"{label}非法：{value!r}（不是数字）")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}非法：{value!r}（不是数字）") from exc
    if not math.isfinite(amount):
        raise ValueError(f"{label}非法：{value!r}（必须是有限数字，不允许 nan/inf）")
    if amount < 0 and not allow_negative:
        raise ValueError(f"{label}不能为负数：{value!r}")
    return amount


def _normalize_sandbox_months(months) -> int:
    """推演月数必须是 1~24 的 int（bool / float / 字符串一律拒绝，不静默截断）。

    不静默转换的理由与 _normalize_forecast_days 同口径：``int(2.5)`` 截断成 2
    会把"写错区间"伪装成"写对区间"；上界 24 对应"最多看两年"的产品口径，
    months=0 则推演结果为空、min() 直接报错。
    """
    if isinstance(months, bool) or not isinstance(months, int):
        raise ValueError(f"months 必须是 1~{SANDBOX_MONTHS_MAX} 的整数：{months!r}")
    if not SANDBOX_MONTHS_MIN <= months <= SANDBOX_MONTHS_MAX:
        raise ValueError(f"months 必须是 1~{SANDBOX_MONTHS_MAX} 的整数：{months!r}")
    return months


def simulate_sandbox(initial_balance, monthly_base_expense,
                     extra_expense=0.0, cut_expense=0.0,
                     months: int = 6, today=None) -> dict:
    """模拟「按当前收支节奏，未来 N 个月每个月末还能剩多少钱」：纯函数，不碰 DB。

    基线月支出由调用方决定（预算或日均×30），本函数只做加减推演：
    它不读本月已花金额、不读预算表、也不关心钱花在哪一类，只回答
    "每月固定净变化 net_change 连续作用 N 个月，余额曲线长什么样"。

    模型（每月月末结算；月份从今天所在月的**下一个月**开始，共 months 个）
    ---------------------------------------------------------------------
      net_change = cut_expense - extra_expense - monthly_base_expense
      balance_i  = balance_{i-1} + net_change          （i = 1..months）
      balance_0  = initial_balance

    参数
    ----
    initial_balance     : 起始余额（允许为负：负值表示**已经**透支）。
    monthly_base_expense: 每月基线支出（预算，或日均×30；≥ 0）。
    extra_expense       : 每月额外支出（≥ 0，默认 0）。
    cut_expense         : 每月削减掉的支出（≥ 0，默认 0）。
    months              : 推演月数，1~24 的 int（bool / float / 字符串拒绝）。
    today               : date / datetime / "YYYY-MM-DD"；None → 今天。
                          复用 _normalize_forecast_today，**只**用于确定月份标签起点。

    返回（逐字固定，前端按 key 取，顺序无关）
    ----------------------------------------
      months        : [{"month": "YYYY-MM", "balance": float,
                        "net_change": float, "below_zero": bool}, ...]
      broke_month   : 第一次 balance < 0（**严格小于 0**）的月份，没有则 None
      broke_index   : 对应的第几个月（1-based），没有则 None
      min_balance   : 各月月末余额的最小值（只统计推演出的月份，不含初始余额）
      final_balance : 最后一个月月末余额
      initial_balance / monthly_base_expense / extra_expense / cut_expense
                    : 定量到两位小数后的入参回显
      months_count  : 推演月数 months

    口径与边界
    ----------
    · 金额全部 round2（_money_to_float，ROUND_HALF_UP）：net_change 先定量，
      之后每个月的 balance 都基于**已定量**的上月余额相加，所以返回字典里
      "本月余额 - 上月余额 == net_change" 严格成立，前端再算也不会差一分钱；
    · 余额 0 不算破产（严格 < 0 才是 broke），"刚好月光"不会被误报；
    · 初始余额已经 < 0、且第 1 个月仍然 < 0 → broke_index=1、broke_month=第 1 个月；
    · net_change 为正（削减 > 基线 + 额外）→ 余额逐月上升，broke_month=None；
    · months=1 正常推演一个月；非法入参在计算任何字段之前抛 ValueError，
      绝不返回"半个结果"。
    """
    initial = _normalize_sandbox_amount(
        initial_balance, "初始结余 initial_balance", allow_negative=True
    )
    base = _normalize_sandbox_amount(
        monthly_base_expense, "每月基线支出 monthly_base_expense"
    )
    extra = _normalize_sandbox_amount(extra_expense, "额外支出 extra_expense")
    cut = _normalize_sandbox_amount(cut_expense, "削减支出 cut_expense")
    count = _normalize_sandbox_months(months)
    start = _normalize_forecast_today(today)

    # 入参与净变化先定量：后面每月都用"已定量"的值相加，展示值自洽。
    initial = _money_to_float(initial)
    base = _money_to_float(base)
    extra = _money_to_float(extra)
    cut = _money_to_float(cut)
    net_change = _money_to_float(cut - extra - base)

    balance = initial
    month_rows: list[dict] = []
    broke_month = None
    broke_index = None
    for index in range(1, count + 1):
        month_day = _liability_month_shift(start, index)
        balance = _money_to_float(balance + net_change)
        below_zero = balance < 0
        month_rows.append({
            "month": _liability_month_key(month_day),
            "balance": balance,
            "net_change": net_change,
            "below_zero": below_zero,
        })
        # 只记**第一次**跌破 0 的月份；之后再怎么跌也不覆盖。
        if below_zero and broke_month is None:
            broke_month = month_rows[-1]["month"]
            broke_index = index

    return {
        "months": month_rows,
        "broke_month": broke_month,
        "broke_index": broke_index,
        "min_balance": min(row["balance"] for row in month_rows),
        "final_balance": month_rows[-1]["balance"],
        "initial_balance": initial,
        "monthly_base_expense": base,
        "extra_expense": extra,
        "cut_expense": cut,
        "months_count": count,
    }


# ===========================================================================
# 财务沙盘推演 · 多情景对比（保守 / 现行 / 激进）
# ---------------------------------------------------------------------------
# 只换「每月基线支出」，逐档复用 simulate_sandbox，不复制任何算法：
#   · 保守 = 基线 × 0.8（消费习惯收紧 20%）
#   · 现行 = 基线 × 1.0（与单线推演逐分钱一致）
#   · 激进 = 基线 × 1.2（消费习惯放松 20%）
# 额外支出 / 削减支出三档共用：它们是用户主动加的「计划修正」，
# 不是消费习惯本身，所以不跟着 ±20% 一起缩放。
# ===========================================================================

SANDBOX_SCENARIO_SPECS = (
    ("conservative", "保守消费（省20%）", 0.8),
    ("current", "现行消费", 1.0),
    ("aggressive", "激进消费（多花20%）", 1.2),
)


def _normalize_sandbox_ratio(value, label: str) -> float:
    """情景系数必须是 > 0 的有限 float（bool / 数字串 / nan / inf / 0 一律拒绝）。"""
    if isinstance(value, bool) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label}非法：{value!r}（不是数字）")
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}非法：{value!r}（不是数字）") from exc
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError(f"{label}必须是大于 0 的有限数字：{value!r}")
    return ratio


def _normalize_sandbox_specs(specs):
    """把情景定义收敛成 ((key, label, ratio), ...)，且必须恰好有一个 key='current'。

    拒绝：不是序列 / 条目不是三元组 / key 或 label 为空 / key 重复 /
    系数非法 / 没有 current（前端要靠它取主指标与 0 元参考线）。
    """
    if specs is None:
        specs = SANDBOX_SCENARIO_SPECS
    if isinstance(specs, (str, bytes)) or not isinstance(specs, (list, tuple)):
        raise ValueError(f"specs 必须是 (key, label, ratio) 序列：{specs!r}")
    if not specs:
        raise ValueError("specs 不能为空：至少要有一档情景")

    normalized = []
    seen = set()
    for item in specs:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ValueError(f"specs 的每一项必须是 (key, label, ratio)：{item!r}")
        key, label, ratio = item
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"情景 key 必须是非空字符串：{key!r}")
        key = key.strip()
        if key in seen:
            raise ValueError(f"情景 key 重复：{key!r}")
        seen.add(key)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"情景 label 必须是非空字符串：{label!r}")
        normalized.append(
            (key, label.strip(), _normalize_sandbox_ratio(ratio, f"情景 {key} 的系数"))
        )
    if "current" not in seen:
        raise ValueError("specs 必须包含 key='current' 的现行情景")
    return tuple(normalized)


def simulate_sandbox_scenarios(initial_balance, monthly_base_expense,
                               extra_expense=0.0, cut_expense=0.0,
                               months: int = 6, today=None, specs=None) -> dict:
    """在同一组 today / months 上并行推演三档消费习惯，返回可直接喂给图表的结构。

    口径
    ----
    · 每档只改「每月基线支出」：base × ratio（先定量到两位小数再推演），
      初始结余 / 额外支出 / 削减支出三档共用；
    · 本函数不复制算法，逐档调用 simulate_sandbox，因此「现行」一档与直接调用
      simulate_sandbox(initial, base, extra, cut, months, today) **逐分钱相等**；
    · 非法入参在推演任何一档之前抛 ValueError（金额 / months / today 由
      simulate_sandbox 校验，specs 与 ratio 由本函数校验）。

    参数
    ----
    initial_balance / monthly_base_expense / extra_expense / cut_expense :
        同 simulate_sandbox；monthly_base_expense 是「现行档」的基线。
    months : 1~24 的 int，三档共用。
    today  : date / datetime / "YYYY-MM-DD"，只决定月份标签起点。
    specs  : 自定义 ((key, label, ratio), ...)；None → SANDBOX_SCENARIO_SPECS。

    返回（逐字固定，前端按 key 取）
    --------------------------------
      months        : ["YYYY-MM", ...] 三档共享的 x 轴标签（长度 = months）
      months_count  : 推演月数
      initial_balance / extra_expense / cut_expense : 定量后的公共入参回显
      scenarios     : [{"key", "label", "ratio", "monthly_base_expense",
                        "months": [{...}], "broke_month", "broke_index",
                        "min_balance", "final_balance", "months_count"}, ...]
                      顺序与 specs 一致
      current       : scenarios 中 key="current" 的那一项（同一对象，不是副本）
    """
    normalized = _normalize_sandbox_specs(specs)
    # 先校验并定量基线：基线是数字串 / None 时要在乘法之前就报错，
    # 而不是让 "100" * 0.8 抛 TypeError。
    base = _money_to_float(
        _normalize_sandbox_amount(
            monthly_base_expense, "每月基线支出 monthly_base_expense"
        )
    )
    initial = _money_to_float(
        _normalize_sandbox_amount(
            initial_balance, "初始结余 initial_balance", allow_negative=True
        )
    )
    extra = _money_to_float(
        _normalize_sandbox_amount(extra_expense, "额外支出 extra_expense")
    )
    cut = _money_to_float(
        _normalize_sandbox_amount(cut_expense, "削减支出 cut_expense")
    )

    scenarios = []
    for key, label, ratio in normalized:
        result = simulate_sandbox(
            initial,
            _money_to_float(base * ratio),
            extra_expense=extra,
            cut_expense=cut,
            months=months,
            today=today,
        )
        scenarios.append({
            "key": key,
            "label": label,
            "ratio": ratio,
            "monthly_base_expense": result["monthly_base_expense"],
            "months": result["months"],
            "months_count": result["months_count"],
            "broke_month": result["broke_month"],
            "broke_index": result["broke_index"],
            "min_balance": result["min_balance"],
            "final_balance": result["final_balance"],
        })

    current = next(item for item in scenarios if item["key"] == "current")
    return {
        "months": [row["month"] for row in current["months"]],
        "months_count": current["months_count"],
        "initial_balance": initial,
        "extra_expense": extra,
        "cut_expense": cut,
        "scenarios": scenarios,
        "current": current,
    }


# ---------------------------------------------------------------------------
# 同龄人财务基准 · 四维雷达（模块末尾新增 section，不依赖任何既有函数的行为）
# ---------------------------------------------------------------------------
# 设计口径
# --------
# · 四个维度固定为「消费克制力 / 饮食健康度 / 结余能力 / 理财意识」，与
#   peer_chart.PEER_DIMENSIONS 逐字、逐序一致（两边任一改动必须同步）。
# · 全部是**纯函数 + 一条只读 SQL**：页面拿到 Tab1 的 DataFrame 后直接算「我」，
#   再单独取一次全站匿名均值；函数之间不共享状态，单测可直接断言。
# · 金额一律走 _money_to_float（Decimal + ROUND_HALF_UP 定量），比率一律收敛到
#   合理区间并保留 4 位小数：前端只做展示，不在展示层再兜底脏值。
# · 「全站匿名均值」只吐聚合结果，**绝不吐 user_id / 单笔明细**；SQL 参数全部绑定，
#   日期窗口用 [月初, 下月初) 与日报口径一致。
# ---------------------------------------------------------------------------

# 雷达图的四个维度。顺序即 ECharts radar.indicator 的顺序，也是 score_radar 的 key 顺序。
RADAR_DIMENSIONS = ("消费克制力", "饮食健康度", "结余能力", "理财意识")

# 少于 2 个人就不叫「对比」：页面据此提示样本不足，只展示自己的分数。
PEER_MIN_USERS = 2

# 迁移 012 未执行（bills.category 不存在）时的固定文案：页面会 try/except 接住并降级，
# 所以这里只要保证是 RuntimeError + 中文、可读，不红屏。
_PEER_CATEGORY_MISSING = "分类功能未启用：请先执行 migrations/012_bills_category.sql"

# 参与「饮食习惯」计算的四个分类。餐饮 = 恩格尔系数近似；奶茶咖啡单独一档；
# 娱乐 + 购物合并成「非必要消费」，用于消费克制力。
_RADAR_PEER_CATEGORY_KEYS = ("餐饮", "奶茶咖啡", "购物", "娱乐")

# 数据库连接串脱敏：异常文本里可能带着 postgresql+psycopg2://user:pass@host/db，
# 而 get_peer_benchmark 的 RuntimeError 会一路冒到页面的 st.warning 里 —— 必须抹掉。
# 只匹配「scheme://...」整段，替换成固定占位，既保留排错信息又不泄露凭据。
_DB_URL_IN_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+")


def _radar_month_label(month: str) -> str:
    """把 "YYYY-MM" 渲染成页面用的 "2026年10月"。

    调用前必须已通过 _parse_report_month 校验，这里只做展示层拼接：
    月份去前导零（10 月显示 "10"，1 月显示 "1"），与 app.py 既有文案一致。
    """
    return f"{int(month[:4])}年{int(month[5:7])}月"


def _radar_normalize_today(today=None) -> date:
    """把 today 收敛成 datetime.date：None → 北京时间今天；非法输入抛 ValueError。

    datetime 是 date 的子类，必须先拦下来取 .date()，否则后续和 date 比较时
    会带时分秒，把「当月/历史月」的判断弄出边界误差。
    """
    if today is None:
        return _report_today()
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    if isinstance(today, str):
        raw = today.strip()
        if _ISO_DATE_PATTERN.fullmatch(raw):
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        raise ValueError(f"today 必须是 date / datetime / YYYY-MM-DD：{today!r}")
    raise ValueError(f"today 必须是 date / datetime / YYYY-MM-DD：{today!r}")


def _radar_elapsed_days(month_start: date, month_end: date, today: date) -> int:
    """已过天数：当月 → today.day；历史月份 → 整月天数；未来月份 → 0。

    口径与 report_user_stats 完全一致（那边把 0 当「不做除法」的哨兵）。
    这里保留 0 的语义，除法统一在 _peer_metrics_from_row 里按 1 兜底。
    """
    if month_start <= today < month_end:
        return today.day
    if today >= month_end:
        return (month_end - month_start).days
    return 0


def _radar_decimal(value) -> Decimal:
    """把任意金额收敛成 Decimal；脏值（None / 非数字 / NaN / inf）一律当 0，绝不抛错。

    必须把非有限值也归 0：Decimal("NaN") 参与大小比较会抛 InvalidOperation，
    一旦漏到后面的 `expense_dec > 0` 就会把整块模块炸掉。
    """
    try:
        number = _as_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not number.is_finite():
        return Decimal("0")
    return number


def _radar_decimal_sum(values) -> Decimal:
    """逐项累加金额（Decimal），单条脏值跳过，不影响其余行。"""
    total = Decimal("0")
    for value in values:
        total += _radar_decimal(value)
    return total


def _radar_int(value) -> int:
    """把计数收敛成非负整数；None / NaN / 非法值 → 0。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _radar_ratio(numerator, denominator) -> float:
    """比率 = numerator / denominator，收敛到 [0, 1] 并保留 4 位小数。

    分母 <= 0（支出为 0 / 本月还没开始）→ 0.0，不做除法；NaN / inf 同样归 0。
    """
    num = _radar_decimal(numerator)
    den = _radar_decimal(denominator)
    try:
        if den <= 0:
            return 0.0
        value = float(num / den)
    except (InvalidOperation, OverflowError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return round(max(0.0, min(1.0, value)), 4)


def _radar_score_clamp(value) -> float:
    """得分收敛到 [0, 100] 并保留 1 位小数；NaN / inf / 非数字 → 0.0。

    score_radar 的每个维度都经过这里：页面渲染前不需要再防御脏值。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(100.0, number)), 1)


def _radar_metric_value(metrics, key: str) -> float:
    """从 metrics 里安全取一个数值：不是 dict / 缺字段 / 脏值一律返回 0。

    score_radar 的契约是「绝不 KeyError」—— 页面即使传了半成品 dict 也只是
    该字段按 0 代入公式，而不是整块模块红屏。
    """
    if not isinstance(metrics, dict):
        return 0.0
    try:
        number = float(metrics.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _peer_metrics_from_row(*, month, month_label, expense, income, bill_count,
                           active_days, elapsed_days, category_totals=None) -> dict:
    """由一组「已聚合」的原始数字算出雷达图需要的全部指标（唯一公式来源）。

    compute_radar_metrics（DataFrame 口径）和 get_peer_benchmark（SQL 口径）都调用
    本函数，公式只写一遍：两边的差异仅在「怎么把原始账单聚合成这几个数」。

    参数
    ----
    month / month_label : 归一后的 "YYYY-MM" 与 "YYYY年M月" 展示文案。
    expense / income    : 本月支出 / 收入合计（Decimal / float / str 均可）。
    bill_count          : 本月有效账单笔数（收入 + 支出）。
    active_days         : 本月有记账的自然日数（去重后的天数）。
    elapsed_days        : 已过天数（当月 = 今天几号；历史月 = 整月；未来月 = 0）。
    category_totals     : {"餐饮","奶茶咖啡","购物","娱乐"} → 金额；None 表示
                          「没有 category 列」，此时三个占比一律 0.0。

    返回
    ----
    逐字固定的 dict（key 与 app.py Tab1 的消费方约定一致）：
        month, month_label, has_data, has_expense, expense, income, net,
        bill_count, active_days, elapsed_days, daily_expense,
        milk_tea_share, food_share, fun_share, saving_rate, record_ratio
    金额保留两位小数；占比 / 记录率保留 4 位小数且在 [0, 1]；saving_rate 可为负。
    """
    expense_dec = _radar_decimal(expense)
    income_dec = _radar_decimal(income)
    expense_value = _money_to_float(expense_dec)
    income_value = _money_to_float(income_dec)
    net_dec = income_dec - expense_dec
    net_value = _money_to_float(net_dec)

    bill_count_value = _radar_int(bill_count)
    active_days_value = _radar_int(active_days)
    elapsed_days_value = _radar_int(elapsed_days)

    # 未来月份 elapsed_days = 0：对外仍返回 0，只在除法处按 1 兜底，避免 ZeroDivisionError。
    elapsed_for_division = elapsed_days_value if elapsed_days_value > 0 else 1
    daily_expense = _money_to_float(expense_dec / Decimal(elapsed_for_division))

    # 三个占比只在「有支出且分类可用」时才算；否则 0.0（页面此时会隐藏分类模块）。
    milk_tea_share = 0.0
    food_share = 0.0
    fun_share = 0.0
    if category_totals is not None and expense_dec > 0:
        totals = {
            name: _radar_decimal(category_totals.get(name, 0))
            for name in _RADAR_PEER_CATEGORY_KEYS
        }
        milk_tea_share = _radar_ratio(totals["奶茶咖啡"], expense_dec)
        food_share = _radar_ratio(totals["餐饮"], expense_dec)
        fun_share = _radar_ratio(totals["购物"] + totals["娱乐"], expense_dec)

    # 结余率：收入 > 0 时按 net / income 如实算（可为负、可超过 -1）；
    # 收入 <= 0 时用哨兵值把「纯花钱」和「完全没数据」区分开：
    #   有支出 → -1.0（结余能力最差），无支出 → 0.0（无数据，不奖不罚）。
    if income_dec > 0:
        try:
            saving_value = float(net_dec / income_dec)
        except (InvalidOperation, OverflowError, ValueError):
            saving_value = 0.0
        saving_rate = round(saving_value, 4) if math.isfinite(saving_value) else 0.0
    elif expense_dec > 0:
        saving_rate = -1.0
    else:
        saving_rate = 0.0

    # 记录率：已过天数为 0（未来月份）时记 0，不做除法。
    record_ratio = (
        _radar_ratio(active_days_value, elapsed_days_value)
        if elapsed_days_value > 0
        else 0.0
    )

    return {
        "month": month,
        "month_label": month_label,
        "has_data": bill_count_value > 0,
        "has_expense": expense_dec > 0,
        "expense": expense_value,
        "income": income_value,
        "net": net_value,
        "bill_count": bill_count_value,
        "active_days": active_days_value,
        "elapsed_days": elapsed_days_value,
        "daily_expense": daily_expense,
        "milk_tea_share": milk_tea_share,
        "food_share": food_share,
        "fun_share": fun_share,
        "saving_rate": saving_rate,
        "record_ratio": record_ratio,
    }


def compute_radar_metrics(df, month: str, today=None) -> dict:
    """统计某个月的雷达图原始指标（纯函数，不读库、不改 df）。

    入参
    ----
    df    : app.py Tab1 已有的账单 DataFrame，列：date / date_parsed / merchant /
            amount / type / category（category 列可能不存在）。
    month : "YYYY-MM"，严格校验；非法直接 ValueError（复用 _parse_report_month）。
    today : 默认北京时间今天；决定「已过天数」，测试可注入固定日期。

    口径
    ----
    · 只统计 month 这一个月：[月初, 下月初)，与日报口径一致；
    · 金额全部先用 _as_decimal 收敛再累加，最后 _money_to_float 定量到分；
    · 脏数据逐行跳过：日期解析失败（NaT）或金额非数字的行**不进任何统计**；
    · 空表 / 本月没有账单 / 没有日期列 → has_data=False，金额 / 占比 / 笔数全 0，
      elapsed_days 仍按月份回显（它是「已过天数」，不是消费指标），不抛错；
    · 没有 category 列 → milk_tea_share / food_share / fun_share 均为 0.0
      （页面据此隐藏这个模块，不会把「分类没启用」误读成「习惯很好」）。

    返回的 dict 逐字固定，见 _peer_metrics_from_row 的 docstring。
    """
    month_start, month_end = _parse_report_month(month)
    month_text = month.strip()
    month_label = _radar_month_label(month_text)
    today_date = _radar_normalize_today(today)
    elapsed_days = _radar_elapsed_days(month_start, month_end, today_date)

    empty = _peer_metrics_from_row(
        month=month_text,
        month_label=month_label,
        expense=0,
        income=0,
        bill_count=0,
        active_days=0,
        elapsed_days=elapsed_days,
        category_totals=None,
    )
    if df is None or not hasattr(df, "columns") or len(df) == 0:
        return empty

    # 日期列：优先用 app.py 已经解析好的 date_parsed，没有就现场解析 date。
    try:
        if "date_parsed" in df.columns:
            parsed = pd.to_datetime(df["date_parsed"], errors="coerce")
            # date_parsed 整列都解析不出来（例如上游只给了 date）时退回到 date 列，
            # 只在这一列**完全没有有效值**时兜底，避免把个别 NaT 脏行「复活」。
            if not bool(parsed.notna().any()) and "date" in df.columns:
                parsed = pd.to_datetime(df["date"], errors="coerce")
        elif "date" in df.columns:
            parsed = pd.to_datetime(df["date"], errors="coerce")
        else:
            return empty
        # 带时区的 date_parsed（少见）先摘掉时区再和 naive 的月初/下月初比较，
        # 否则 pandas 会直接抛「Cannot compare tz-naive and tz-aware」。
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_localize(None)
    except (TypeError, ValueError, AttributeError):
        logging.getLogger(__name__).warning(
            "[db] 雷达图：账单日期列解析失败，本月按「无数据」处理", exc_info=True
        )
        return empty

    if "amount" not in df.columns:
        return empty
    try:
        # errors="coerce"：非数字金额变 NaN，随后和坏日期一起被 valid 掩码剔除。
        amounts = pd.to_numeric(df["amount"], errors="coerce").astype(float)
    except (TypeError, ValueError):
        return empty

    # 月份边界用 Timestamp 比较：parsed 是 datetime64[ns]，两边必须同类型。
    try:
        in_month = (parsed >= pd.Timestamp(month_start)) & (parsed < pd.Timestamp(month_end))
    except TypeError:
        return empty
    valid = parsed.notna() & amounts.notna() & in_month
    if not bool(valid.any()):
        return empty

    work = df.loc[valid]
    parsed = parsed.loc[valid]
    amounts = amounts.loc[valid]

    # type 列缺失 / 脏值：按「既不是收入也不是支出」处理（不计入两侧金额），
    # 但仍是本月的一笔账单 → 参与 bill_count / active_days。
    if "type" in work.columns:
        types = work["type"].astype(str).str.strip()
    else:
        types = pd.Series("", index=work.index, dtype=object)
    expense_mask = types == EXPENSE_TYPE
    income_mask = types == INCOME_TYPE

    expense_dec = _radar_decimal_sum(amounts[expense_mask])
    income_dec = _radar_decimal_sum(amounts[income_mask])

    try:
        active_days = int(parsed.dt.normalize().nunique())
    except (TypeError, ValueError):
        active_days = 0

    # 分类占比：只对支出行求和；列不存在 → None（三个 share 由 _peer_metrics_from_row 记 0）。
    category_totals = None
    if "category" in work.columns:
        normalized_categories = work["category"].map(_normalize_category_value)
        category_totals = {}
        for name in _RADAR_PEER_CATEGORY_KEYS:
            category_totals[name] = _radar_decimal_sum(
                amounts[expense_mask & (normalized_categories == name)]
            )

    return _peer_metrics_from_row(
        month=month_text,
        month_label=month_label,
        expense=expense_dec,
        income=income_dec,
        bill_count=len(work),
        active_days=active_days,
        elapsed_days=elapsed_days,
        category_totals=category_totals,
    )


def score_radar(metrics: dict) -> dict:
    """把 compute_radar_metrics 的指标折成四个 0~100 的得分（纯函数）。

    公式（与页面文案、README 的口径逐字一致）
    ------------------------------------------
    消费克制力 = clamp(100 - fun_share * 150)
        娱乐 + 购物 占比越低越高：占比 0 → 100 分，占比 2/3 → 0 分。
    饮食健康度 = clamp(100 - milk_tea_share * 200 - max(0, food_share - 0.5) * 100)
        奶茶咖啡占比每 1% 扣 2 分（翻倍惩罚）；餐饮占比超过 50% 之后再按超出部分扣分。
    结余能力   = clamp(50 + saving_rate * 100)
        结余率 0% = 50 分，省下 30% = 80 分，透支 50% = 0 分。
    理财意识   = clamp(record_ratio * 70 + min(bill_count / 30, 1) * 30)
        记账天数占比占 7 成、笔数密度（30 笔封顶）占 3 成。

    容错
    ----
    · metrics 不是 dict 或空 dict（一点有效数据都没有）→ 四个维度整体记 0 分：
      「无数据」绝不能因为公式的常数项被误读成「100 分的好习惯」；
    · metrics 是 dict 但只缺部分字段 → 缺的字段按 0 代入公式，**绝不 KeyError**；
      值非数字 / NaN / inf 同样按 0，并在 clamp 内部兜底。
    返回值的 key 恰好是 RADAR_DIMENSIONS 四项，value 是保留 1 位小数的 float
    （页面 tooltip 再取整展示）。
    """
    if not isinstance(metrics, dict) or not metrics:
        return {dim: 0.0 for dim in RADAR_DIMENSIONS}

    fun_share = _radar_metric_value(metrics, "fun_share")
    milk_tea_share = _radar_metric_value(metrics, "milk_tea_share")
    food_share = _radar_metric_value(metrics, "food_share")
    saving_rate = _radar_metric_value(metrics, "saving_rate")
    record_ratio = _radar_metric_value(metrics, "record_ratio")
    bill_count = _radar_metric_value(metrics, "bill_count")

    return {
        "消费克制力": _radar_score_clamp(100 - fun_share * 150),
        "饮食健康度": _radar_score_clamp(
            100 - milk_tea_share * 200 - max(0.0, food_share - 0.5) * 100
        ),
        "结余能力": _radar_score_clamp(50 + saving_rate * 100),
        "理财意识": _radar_score_clamp(
            record_ratio * 70 + min(bill_count / 30, 1) * 30
        ),
    }


def _peer_avg_money(metric_rows: list[dict], key: str) -> float:
    """多用户某项金额指标的算术平均（每人等权），用 Decimal 求和后定量到分。"""
    if not metric_rows:
        return 0.0
    total = sum((_radar_decimal(row.get(key, 0)) for row in metric_rows), Decimal("0"))
    return _money_to_float(total / Decimal(len(metric_rows)))


def _peer_avg_ratio(metric_rows: list[dict], key: str) -> float:
    """多用户某项比率指标的算术平均，保留 4 位小数。

    注意：不在这里 clamp —— share / record_ratio 在单人指标里已经收敛到 [0, 1]，
    而 saving_rate 允许为负，硬 clamp 会把「平均透支」显示成 0。
    """
    if not metric_rows:
        return 0.0
    total = sum((_radar_metric_value(row, key) for row in metric_rows), 0.0)
    return round(total / len(metric_rows), 4)


def _peer_empty_avg() -> dict:
    """样本为 0 时的 avg 骨架：key 与正常返回逐字一致，值全 0。"""
    return {
        "expense": 0.0,
        "income": 0.0,
        "net": 0.0,
        "daily_expense": 0.0,
        "milk_tea_share": 0.0,
        "food_share": 0.0,
        "fun_share": 0.0,
        "saving_rate": 0.0,
        "bill_count": 0.0,
        "record_ratio": 0.0,
    }


def _peer_error_text(exc: Exception) -> str:
    """把数据库异常压成一句可展示的中文原因：抹掉连接串、截断超长 SQL。

    SQLAlchemy 的 OperationalError 文本里可能带 `postgresql+psycopg2://user:pass@host`，
    而本函数的返回值会进页面提示 —— 先脱敏再展示，同时保留异常类名方便排错。
    """
    message = str(exc).strip() or exc.__class__.__name__
    message = _DB_URL_IN_TEXT.sub("<数据库连接已隐藏>", message)
    if len(message) > 200:
        message = message[:200] + "…"
    return message


def get_peer_benchmark(month: str | None = None, today=None) -> dict:
    """统计全站「每用户本月指标 → 四维得分」的匿名算术平均（每人等权）。

    参数
    ----
    month : "YYYY-MM"；None → 取 today 所在月。非法格式直接 ValueError。
    today : 默认北京时间今天；决定当月「已过天数」（记录率的分母）。

    口径
    ----
    · 一条 GROUP BY user_id 的只读 SQL 取每用户当月的：支出、收入、奶茶咖啡、
      餐饮、娱乐 + 购物、账单笔数、活跃天数（COUNT(DISTINCT date::date)）；
    · 每用户先按 _peer_metrics_from_row 算指标（与 compute_radar_metrics 同一公式），
      再 score_radar 算分，最后**对指标/得分分别取算术平均**；
    · 只统计**当月有支出**的用户（HAVING SUM(支出) > 0）：0 支出用户的
      fun_share / milk_tea_share 恒为 0，会把「消费克制力 / 饮食健康度」拉到 100 分，
      让全站均值虚高，失去对比意义；
    · 返回结构绝不含 user_id / 单笔明细，只有聚合值与人数。

    返回
    ----
    {
      "month": "2026-10", "month_label": "2026年10月",
      "user_count": int, "min_users": PEER_MIN_USERS, "enough_users": bool,
      "avg": {... 10 个指标 ...},
      "scores": {四个维度: float},
    }
    没有用户 → user_count=0、avg / scores 全 0、enough_users=False（不抛错）。

    异常
    ----
    · bills.category 不存在 → RuntimeError("分类功能未启用：请先执行 migrations/012_...")
      （页面 try/except 接住后降级，不红屏）；
    · 数据库异常（含历史脏日期导致 date::date cast 失败）→ RuntimeError(
      "同龄人基准统计失败：...")，文本已脱敏，不含连接串 / 密码。
    """
    today_date = _radar_normalize_today(today)
    # 先解析再 strip：month 不是字符串时让 _parse_report_month 抛 ValueError，
    # 而不是在这里先吃一个 AttributeError（对外契约是「非法月份 → ValueError」）。
    raw_month = (
        f"{today_date.year:04d}-{today_date.month:02d}" if month is None else month
    )
    month_start, month_end = _parse_report_month(raw_month)
    month_text = raw_month.strip()
    month_label = _radar_month_label(month_text)
    elapsed_days = _radar_elapsed_days(month_start, month_end, today_date)

    empty_scores = {dim: 0.0 for dim in RADAR_DIMENSIONS}

    if engine is None:
        try:
            init_db()
        except Exception as exc:  # noqa: BLE001 - 统一包装成页面能接住的 RuntimeError
            # 只记脱敏后的一句话：SQLAlchemy 的异常文本里可能带连接串 / 密码，
            # 直接 exc_info=True 会把凭据写进日志文件。
            logging.getLogger(__name__).warning(
                "[db] 同龄人基准统计前初始化数据库失败：%s", _peer_error_text(exc)
            )
            raise RuntimeError(f"同龄人基准统计失败：{_peer_error_text(exc)}") from exc

    # 分类列不存在时，三个「饮食习惯」指标没有口径可言：显式抛错让页面隐藏模块，
    # 而不是悄悄返回一堆 0 分（那会被误读成「大家都很健康」）。
    if not _bills_category_supported():
        raise RuntimeError(_PEER_CATEGORY_MISSING)

    sql = text("""
        WITH scoped AS (
            SELECT b.user_id,
                   b.amount,
                   b.type,
                   COALESCE(NULLIF(btrim(b.category), ''), :fallback) AS category,
                   b.date::date AS bill_date
            FROM public.bills b
            WHERE b.date::date >= :start
              AND b.date::date <  :end
        ),
        per_user AS (
            SELECT s.user_id,
                   COALESCE(SUM(s.amount) FILTER (WHERE s.type = :expense), 0) AS expense,
                   COALESCE(SUM(s.amount) FILTER (WHERE s.type = :income), 0)  AS income,
                   COUNT(*) AS bill_count,
                   COUNT(DISTINCT s.bill_date) AS active_days,
                   COALESCE(SUM(s.amount) FILTER (
                       WHERE s.type = :expense AND s.category = :cat_food), 0) AS food_expense,
                   COALESCE(SUM(s.amount) FILTER (
                       WHERE s.type = :expense AND s.category = :cat_milk), 0) AS milk_expense,
                   COALESCE(SUM(s.amount) FILTER (
                       WHERE s.type = :expense AND s.category = :cat_shop), 0) AS shop_expense,
                   COALESCE(SUM(s.amount) FILTER (
                       WHERE s.type = :expense AND s.category = :cat_fun), 0) AS fun_expense
            FROM scoped s
            GROUP BY s.user_id
            -- 0 支出用户会把「消费克制力 / 饮食健康度」顶到 100 分，必须排除。
            HAVING COALESCE(SUM(s.amount) FILTER (WHERE s.type = :expense), 0) > 0
        )
        SELECT expense, income, bill_count, active_days,
               food_expense, milk_expense, shop_expense, fun_expense
        FROM per_user
        ORDER BY user_id
    """)
    params = {
        "start": month_start,
        "end": month_end,
        "fallback": FALLBACK_CATEGORY,
        "expense": EXPENSE_TYPE,
        "income": INCOME_TYPE,
        "cat_food": "餐饮",
        "cat_milk": "奶茶咖啡",
        "cat_shop": "购物",
        "cat_fun": "娱乐",
    }

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001 - 统一包装成 RuntimeError，且文本脱敏
        # 同上：日志只写脱敏后的原因，不写原始异常（可能含连接串 / 密码）。
        logging.getLogger(__name__).warning(
            "[db] 同龄人基准统计失败（SQL 执行阶段）：%s", _peer_error_text(exc)
        )
        raise RuntimeError(f"同龄人基准统计失败：{_peer_error_text(exc)}") from exc

    if not rows:
        return {
            "month": month_text,
            "month_label": month_label,
            "user_count": 0,
            "min_users": PEER_MIN_USERS,
            "enough_users": False,
            "avg": _peer_empty_avg(),
            "scores": empty_scores,
        }

    metric_rows = []
    score_rows = []
    for row in rows:
        metrics = _peer_metrics_from_row(
            month=month_text,
            month_label=month_label,
            expense=row.expense,
            income=row.income,
            bill_count=row.bill_count,
            active_days=row.active_days,
            elapsed_days=elapsed_days,
            category_totals={
                "餐饮": row.food_expense,
                "奶茶咖啡": row.milk_expense,
                "购物": row.shop_expense,
                "娱乐": row.fun_expense,
            },
        )
        metric_rows.append(metrics)
        score_rows.append(score_radar(metrics))

    user_count = len(metric_rows)
    bill_count_total = sum(row["bill_count"] for row in metric_rows)
    avg = {
        "expense": _peer_avg_money(metric_rows, "expense"),
        "income": _peer_avg_money(metric_rows, "income"),
        "net": _peer_avg_money(metric_rows, "net"),
        "daily_expense": _peer_avg_money(metric_rows, "daily_expense"),
        "milk_tea_share": _peer_avg_ratio(metric_rows, "milk_tea_share"),
        "food_share": _peer_avg_ratio(metric_rows, "food_share"),
        "fun_share": _peer_avg_ratio(metric_rows, "fun_share"),
        "saving_rate": _peer_avg_ratio(metric_rows, "saving_rate"),
        # 笔数均值保留 2 位：1 个人 10 笔、1 个人 1 笔 → 5.5 笔，取整会丢信息。
        "bill_count": round(bill_count_total / user_count, 2),
        "record_ratio": _peer_avg_ratio(metric_rows, "record_ratio"),
    }
    scores = {
        dim: round(
            sum(row[dim] for row in score_rows) / user_count, 1
        )
        for dim in RADAR_DIMENSIONS
    }
    # 平均分理论上仍在 [0, 100]，这里再夹一次：浮点平均 + round 不会越界，
    # 但显式收敛能保证页面拿到的值一定符合雷达图 max=100 的刻度。
    scores = {dim: _radar_score_clamp(value) for dim, value in scores.items()}

    return {
        "month": month_text,
        "month_label": month_label,
        "user_count": user_count,
        "min_users": PEER_MIN_USERS,
        "enough_users": user_count >= PEER_MIN_USERS,
        "avg": avg,
        "scores": scores,
    }


def radar_available() -> bool:
    """雷达图模块是否可用：等价于 bills.category 是否存在（迁移 012 是否执行）。

    页面在渲染前先问一句，可以在「没有分类」时直接隐藏整块，而不是先打一次
    get_peer_benchmark 再靠异常分支降级。函数本身不抛错（探测失败按 False 处理）。
    """
    return bool(_bills_category_supported())


# ===========================================================================
# 个人资产负债表 / 数码资产折旧仪（migrations/020_assets.sql）
# ---------------------------------------------------------------------------
# 本段只**追加**，不改动上面任何既有函数、常量与取数口径。
#
# 分工：
#   · assets_available / _warn_asset_once    探测 public.assets 是否存在（020 是否执行）；
#   · 纯计算（不碰库）：normalize_asset_category / compute_asset_value / compute_asset_curve；
#   · 读写资产台账：add_asset / delete_asset / list_assets / get_asset_overview。
#
# 折旧口径（本功能的正确性核心）：
#   · 直线法，按**天**折算年数，但先按「整月 + 不足一月的零头天」拆开：
#       满一个自然年必须恰好折旧掉一个 rate（6000 元、20% → 一年后 4800.00，
#       而不是 4800.82 —— 纯 days/365.25 会因闰年差 0.08% 漂移）；
#   · 365.25 天/年只用于折算「不足一月」的零头天，跨闰年稳定；
#   · 一律 Decimal 计算，最后经 _money_to_float 定量两位（ROUND_HALF_UP）。
#
# 降级约定（020 未执行时）：
#   · assets_available() → False（进程内缓存 + 只告警一次，照抄 _bills_mood_supported）；
#   · 读接口返回空结构（[] / 全 0 的 overview），绝不红屏；
#   · 写接口 add_asset 抛 AssetTableMissing（中文 message 可直接展示给用户），
#     delete_asset 返回 False。
# ===========================================================================

ASSET_CATEGORIES = ("数码", "生活", "其他")
ASSET_FALLBACK_CATEGORY = "其他"
ASSET_DEFAULT_DEPRECIATION = 0.20
ASSET_NAME_MAX_LEN = 100
ASSET_CURVE_MONTHS = 36
ASSET_CURVE_MONTHS_MIN = 1
ASSET_CURVE_MONTHS_MAX = 120

# NUMERIC(5,4)：年折旧率最多 4 位小数（12.5% / 20% / 33.33% 都够表达）。
ASSET_RATE_QUANT = Decimal("0.0001")
# 一年按 365.25 天折算（只作用于「不足一月」的零头，见 _asset_elapsed_years）。
ASSET_DAYS_PER_YEAR = Decimal("365.25")

ASSET_TABLE_MISSING_MESSAGE = (
    "资产台账功能未启用：请先执行 migrations/020_assets.sql"
)
ASSET_LIABILITY_NOTE = "按近 6 个月还款记录外推未来 6 个月待还（估算）"
ASSET_LIABILITY_UNAVAILABLE_NOTE = "暂无法估算负债"

# public.assets 是否存在的进程级缓存：None = 还没探测过；True/False = 探测结果。
# 与 _BILLS_MOOD_SUPPORT 同一套取舍：只缓存**探测成功**的结果，探测失败
# （库暂时不可用）不缓存，否则一次抖动就会让整个进程永久降级。
_ASSETS_TABLE_SUPPORT = None
# 告警只打一次：资产页每轮 rerun 都会调 assets_available，
# 020 未执行时逐次 print 会刷屏。
_ASSET_WARNED = False


class AssetTableMissing(Exception):
    """020_assets.sql 未执行（public.assets 不存在）。

    message 是**可直接展示**的中文提示（页面捕获后 st.warning 即可，不红屏）。
    """


def _warn_asset_once(message: str) -> None:
    """[assets] 告警只打一次（进程内）：页面每轮 rerun 都会调资产接口，不能刷屏。"""
    global _ASSET_WARNED
    if _ASSET_WARNED:
        return
    _ASSET_WARNED = True
    print(f"[assets] {message}")


def assets_available() -> bool:
    """探测 public.assets 是否存在（迁移 020 是否已执行），结果进程内缓存。

    与 _bills_mood_supported() 同一套模式：
      · 探测成功才写缓存（True/False），探测失败（库暂时不可用）不缓存、下次再探；
      · 020 未执行只告警一次，调用方据此走降级分支（读空 / 写抛 AssetTableMissing）。
    本函数**不抛异常**：探测失败按 False 处理（宁可显示"暂不可用"，也不红屏）。
    """
    global _ASSETS_TABLE_SUPPORT
    if _ASSETS_TABLE_SUPPORT is not None:
        return _ASSETS_TABLE_SUPPORT
    if engine is None:
        # 正常入口 app.py 会先 init_db()；这里兜底，避免脚本里直接调 add_asset 时
        # 把"引擎没建"误判成"表不存在"并永久缓存。
        try:
            init_db()
        except Exception:
            _warn_asset_once(
                "探测 public.assets 前初始化数据库失败，本次按「表不存在」处理"
            )
            return False
    try:
        with engine.connect() as conn:
            exists = bool(conn.execute(
                text("SELECT to_regclass('public.assets')")
            ).scalar())
    except Exception:
        # 探测失败 ≠ 表不存在：不写缓存，下次再探（写库本身也会失败并照常抛出）。
        _warn_asset_once("探测 public.assets 失败，本次按「表不存在」处理")
        return False
    _ASSETS_TABLE_SUPPORT = exists
    if not exists:
        _warn_asset_once(
            "public.assets 不存在（migrations/020_assets.sql 未执行）："
            "资产台账暂不可用，请执行 020 迁移后重启应用"
        )
    return exists


def normalize_asset_category(value) -> str:
    """把任意传入值收敛成 ASSET_CATEGORIES 里的原值；认不出就返回 "其他"。

    · 合法值（数码 / 生活 / 其他）原样返回；
    · None / "" / 纯空白 / NaN / 非法字符串 → "其他"；
    · **绝不抛异常**：分类来自页面控件 / AI 输出，脏值最多落成"其他"，
      不能让一次分类写错把整笔资产写入打回去。
    """
    try:
        if isinstance(value, str):
            text = value.strip()
        else:
            if value is None:
                return ASSET_FALLBACK_CATEGORY
            try:
                if pd.isna(value):  # NaN / NaT / pd.NA
                    return ASSET_FALLBACK_CATEGORY
            except (TypeError, ValueError):
                pass
            text = str(value).strip()
        return text if text in ASSET_CATEGORIES else ASSET_FALLBACK_CATEGORY
    except Exception:
        return ASSET_FALLBACK_CATEGORY


def _normalize_asset_date(value, label: str) -> date:
    """购入日期 / today 收敛成 datetime.date：date / datetime / "YYYY-MM-DD"，其余 ValueError。

    datetime 是 date 的子类，必须先取 .date()：否则带时分秒的值与 date 比较 /
    相减会按"不足 24h 就少一天"截断，elapsed_days 少 1。
    非法字符串（2026/10/21、2026-13-40、2026-02-30）与非法类型都抛中文 ValueError。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _ISO_DATE_PATTERN.fullmatch(text):
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                pass  # 格式对但日子不存在（2026-02-30）→ 落到下面统一报错
    raise ValueError(f"{label}必须是 datetime.date 或 YYYY-MM-DD 字符串：{value!r}")


def _normalize_asset_price(value) -> Decimal:
    """购入价收敛成两位小数的 Decimal：有限、>= 0、<= MAX_ABS_AMOUNT。

    拒绝 bool（True 会被 float 成 1.0，把"开关"静默当成金额）；
    nan / inf 一律拒绝（会顺着残值把页面变成 "nan 元"）；
    0 是合法值（赠品 / 已报废但留档），残值恒 0。
    """
    if isinstance(value, bool):
        raise ValueError(f"购入价非法：{value!r}（不是数字）")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"购入价非法：{value!r}（不是数字）") from exc
    if not price.is_finite():
        raise ValueError(f"购入价非法：{value!r}（必须是有限数字，不允许 nan/inf）")
    if price < 0:
        raise ValueError(f"购入价不能为负数：{value!r}")
    try:
        price = price.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"购入价超出上限 {MAX_ABS_AMOUNT}：{value!r}") from exc
    if price < 0:
        raise ValueError(f"购入价不能为负数：{value!r}")
    if price > Decimal(str(MAX_ABS_AMOUNT)):
        raise ValueError(f"购入价超出上限 {MAX_ABS_AMOUNT}：{value!r}")
    return price


def _normalize_asset_rate(value) -> Decimal:
    """年折旧率收敛成 0~1 的 Decimal（4 位小数）：20% 必须传 0.2。

    >1 或 <0 抛 ValueError，**不做 20 → 0.2 的自动换算**：把 20 当 20% 还是
    2000% 无法从值本身判断，静默换算一旦猜错，残值会瞬间变成 0 且用户毫无察觉。
    """
    if isinstance(value, bool):
        raise ValueError(f"折旧率非法：{value!r}（必须是 0~1 的小数，20% 请传 0.2）")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"折旧率非法：{value!r}（必须是 0~1 的小数，20% 请传 0.2）"
        ) from exc
    if not rate.is_finite():
        raise ValueError(f"折旧率非法：{value!r}（必须是有限数字，不允许 nan/inf）")
    if rate < 0 or rate > 1:
        raise ValueError(
            f"折旧率必须在 0~1 之间（20% 请传 0.2，不允许把 20 当 20%）：{value!r}"
        )
    try:
        rate = rate.quantize(ASSET_RATE_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"折旧率非法：{value!r}") from exc
    if rate < 0 or rate > 1:  # 量化后重新校验（0.99999 → 1.0000 属合法边界）
        raise ValueError(
            f"折旧率必须在 0~1 之间（20% 请传 0.2）：{value!r}"
        )
    return rate


def _asset_month_length(year: int, month: int) -> int:
    """某年某月的天数：用「下月 1 日 - 本月 1 日」算，天然覆盖 2 月 / 闰年 / 大小月。"""
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    return (next_first - date(year, month, 1)).days


def _asset_add_months(day: date, months: int) -> date:
    """把 day 平移 months 个自然月，保持"同日"；目标月没有该日则 clamp 到月末。

    例：2024-01-31 + 1 月 → 2024-02-29；2024-02-29 + 12 月 → 2025-02-28。
    与 _liability_month_shift 同一套「月份序号」算法，跨年天然正确。
    """
    index = day.year * 12 + day.month - 1 + int(months)
    year, month = index // 12, index % 12 + 1
    return date(year, month, min(day.day, _asset_month_length(year, month)))


def _asset_shift_month(day: date, months: int) -> str:
    """自然月键 "YYYY-MM"（零填充 → 字典序 == 时间序），从 day 所在月平移 months。"""
    index = day.year * 12 + day.month - 1 + int(months)
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _asset_elapsed_years(purchase_date: date, today: date) -> Decimal:
    """购入至今的「年数」= 整月数/12 + 不足一月的零头天/365.25。

    为什么不用纯 days/365.25：365.25 与自然年有 0.08% 的差，6000 元、20% 的资产
    满一年会算出 4800.82 而不是 4800.00 —— 用户一眼就能看出"满一年没扣够 20%"。
    先按自然月对齐（同日 / 月末 clamp），零头天才按 365.25 折算，于是：
      · 满 1 年恰好 1.0 年 → 恰好折旧掉一个 rate；
      · 跨闰年稳定（不因 366 天多折一天）；
      · 预购（today <= purchase_date）→ 0，残值 = 原价。

    返回 Decimal（后续金额运算全程 Decimal，不做 float 累加）。
    """
    if today <= purchase_date:
        return Decimal("0")
    months = (
        (today.year - purchase_date.year) * 12
        + (today.month - purchase_date.month)
    )
    anchor = _asset_add_months(purchase_date, months)
    if anchor > today:  # 还没到当月同日（例如 1/31 购入、2/28 的今天）
        months -= 1
        anchor = _asset_add_months(purchase_date, months)
    whole_years = Decimal(months) / Decimal(12)
    remainder_days = Decimal((today - anchor).days)
    return whole_years + remainder_days / ASSET_DAYS_PER_YEAR


def _asset_residual(price: Decimal, rate: Decimal, years: Decimal) -> tuple[float, bool]:
    """按「原价 × max(0, 1 - rate*years)」算残值，返回 (两位小数 float, 是否已归零)。

    fully_depreciated 的判据是折旧因子 <= 0（rate*years >= 1），
    与"残值恰好为 0"区分开：price=0 时残值恒 0，但它不是"折完了"。
    """
    factor = Decimal("1") - rate * years
    fully = factor <= 0
    if factor < 0:
        factor = Decimal("0")
    return _money_to_float(price * factor), fully


def _normalize_asset_curve_months(months) -> int:
    """曲线月数收敛到 1~120：非法（非 int / bool / 越界）→ 默认 36（只读展示，不抛）。"""
    if isinstance(months, bool) or not isinstance(months, int):
        return ASSET_CURVE_MONTHS
    if months < ASSET_CURVE_MONTHS_MIN or months > ASSET_CURVE_MONTHS_MAX:
        return ASSET_CURVE_MONTHS
    return months


def compute_asset_value(purchase_price, purchase_date, depreciation_rate,
                        today=None) -> dict:
    """单件资产的当前残值与折旧指标（纯函数，不碰 DB）。

    参数校验（非法一律 ValueError）：
      · purchase_price：有限、>= 0（0 合法），Decimal 定量两位；
      · purchase_date：date / datetime / "YYYY-MM-DD"；
      · depreciation_rate：0~1 的小数（20% 传 0.2），>1 / <0 拒绝；
      · today：同上，默认 date.today()。

    返回字段（键名固定，页面/图表直接取）：
      price / purchase_date / depreciation_rate / elapsed_days / elapsed_years /
      elapsed_months / residual / residual_rate / monthly_loss / annual_loss /
      fully_depreciated / months_remaining
    """
    price = _normalize_asset_price(purchase_price)
    rate = _normalize_asset_rate(depreciation_rate)
    purchase = _normalize_asset_date(purchase_date, "购入日期")
    current = _normalize_forecast_today(today)

    elapsed_days = max(0, (current - purchase).days)
    years = _asset_elapsed_years(purchase, current)
    residual, fully_depreciated = _asset_residual(price, rate, years)
    price_float = _money_to_float(price)

    residual_rate = round(residual / price_float, 4) if price_float > 0 else 0.0
    annual_loss = _money_to_float(price * rate)
    # 已经折完的资产不再蒸发：否则页面会显示「已归零却还每月蒸发 ¥83」的自相矛盾数字
    monthly_loss = (
        0.0 if fully_depreciated
        else _money_to_float(price * rate / Decimal(12))
    )
    elapsed_months = round(float(years) * 12, 1)
    if rate > 0:
        # 通用式：(1/rate)*12 - elapsed_months（20% → 60 个月折完）。
        months_remaining = max(
            0.0, round(float(Decimal(12) / rate) - elapsed_months, 1)
        )
    else:
        months_remaining = None  # 不折旧 → 没有"折完"的时间点

    return {
        "price": price_float,
        "purchase_date": purchase.isoformat(),
        "depreciation_rate": float(rate),
        "elapsed_days": elapsed_days,
        "elapsed_years": round(float(years), 4),
        "elapsed_months": elapsed_months,
        "residual": residual,
        "residual_rate": residual_rate,
        "monthly_loss": monthly_loss,
        "annual_loss": annual_loss,
        "fully_depreciated": bool(fully_depreciated),
        "months_remaining": months_remaining,
    }


def compute_asset_curve(purchase_price, purchase_date, depreciation_rate,
                        months=36, today=None) -> list[dict]:
    """未来 months 个月的残值曲线（**纯函数**，不碰 DB）。

    返回 **months+1** 个点（含今天这个月）：第 k 个点 = 今天往后 k 个自然月，
    month 用「月份序号」平移（2026-12 之后是 2027-01，跨年正确），
    value 的折算口径是 elapsed_years + k/12（与 compute_asset_value 同一套）。
    months 收敛到 1~120，非法用 36。
    """
    price = _normalize_asset_price(purchase_price)
    rate = _normalize_asset_rate(depreciation_rate)
    purchase = _normalize_asset_date(purchase_date, "购入日期")
    current = _normalize_forecast_today(today)
    count = _normalize_asset_curve_months(months)

    years = _asset_elapsed_years(purchase, current)
    points = []
    for offset in range(count + 1):
        point_years = years + Decimal(offset) / Decimal(12)
        value, _ = _asset_residual(price, rate, point_years)
        points.append({
            "month": _asset_shift_month(current, offset),
            "value": value,
        })
    return points


def _asset_row_to_dict(row) -> dict:
    """把 public.assets 的一行转成给页面/图表用的 dict（键名与类型固定）。

    · purchase_date 统一成 "YYYY-MM-DD" 字符串（DATE 列原样，datetime 先取 .date()）；
    · 金额走 _money_to_float（Decimal + ROUND_HALF_UP，两位小数）；
    · category 再归一一次：历史脏值最多显示成"其他"，不让页面 KeyError。
    """
    mapping = row._mapping
    purchased = mapping["purchase_date"]
    if isinstance(purchased, datetime):
        purchased = purchased.date()
    purchase_text = (
        purchased.isoformat() if isinstance(purchased, date) else str(purchased)
    )
    return {
        "id": int(mapping["id"]),
        "name": str(mapping["name"] or ""),
        "category": normalize_asset_category(mapping["category"]),
        "purchase_price": _money_to_float(mapping["purchase_price"]),
        "purchase_date": purchase_text,
        "depreciation_rate": float(_as_decimal(mapping["depreciation_rate"])),
    }


def add_asset(user_id, name, category, purchase_price, purchase_date,
              depreciation_rate=ASSET_DEFAULT_DEPRECIATION) -> int:
    """新增一件资产，返回新 id；020 未执行抛 AssetTableMissing。

    参数校验**全部在开连接之前**完成：
      · user_id 复用 _normalize_bill_query_user_id（正整数，拒绝 bool/float）；
      · name：strip 后 1~100 个字符（与 020 的 assets_name_len CHECK 同口径）；
      · category：normalize_asset_category（脏值 → "其他"，绝不抛）；
      · purchase_price / depreciation_rate / purchase_date：见各自 normalize 函数。
    """
    uid = _normalize_bill_query_user_id(user_id)
    if not isinstance(name, str):
        raise ValueError(f"资产名称必须是字符串：{name!r}")
    cleaned_name = name.strip()
    if not 1 <= len(cleaned_name) <= ASSET_NAME_MAX_LEN:
        raise ValueError(
            f"资产名称必须是 1~{ASSET_NAME_MAX_LEN} 个字符：{name!r}"
        )
    category_value = normalize_asset_category(category)
    price = _normalize_asset_price(purchase_price)
    rate = _normalize_asset_rate(depreciation_rate)
    purchased = _normalize_asset_date(purchase_date, "购入日期")

    if not assets_available():
        raise AssetTableMissing(ASSET_TABLE_MISSING_MESSAGE)

    global _ASSETS_TABLE_SUPPORT
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    INSERT INTO public.assets
                        (user_id, name, category, purchase_price,
                         purchase_date, depreciation_rate)
                    VALUES
                        (:uid, :name, :category, :price, :purchased, :rate)
                    RETURNING id
                """),
                {
                    "uid": uid,
                    "name": cleaned_name,
                    "category": category_value,
                    "price": price,
                    "purchased": purchased,
                    "rate": rate,
                },
            ).fetchone()
            conn.commit()
    except SQLAlchemyError as exc:
        # 探测通过但 INSERT 撞 42P01（表刚被回滚 / 别的进程删了）→ 同样翻译成
        # AssetTableMissing，并同步缓存，后续调用直接走降级分支。
        if _is_undefined_table(exc):
            _ASSETS_TABLE_SUPPORT = False
            _warn_asset_once(
                "写入资产时 public.assets 不存在（020 未执行或被回滚）：资产台账暂不可用"
            )
            raise AssetTableMissing(ASSET_TABLE_MISSING_MESSAGE) from exc
        raise
    if row is None:
        raise RuntimeError("资产写入失败：数据库没有返回新 id")
    return int(row[0])


def _normalize_asset_id(asset_id) -> int:
    """asset_id 必须是正整数（拒绝 bool / float / 0 / 负数），非法抛中文 ValueError。"""
    if isinstance(asset_id, bool) or not isinstance(asset_id, (int, str)):
        raise ValueError(f"asset_id 必须是正整数：{asset_id!r}")
    try:
        aid = int(asset_id.strip()) if isinstance(asset_id, str) else int(asset_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"asset_id 必须是正整数：{asset_id!r}") from exc
    if aid <= 0:
        raise ValueError(f"asset_id 必须是正整数：{asset_id!r}")
    return aid


def delete_asset(user_id, asset_id) -> bool:
    """删除某用户名下的一件资产：rowcount > 0 才算删成功。

    DELETE 语句恒带 `AND user_id = :uid` —— 不存在"只按 id 删除"的入口，
    跨用户删除在 SQL 层面就不可能发生。020 未执行 / 删除异常 → False（不抛）。
    """
    uid = _normalize_bill_query_user_id(user_id)
    aid = _normalize_asset_id(asset_id)

    if not assets_available():
        return False

    global _ASSETS_TABLE_SUPPORT
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    DELETE FROM public.assets
                    WHERE id = :aid AND user_id = :uid
                """),
                {"aid": aid, "uid": uid},
            )
            conn.commit()
            return bool(result.rowcount and result.rowcount > 0)
    except SQLAlchemyError as exc:
        if _is_undefined_table(exc):
            _ASSETS_TABLE_SUPPORT = False
            _warn_asset_once(
                "删除资产时 public.assets 不存在（020 未执行或被回滚）：资产台账暂不可用"
            )
        else:
            _warn_asset_once("删除资产失败（库不可写 / 连接异常），本次按「未删除」处理")
        return False


def list_assets(user_id) -> list[dict]:
    """某用户的全部资产（purchase_date DESC, id DESC）。空表 / 异常 / 020 未执行 → []。

    读取侧刻意**不抛**：资产页是展示模块，库暂时不可用时显示"暂无资产"比红屏好；
    写接口才有 AssetTableMissing 的强契约。
    """
    uid = _normalize_bill_query_user_id(user_id)
    if not assets_available():
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, name, category, purchase_price,
                           purchase_date, depreciation_rate
                    FROM public.assets
                    WHERE user_id = :uid
                    ORDER BY purchase_date DESC, id DESC
                """),
                {"uid": uid},
            ).fetchall()
    except SQLAlchemyError:
        _warn_asset_once("读取资产台账失败（库不可用 / 表结构异常），本次返回空列表")
        return []
    return [_asset_row_to_dict(row) for row in rows]


def _empty_asset_overview(today: date) -> dict:
    """降级 / 空数据时返回的完整结构：键名与正常返回一致，调用方无需写分支。"""
    return {
        "today": today.isoformat(),
        "has_assets": False,
        "cash_balance": 0.0,
        "assets": [],
        "assets_cost": 0.0,
        "assets_residual": 0.0,
        "liabilities": 0.0,
        "monthly_payment": 0.0,
        "liability_note": ASSET_LIABILITY_NOTE,
        "net_worth": 0.0,
        "total_assets": 0.0,
        "asset_ratio": 0.0,
        "debt_ratio": 0.0,
    }


def _cash_balance(user_id: int) -> Decimal:
    """全历史现金结余 = 收入合计 - 支出合计（单条 SQL 聚合，Decimal）。

    先 `amount::numeric(12,2)` 再 SUM：bills.amount 是 FLOAT，直接 SUM 会带
    二进制浮点尾差（123.45000000000001）；先转 numeric 让库内聚合就是精确十进制。
    查询失败 → 0 并告警（资产页不该因为账单表异常整体红屏）。
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT
                        COALESCE(SUM(
                            CASE WHEN b.type = :income
                                 THEN b.amount::numeric(12, 2) ELSE 0 END
                        ), 0)
                        - COALESCE(SUM(
                            CASE WHEN b.type = :expense
                                 THEN b.amount::numeric(12, 2) ELSE 0 END
                        ), 0) AS cash_balance
                    FROM public.bills AS b
                    WHERE b.user_id = :uid
                """),
                {"uid": user_id, "income": INCOME_TYPE, "expense": EXPENSE_TYPE},
            ).fetchone()
    except SQLAlchemyError:
        _warn_asset_once("计算现金结余失败（bills 不可读），本次按 0 处理")
        return Decimal("0")
    if row is None:
        return Decimal("0")
    return _as_decimal(row[0])


def get_asset_overview(user_id, today=None) -> dict:
    """个人资产负债表总览：现金 + 资产残值 - 待还负债 = 净资产。

    返回字段（键名固定）：
      today / has_assets / cash_balance / assets / assets_cost / assets_residual /
      liabilities / monthly_payment / liability_note / net_worth / total_assets /
      asset_ratio / debt_ratio

    口径：
      · cash_balance：全历史 收入合计 - 支出合计（SQL 单条聚合，Decimal）；
      · assets：list_assets 的每行 + residual / residual_rate / monthly_loss /
        elapsed_months / fully_depreciated / curve（37 个点，app 的折线图直接用）；
      · liabilities / monthly_payment：**估算**，复用
        forecast_repayments(months_ahead=6, lookback_months=6) 的
        total_forecast / monthly_avg；该函数缺失或抛错 → 0 / 0 + 换提示，
        **绝不因此抛错**；
      · net_worth = cash_balance + assets_residual - liabilities；
      · total_assets = cash_balance + assets_residual；
      · asset_ratio = assets_residual / total_assets（无资产 / 分母 <= 0 → 0）；
      · debt_ratio = liabilities / total_assets（同上）。

    020 未执行 → _empty_asset_overview（全 0、assets=[]、has_assets False），不抛。
    """
    uid = _normalize_bill_query_user_id(user_id)
    current = _normalize_forecast_today(today)

    if not assets_available():
        return _empty_asset_overview(current)

    cash_balance = _money_to_float(_cash_balance(uid))

    # ---- 资产明细：每件都带 37 个点的残值曲线 ----
    assets: list[dict] = []
    cost_total = Decimal("0")
    residual_total = Decimal("0")
    for row in list_assets(uid):
        price = _as_decimal(row["purchase_price"])
        rate = _as_decimal(row["depreciation_rate"])
        purchased = _normalize_asset_date(row["purchase_date"], "购入日期")
        value = compute_asset_value(price, purchased, rate, today=current)
        curve = compute_asset_curve(
            price, purchased, rate, months=ASSET_CURVE_MONTHS, today=current
        )
        cost_total += price
        residual_total += _as_decimal(value["residual"])
        item = dict(row)
        item.update({
            "residual": value["residual"],
            "residual_rate": value["residual_rate"],
            "monthly_loss": value["monthly_loss"],
            "elapsed_months": value["elapsed_months"],
            "fully_depreciated": value["fully_depreciated"],
            "curve": curve,
        })
        assets.append(item)

    assets_cost = _money_to_float(cost_total)
    assets_residual = _money_to_float(residual_total)

    # ---- 负债估算：forecast_repayments 不可用/抛错都只降级，不阻断资产负债表 ----
    try:
        forecast = forecast_repayments(
            uid, months_ahead=6, lookback_months=6, today=current
        )
        liabilities = _money_to_float(forecast.get("total_forecast", 0))
        monthly_payment = _money_to_float(forecast.get("monthly_avg", 0))
        liability_note = ASSET_LIABILITY_NOTE
    except Exception:
        liabilities = 0.0
        monthly_payment = 0.0
        liability_note = ASSET_LIABILITY_UNAVAILABLE_NOTE
        _warn_asset_once("负债估算失败（forecast_repayments 不可用），本次按 0 处理")

    total_assets = _money_to_float(
        Decimal(str(cash_balance)) + Decimal(str(assets_residual))
    )
    net_worth = _money_to_float(
        Decimal(str(cash_balance))
        + Decimal(str(assets_residual))
        - Decimal(str(liabilities))
    )
    asset_ratio = round(assets_residual / total_assets, 4) if total_assets > 0 else 0.0
    debt_ratio = round(liabilities / total_assets, 4) if total_assets > 0 else 0.0

    return {
        "today": current.isoformat(),
        "has_assets": bool(assets),
        "cash_balance": cash_balance,
        "assets": assets,
        "assets_cost": assets_cost,
        "assets_residual": assets_residual,
        "liabilities": liabilities,
        "monthly_payment": monthly_payment,
        "liability_note": liability_note,
        "net_worth": net_worth,
        "total_assets": total_assets,
        "asset_ratio": asset_ratio,
        "debt_ratio": debt_ratio,
    }

# ===========================================================================
# 闲置资产断舍离 / 回血引擎（只读；不新建表、不写库）
# ---------------------------------------------------------------------------
# 判定口径（产品约定）：
#   「高危贬值闲置」= 已用满 IDLE_MIN_MONTHS 个月（按整月+零头天，与折旧同一套）
#                     + 年折旧率 >= IDLE_MIN_RATE
#                     + 当前残值 > 0（已经折成 0 的没有"回血"空间，不列）
#   两个阈值都可以从函数参数覆盖（答辩演示 / 以后调参不用改代码）。
#
#   为什么不做「很久没买相关配件」这条可选信号：assets 没有品类关键词，账单里
#   的商户名（"京东"、"食堂"）无法稳定映射到具体资产，强做只会把"电动车"和
#   "电动车充电器"混在一起，误判比不判更糟。本实现只用「时间 + 折旧率」，
#   在 docstring 与页面 caption 里都写清楚口径，不假装更聪明。
#
# 回血估价（estimate_resale_value）：
#   二手挂牌价 = 当前残值 × 品类系数（数码 0.85 / 生活 0.70 / 其他 0.60），
#   再四舍五入到 10 元（闲鱼挂牌习惯），最低 10 元；
#   系数是"平台折价"经验值，不是真实成交价 —— 页面必须写"估算/参考"。
# ===========================================================================

IDLE_MIN_MONTHS = 6
IDLE_MIN_RATE = Decimal("0.15")     # 15%/年以下不算"疯狂贬值"
IDLE_HIGH_RATE = Decimal("0.20")    # >=20% 标「高危」
RESALE_FACTORS = {
    "数码": Decimal("0.85"),
    "生活": Decimal("0.70"),
    "其他": Decimal("0.60"),
}
RESALE_MIN_PRICE = Decimal("10")


def _resale_factor(category) -> Decimal:
    """品类 → 二手折价系数；未知分类按「其他」。"""
    return RESALE_FACTORS.get(normalize_asset_category(category), RESALE_FACTORS["其他"])


def _round_to_ten(value: Decimal) -> Decimal:
    """四舍五入到 10 元（闲鱼挂牌习惯）：2432 → 2430，2436 → 2440。

    注意必须用 Decimal("1E1") 而不是 Decimal("10")：后者指数是 0，
    quantize 会退化成「取整到元」（2432.139 → 2432），不是取整到十位。
    """
    return value.quantize(Decimal("1E1"), rounding=ROUND_HALF_UP)


def estimate_resale_value(purchase_price, purchase_date, depreciation_rate,
                          category="其他", today=None) -> dict:
    """单件资产的「二手回血估价」（纯函数，不碰 DB）。

    返回：price / purchase_date / depreciation_rate / category / residual /
          residual_rate / elapsed_months / monthly_loss / fully_depreciated /
          resale_factor / suggested_price

    suggested_price = max(10, round10(残值 × 品类系数))；残值为 0 时给 0。
    """
    value = compute_asset_value(
        purchase_price, purchase_date, depreciation_rate, today=today
    )
    factor = _resale_factor(category)
    residual = _as_decimal(value["residual"])
    if residual <= 0:
        suggested = Decimal("0")
    else:
        suggested = max(RESALE_MIN_PRICE, _round_to_ten(residual * factor))
    return {
        "price": value["price"],
        "purchase_date": value["purchase_date"],
        "depreciation_rate": value["depreciation_rate"],
        "category": normalize_asset_category(category),
        "residual": value["residual"],
        "residual_rate": value["residual_rate"],
        "elapsed_months": value["elapsed_months"],
        "monthly_loss": value["monthly_loss"],
        "fully_depreciated": value["fully_depreciated"],
        "resale_factor": float(factor),
        "suggested_price": _money_to_float(suggested),
    }


def get_idle_assets(user_id, today=None, min_months: int = IDLE_MIN_MONTHS,
                    min_rate=IDLE_MIN_RATE) -> dict:
    """「断舍离集市」数据：哪些资产正在疯狂贬值、每月蒸发多少钱。

    返回字段（键名固定）：
      today / has_idle / count / checked / monthly_loss_total / min_months /
      min_rate / items

    items[k] = list_assets 的一行 + 以下字段：
      idle_months / idle_level("high"/"medium") / monthly_loss / monthly_loss_ratio /
      residual / residual_rate / fully_depreciated / resale_factor / suggested_price

    规则：
      · 只读 assets 表（assets_available False → 空结构，不抛）；
      · 满 min_months 个月 + 年折旧率 >= min_rate + 残值 > 0 才进集市；
      · idle_level：rate >= 20% → "high"，否则 "medium"（页面可据此换文案）；
      · monthly_loss_ratio = 每月蒸发 / 当前残值（残值 0 → 0），用于「贬值速度」展示；
      · 排序：每月蒸发降序 → 已用月数降序 → 名称升序（结果可复现）；
      · monthly_loss_total = 各件每月蒸发之和（Decimal 累加，两位小数）；
      · checked = 资产总数（页面可写「3 件资产里有 2 件在贬值」）。
    """
    uid = _normalize_bill_query_user_id(user_id)
    current = _normalize_forecast_today(today)
    months_threshold = IDLE_MIN_MONTHS if not isinstance(min_months, int) else max(1, min_months)
    try:
        rate_threshold = _as_decimal(min_rate)
    except (InvalidOperation, TypeError, ValueError):
        rate_threshold = IDLE_MIN_RATE
    if rate_threshold < 0 or rate_threshold > 1:
        rate_threshold = IDLE_MIN_RATE

    if not assets_available():
        return {
            "today": current.isoformat(),
            "has_idle": False,
            "count": 0,
            "checked": 0,
            "monthly_loss_total": 0.0,
            "min_months": months_threshold,
            "min_rate": float(rate_threshold),
            "items": [],
        }

    rows = list_assets(uid)
    items: list[dict] = []
    loss_total = Decimal("0")
    for row in rows:
        value = compute_asset_value(
            row["purchase_price"], row["purchase_date"],
            row["depreciation_rate"], today=current,
        )
        rate = _as_decimal(row["depreciation_rate"])
        residual = _as_decimal(value["residual"])
        if value["elapsed_months"] < months_threshold:
            continue
        if rate < rate_threshold:
            continue
        if residual <= 0:
            continue
        resale = estimate_resale_value(
            row["purchase_price"], row["purchase_date"],
            row["depreciation_rate"], category=row["category"], today=current,
        )
        monthly_loss = _as_decimal(value["monthly_loss"])
        loss_total += monthly_loss
        item = dict(row)
        item.update({
            "idle_months": value["elapsed_months"],
            "idle_level": "high" if rate >= IDLE_HIGH_RATE else "medium",
            "monthly_loss": value["monthly_loss"],
            "monthly_loss_ratio": (
                round(float(monthly_loss / residual), 4) if residual > 0 else 0.0
            ),
            "residual": value["residual"],
            "residual_rate": value["residual_rate"],
            "fully_depreciated": value["fully_depreciated"],
            "resale_factor": resale["resale_factor"],
            "suggested_price": resale["suggested_price"],
        })
        items.append(item)

    items.sort(key=lambda item: (
        -item["monthly_loss"], -item["idle_months"], item["name"]
    ))
    return {
        "today": current.isoformat(),
        "has_idle": bool(items),
        "count": len(items),
        "checked": len(rows),
        "monthly_loss_total": _money_to_float(loss_total),
        "min_months": months_threshold,
        "min_rate": float(rate_threshold),
        "items": items,
    }


# ===========================================================================
# 全站宏观统计（get_macro_stats）
# ---------------------------------------------------------------------------
# 本段只**追加**：不改动上面任何既有函数、常量与取数口径。
#
# 定位：给「我 vs 全站」的对比卡片与 AI 解读提供一份**匿名聚合**数据。
#   · 每人等权：先算个人指标，再对有支出的用户取算术平均 —— 大额/多笔用户
#     不会把均值拉偏（与 get_peer_benchmark 同源的权重口径）；
#   · 只统计当月**有支出**的用户：0 支出用户的恩格尔系数、分类结构、商户榜
#     没有对比意义，留在样本里只会把全站均值稀释成「没人消费」；
#   · 返回体里绝不出现别人的 user_id、单笔明细或可反查的商户组合 ——
#     唯一允许的用户标识是 me.user_id（调用方自己）。
#
# 与 get_peer_benchmark 的关系：同为「每人等权 + 当月账单」，但本函数面向宏观
# 画像（恩格尔系数、分类结构、商户榜、结余率），因此不复用它的四维得分。
# ===========================================================================

MACRO_MIN_USERS = 5        # 样本少于这个数 → low_sample=True（均值照给，仅提示参考性弱）
MACRO_TOP_MERCHANTS = 3    # 全站商户榜取前几名

# 分类列缺失（迁移 012 未执行）时的固定文案：与 _PEER_CATEGORY_MISSING 同一个降级约定
# （RuntimeError + 中文 + 可操作，页面 try/except 接住后隐藏模块，不红屏）。
_MACRO_CATEGORY_MISSING = (
    "宏观统计失败：分类功能未启用，请先执行 migrations/012_bills_category.sql"
)

# 输出键序固定成 BILL_CATEGORIES 的顺序：dict 有序，前端/AI 每次拿到的键序可复现，
# 测试也能逐字比对。不在这个集合里的历史脏分类统一归 FALLBACK_CATEGORY，
# 保证「各分类占比之和 = 1」不会因为一个陌生标签而漏掉一块。
_MACRO_CATEGORY_KEYS = tuple(BILL_CATEGORIES)


def _macro_uid(user_id):
    """把 user_id 收敛成 int / None：None、布尔、空串、非数字一律 None。

    None 表示「没有『我』这个视角」：不查个人数据、me 全 0、delta 按 0 参与减法。
    绝不在这里抛错 —— 页面把 session 里的脏值透传进来时也只该降级，不该红屏。
    """
    if user_id is None or isinstance(user_id, bool):
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _macro_money(value) -> float:
    """金额 → float，两位小数（ROUND_HALF_UP，与 NUMERIC(12,2) / _CENT 同口径）。

    脏值（None / 非数字 / NaN / inf）→ 0.0：宏观统计绝不能因为一行脏数据抛错。
    """
    try:
        return _money_to_float(_radar_decimal(value))
    except (InvalidOperation, OverflowError, ValueError):
        return 0.0


def _macro_money_avg(values) -> float:
    """多用户金额指标的算术平均（每人等权），Decimal 求和后再定量到分。"""
    values = list(values)
    if not values:
        return 0.0
    total = sum((_radar_decimal(value) for value in values), Decimal("0"))
    return _macro_money(total / Decimal(len(values)))


def _macro_ratio(numerator, denominator, *, clamp: bool = False) -> float:
    """比率 = numerator / denominator，保留 4 位小数；分母 <= 0 → 0.0，NaN/inf → 0.0。

    clamp=True 只用于「占比」（0..1，浮点误差不该把 0.999999 显示成 1.0001）；
    结余率与其差值一律不 clamp —— 允许为负，硬夹会把「平均透支」显示成 0。
    """
    num = _radar_decimal(numerator)
    den = _radar_decimal(denominator)
    try:
        if den <= 0:
            return 0.0
        value = float(num / den)
    except (InvalidOperation, OverflowError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    if clamp:
        value = max(0.0, min(1.0, value))
    return round(value, 4)


def _macro_saving_rate(income, expense):
    """结余率 = (income - expense) / income；income <= 0 → None（不参与全站均值）。

    与 _peer_metrics_from_row 的 saving_rate 刻意不同：那边用 -1.0 当作「纯花钱」
    的哨兵给雷达图打分；宏观统计要的是「能对比的真实比率」，所以这里如实返回 None。
    """
    income_dec = _radar_decimal(income)
    if income_dec <= 0:
        return None
    return _macro_ratio(income_dec - _radar_decimal(expense), income_dec)


def _macro_number(value, digits: int = 4) -> float:
    """把差值 / 比率收敛成有限 float 并保留 digits 位（NaN / inf / 非数字 → 0.0）。

    差值是「我 − 全站」，允许为负、也允许超过 1（例如结余率我 0.8、全站 -0.5），
    所以这里不做 [0, 1] 夹取，只保证「一定是有限值」。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(number, digits)


def _macro_count_avg(values) -> float:
    """笔数均值：保留 2 位（1 人 10 笔 + 1 人 1 笔 → 5.5 笔，取整会丢信息）。"""
    values = list(values)
    if not values:
        return 0.0
    try:
        total = sum(int(value) for value in values)
        value = total / len(values)
    except (TypeError, ValueError):
        return 0.0
    return round(value, 2) if math.isfinite(value) else 0.0


def get_macro_stats(user_id=None, month=None, today=None) -> dict:
    """统计某个月全站「每人等权」的宏观指标，并与指定用户做「我 − 全站」差值。

    参数
    ----
    user_id : 要对比的「我」。None / 不存在 / 当月无账单 → me.has_data=False，其余按 0。
    month   : "YYYY-MM"；None → today 所在月。非法格式直接 ValueError
              （复用 _parse_report_month，在开连接之前就拒绝）。
    today   : 默认北京时间今天；只用于 month=None 时确定「本月」。

    口径
    ----
    · 一条只读 SQL 取 [月初, 下月初) 的账单（date::date 比较、参数化），
      在 Python 里按 user_id 聚合 —— 学生项目的数据量，单查询完全够用；
    · 个人指标：expense / income / bill_count（收入 + 支出笔数）/
      engel（餐饮支出 / 总支出，无支出 → 0.0）/
      saving_rate（(income-expense)/income，income<=0 → None）/
      category_shares（各支出分类占个人支出的比例，只保留真正出现过的分类）/
      top_merchant（个人「消费笔数」最多的商户：笔数降序 → 金额降序 → 名称升序）；
    · 全站均值只对**当月有支出**（expense > 0）的用户取算术平均：
      avg_expense / avg_income / avg_engel（对每人的恩格尔系数取平均，不是金额合并）/
      avg_saving_rate（只对有收入用户取平均，一个都没有 → 0.0）/ avg_bill_count；
      category_shares 逐分类取平均（缺失分类按 0 参与，保证每人权重相同）；
      top_merchants 按「去重用户数降序 → 笔数降序 → 金额降序」取前 MACRO_TOP_MERCHANTS 个；
    · delta 一律「我 − 全站」：expense（元）/ expense_ratio（avg<=0 → None）/
      engel（小数差）/ saving_rate（小数差，任一侧 None → None）；
    · 金额保留 2 位小数、比率保留 4 位小数，所有 float 保证有限（NaN/inf → 0.0）；
    · 隐私：只返回聚合值，唯一出现的用户标识是 me.user_id。

    异常
    ----
    · bills.category 不存在（迁移 012 未执行）→ RuntimeError（中文、可操作提示）；
    · 数据库异常（含历史脏日期导致 date::date cast 失败）→ RuntimeError(
      "宏观统计失败：..."），文本已脱敏（_peer_error_text），日志只写脱敏原因；
    · 没有账单 → **不抛错**：user_count=0、has_data=False、global 全 0 / 空 dict / 空 list，
      me 各 0、delta 无假数据（expense_ratio / saving_rate 为 None）。
    """
    today_date = _radar_normalize_today(today)
    # 先解析再 strip：month 不是字符串时让 _parse_report_month 抛 ValueError（对外契约），
    # 而不是在这里先吃一个 AttributeError。
    raw_month = (
        f"{today_date.year:04d}-{today_date.month:02d}" if month is None else month
    )
    month_start, month_end = _parse_report_month(raw_month)
    month_text = raw_month.strip()
    month_label = _radar_month_label(month_text)
    me_uid = _macro_uid(user_id)

    if engine is None:
        try:
            init_db()
        except Exception as exc:  # noqa: BLE001 - 统一包装成页面能接住的 RuntimeError
            # 只记脱敏后的一句话：SQLAlchemy 的异常文本里可能带连接串 / 密码。
            logging.getLogger(__name__).warning(
                "[db] 宏观统计前初始化数据库失败：%s", _peer_error_text(exc)
            )
            raise RuntimeError(f"宏观统计失败：{_peer_error_text(exc)}") from exc

    # 分类列不存在时，恩格尔系数 / 分类结构没有口径可言：显式抛错让页面隐藏模块，
    # 而不是悄悄返回一堆 0（那会被误读成「大家都很健康」）。
    if not _bills_category_supported():
        raise RuntimeError(_MACRO_CATEGORY_MISSING)

    # 唯一一条取数 SQL：只读、只取聚合需要的五个字段、全部绑定参数（不拼接）。
    # date 是 VARCHAR，统一 ::date 转换后按 [月初, 下月初) 比较（与日报 / 雷达同口径）。
    sql = text("""
        SELECT b.user_id,
               b.amount,
               b.type,
               COALESCE(NULLIF(btrim(b.category), ''), :fallback) AS category,
               COALESCE(NULLIF(btrim(b.merchant), ''), '') AS merchant
        FROM public.bills b
        WHERE b.date::date >= :start
          AND b.date::date <  :end
    """)
    params = {
        "start": month_start,
        "end": month_end,
        "fallback": FALLBACK_CATEGORY,
    }
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001 - 统一包装成 RuntimeError，且文本脱敏
        logging.getLogger(__name__).warning(
            "[db] 宏观统计失败（SQL 执行阶段）：%s", _peer_error_text(exc)
        )
        raise RuntimeError(f"宏观统计失败：{_peer_error_text(exc)}") from exc

    # ------------------------------------------------------------------
    # 第一步：按 user_id 聚合原始账单（user_id 为 NULL 的账单无法归到人头上，
    # 不参与「每人等权」的均值；商户榜同样只由这些能归属的消费构成）。
    # ------------------------------------------------------------------
    buckets: dict[int, dict] = {}
    for row in rows:
        uid = _macro_uid(getattr(row, "user_id", None))
        if uid is None:
            continue
        amount = _radar_decimal(getattr(row, "amount", None))
        bill_type = getattr(row, "type", None)
        category = getattr(row, "category", None)
        if category not in _MACRO_CATEGORY_KEYS:
            category = FALLBACK_CATEGORY
        merchant = str(getattr(row, "merchant", None) or "").strip()

        bucket = buckets.get(uid)
        if bucket is None:
            bucket = buckets[uid] = {
                "expense": Decimal("0"),
                "income": Decimal("0"),
                "bill_count": 0,
                "categories": {},
                "merchants": {},
            }
        bucket["bill_count"] += 1
        if bill_type == EXPENSE_TYPE:
            bucket["expense"] += amount
            bucket["categories"][category] = (
                bucket["categories"].get(category, Decimal("0")) + amount
            )
            if merchant:
                entry = bucket["merchants"].get(merchant)
                if entry is None:
                    entry = bucket["merchants"][merchant] = [0, Decimal("0")]
                entry[0] += 1
                entry[1] += amount
        elif bill_type == INCOME_TYPE:
            bucket["income"] += amount
        # 其它脏 type：不计入任何一侧（与 get_peer_benchmark 的 `s.type = :expense` 同口径），
        # 但仍算一笔账单（bill_count），不会凭空消失。

    # ------------------------------------------------------------------
    # 第二步：每人等权的个人指标（唯一公式来源；me 与全站均值都从这里取）。
    # ------------------------------------------------------------------
    person_metrics: dict[int, dict] = {}
    for uid, bucket in buckets.items():
        expense_dec = bucket["expense"]
        income_dec = bucket["income"]
        expense_value = _macro_money(expense_dec)
        income_value = _macro_money(income_dec)

        category_shares = {}
        engel = 0.0
        if expense_dec > 0:
            for name in _MACRO_CATEGORY_KEYS:
                amount = bucket["categories"].get(name)
                if amount is not None and amount > 0:
                    category_shares[name] = _macro_ratio(amount, expense_dec, clamp=True)
            engel = _macro_ratio(
                bucket["categories"].get("餐饮", Decimal("0")), expense_dec, clamp=True
            )

        top_merchant = None
        if bucket["merchants"]:
            ranked = sorted(
                bucket["merchants"].items(),
                key=lambda item: (-item[1][0], -item[1][1], item[0]),
            )
            name, (count, amount) = ranked[0]
            top_merchant = {
                "name": name,
                "count": int(count),
                "amount": _macro_money(amount),
            }

        person_metrics[uid] = {
            "user_id": uid,
            "expense": expense_value,
            "income": income_value,
            "engel": engel,
            "saving_rate": _macro_saving_rate(income_dec, expense_dec),
            "bill_count": int(bucket["bill_count"]),
            "category_shares": category_shares,
            "top_merchant": top_merchant,
        }

    # ------------------------------------------------------------------
    # 第三步：全站均值（只取当月有支出的用户；顺序按 uid 排序，结果可复现）。
    # ------------------------------------------------------------------
    sample_uids = sorted(
        uid for uid, metrics in person_metrics.items() if metrics["expense"] > 0
    )
    sample = [person_metrics[uid] for uid in sample_uids]

    if sample:
        avg_expense = _macro_money_avg(metrics["expense"] for metrics in sample)
        avg_income = _macro_money_avg(metrics["income"] for metrics in sample)
        avg_engel = _macro_ratio(
            sum(metrics["engel"] for metrics in sample), len(sample)
        )
        saving_rates = [
            metrics["saving_rate"]
            for metrics in sample
            if metrics["saving_rate"] is not None
        ]
        avg_saving_rate = (
            _macro_ratio(sum(saving_rates), len(saving_rates)) if saving_rates else 0.0
        )
        avg_bill_count = _macro_count_avg(
            metrics["bill_count"] for metrics in sample
        )
        share_keys = [
            name
            for name in _MACRO_CATEGORY_KEYS
            if any(name in metrics["category_shares"] for metrics in sample)
        ]
        category_shares = {
            name: _macro_ratio(
                sum(metrics["category_shares"].get(name, 0.0) for metrics in sample),
                len(sample),
            )
            for name in share_keys
        }
    else:
        avg_expense = 0.0
        avg_income = 0.0
        avg_engel = 0.0
        avg_saving_rate = 0.0
        avg_bill_count = 0.0
        category_shares = {}

    # 全站商户榜：在**样本用户**范围内聚合「消费笔数」，同一个商户按去重用户数计权重。
    merchant_stats: dict[str, dict] = {}
    for uid in sample_uids:
        for name, (count, amount) in buckets[uid]["merchants"].items():
            entry = merchant_stats.get(name)
            if entry is None:
                entry = merchant_stats[name] = {
                    "users": set(),
                    "count": 0,
                    "amount": Decimal("0"),
                }
            entry["users"].add(uid)
            entry["count"] += count
            entry["amount"] += amount
    ranked_merchants = sorted(
        merchant_stats.items(),
        key=lambda item: (
            -len(item[1]["users"]), -item[1]["count"], -item[1]["amount"], item[0]
        ),
    )
    top_merchants = [
        {
            "name": name,
            "users": len(entry["users"]),
            "count": int(entry["count"]),
            "amount": _macro_money(entry["amount"]),
        }
        for name, entry in ranked_merchants[:MACRO_TOP_MERCHANTS]
    ]

    # ------------------------------------------------------------------
    # 第四步：我的视角 + 差值（「我 − 全站」；没有个人数据时「我」按 0 参与减法）。
    # ------------------------------------------------------------------
    me_person = person_metrics.get(me_uid) if me_uid is not None else None
    me_has_data = bool(me_person) and me_person["bill_count"] > 0
    if me_has_data:
        me = {
            "user_id": me_uid,
            "has_data": True,
            "expense": me_person["expense"],
            "income": me_person["income"],
            "engel": me_person["engel"],
            "saving_rate": me_person["saving_rate"],
            "bill_count": me_person["bill_count"],
            "category_shares": dict(me_person["category_shares"]),
            "top_merchant": (
                dict(me_person["top_merchant"]) if me_person["top_merchant"] else None
            ),
        }
    else:
        me = {
            "user_id": me_uid,
            "has_data": False,
            "expense": 0.0,
            "income": 0.0,
            "engel": 0.0,
            "saving_rate": None,
            "bill_count": 0,
            "category_shares": {},
            "top_merchant": None,
        }

    me_expense = me["expense"]
    me_saving = me["saving_rate"]
    delta = {
        "expense": _macro_money(_as_decimal(me_expense) - _as_decimal(avg_expense)),
        "expense_ratio": (
            _macro_ratio(me_expense, avg_expense) if avg_expense > 0 else None
        ),
        "engel": _macro_number(me["engel"] - avg_engel),
        "saving_rate": (
            _macro_number(me_saving - avg_saving_rate)
            if me_saving is not None and avg_saving_rate is not None
            else None
        ),
    }

    user_count = len(sample)
    return {
        "month": month_text,
        "month_label": month_label,
        "user_count": user_count,
        "min_users": MACRO_MIN_USERS,
        "low_sample": user_count < MACRO_MIN_USERS,
        "has_data": user_count > 0,
        "global": {
            "avg_expense": avg_expense,
            "avg_income": avg_income,
            "avg_engel": avg_engel,
            "avg_saving_rate": avg_saving_rate,
            "avg_bill_count": avg_bill_count,
            "category_shares": category_shares,
            "top_merchants": top_merchants,
        },
        "me": me,
        "delta": delta,
    }
