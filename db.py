from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
import pandas as pd
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


def auto_categorize(merchant, description="", api_key=None) -> str:
    """给一笔账单推断分类，返回 BILL_CATEGORIES 中的一个（失败一律 "其他"）。

    优先级：
      ① 关键词规则（CATEGORY_RULES 顺序，命中即返回）：零延迟、零成本、可复现；
      ② 规则未命中且 api_key 非空 → 懒加载 ai_agent.classify_category 问一次 AI；
      ③ 其余所有情况（无 key / 空输入 / AI 异常 / AI 返回值不在枚举内）→ "其他"。

    为什么 from ai_agent import classify_category 放在函数内部：
    db.py 与 ai_agent.py 在 `import db` 时不能互相顶层 import（循环依赖，
    ai_agent 顶层还会立刻 create_client 初始化 Supabase）。放到函数里既避开
    循环依赖，也让"没配 AI 的部署"完全不加载 ai_agent。

    这个函数**永不抛异常**：调用方（add_bill / CSV 导入 / 示例数据）都把它当
    可选信息，分类失败只能降级，不能影响记账。
    """
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
        return category
    logging.getLogger(__name__).warning(
        "[db] AI 返回分类 %r 不在 BILL_CATEGORIES 内，回落「%s」（商户=%r）",
        category, FALLBACK_CATEGORY, merchant_text[:50],
    )
    return FALLBACK_CATEGORY
