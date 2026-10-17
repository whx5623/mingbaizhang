-- =============================================================================
-- 003_amount_precision_and_constraints.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【执行顺序：必须 001 → 002 → 003，顺序不可颠倒】
--   001_normalize_bill_type.sql       清洗 bills.type 脏值（本脚本的硬性前置条件）
--   002_fk_cascade_delete.sql         把指向 users 的外键改成 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql（本文件）
--   为什么必须最后跑本脚本：本脚本要在 bills.type 上建 CHECK 约束，
--   只要库里还有 "expense" / "支出 " / NULL 这类脏值，ADD CONSTRAINT 就会因为
--   “不满足约束”而失败；001 负责先把数据洗干净，本脚本再用 DO 块复查一遍。
--   002 的活儿本脚本也会兜底重做（见步骤 6），所以 002 即使漏跑也不会丢级联删除能力。
--
-- 【用途】
--   1) 把 public.bills.amount 的列类型统一成 NUMERIC(12,2)：
--      数据库侧精确存 2 位小数，避免 float/double 的二进制误差在
--      “月支出 / 预算 / 结余”这类汇总里逐步放大。
--   2) 给 bills 加两条数据完整性 CHECK 约束：
--      · bills_type_check                type 只能是 '收入' / '支出'
--      · bills_expense_amount_positive   支出金额必须 >= 0（收入允许为负，见下）
--   3) 洗掉历史脏数据：负支出转正、金额统一两位小数。
--   3c) 把 bills.amount 收紧为 NOT NULL。理由：CHECK (type = '收入' OR amount >= 0)
--      对 amount IS NULL 求值为 NULL，SQL 又把 CHECK 的 NULL 视为“通过” →
--      一笔 amount 为 NULL 的支出会被约束放行，然后在 app.py 的 .sum() 里
--      静默消失（月支出偏小 → 结余虚高）。金额非空不能靠 CHECK 兜，只能列级约束。
--   4) 清理 bills 上重复的两个 (user_id) REFERENCES users(id) 外键，并保证
--      最终**恰好剩 1 个且为 ON DELETE CASCADE** —— 顺序处理，绝不会出现
--      “删掉重复项顺手把唯一的级联删除也删了”的空档。
--
-- 【与 db.py 的口径必须一致（改动时请同步）】
--   db.py::_normalize_amount() 的符号语义：
--     · 支出 → abs(值)，永远 >= 0；
--     · 收入 → **保留原符号**，负数（退款/冲正）原样保留。
--       负收入只会让结余偏小（保守方向），翻正会让结余虚高 → 误判“已达成目标存款”。
--   所以本脚本的约束写成：CHECK (type = '收入' OR amount >= 0)，
--   **只约束支出、绝不约束收入的符号**。清洗语句也只 abs 支出行。
--   注意：CHECK (type IN ('收入','支出')) 对 type IS NULL 求值为 NULL，SQL 里
--   NULL 视为“通过”，所以 bills_type_check 里额外写了 type IS NOT NULL，
--   否则 NULL type 的行依然能写进来 —— 而 NULL type 正是 app.py 漏算结余的来源。
--
-- 【执行前请务必备份（本脚本会改数据、改列类型、删约束）】
--   CREATE TABLE bills_backup_20260101 AS SELECT * FROM public.bills;
--   -- 表结构备份（约束/列类型靠它才能完整回滚）：
--   pg_dump --schema-only --table=public.bills "<DATABASE_URL>" > bills_schema_backup.sql
--   （把日期换成本次实际执行日期；确认无误后再 DROP TABLE 备份表）
--
-- 【⚠ 重表锁风险（执行前必读）】
--   ALTER TABLE public.bills ALTER COLUMN amount TYPE NUMERIC(12,2) USING ... 会
--   **重写整张 bills 表**，并在重写期间持有 ACCESS EXCLUSIVE 锁：
--     · 这段时间对 bills 的读写（含 Streamlit 页面的每一次查询）都会阻塞排队；
--     · 表越大耗时越长（可粗略按 “行数 × 行宽” 估算 IO 时间）。
--   建议：低峰时段执行；执行期间页面可能卡住，属正常现象。
--   如果表已经很大、又不想长时间锁表，请改用“新建表 + 分批搬数据 + RENAME”的
--   在线改写方案，而不是直接跑本脚本。
--
-- 【执行入口】
--   Supabase 控制台 → 选择本项目 → SQL Editor → 整段粘贴执行（或 psql -f 本文件）。
--
-- 【重要】
--   1) 本脚本由 db-expert 生成，**未自动执行** —— 生成时只在事务里试跑过、
--      随后 ROLLBACK，生产库上的列类型 / CHECK 约束 / 外键都保持原样。
--   2) 执行前请先在演练环境（或 Supabase 的 Preview/Branch 库）跑一遍。
--   3) 可重复执行（幂等）：
--      · 金额清洗第二次执行应为 0 行；
--      · amount 已是 numeric(12,2) → 跳过并 RAISE NOTICE；
--      · amount 已是 NOT NULL → 跳过并 RAISE NOTICE；
--      · CHECK 约束已存在**且定义符合预期** → 跳过并 RAISE NOTICE；
--        同名但定义不符（历史上的 CHECK (amount >= 0) 会把合法的负收入也挡掉，
--        CHECK (type IN ('收入','支出','其他')) 缺 IS NOT NULL）→ 先 DROP 再按预期重建，
--        **绝不只按名字跳过**（那会把错误的口径永久固化）；
--      · 外键已唯一且为 CASCADE → 只 RAISE NOTICE，不再重建；
--      · 最终保留的外键若不是 bills_user_id_fkey → RENAME 成它（保证名字稳定）。
--   3b) 前置校验（不满足就整事务回滚，本脚本不猜数据）：
--      type 非 收入/支出、amount 为 NULL、amount 是 NaN/Infinity、
--      amount 绝对值超 NUMERIC(12,2) 上界 9999999999.99。
--   4) 脚本自带事务边界 BEGIN … COMMIT，并且末尾核对**不通过就 RAISE EXCEPTION**：
--      任何一步失败 → 整个事务自动中止并回滚 → 库里“完全没执行过”，
--      不会留下“金额改了但约束没加”的半成品状态。失败时请 ROLLBACK 后按报错排查。
--      注意：ROLLBACK 之后请确认客户端/psql 没有把整个事务提交过，再重跑。
--   5) 生成时实测到的真实基线（2026 年当前库，bills 为空表）：
--        bills.amount            real（float4）       ← 本脚本改成 numeric(12,2)
--        bills.bills_user_id_fkey   confdeltype='c'   ← 保留者
--        bills.fk_bills_user        confdeltype='c'   ← 历史遗留重复项，本脚本删除
--        chat_history.chat_history_user_id_fkey  'c'  ← 本脚本不碰 chat_history
--      注意：不同环境的基线可能不同（例如只跑过 001 的库，bills_user_id_fkey
--      可能还是 'a'=NO ACTION），所以本脚本全部按语义查出来判断，不硬编码现状。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 步骤 1：预检（全部是只读 SELECT，先把现状看清楚再动手）
-- ---------------------------------------------------------------------------

-- 1.1 账单类型分布：预期只有 收入 / 支出 两行；多出来的都是 001 该处理的脏值
SELECT coalesce(type, '<NULL>') AS bill_type,
       count(*)                 AS row_count
FROM public.bills
GROUP BY 1
ORDER BY 2 DESC;

-- 1.2 type 非法值行数（含 NULL）。> 0 时步骤 2 会直接中止整个事务
SELECT count(*) FILTER (WHERE type IS NULL)               AS null_type_rows,
       count(*) FILTER (WHERE type NOT IN ('收入', '支出')) AS illegal_type_rows,
       count(*)                                           AS total_rows
FROM public.bills;

-- 1.3 负支出（会虚增结余：支出写负反而冲减月支出），步骤 3a 会转正
SELECT count(*) AS negative_expense_rows
FROM public.bills
WHERE type = '支出' AND amount < 0;

-- 1.4 小数位超过 2 位的行数，步骤 3b 会四舍五入到 2 位。
--     注意（实测 PG 12+）：real → numeric 按“最短十进制表示”转换，所以这里数出来的
--     是“十进制真超过两位”的行（例如 2.675）；旧版 PG 或对 real 列做过算术时，
--     才会额外出现纯二进制噪声行。无论哪种，**精度保证都在步骤 4，不在本计数**。
SELECT count(*) AS amount_scale_gt2_rows
FROM public.bills
WHERE amount IS NOT NULL
  AND amount::numeric <> round(amount::numeric, 2);

-- 1.5 非有限值（NaN / Infinity）、超出 NUMERIC(12,2) 上界的行数、以及 amount 为 NULL 的行数。
--     这几类本脚本**不会**猜着改（猜就等于伪造账目），步骤 2 会直接中止并要人处理。
--     用 amount::text 判断非有限值：real / double precision / numeric 三种列类型下
--     字面量都是 'NaN' / 'Infinity' / '-Infinity'，比跨类型比较更稳。
SELECT count(*) FILTER (WHERE amount IS NULL)                              AS null_amount_rows,
       count(*) FILTER (WHERE amount::text IN ('NaN', 'Infinity', '-Infinity')) AS non_finite_amount_rows,
       count(*) FILTER (WHERE amount IS NOT NULL
                          AND amount::text NOT IN ('NaN', 'Infinity', '-Infinity')
                          AND abs(amount::numeric) > 9999999999.99)             AS amount_overflow_rows
FROM public.bills;

-- 1.6 当前外键基线（confdeltype：a=NO ACTION, r=RESTRICT, c=CASCADE, n=SET NULL, d=SET DEFAULT）
SELECT rel.relname                   AS table_name,
       con.conname                   AS constraint_name,
       con.confdeltype               AS on_delete_code,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel    ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname = 'public'
  AND rel.relname IN ('bills', 'chat_history')
  AND con.confrelid = 'public.users'::regclass
ORDER BY rel.relname, con.conname;

-- 1.7 当前 amount 列类型基线（步骤 4 的幂等判断就以这一行的结果为准）
SELECT column_name,
       data_type,
       numeric_precision,
       numeric_scale,
       udt_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'bills'
  AND column_name  = 'amount';

-- 1.8 已有的 CHECK 约束（预期为空；第二次执行时应能看到本脚本加的两条）
SELECT con.conname                   AS constraint_name,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel    ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
WHERE con.contype = 'c'
  AND ns.nspname = 'public'
  AND rel.relname = 'bills'
ORDER BY con.conname;

-- ---------------------------------------------------------------------------
-- 步骤 2：硬性前置校验（不通过就 RAISE EXCEPTION，整个事务回滚，绝不静默继续）
--   ① type 必须是 收入 / 支出，且不能为 NULL —— 否则加 CHECK 一定失败，
--      而且这类行在 app.py 的 == 比较里会被同时漏算（结余虚高）。
--   ② amount 不能是 NaN / Infinity，也不能超出 NUMERIC(12,2) 的上界
--      9999999999.99 —— 本脚本不替你猜真实金额，直接中止交给人处理。
--   ③ amount 不能为 NULL —— 步骤 4b 要把它收紧成 NOT NULL，而且 NULL 金额的支出
--      会被 CHECK (type='收入' OR amount>=0) 以“求值为 NULL = 通过”的方式放行，
--      之后在 .sum() 里静默消失。本脚本不替你填金额（编造金额 = 伪造账目）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_bad_type     integer := 0;
    v_non_finite   integer := 0;
    v_overflow     integer := 0;
    v_null_amount  integer := 0;
BEGIN
    SELECT count(*) INTO v_bad_type
    FROM public.bills
    WHERE type IS NULL OR type NOT IN ('收入', '支出');

    IF v_bad_type > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行 type 不是 收入/支出（或为 NULL），无法在其上建立 CHECK 约束。请先执行 001_normalize_bill_type.sql 清洗 type 脏值，再重跑本脚本（本事务已整体回滚，未写入任何变更）', v_bad_type;
    END IF;

    SELECT count(*) INTO v_non_finite
    FROM public.bills
    WHERE amount::text IN ('NaN', 'Infinity', '-Infinity');

    IF v_non_finite > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行 amount 是 NaN/Infinity。本脚本不会猜测真实金额（猜=伪造账目），请先人工修正这些行再重跑本脚本', v_non_finite;
    END IF;

    SELECT count(*) INTO v_overflow
    FROM public.bills
    WHERE amount IS NOT NULL
      AND amount::text NOT IN ('NaN', 'Infinity', '-Infinity')
      AND abs(amount::numeric) > 9999999999.99;

    IF v_overflow > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行 amount 绝对值超过 NUMERIC(12,2) 的上界 9999999999.99，改成 NUMERIC(12,2) 会报 numeric field overflow，请先人工核对这些行（很可能是录入了多一位/少一位小数）', v_overflow;
    END IF;

    -- ③ amount 为 NULL 的行：不加 CHECK 看不出来（CHECK 求值为 NULL 视为通过），
    --    但会在 .sum() 里被跳过 → 月支出偏小 → 结余虚高。这里必须中止。
    SELECT count(*) INTO v_null_amount
    FROM public.bills
    WHERE amount IS NULL;

    IF v_null_amount > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行 amount 为 NULL。amount 将被收紧为 NOT NULL（步骤 4b），且 NULL 金额的支出会被 CHECK (type=''收入'' OR amount >= 0) 以“求值为 NULL = 通过”的方式放行，再从 .sum() 里静默消失。本脚本不会替你猜真实金额（猜=伪造账目），请先人工补齐这些行（或 DELETE 这些无金额的空行）再重跑本脚本', v_null_amount;
    END IF;

    RAISE NOTICE '步骤2 前置校验通过：type 全部为 收入/支出，amount 无 NULL、无 NaN/Infinity、无超上界值';
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 3：金额清洗（**先洗数据再加约束**，否则 ADD CONSTRAINT 会因为旧脏数据失败）
--   3a 负支出转正：支出写负会反向冲减月支出 → 月支出偏小 → 结余虚高。
--      为了自洽，这里只处理 type='支出'，收入行一格都不碰。
--   3b **只是类型对齐前的预归整，不是精度终点**（本步的 ROW_COUNT 不能当精度凭证）。
--      为什么：此刻 amount 还是 real（float4），real 根本存不下任意两位小数，本步
--      round 的结果又要写回 real 列，等于再经过一次二进制舍入。真正把值钉死的是
--      步骤 4 的 ALTER ... TYPE NUMERIC(12,2) USING round(amount::numeric, 2) ——
--      列变成 numeric 之后才既存得准、又会拒绝第 3 位小数。
--      运维别误读（实测 PostgreSQL 12+）：real → numeric 的转换按“最短十进制表示”
--      输出，12.34::real::numeric 得到的就是 12.34（不是 12.340000152587891）。所以
--      3b 的 ROW_COUNT 基本只统计“十进制表示超过两位”的行（例如 2.675、12.345）；
--      只有在旧版 PG、或对 real 列做过算术的场景下才会看到真正的二进制噪声行。
--      无论哪种情况：**想把“金额已两位小数”当结论，请去看步骤 4 之后 amount 的
--      numeric_scale=2，不要看 3b 的 ROW_COUNT。**
--      口径提醒：PG 的 numeric round 是“半值远离零”（1.005 → 1.01），Python 的
--      round() 是“银行家舍入”（1.005 → 1.00）。所以 Python 侧展示/定量统一用
--      Decimal + ROUND_HALF_UP 对齐数据库（见 db.py::_normalize_amount），
--      不要用 round() 或 f-string {x:.2f} 来定金额，否则会出现 1 分钱的显示差。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_neg   integer := 0;
    v_round integer := 0;
BEGIN
    -- 3a：负支出 → 正数。**收入行的负数必须原样保留**（退款/冲正，
    --     翻正会让收入虚增 → 结余虚高，正是本脚本要守住的红线方向）。
    UPDATE public.bills
       SET amount = abs(amount)
     WHERE type = '支出'
       AND amount < 0;
    GET DIAGNOSTICS v_neg = ROW_COUNT;
    RAISE NOTICE '步骤3a 负支出转正：% 行（type=收入 的负数刻意保留负号；可重复执行，第二次应为 0）', v_neg;

    -- 3b：金额统一两位小数。NULL 不动；已符合的行不产生空更新（幂等）。
    UPDATE public.bills
       SET amount = round(amount::numeric, 2)
     WHERE amount IS NOT NULL
       AND amount::numeric <> round(amount::numeric, 2);
    GET DIAGNOSTICS v_round = ROW_COUNT;
    RAISE NOTICE '步骤3b 金额预归整（**不是精度终点**）：命中 % 行。本步 round 的结果要写回 real 列，不构成两位小数保证；精度由步骤4 的 USING round(amount::numeric, 2) 收口，请以步骤4 之后的 numeric_scale=2 为准，别拿本行数当精度凭证。可重复执行，第二次应为 0', v_round;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 4：列类型改成 NUMERIC(12,2)（幂等：已是该类型就跳过）
--   ⚠ 这条 ALTER 会重写整张 bills 表并短暂持有 ACCESS EXCLUSIVE 锁，
--     期间对该表的所有读写都会阻塞；请低峰执行、执行前务必备份。
--   USING round(amount::numeric, 2)：显式给出 real/double → numeric 的转换路径，
--   顺带把浮点噪声（例如 56.20000076293945）压成精确的 56.20。
--   NUMERIC(12,2) 本身已隐含上界 9999999999.99，不需要额外的上界 CHECK。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_data_type text;
    v_precision integer;
    v_scale     integer;
BEGIN
    SELECT c.data_type, c.numeric_precision, c.numeric_scale
      INTO v_data_type, v_precision, v_scale
    FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.table_name   = 'bills'
      AND c.column_name  = 'amount';

    IF v_data_type IS NULL THEN
        RAISE EXCEPTION '中止：找不到 public.bills.amount 列，表结构与预期不符，请先人工核对表结构';
    END IF;

    IF v_data_type = 'numeric' AND v_precision = 12 AND v_scale = 2 THEN
        RAISE NOTICE '步骤4 跳过（幂等）：public.bills.amount 已经是 numeric(12,2)，无需改类型';
    ELSE
        RAISE NOTICE '步骤4：public.bills.amount 当前为 % (precision=%, scale=%) → 开始改写为 NUMERIC(12,2)。这条 ALTER 会重写整张表并短暂持有 ACCESS EXCLUSIVE 锁',
                     v_data_type, coalesce(v_precision::text, '-'), coalesce(v_scale::text, '-');

        ALTER TABLE public.bills
            ALTER COLUMN amount TYPE NUMERIC(12,2)
            USING round(amount::numeric, 2);

        RAISE NOTICE '步骤4：public.bills.amount 已改为 NUMERIC(12,2)';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 4b：把 amount 收紧为 NOT NULL（幂等：已是 NOT NULL 就跳过）
--   为什么必须非空：CHECK (type = '收入' OR amount >= 0) 对 amount IS NULL 求值为
--   NULL，而 SQL 把 CHECK 的 NULL 视为“通过” → 一笔 amount 为 NULL 的支出会被
--   约束放行，随后在 app.py 的 .sum() 里被跳过 → 月支出偏小 → 结余虚高。
--   金额非空**不能靠 CHECK 兜底**（NULL 求值永远是 NULL=通过），只能列级约束。
--   前置：步骤 2 已对 amount IS NULL 的行 RAISE EXCEPTION 中止（本脚本不编造金额）。
--   幂等判断用 pg_attribute.attnotnull（顺带确认列存在且没被 DROP）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_attnotnull boolean;
BEGIN
    SELECT att.attnotnull
      INTO v_attnotnull
    FROM pg_attribute att
    WHERE att.attrelid = 'public.bills'::regclass
      AND att.attname  = 'amount'
      AND att.attnum   > 0
      AND NOT att.attisdropped;

    IF v_attnotnull IS NULL THEN
        RAISE EXCEPTION '中止：找不到 public.bills.amount 列，表结构与预期不符，请先人工核对表结构';
    END IF;

    IF v_attnotnull THEN
        RAISE NOTICE '步骤4b 跳过（幂等）：public.bills.amount 已经是 NOT NULL，无需变更';
    ELSE
        ALTER TABLE public.bills ALTER COLUMN amount SET NOT NULL;
        RAISE NOTICE '步骤4b：public.bills.amount 已收紧为 NOT NULL（NULL 金额会让 CHECK 求值为 NULL 而被放行，进而在汇总里静默消失）';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 5：加 CHECK 约束（幂等 + **定义校验**，同名但语义不符就 DROP 重建）
--   只判断“约束名是否已存在”不够：历史库上可能已经躺着一个同名但语义相反的约束，
--   例如
--     · bills_type_check              CHECK (type IN ('收入','支出','其他'))
--        → 缺 IS NOT NULL（NULL type 照样写得进来），还放行第三种类型
--     · bills_expense_amount_positive CHECK (amount >= 0)
--        → 把合法的负收入（退款/冲正）也挡在门外，与 db.py 的口径相反
--   这类约束“名字对、语义错”，只按名字跳过会把错误口径永久固化。
--   所以这里取 pg_get_constraintdef(oid) 比对语义，不符就 DROP 后按预期重建。
--
--   语义判定口径（必须与 db.py::_normalize_amount、步骤 7 断言保持一致）：
--     5a 必须同时满足：定义含 IS NOT NULL；定义里的字面量集合**恰好**是 {收入, 支出}
--        （多出 '其他' 之类的第三值 → 判定不符）；
--     5b 必须同时满足：定义含 '收入' 分支；定义里有 amount >= 0 形式的比较
--        （PG 会渲染成 amount >= (0)::numeric，所以正则要容忍 (0)::numeric 强转）。
--   标识符一律用 format('%I') 拼，字面量用 %L，禁止字符串裸拼。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_def  text;
    v_lits text[];
    v_ok   boolean;
BEGIN
    -- 5a：bills_type_check —— type 只能是 收入 / 支出，且非 NULL
    --     额外加 type IS NOT NULL 的原因：CHECK 表达式对 NULL 求值为 NULL，SQL 视为
    --     “通过”，不写 IS NOT NULL 的话 NULL type 依然能写进来（app.py 漏算结余的元凶）。
    SELECT pg_get_constraintdef(con.oid)
      INTO v_def
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'c'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname = 'bills_type_check';

    IF v_def IS NULL THEN
        EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (type IS NOT NULL AND type IN (%L, %L))',
                       'bills', 'bills_type_check', '收入', '支出');
        RAISE NOTICE '步骤5a：已新增约束 bills_type_check：type 只能是 收入/支出 且非 NULL';
    ELSE
        -- 抽出定义里所有单引号字面量（'收入' / '支出' / '其他' …），用来核对类型集合
        SELECT array_agg(DISTINCT m[1])
          INTO v_lits
        FROM regexp_matches(v_def, '''([^'']*)''', 'g') AS m;

        v_ok := v_def LIKE '%IS NOT NULL%'
                AND v_lits IS NOT NULL
                AND array_length(v_lits, 1) = 2
                AND v_lits @> ARRAY['收入', '支出'];

        IF v_ok THEN
            RAISE NOTICE '步骤5a 跳过（幂等）：约束 bills_type_check 已存在且定义符合预期（含 IS NOT NULL、类型集合恰为 收入/支出），定义 = %', v_def;
        ELSE
            RAISE NOTICE '步骤5a：约束 bills_type_check 已存在但定义**不符预期**（缺 IS NOT NULL，或类型集合不是恰为 收入/支出；实测字面量集合 = %），将 DROP 后按预期重建。原定义 = %',
                         coalesce(v_lits::text, '<无字面量>'), v_def;
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', 'bills', 'bills_type_check');
            EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (type IS NOT NULL AND type IN (%L, %L))',
                           'bills', 'bills_type_check', '收入', '支出');
            RAISE NOTICE '步骤5a：已按预期重建 bills_type_check：type IS NOT NULL AND type IN (收入, 支出)';
        END IF;
    END IF;

    -- 5b：bills_expense_amount_positive —— 支出金额必须 >= 0，**收入允许为负**
    --     与 db.py::_normalize_amount() 完全一致：支出取 abs，收入保留原符号。
    --     所以这里绝不能写成 amount >= 0（那会把合法的负收入冲正挡在门外）。
    SELECT pg_get_constraintdef(con.oid)
      INTO v_def
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'c'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname = 'bills_expense_amount_positive';

    IF v_def IS NULL THEN
        EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (type = %L OR amount >= 0)',
                       'bills', 'bills_expense_amount_positive', '收入');
        RAISE NOTICE '步骤5b：已新增约束 bills_expense_amount_positive：支出金额必须 >= 0（收入允许为负）';
    ELSE
        SELECT array_agg(DISTINCT m[1])
          INTO v_lits
        FROM regexp_matches(v_def, '''([^'']*)''', 'g') AS m;

        -- 必须同时有 '收入' 分支（放行负收入）和 amount >= 0 形式的下界比较。
        -- PG 的渲染形式取决于建约束时列的类型，实测至少两种：
        --   (amount >= (0)::numeric)                      ← 列是 numeric 时
        --   ((amount)::double precision >= (0)::double precision)  ← 列还是 real 时建的
        -- 所以正则要同时容忍「列侧强转」与「常量侧强转」。
        v_ok := v_lits = ARRAY['收入']
                AND v_def ~ 'amount\)?\s*(::\s*[a-z ]+(\(\s*\d+(\s*,\s*\d+)?\s*\))?)?\s*>=\s*\(?\s*0';

        IF v_ok THEN
            RAISE NOTICE '步骤5b 跳过（幂等）：约束 bills_expense_amount_positive 已存在且定义符合预期（只约束支出、放行负收入），定义 = %', v_def;
        ELSE
            RAISE NOTICE '步骤5b：约束 bills_expense_amount_positive 已存在但定义**不符预期**（缺 收入 分支或缺少 amount >= 0；实测字面量集合 = %），将 DROP 后按预期重建。原定义 = %',
                         coalesce(v_lits::text, '<无字面量>'), v_def;
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', 'bills', 'bills_expense_amount_positive');
            EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (type = %L OR amount >= 0)',
                           'bills', 'bills_expense_amount_positive', '收入');
            RAISE NOTICE '步骤5b：已按预期重建 bills_expense_amount_positive：type = 收入 OR amount >= 0';
        END IF;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 6：清理重复外键，并自己保证最终状态（关键顺序问题，前面几行注释务必看完）
--
--   现状（生成时实测）：bills 上有两个等价外键，都指向 public.users(id)：
--     bills_user_id_fkey  confdeltype='c'   ← 保留者
--     fk_bills_user       confdeltype='c'   ← 历史遗留重复项，要删
--
--   陷阱：如果某个环境只跑过 001、没跑过 002，bills_user_id_fkey 可能还是
--   NO ACTION('a')，而 fk_bills_user 才是唯一的 CASCADE —— 这时直接“删重复项”
--   会把唯一的级联删除删掉，删用户的级联能力就没了。所以本步骤不依赖 002，
--   自己把最终状态做出来，顺序是：
--     (a) 孤儿行校验（有孤儿行 ADD CONSTRAINT 必然失败，先中止并给出处理提示）
--     (a2) 取 public.users.id 的 attnum（后面要断言外键 confkey 精确指向这一列）
--     (b) 按语义查出「bills.user_id → public.users」的所有外键（不硬编码名字）
--     (c) 选保留者：优先 bills_user_id_fkey，否则取第一个命中的约束名
--     (d) **先保证保留者是 CASCADE**（不是就删掉重建，等价于 002 的做法）
--     (d3) 保留者若是 NOT VALID → VALIDATE CONSTRAINT（补上对历史行的校验）
--     (d2) 统一改名：保留者若不是 bills_user_id_fkey → RENAME 成它（名字稳定，便于排查）
--     (e) 再删除除保留者以外的重复外键
--     (g) 断言：恰好 1 个 + 名字=bills_user_id_fkey + CASCADE + convalidated=true
--         + confkey 精确等于 users.id 的 attnum（防止外键指向 users 的其它列）
--   约束名一律从 pg_constraint 查出来，ALTER 用 format('%I') 拼接，禁止裸拼。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_expected    constant text := 'bills_user_id_fkey';  -- 最终必须叫这个名字（不变量）
    v_keeper      text := 'bills_user_id_fkey';  -- 期望的保留者名字（先按期望名找）
    v_renamed_from text;                         -- 保留者 RENAME 之前的旧名（删重复项时要跳过它）
    v_names       text[];                        -- 命中的全部约束名
    v_deltypes    text[];                        -- 与 v_names 一一对应的 confdeltype
    v_keeper_type text;                          -- 保留者当前的 confdeltype（NULL=不存在）
    v_orphans     integer := 0;
    v_user_id_attnum smallint;                   -- users.id 的 attnum（断言 confkey 用）
    v_keeper_valid boolean;                      -- 保留者的 convalidated（NULL=不存在）
    v_total       integer := 0;
    v_cascade     integer := 0;
    v_validated   integer := 0;
    v_named       integer := 0;
    v_keyed       integer := 0;
    v_detail      text;
    i             integer;
BEGIN
    -- (a) 孤儿行校验
    SELECT count(*) INTO v_orphans
    FROM public.bills b
    LEFT JOIN public.users u ON u.id = b.user_id
    WHERE b.user_id IS NOT NULL AND u.id IS NULL;

    IF v_orphans > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行孤儿账单（user_id 指向不存在的 users.id）。ADD CONSTRAINT 会校验失败，请先 DELETE 这些行或把 user_id 置 NULL，然后重跑本脚本', v_orphans;
    END IF;

    -- (a2) 取 public.users.id 的 attnum：后面断言 confkey 必须**精确**指向这一列。
    --      只比 confrelid=users 是不够的 —— 外键也可能指向 users 的其它唯一列
    --      （例如 username），那样 user_id 就和用户的 id 没真正关联上。
    SELECT att.attnum
      INTO v_user_id_attnum
    FROM pg_attribute att
    WHERE att.attrelid = 'public.users'::regclass
      AND att.attname  = 'id'
      AND att.attnum   > 0
      AND NOT att.attisdropped;

    IF v_user_id_attnum IS NULL THEN
        RAISE EXCEPTION '中止：找不到 public.users.id 列，表结构与预期不符，请先人工核对表结构';
    END IF;

    -- (b) 语义查找：contype='f' + 表是 public.bills + 指向 public.users + 约束列正好只有 user_id
    SELECT array_agg(con.conname           ORDER BY con.conname),
           array_agg(con.confdeltype::text ORDER BY con.conname)
      INTO v_names, v_deltypes
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'f'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.confrelid = 'public.users'::regclass
      AND ARRAY(
            SELECT att.attname::text
            FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute att
              ON att.attrelid = con.conrelid AND att.attnum = k.attnum
            ORDER BY k.ord
          ) = ARRAY['user_id'];

    IF v_names IS NULL THEN
        v_keeper_type := NULL;
        RAISE NOTICE '步骤6：bills 上没有任何 user_id → public.users 的外键，将新建 % 为 ON DELETE CASCADE', v_keeper;
    ELSE
        RAISE NOTICE '步骤6：命中 % 个 user_id → public.users 外键：%（对应 ON DELETE：%）',
                     array_length(v_names, 1), v_names, v_deltypes;

        -- (c) 选保留者：优先用期望名，否则取第一个命中的约束名（随后在 (d2) 改名回来）
        IF v_expected = ANY (v_names) THEN
            v_keeper := v_expected;
        ELSE
            v_keeper := v_names[1];
            RAISE NOTICE '步骤6：没有名为 % 的外键，改以 % 作为保留者（随后会重命名回 %）', v_expected, v_keeper, v_expected;
        END IF;
        v_keeper_type := v_deltypes[array_position(v_names, v_keeper)];
    END IF;

    -- (d) 先保证保留者是 CASCADE —— 这一步必须在“删重复项”之前做，
    --     否则在“保留者=NO ACTION、重复项=CASCADE”的环境里会删掉唯一的级联。
    IF v_keeper_type IS DISTINCT FROM 'c' THEN
        IF v_keeper_type IS NOT NULL THEN
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', 'bills', v_keeper);
            RAISE NOTICE '步骤6：保留者 % 当前 ON DELETE 是 %（不是 CASCADE），已先删除，准备按 002 的方式重建成 CASCADE',
                         v_keeper, v_keeper_type;
        END IF;
        EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE',
                       'bills', v_keeper);
        RAISE NOTICE '步骤6：已把 % 建成 ON DELETE CASCADE（保证“删除用户自动连带删除其账单”不丢）', v_keeper;
    ELSE
        RAISE NOTICE '步骤6：保留者 % 已经是 ON DELETE CASCADE，无需重建', v_keeper;
    END IF;

    -- (d3) 保留者若是 NOT VALID（convalidated=false），补一次 VALIDATE。
    --   为什么：NOT VALID 的外键只对新写入的行生效，历史行根本没被校验过 ——
    --   级联删除能力有了，但“历史数据一致性已校验”这个前提不成立。
    --   前面 (a) 已确认没有孤儿行，所以这里的 VALIDATE 一定能过；
    --   放在 (d2) 改名之前，是为了用当前真实名字操作，避免改名后拿旧名找不到。
    SELECT con.convalidated
      INTO v_keeper_valid
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'f'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname = v_keeper;

    IF v_keeper_valid IS FALSE THEN
        EXECUTE format('ALTER TABLE public.%I VALIDATE CONSTRAINT %I', 'bills', v_keeper);
        RAISE NOTICE '步骤6：保留者 % 之前是 NOT VALID，已 VALIDATE（历史行现在也受外键约束）', v_keeper;
    END IF;

    -- (d2) 统一改名：最终外键必须叫 bills_user_id_fkey（不变量）。
    --   为什么必须改名：只按语义“留一个”不够 —— 若某环境留下的唯一外键是历史名
    --   fk_bills_user（NO ACTION 被原地重建成 CASCADE），后续迁移/排查按
    --   bills_user_id_fkey 查就会找不到，脚本的幂等前提也会失效。
    --   用 RENAME CONSTRAINT：只改系统目录里的名字，不重建索引、不重校验数据。
    IF v_keeper IS DISTINCT FROM v_expected THEN
        EXECUTE format('ALTER TABLE public.%I RENAME CONSTRAINT %I TO %I', 'bills', v_keeper, v_expected);
        RAISE NOTICE '步骤6：保留者 % 已重命名为 %（保证最终外键名稳定）', v_keeper, v_expected;
        v_renamed_from := v_keeper;
        v_keeper := v_expected;
    END IF;

    -- (e) 再删重复项
    IF v_names IS NOT NULL THEN
        FOR i IN 1 .. array_length(v_names, 1) LOOP
            -- 既要跳过保留者的现名，也要跳过它改名前的旧名：
            -- 旧名此刻已不存在（改名后），DROP IF EXISTS 会静默成功并打出误导性的“已删除”日志。
            IF v_names[i] IS DISTINCT FROM v_keeper
               AND v_names[i] IS DISTINCT FROM v_renamed_from THEN
                EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', 'bills', v_names[i]);
                RAISE NOTICE '步骤6：已删除重复外键 public.bills.%（原 ON DELETE %）', v_names[i], v_deltypes[i];
            END IF;
        END LOOP;
    END IF;

    -- (g) 断言（强化版）：不只数个数，还要把「名字 / CASCADE / 已校验 / 指向的列」逐个钉死
    --   ① 恰好 1 个「bills.user_id → public.users」外键
    --   ② 名字必须是 bills_user_id_fkey
    --   ③ confdeltype 必须是 'c'（ON DELETE CASCADE）
    --   ④ convalidated 必须是 true（NOT VALID 的外键只对新数据生效，历史孤儿行不会被校验）
    --   ⑤ confkey 必须精确等于 users.id 的 attnum（不能指向 users 的其它列，如 username）
    --   子查询只按 conkey=['user_id'] + confrelid=users 过滤，**刻意不过滤 confkey**，
    --   这样 ⑤ 才是真断言，而不是同义反复。
    SELECT count(*),
           count(*) FILTER (WHERE t.is_cascade),
           count(*) FILTER (WHERE t.is_validated),
           count(*) FILTER (WHERE t.name_ok),
           count(*) FILTER (WHERE t.key_ok),
           string_agg(t.conname || ' [' || t.deltype
                      || CASE WHEN t.is_validated THEN '' ELSE ', NOT VALID' END || ']', ', ')
      INTO v_total, v_cascade, v_validated, v_named, v_keyed, v_detail
    FROM (
        SELECT con.conname,
               con.confdeltype::text                 AS deltype,
               con.confdeltype = 'c'                 AS is_cascade,
               con.convalidated                      AS is_validated,
               con.conname = v_expected              AS name_ok,
               con.confkey = ARRAY[v_user_id_attnum] AS key_ok
        FROM pg_constraint con
        JOIN pg_class rel    ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
        WHERE con.contype = 'f'
          AND ns.nspname = 'public'
          AND rel.relname = 'bills'
          AND con.confrelid = 'public.users'::regclass
          AND ARRAY(
                SELECT att.attname::text
                FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                ORDER BY k.ord
              ) = ARRAY['user_id']
    ) t;

    IF v_total IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '步骤6 断言①失败：bills 上 user_id → users 的外键应恰好 1 个，实际 % 个（%）。事务已回滚', v_total, coalesce(v_detail, '<无>');
    END IF;
    IF v_named IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '步骤6 断言②失败：最终外键名必须是 %，实际为 %。事务已回滚', v_expected, coalesce(v_detail, '<无>');
    END IF;
    IF v_cascade IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '步骤6 断言③失败：外键 % 的 ON DELETE 必须是 CASCADE(''c'')。事务已回滚', v_expected;
    END IF;
    IF v_validated IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '步骤6 断言④失败：外键 % 的 convalidated 必须是 true（NOT VALID 的外键不校验历史行）。事务已回滚', v_expected;
    END IF;
    IF v_keyed IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '步骤6 断言⑤失败：外键 % 的 confkey 必须精确指向 users.id（attnum=%），不能指向 users 的其它列。事务已回滚', v_expected, v_user_id_attnum;
    END IF;

    RAISE NOTICE '步骤6 通过：user_id → users.id 外键恰好 1 个且名为 %，ON DELETE CASCADE，convalidated=true，confkey 精确等于 users.id(attnum=%)', v_expected, v_user_id_attnum;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 7：执行后核对（任何一条不满足就 RAISE EXCEPTION → 整个事务回滚）
--   ① type 只有 收入/支出（且非 NULL）
--   ② 支出金额全部 >= 0
--   ③ amount 列类型确实是 numeric(12,2)，且已 NOT NULL
--   ④ user_id→users.id 外键：恰好 1 个、名字 = bills_user_id_fkey、ON DELETE CASCADE、
--      convalidated = true、confkey 精确等于 users.id 的 attnum
--   ⑤ 两条 CHECK 约束都在，且**按定义**符合预期（含 IS NOT NULL / 类型集合恰为两值 /
--      有 收入 分支 / 有 amount >= 0 形式的下界），不是只看名字和数量
-- ---------------------------------------------------------------------------
SELECT con.conname                   AS constraint_name,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel    ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
WHERE con.contype = 'c'
  AND ns.nspname = 'public'
  AND rel.relname = 'bills'
ORDER BY con.conname;

SELECT rel.relname                   AS table_name,
       con.conname                   AS constraint_name,
       con.confdeltype               AS on_delete_code,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel    ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname = 'public'
  AND rel.relname = 'bills'
  AND con.confrelid = 'public.users'::regclass
ORDER BY con.conname;

DO $$
DECLARE
    v_bad_type      integer := 0;
    v_neg_expense   integer := 0;
    v_amount_type   text;
    v_amount_nullable text;
    v_amount_attnotnull boolean;
    v_fk_total      integer := 0;
    v_fk_cascade    integer := 0;
    v_fk_validated  integer := 0;
    v_fk_named      integer := 0;
    v_fk_keyed      integer := 0;
    v_fk_detail     text;
    v_user_id_attnum smallint;
    v_check_count   integer := 0;
    v_type_check    text;
    v_exp_check     text;
    v_lits          text[];
    v_type_check_ok boolean;
    v_exp_check_ok  boolean;
BEGIN
    -- ① type 只有 收入/支出
    SELECT count(*) INTO v_bad_type
    FROM public.bills
    WHERE type IS NULL OR type NOT IN ('收入', '支出');
    IF v_bad_type > 0 THEN
        RAISE EXCEPTION '核对①未通过：仍有 % 行 type 不是 收入/支出，事务已回滚', v_bad_type;
    END IF;

    -- ② 支出金额全部 >= 0（收入允许为负，不在核对范围内）
    SELECT count(*) INTO v_neg_expense
    FROM public.bills
    WHERE type = '支出' AND amount < 0;
    IF v_neg_expense > 0 THEN
        RAISE EXCEPTION '核对②未通过：仍有 % 行 type=支出 且 amount < 0（会虚增结余），事务已回滚', v_neg_expense;
    END IF;

    -- ③ amount 列确实是 numeric(12,2)
    SELECT c.data_type || '(' || c.numeric_precision || ',' || c.numeric_scale || ')',
           c.is_nullable
      INTO v_amount_type, v_amount_nullable
    FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.table_name   = 'bills'
      AND c.column_name  = 'amount';
    IF v_amount_type IS DISTINCT FROM 'numeric(12,2)' THEN
        RAISE EXCEPTION '核对③未通过：public.bills.amount 的实际类型是 %，不是 numeric(12,2)，事务已回滚', coalesce(v_amount_type, '<列不存在>');
    END IF;

    -- ③b amount 必须 NOT NULL（两条独立证据：information_schema + pg_attribute）
    --    为什么单独立一条：NULL 金额会让 CHECK (type='收入' OR amount>=0) 求值为 NULL，
    --    SQL 视为“通过”，这笔支出随后在 .sum() 里静默消失（结余虚高）。
    IF v_amount_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对③b未通过：public.bills.amount 仍允许 NULL（information_schema.is_nullable=%），NULL 金额会被 CHECK 以“求值为 NULL=通过”放行并在汇总里静默消失，事务已回滚', coalesce(v_amount_nullable, '<列不存在>');
    END IF;

    SELECT att.attnotnull
      INTO v_amount_attnotnull
    FROM pg_attribute att
    WHERE att.attrelid = 'public.bills'::regclass
      AND att.attname  = 'amount'
      AND att.attnum   > 0
      AND NOT att.attisdropped;
    IF v_amount_attnotnull IS NOT TRUE THEN
        RAISE EXCEPTION '核对③b未通过：pg_attribute.attnotnull 不是 true（实际 %），amount 仍未 NOT NULL，事务已回滚', coalesce(v_amount_attnotnull::text, '<列不存在>');
    END IF;

    -- ④ user_id → users.id 外键：恰好 1 个 + 名字对 + CASCADE + convalidated + confkey 精确
    SELECT att.attnum
      INTO v_user_id_attnum
    FROM pg_attribute att
    WHERE att.attrelid = 'public.users'::regclass
      AND att.attname  = 'id'
      AND att.attnum   > 0
      AND NOT att.attisdropped;
    IF v_user_id_attnum IS NULL THEN
        RAISE EXCEPTION '核对④未通过：找不到 public.users.id 列，事务已回滚';
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE t.is_cascade),
           count(*) FILTER (WHERE t.is_validated),
           count(*) FILTER (WHERE t.name_ok),
           count(*) FILTER (WHERE t.key_ok),
           string_agg(t.conname || ' [' || t.deltype
                      || CASE WHEN t.is_validated THEN '' ELSE ', NOT VALID' END || ']', ', ')
      INTO v_fk_total, v_fk_cascade, v_fk_validated, v_fk_named, v_fk_keyed, v_fk_detail
    FROM (
        SELECT con.conname,
               con.confdeltype::text                 AS deltype,
               con.confdeltype = 'c'                 AS is_cascade,
               con.convalidated                      AS is_validated,
               con.conname = 'bills_user_id_fkey'    AS name_ok,
               con.confkey = ARRAY[v_user_id_attnum] AS key_ok
        FROM pg_constraint con
        JOIN pg_class rel    ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
        WHERE con.contype = 'f'
          AND ns.nspname = 'public'
          AND rel.relname = 'bills'
          AND con.confrelid = 'public.users'::regclass
          AND ARRAY(
                SELECT att.attname::text
                FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                ORDER BY k.ord
              ) = ARRAY['user_id']
    ) t;

    IF v_fk_total IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对④未通过：bills 上 user_id → users 的外键应恰好 1 个，实际 % 个（%），事务已回滚', v_fk_total, coalesce(v_fk_detail, '<无>');
    END IF;
    IF v_fk_named IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对④未通过：外键名必须是 bills_user_id_fkey，实际为 %，事务已回滚', coalesce(v_fk_detail, '<无>');
    END IF;
    IF v_fk_cascade IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对④未通过：外键 bills_user_id_fkey 的 ON DELETE 必须是 CASCADE(''c'')，事务已回滚';
    END IF;
    IF v_fk_validated IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对④未通过：外键 bills_user_id_fkey 的 convalidated 必须是 true（NOT VALID 不校验历史行），事务已回滚';
    END IF;
    IF v_fk_keyed IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对④未通过：外键 bills_user_id_fkey 的 confkey 必须精确指向 users.id（attnum=%），实际不是，事务已回滚', v_user_id_attnum;
    END IF;

    -- ⑤ 两条独立 CHECK 约束都在，且**按定义**符合预期（不只数名字）
    SELECT count(*),
           min(pg_get_constraintdef(con.oid)) FILTER (WHERE con.conname = 'bills_type_check'),
           min(pg_get_constraintdef(con.oid)) FILTER (WHERE con.conname = 'bills_expense_amount_positive')
      INTO v_check_count, v_type_check, v_exp_check
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'c'
      AND ns.nspname = 'public'
      AND rel.relname = 'bills'
      AND con.conname IN ('bills_type_check', 'bills_expense_amount_positive');
    IF v_check_count <> 2 THEN
        RAISE EXCEPTION '核对⑤未通过：bills 上应同时存在 bills_type_check 与 bills_expense_amount_positive，实际只找到 % 条，事务已回滚', v_check_count;
    END IF;

    -- ⑤a：type 约束必须同时含 IS NOT NULL，且字面量集合恰为 {收入, 支出}
    SELECT array_agg(DISTINCT m[1])
      INTO v_lits
    FROM regexp_matches(v_type_check, '''([^'']*)''', 'g') AS m;
    v_type_check_ok := v_type_check LIKE '%IS NOT NULL%'
                       AND v_lits IS NOT NULL
                       AND array_length(v_lits, 1) = 2
                       AND v_lits @> ARRAY['收入', '支出'];
    IF NOT v_type_check_ok THEN
        RAISE EXCEPTION '核对⑤未通过：bills_type_check 的定义不符合预期（必须含 IS NOT NULL，且类型集合恰为 收入/支出；实测字面量集合 = %），实际定义 = %，事务已回滚。请人工 DROP 该约束后重跑本脚本', coalesce(v_lits::text, '<无字面量>'), coalesce(v_type_check, '<NULL>');
    END IF;

    -- ⑤b：金额约束必须有 '收入' 分支（放行负收入）且含 amount >= 0 形式的下界
    SELECT array_agg(DISTINCT m[1])
      INTO v_lits
    FROM regexp_matches(v_exp_check, '''([^'']*)''', 'g') AS m;
    v_exp_check_ok := v_lits = ARRAY['收入']
                      AND v_exp_check ~ 'amount\)?\s*(::\s*[a-z ]+(\(\s*\d+(\s*,\s*\d+)?\s*\))?)?\s*>=\s*\(?\s*0';
    IF NOT v_exp_check_ok THEN
        RAISE EXCEPTION '核对⑤未通过：bills_expense_amount_positive 的定义不符合预期（必须写成 type = 收入 OR amount >= 0，否则会把合法的负收入也挡在门外；实测字面量集合 = %），实际定义 = %，事务已回滚', coalesce(v_lits::text, '<无字面量>'), coalesce(v_exp_check, '<NULL>');
    END IF;

    RAISE NOTICE '核对通过：type 只有 收入/支出；支出金额全部 >= 0；amount 为 numeric(12,2) 且 NOT NULL；user_id→users.id 外键恰好 1 个（名为 bills_user_id_fkey、CASCADE、convalidated、confkey=users.id）；两条 CHECK 约束定义均符合预期';
END $$;

-- 最后一行给人肉眼确认：看到 OK 就说明前面每一步都过了（否则事务已因 EXCEPTION 回滚）
SELECT 'OK' AS result;

COMMIT;
