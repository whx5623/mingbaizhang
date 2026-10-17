-- =============================================================================
-- 007_bill_date_check.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.bills 加一条 CHECK 约束 bills_date_iso_check，把 date 列钉死成
--   **零填充 ISO 'YYYY-MM-DD'、且是真实存在的日历日**，作为写入侧的数据库兜底。
--
--   为什么需要它（bills.date 是 VARCHAR/TEXT，不是 DATE）：
--     读取侧三处全部靠"字符串"工作，只有在值是严格 ISO 时才成立（位数固定 →
--     字典序 == 日期序）：
--       · app.py 分页：            ORDER BY date DESC
--       · 管理员活跃图（db.py）：   WHERE date >= :start_text AND date < :end_text
--       · pandas 归期：            pd.to_datetime(..., errors="coerce")
--     一旦库里出现 '2026/10/1' / '2026-1-5' / '' / 'abc'，后果都是**静默**的：
--       · 活跃图少算：实测 '2026/10/1' >= '2026-10-11' 为 FALSE → 该行被当成
--         "不在窗口内"，不报错不告警，图上凭空少一天；
--       · 分页排序错乱：字典序会把 '2026-1-5' 排到 '2026-10-17' 之后；
--       · pd.to_datetime(errors="coerce") 把解析不了的行变成 NaT 直接丢掉。
--     写入侧（db.py::_normalize_bill_date）已经在 Python 层拦了一遍：
--     add_bill / import_csv_to_bills / insert_demo_bills 三条路径都过同一个校验。
--     本脚本是**第二道防线**，它挡的是绕过 db.py 的写入：手工 SQL、别的脚本、
--     别的服务、以后的 API。
--
--   背景事实（别被"现有数据全是 ISO"骗了）：改版前三条写入路径都是**原样入库**，
--   没有任何格式校验；现网恰好没有脏值，属于运气，不是保证。
--
-- 【本脚本只做"校验现有数据 + 加约束"，不改任何数据、不改列类型】
--   · 不把 date 改成 DATE 类型（那会引入"会话时区折日"和读取侧 cast 的一整套问题）；
--   · 不迁移/改写历史日期值（'2026/10/1' 到底该是 10 月 1 日还是 1 月 5 日，
--     是业务判断，脚本猜就等于伪造账目）；
--   · 有非法行时**直接 RAISE EXCEPTION 中止**，把样本交给人工处理。
--
-- =============================================================================
-- 【执行顺序：001 → 002 → 003 → 004 → 005 → 006 → 007（本文件），必须最后执行】
-- =============================================================================
--   001_normalize_bill_type.sql               账单类型脏值清洗
--   002_fk_cascade_delete.sql                 外键改 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql  amount 精度 + 约束
--   004_password_bcrypt.sql                   密码明文 → bcrypt + 兜底 CHECK 约束
--   005_last_login.sql                        users.last_login 列 + 索引
--   006_user_admin_flag.sql                   users.is_admin 列
--   007_bill_date_check.sql（本文件）          bills.date 的 ISO + 日历日 CHECK
--   与 003 的关系：003 在 bills 上已经加了两条 CHECK（bills_type_check /
--   bills_expense_amount_positive）。本脚本只**新增**第三条，不去动那两条；
--   但本脚本的验证探针要 INSERT 一行临时数据，必须同时满足那两条约束
--   （type = '支出'、amount = 1），否则探针会被别的约束拦下、核对结论失真。
--   排在最后还有一个好处：本脚本的预检是在 001–003 洗过类型/金额之后才跑的，
--   看到的计数不会被那些历史脏值干扰。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/007_bill_date_check.sql
--                  （PowerShell：psql $env:DATABASE_URL -f migrations\007_bill_date_check.sql）
--   ⚠ 必须**整段一次执行**，不要逐句执行：脚本依赖 BEGIN … COMMIT 这一个事务，
--     逐句执行（每条自动提交）就失去了"任何一步失败 → 整体回滚"的保证。
--   ⚠ 本库 DATABASE_URL 走 Supabase pooler（6543 / transaction 模式）：显式
--     BEGIN … COMMIT 的整段脚本会被固定到同一个后端连接，事务语义正常；
--     但**不要**在 BEGIN 与 COMMIT 之间手工断连，也别把断点调试夹在中间。
--
-- =============================================================================
-- 【⚠ 本脚本不会被自动执行（未自动执行声明）】
-- =============================================================================
--   项目里没有任何代码会执行 migrations/ 下的 .sql：
--     · db.py 的 init_db() 只跑 CREATE TABLE IF NOT EXISTS，不建约束、不补列；
--     · 001–006 也全靠人手工执行，本脚本保持同一约定。
--   没执行时的行为（优雅降级，不是红屏）：
--     · db.py 侧照样拦非法日期（ValueError → 中文提示），功能不受影响；
--     · 只是少了"绕过 Python 直接写库"这一层保护，以及对历史脏值的体检。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本**不写、不改、不删任何业务数据**（唯一的写操作是步骤 3 的自检探针，
--   用 SAVEPOINT/子事务包住并当场删除/回滚），所以数据级备份不是必需的。
--   仍然建议先留一份"执行前的非法行清单"，便于事后核对人工修数据的范围：
--       psql "$DATABASE_URL" -c "SELECT id, date FROM public.bills WHERE NOT (date IS NOT NULL AND date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');"
--   结构快照（只为回滚结构用，可选）：
--       pg_dump --schema-only -t public.bills "$DATABASE_URL" > bills_schema_before_007.sql
--   ⛔ 不要用 `CREATE TABLE bills_backup_YYYYMMDD AS SELECT * FROM bills;` 这种
--      "库内备份表"：public schema 默认经 PostgREST 对外暴露、新表默认不开 RLS，
--      等于顺手造了一份可远程读取的账单副本。
--
-- =============================================================================
-- 【回滚（结构级，自动提交即可）】
-- =============================================================================
--   ALTER TABLE public.bills DROP CONSTRAINT IF EXISTS bills_date_iso_check;
--   DROP 之后 db.py 的日期校验照旧工作（它在 Python 侧，不依赖本约束），
--   不需要同步改代码，也不会红屏；只是"绕过 db.py 直接写库"这一层兜底没了。
--
-- =============================================================================
-- 【为什么约束长这样 —— 一个"绝不抛异常"的日期表达式】
-- =============================================================================
--   约束表达式（步骤 1 的 v_expr 是**唯一**定义处：预检、新建、重建、形状核对
--   全部走同一段文本，不会再出现"预检一套、约束另一套"的漂移）：
--
--     CHECK (date IS NOT NULL
--            AND date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
--            AND substring(date from 1 for 4)::int BETWEEN 1900 AND 2999
--            AND substring(date from 6 for 2)::int BETWEEN 1 AND 12
--            AND substring(date from 9 for 2)::int BETWEEN 1 AND 31
--            AND to_char(make_date(substring(date from 1 for 4)::int,
--                                  substring(date from 6 for 2)::int, 1)
--                        + (substring(date from 9 for 2)::int - 1), 'YYYY-MM-DD') = date)
--
--   六个分支各挡一类脏值，**顺序不可换**（详见下面"短路"一节）：
--     ① date IS NOT NULL —— CHECK 对 NULL 求值为 NULL，而 SQL 把 NULL 当"通过"，
--                           不写这一句 NULL 照样写得进来（读取侧再静默漏算）；
--     ② ISO 正则        —— 挡 '2026/10/1'、'2026-1-5'、'abc'、'2026-10-17T00:00:00'；
--     ③④⑤ 年月日范围     —— 先把三个数字段**夹到 make_date 一定接受的范围**
--                           （年 1900–2999、月 1–12、日 1–31），
--                           这样 ⑥ 里的 ::int / make_date / 日期加法**永远不会抛**；
--     ⑥ make_date(年,月,1) + (日-1) 回写比对 —— 挡"格式对、日子不存在"：
--                           '2026-02-30' 会被算成 2026-02-01 + 29 天 = **2026-03-02**，
--                           to_char 得到 '2026-03-02' ≠ 输入 → 约束不满足；
--                           闰年也是同一套逻辑：'2024-02-29' → 2024-02-29（相等，放行），
--                           '2026-02-29' → 2026-03-01（不等，拒绝）。
--
--   ⚠ 为什么换成 make_date（这是本次返工的核心，别改回 to_date）：
--     老版本写的是 `to_char(to_date(date,'YYYY-MM-DD'),'YYYY-MM-DD') = date`。
--     实测（PostgreSQL 17.11，本库）：`to_date('2026-02-30','YYYY-MM-DD')` 不是"宽容"
--     而是**直接抛错** `date/time field value out of range`，SQLSTATE = **22008**
--     (datetime_field_overflow)。后果是两个真实问题：
--       (a) 报错归类错：应用/监控按 SQLSTATE 分辨"约束冲突(23514)"和"脏值"，
--           22008 会被归到"日期解析错误"这一类，看起来像代码 bug 而不是数据被拒；
--           而且报错文本里没有约束名/列名，追问题的第一跳就断了。
--       (b) 老版预检没法写成一条 `SELECT count(*)`：只要库里有 1 行 '2026-02-30'，
--           整条查询就抛 22008、整个事务中止，连"有几行非法"都统计不出来 ——
--           老脚本只好逐行循环 + 每行一个子事务（O(行数)、还和约束两套判据）。
--     make_date + 日期加法**只有"合法输入"这一条路径**：年/月/日都已被 ③④⑤ 夹住，
--     剩下的运算是"造月初 + 加 0~30 天"，任何输入都不会抛。于是：
--       · 非法值一律落成"约束不满足" → SQLSTATE 23514 (check_violation)；
--       · 预检可以退化成一条 `SELECT count(*)`（见步骤 1），全表一次扫描。
--
--   ⚠ "绝不抛"依赖 AND 的**短路顺序**（本条实测有效，但要写清楚，别乱调顺序）：
--     PostgreSQL 文档不保证 AND/OR 的求值顺序，实际执行器是按书写顺序逐项短路
--     （EEOP_BOOL_AND_STEP）。所以正则 ② 必须排在所有 `::int` 之前 —— 否则
--     'abc' / '' 会被 `substring(...)::int` 拦下并抛 **22P02**（invalid_text_representation），
--     又把"数据被拒"变成"解析炸了"。实测（本库 PG 17）：'abc'、''、'2026-1-5'、
--     '2026/10/1' 全部只走到 ② 就短路，**没有一个抛 22P02**。
--     如果将来要连这个顺序都不依赖（例如 PG 换了执行器策略），只需把 ③④⑤⑥ 包进
--     `CASE WHEN date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN (③AND④AND⑤AND⑥) ELSE false END`，
--     CASE 的分支求值是**有保证**的惰性。本次按上表写法（更接近原始意图、可读性好）。
--
--   ✔ 为什么不用 `date::date` 之类的"直接转型"：cast 对 '2026-02-30' 同样抛
--     22008，而且 `'2026-1-5'::date` 会**成功**（PG 接受非零填充）—— 既抛又宽松，
--     两头都不合适。真正需要的只是"零填充 + 真实日历日"这一条窄口径。
--
--   ✔ 时区无关性（记下来，别让下一个人重新怀疑一遍）：表达式里没有 timestamptz，
--     make_date 出的是**无时区的 date**，`date + int` 也是 date，to_char(date,…)
--     直接按年月日渲染 —— 整条链不经过会话 TimeZone，所以结果与 TimeZone 无关
--     （本库 TimeZone=UTC 只是其中一种取值）。
--
-- 【锁：为什么 LOCK TABLE 是 BEGIN 之后的第一句】
--   预检（count(*)）和 VALIDATE CONSTRAINT 是**两条语句**：不加锁的话，两条语句之间
--   可能有人插进一行 '2026/10/1'，于是"预检说 0 行非法"和"VALIDATE 通过"说的不是
--   同一个数据集（VALIDATE 反而会在扫描时报 23514，报错位置离真正的原因很远）。
--   `LOCK TABLE public.bills IN SHARE ROW EXCLUSIVE MODE` 让它们**看到同一份数据**：
--   · SHARE ROW EXCLUSIVE 与 INSERT/UPDATE/DELETE 的 ROW EXCLUSIVE **互斥**
--     （= 期间没有新写入），但与 SELECT 的 ACCESS SHARE **兼容**（= 不挡读）。
--   · 顺带一提：ADD CONSTRAINT / VALIDATE CONSTRAINT 自己也要拿这个锁，
--     我们先拿到之后再执行它们，就是在同一把锁下把预检与校验串起来。
--   · 表不存在时这句会报 `42P01 relation "public.bills" does not exist` ——
--     这就是本脚本的"前置检查"（老版本额外用 to_regclass 探了一次并给中文提示；
--     现在为了把锁放在"拿到锁"的位置，改成依赖这条报错本身：错误文本里带着表名，
--     已经足够定位"该先启动应用建表"）。
--
-- 【幂等（可重复执行）】
--   跑第二遍只会看到"跳过（幂等）"的 NOTICE，结构不变、数据不动：
--     · 步骤 1：纯只读预检 +（按需）建约束/VALIDATE，判据是
--       "同名 CHECK 是否存在 + 定义形状是否符合 + 是否已 convalidated"：
--         - 不存在                  → ADD CONSTRAINT … NOT VALID → VALIDATE；
--         - 已存在且形状符合且已校验 → NOTICE 跳过（不重建，避免白扫一遍全表）；
--         - 已存在且形状符合但 NOT VALID → **只 VALIDATE**（不 DROP，历史行还没校验过）；
--         - 已存在但形状不符（老版本 to_date 那一版 / 别人手工加的宽松版）→ DROP 重建。
--       **绝不只按名字跳过**（那会把错误口径永久固化），也**绝不只按"存在"就 VALIDATE**
--       （形状不符时 VALIDATE 等于给错误口径盖章）。
--     · 步骤 2/3：约束清单 + 核对/探针，永远执行。
--   注意：幂等指的是"结构"，不是"数据"——本脚本从不改业务数据。
--
-- 【为什么"NOT VALID → VALIDATE"分两步，而不是一句 ADD CONSTRAINT】
--   一句 `ADD CONSTRAINT … CHECK (…)` 会**在一条语句里**做两件事：拿 SHARE ROW
--   EXCLUSIVE 锁 → 全表扫描校验历史行 → 建约束。扫描期间锁一直被持有（大表上可能是
--   几分钟），这段时间**所有写入该表的会话都在排队**（读不挡）。
--   分成两步之后：
--     ① `ADD CONSTRAINT … NOT VALID`：只改系统目录，**不扫表**、瞬间返回，
--        立刻开始拦新写入（NOT VALID 的 CHECK 对新行照样生效）；
--     ② `VALIDATE CONSTRAINT`：单独扫表校验历史行，锁强度降到 SHARE UPDATE EXCLUSIVE
--        （只挡其它 DDL/清理，**不挡读写**）。
--   净效果：阻塞窗口从"一次全表扫描"缩到"两条 DDL 之间的目录操作"，写入方几乎无感。
--   （本脚本开头已先拿了 SHARE ROW EXCLUSIVE，所以这一段在本脚本内部不会降级成
--     "完全不挡写" —— 那是**故意**的：预检与 VALIDATE 必须同一个快照。
--     手工在别处补约束时才会真正享受到两步法的并发收益。）
--
-- 【性能提示】
--   · 预检是**两条单条 SELECT**（一条判非法行数、一条取总行数），不再是逐行子事务循环；
--   · VALIDATE CONSTRAINT 也扫一遍全表。当前库 bills 为 0 行，两者都无感。
--     若某环境 bills 很大，请低峰执行（或先在低峰手工 `ADD CONSTRAINT … NOT VALID`，
--     再用本脚本把 VALIDATE 补上）。
--   · 第一句 `SET LOCAL lock_timeout='5s'`：任何一步等锁最多 5 秒就报错回滚，
--     不会把 bills 锁很久，也不会让排在后面的读写一直排队。
--   · 想再兜一层"别把 SQL Editor 会话挂太久"，可以解开下面这句（默认注释掉，
--     因为大表上合法的 VALIDATE 可能真的要几十秒，硬超时反而做一半回滚）：
--       SET LOCAL statement_timeout = '60s';
-- =============================================================================

BEGIN;

-- 第一句就设锁等待上限：DDL 卡在等锁上最多等 5 秒，然后**报错并回滚整个事务**。
-- 拿到 "canceling statement due to lock timeout" 时的正确做法是稍后重试；
-- 调大超时只会让你的会话更久地锁着表、让排队的读写更多。
-- 用 SET LOCAL 而不是 SET：只在当前事务内生效，事务结束自动失效，
-- 不会把 lock_timeout 粘到连接池（Supabase pooler）里的后续会话上。
SET LOCAL lock_timeout = '5s';

-- 可选（默认关闭，见头部【性能提示】）：
-- SET LOCAL statement_timeout = '60s';

-- 拿到锁之后再做任何读：预检与建约束/VALIDATE 从此处在同一份数据上。
-- 表不存在 → 这里直接报 42P01（错误文本里带 public.bills，足够定位问题）。
LOCK TABLE public.bills IN SHARE ROW EXCLUSIVE MODE;


-- ---------------------------------------------------------------------------
-- 步骤 1：预检 + 幂等建约束（一个 DO 块，表达式只定义一处）
--   为什么三步放在**同一个块**里：
--     · v_expr 是局部变量，跨块共享就只能靠 set_config/临时表这种旁门；合在一块最省事；
--     · 预检（count），建约束（ADD … NOT VALID），校验历史行（VALIDATE）用的都是
--       同一份 v_expr、同一把表锁、同一个快照 —— 三者的结论不可能互相矛盾。
--   预检为什么现在能写成**一条** count：
--     老版本要用"逐行循环 + 每行一个子事务"，是因为 to_date 遇到 '2026-02-30' 会抛
--     22008，一条普通 SELECT 就会被一行脏数据整死。新表达式（make_date 回卷）**绝不抛**，
--     所以预检回归成一句 `WHERE NOT (expr)` —— 既快，又与约束判据**逐字同源**。
--   预检为什么放在建约束之前：给出"有几行、都是谁"的中文清单后**整体中止**，
--     而不是让 VALIDATE 用一句 23514 把人拦在半个结构变更里。两者都失败，
--     但前者能直接告诉人该修哪几行。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_expr      text;
    v_total     bigint  := 0;
    v_bad       bigint  := 0;
    v_samples   text    := '';
    v_def       text;
    v_flat      text;
    v_validated boolean;
BEGIN
    -- ① 表达式：全脚本**唯一**定义处（预检 / 新建 / 重建 / 形状核对 都走它）
    v_expr := 'date IS NOT NULL'
              || ' AND date ~ ''^[0-9]{4}-[0-9]{2}-[0-9]{2}$'''
              || ' AND substring(date from 1 for 4)::int BETWEEN 1900 AND 2999'
              || ' AND substring(date from 6 for 2)::int BETWEEN 1 AND 12'
              || ' AND substring(date from 9 for 2)::int BETWEEN 1 AND 31'
              || ' AND to_char(make_date(substring(date from 1 for 4)::int,'
              || '                      substring(date from 6 for 2)::int, 1)'
              || '            + (substring(date from 9 for 2)::int - 1), ''YYYY-MM-DD'') = date';

    -- ② 预检：单条 SELECT count(*)，把表达式取反即"非法行"
    --    （NOT (expr) 对 NULL 行同样为 true：expr 首项 date IS NOT NULL 已经是 false）
    EXECUTE 'SELECT count(*) FROM public.bills WHERE NOT (' || v_expr || ')' INTO v_bad;
    EXECUTE 'SELECT count(*) FROM public.bills' INTO v_total;

    IF v_bad > 0 THEN
        EXECUTE 'SELECT string_agg(format(''    id=%s date=%L'', id, date), chr(10))'
                || ' FROM (SELECT id, date FROM public.bills WHERE NOT (' || v_expr || ')'
                || '       ORDER BY id LIMIT 10) s'
           INTO v_samples;
        RAISE EXCEPTION '步骤1 中止：public.bills 有 % 行（共 % 行）日期非法，无法在其上建立 bills_date_iso_check。本脚本**不会**自动改写历史日期（改哪一天是业务判断，猜=伪造账目）。请人工确认并修正这些行后重跑本脚本。非法样本（最多 10 行）：%.本事务已整体回滚，未改动任何数据',
                        v_bad, v_total, chr(10) || coalesce(v_samples, '（样本收集失败）');
    END IF;

    RAISE NOTICE '步骤1 预检通过：public.bills 共 % 行，日期的格式（ISO 零填充）与日历日全部合法', v_total;

    -- ③ 幂等加约束：先看同名 CHECK 的现状
    SELECT pg_get_constraintdef(con.oid), con.convalidated
      INTO v_def, v_validated
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'c'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname = 'bills_date_iso_check';

    IF v_def IS NULL THEN
        -- 不存在 → 新建：先 NOT VALID（瞬间，立刻开始拦新写入），再 VALIDATE（扫历史行）
        EXECUTE 'ALTER TABLE public.bills ADD CONSTRAINT bills_date_iso_check CHECK ('
                || v_expr || ') NOT VALID';
        EXECUTE 'ALTER TABLE public.bills VALIDATE CONSTRAINT bills_date_iso_check';
        RAISE NOTICE '步骤1：已新增约束 bills_date_iso_check（NOT VALID → VALIDATE 两步完成）。定义 = CHECK (%)', v_expr;
    ELSE
        -- 形状核对：必须同时出现 ①非空判断 ②ISO 正则 ⑥make_date 回写比对 + to_char
        --   ⚠ 用 translate 先删掉所有反斜杠再比：LIKE 默认把反斜杠当转义符，
        --     而 pg_get_constraintdef 的渲染在不同版本下可能给出 \d 或 \\d。
        --     （新版正则里已经没有反斜杠了，这句留着是为了让同一段核对逻辑对
        --       老定义、手工定义也照样可靠。）
        v_flat := translate(v_def, chr(92), '');
        IF v_flat LIKE '%date IS NOT NULL%'
           AND v_flat LIKE '%[0-9]{4}-[0-9]{2}-[0-9]{2}%'
           AND v_flat LIKE '%make_date%'
           AND v_flat LIKE '%to_char%' THEN
            IF v_validated IS TRUE THEN
                RAISE NOTICE '步骤1 跳过（幂等）：约束 bills_date_iso_check 已存在、形状符合、convalidated=true，不重建。定义 = %', v_def;
            ELSE
                -- 存在、形状对、但历史行没校验过 → 只补 VALIDATE（绝不 DROP 重建）
                EXECUTE 'ALTER TABLE public.bills VALIDATE CONSTRAINT bills_date_iso_check';
                RAISE NOTICE '步骤1：约束已存在且形状符合，但原本是 NOT VALID → 已补 VALIDATE（未重建）';
            END IF;
        ELSE
            -- 形状不符：老版本 to_date 那一版会抛 22008，必须换掉；别人手工加的宽松版同理
            RAISE NOTICE '步骤1：约束已存在但形状**不符预期**（应含 date IS NOT NULL + ISO 正则 + make_date 回卷比对，即"绝不抛异常"的那一版），将 DROP 后按预期重建。原定义 = %', v_def;
            EXECUTE 'ALTER TABLE public.bills DROP CONSTRAINT bills_date_iso_check';
            EXECUTE 'ALTER TABLE public.bills ADD CONSTRAINT bills_date_iso_check CHECK ('
                    || v_expr || ') NOT VALID';
            EXECUTE 'ALTER TABLE public.bills VALIDATE CONSTRAINT bills_date_iso_check';
            RAISE NOTICE '步骤1：已按预期重建 bills_date_iso_check（NOT VALID → VALIDATE）。定义 = CHECK (%)', v_expr;
        END IF;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 2：约束清单（给人眼核对用；失败与否以步骤 3 的断言为准）
-- ---------------------------------------------------------------------------

SELECT con.conname                   AS constraint_name,
       con.convalidated              AS is_validated,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel    ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
WHERE con.contype = 'c'
  AND ns.nspname = 'public'
  AND rel.relname = 'bills'
ORDER BY con.conname;


-- ---------------------------------------------------------------------------
-- 步骤 3：执行后核对 + 行为探针（任何一条不满足就 RAISE EXCEPTION → 整个事务回滚）
--   ① 约束存在；
--   ② convalidated = true（**必须断言**：NOT VALID 的约束只对新行生效，
--      历史行根本没校验过，那等于"结构变了但没体检"）；
--   ③ 定义形状符合预期（含 IS NOT NULL / ISO 正则 / make_date 回卷比对）；
--   ④ 行为探针 —— 比 ③ 的字符串比对更硬的证据，直接拿真数据试，
--      而且**逐条断言 SQLSTATE = 23514**（不是泛泛的"被拒了"）：
--        非法：'2026-02-30'、'2026-04-31'、'2026-13-01'、'2026-1-5'、''、NULL
--              → 每一条都必须报 23514 (check_violation)；
--        合法：'2024-02-29'（闰年）、'2026-10-17' → 必须写入成功。
--      为什么非要断言 SQLSTATE 而不是"有没有抛"：这次返工的全部意义就在这里 ——
--      老版本对 '2026-02-30' 报的是 22008，也是"抛了、也写不进去"，但应用/监控
--      按 23514 归类的手册、告警、重试策略全都不成立。只判"被拒"等于把 bug 当通过。
--      实测值会在 NOTICE 里逐条打印（每个非法值一行 SQLSTATE），便于直接抄进报告。
--   探针都用 BEGIN … EXCEPTION 子事务包住：被拒的那一刻子事务回滚，插入的行不会残留；
--   合法值那条则显式 DELETE。探针行的 merchant 固定为 '007-constraint-probe'，
--   万一中途异常，整段事务回滚也一并带走，绝不会给业务表留下垃圾行。
--   探针 INSERT 还要满足 003 的两条约束（type='支出'、amount >= 0），
--   所以写 type='支出'、amount=1；user_id 留 NULL（bills.user_id 可空，外键允许 NULL）。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_def        text;
    v_flat       text;
    v_validated  boolean;
    v_bad_values text[]  := ARRAY['2026-02-30', '2026-04-31', '2026-13-01',
                                  '2026-1-5', '', NULL];
    v_ok_values  text[]  := ARRAY['2024-02-29', '2026-10-17'];
    v_val        text;
    v_state      text;
    v_msg        text;
    v_rows       integer := 0;
BEGIN
    -- ① 约束存在 + 取定义/校验状态
    SELECT pg_get_constraintdef(con.oid), con.convalidated
      INTO v_def, v_validated
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'c'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname = 'bills_date_iso_check';

    IF v_def IS NULL THEN
        RAISE EXCEPTION '核对①未通过：public.bills 上找不到约束 bills_date_iso_check，事务已回滚';
    END IF;

    -- ② 已校验（VALIDATE 过的约束才对历史行有效）
    IF v_validated IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对②未通过：约束 bills_date_iso_check 的 convalidated = %（应为 true）。NOT VALID 的约束只对新写入生效，历史行没被校验过，事务已回滚',
                        coalesce(v_validated::text, '<NULL>');
    END IF;

    -- ③ 定义形状（与步骤 1 同一套判据：先去掉反斜杠，绕开 LIKE 转义 + 渲染差异）
    v_flat := translate(v_def, chr(92), '');
    IF NOT (v_flat LIKE '%date IS NOT NULL%'
            AND v_flat LIKE '%[0-9]{4}-[0-9]{2}-[0-9]{2}%'
            AND v_flat LIKE '%make_date%'
            AND v_flat LIKE '%to_char%') THEN
        RAISE EXCEPTION '核对③未通过：约束 bills_date_iso_check 的定义形状不符预期（应含 date IS NOT NULL + ISO 正则 + make_date 回卷比对 + to_char），实际 = %，事务已回滚。请人工 DROP 该约束后重跑本脚本', v_def;
    END IF;

    -- ④a 非法值逐条断言：必须是 23514（check_violation），不许是 22008 / 22P02
    FOREACH v_val IN ARRAY v_bad_values LOOP
        v_state := NULL;   -- NULL 表示"没抛异常"（= 约束没拦住，属于失败）
        v_msg   := NULL;
        BEGIN
            INSERT INTO public.bills(user_id, date, merchant, amount, payment_method, type)
            VALUES (NULL, v_val, '007-constraint-probe', 1, 'probe', '支出');
        EXCEPTION WHEN others THEN
            v_state := SQLSTATE;
            v_msg   := SQLERRM;
        END;

        IF v_state IS DISTINCT FROM '23514' THEN
            RAISE EXCEPTION '核对④a未通过：date = % 期望 SQLSTATE = 23514 (check_violation)，实际 = %（%）。约束没有按"约束冲突"归类拦住这个值，事务已回滚',
                            coalesce(quote_literal(v_val), 'NULL'),
                            coalesce(v_state, '未抛异常（被写入了！）'),
                            coalesce(v_msg, '-');
        END IF;
        RAISE NOTICE '探针④a 拒绝 date=% → SQLSTATE=23514 (check_violation)', coalesce(quote_literal(v_val), 'NULL');
    END LOOP;

    -- ④b 合法值必须能写入（否则说明约束写反了，把合法数据也挡了）
    FOREACH v_val IN ARRAY v_ok_values LOOP
        v_state := NULL;
        v_msg   := NULL;
        v_rows  := 0;
        BEGIN
            INSERT INTO public.bills(user_id, date, merchant, amount, payment_method, type)
            VALUES (NULL, v_val, '007-constraint-probe', 1, 'probe', '支出');
            GET DIAGNOSTICS v_rows = ROW_COUNT;
        EXCEPTION WHEN others THEN
            v_state := SQLSTATE;
            v_msg   := SQLERRM;
        END;

        IF v_state IS NOT NULL OR v_rows <> 1 THEN
            RAISE EXCEPTION '核对④b未通过：合法日期 % 被约束拒绝了（约束写反了，或表上有别的冲突约束）。SQLSTATE = %（%），事务已回滚',
                            quote_literal(v_val), coalesce(v_state, '-无异常-'), coalesce(v_msg, '-');
        END IF;
        RAISE NOTICE '探针④b 接受 date=% → 写入成功（SQLSTATE 无异常）', quote_literal(v_val);
    END LOOP;

    -- 探针绝不留在业务表里（④a 的插入已被子事务回滚，④b 的插入在这里删掉）
    DELETE FROM public.bills WHERE merchant = '007-constraint-probe';

    RAISE NOTICE '核对通过：bills_date_iso_check 存在、convalidated=true、形状符合预期；6 个非法值全部 23514，2 个合法值全部写入成功（探针行已清理）。定义 = %', v_def;
END $$;

-- 最后一行给人肉眼确认：看到 OK 就说明前面每一步都过了（否则事务已因 EXCEPTION 回滚）
SELECT 'OK' AS result;

COMMIT;
