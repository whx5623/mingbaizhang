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

def add_bill(user_id, date, merchant, amount, payment_method, type, category=None):
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
    # 列存在性探测放在开写连接之前：避免在一条已检出的连接上再借第二条连接做元数据查询。
    write_category = _bills_category_supported()
    with engine.connect() as conn:
        if write_category:
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat)
            """)
            params = {
                "uid": user_id,
                "dt": bill_date,
                "mer": merchant,
                "amt": amt,
                "pm": payment_method,
                "tp": bill_type,
                "cat": category_value,
            }
        else:
            # 迁移 012 未执行：列不存在，INSERT 不能带 category，否则整笔账单都会失败。
            # 降级只发生在这里，告警由 _bills_category_supported() 负责（每个进程一次）。
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
            """)
            params = {
                "uid": user_id,
                "dt": bill_date,
                "mer": merchant,
                "amt": amt,
                "pm": payment_method,
                "tp": bill_type,
            }
        conn.execute(stmt, params)
        conn.commit()

def get_all_bills(user_id):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid")
    df = pd.read_sql(sql, engine, params={"uid": user_id})
    # 顺序无所谓，但三列都必须收口：type 归一化（字符串口径）+ amount 转 float（数值口径）
    # + category 空值收敛（列不存在时原样返回，绝不凭空造列）。
    return _normalize_category_column(_normalize_type_column(_normalize_amount_column(df)))

def get_bills_paginated(user_id, limit, offset):
    global engine
    sql = text("SELECT * FROM bills WHERE user_id = :uid ORDER BY date DESC LIMIT :lim OFFSET :off")
    df = pd.read_sql(sql, engine, params={"uid": user_id, "lim": limit, "off": offset})
    return _normalize_category_column(_normalize_type_column(_normalize_amount_column(df)))

def get_total_bill_count(user_id):
    global engine
    with engine.connect() as conn:
        stmt = text("SELECT COUNT(id) FROM bills WHERE user_id = :uid")
        return conn.execute(stmt, {"uid": user_id}).scalar() or 0

def insert_demo_bills(user_id):
    global engine
    demo_data = [
        {"date":"2026-10-01","merchant":"食堂","amount":12.5,"payment_method":"校园卡","type":"支出"},
        {"date":"2026-10-02","merchant":"蜜雪冰城","amount":8.0,"payment_method":"微信","type":"支出"},
        {"date":"2026-10-03","merchant":"淘宝网购","amount":129.0,"payment_method":"支付宝","type":"支出"},
        {"date":"2026-10-04","merchant":"兼职","amount":400.0,"payment_method":"银行卡","type":"收入"},
        {"date":"2026-10-05","merchant":"白条分期","amount":99.0,"payment_method":"白条","type":"支出"},
        {"date":"2026-10-06","merchant":"食堂","amount":15.0,"payment_method":"校园卡","type":"支出"},
        {"date":"2026-10-07","merchant":"瑞幸咖啡","amount":14.0,"payment_method":"微信","type":"支出"},
        {"date":"2026-10-08","merchant":"拼多多","amount":45.5,"payment_method":"支付宝","type":"支出"},
        {"date":"2026-10-09","merchant":"家教兼职","amount":300.0,"payment_method":"微信","type":"收入"},
        {"date":"2026-10-10","merchant":"超市","amount":56.2,"payment_method":"支付宝","type":"支出"},
    ]
    write_category = _bills_category_supported()
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM bills WHERE user_id = :uid"), {"uid": user_id})
        if write_category:
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type, category)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp, :cat)
            """)
        else:
            # 012 未执行：不带 category 写入（降级，记账优先）
            stmt = text("""
                INSERT INTO bills(user_id, date, merchant, amount, payment_method, type)
                VALUES (:uid, :dt, :mer, :amt, :pm, :tp)
            """)
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
