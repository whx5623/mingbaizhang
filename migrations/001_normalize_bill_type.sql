-- =============================================================================
-- 001_normalize_bill_type.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
-- 把 public.bills.type 的历史脏值清洗成唯一允许的两种中文取值："收入" / "支出"。
-- 背景：app.py 的收支汇总全部依赖字符串精确比较
--   （df[df["type"] == "收入"]["amount"].sum() / == "支出"），任何 "expense"、
--   " Income "、"支出 "（带半角/全角空格、换行）之类的脏值，都会让这笔钱在
--   收入侧和支出侧同时漏算 → 结余虚高 → 误判“已达成目标存款”。
-- 口径与 db.py 的 normalize_bill_type() 完全一致（同一套同义词表）。
-- 注意：同义词表**刻意不含** '1' / '0' / '-1' 这类数字编码 —— 它们没有公认约定
--   （很多账本里 1 恰恰表示支出），一旦命中就会把支出永久改写成收入。
--   这些取值走“无法识别 → 步骤 3b 兜底为支出”的保守路径，宁可少算结余。
--
-- 【执行入口】
-- Supabase 控制台 → 选择本项目 → SQL Editor → 整段粘贴执行（或 psql -f 本文件）。
--
-- 【重要】
-- 1) 本脚本由 db-expert 生成，未自动执行 —— 生成时没有在生产库运行过其中任何语句。
-- 2) 执行前请先在演练环境（或 Supabase 的 Preview/Branch 库）跑一遍。
-- 3) 执行前建议备份：
--      CREATE TABLE bills_backup_20260101 AS SELECT * FROM bills;
--    （把日期换成实际执行日期；确认无误后再 DROP TABLE 备份表）
-- 4) 可重复执行（幂等）：第二次执行时同义词映射 0 行、NULL/残余兜底 0 行。
-- 5) 自带事务边界 BEGIN ... COMMIT；中途报错请 ROLLBACK 后排查，不要手工提交半成品。
-- 6) 末尾的核对块在**不通过时用 RAISE EXCEPTION**：整个事务会自动中止并回滚，
--    不会留下“改了一半”的库里数据。也就是说：脚本要么整体生效，要么完全没生效。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 步骤 1：清洗前类型分布（人工确认哪些值属于“同义词”，哪些是“真·未知”）
-- ---------------------------------------------------------------------------
SELECT type, COUNT(*) FROM bills GROUP BY type ORDER BY 2 DESC;

-- 单独看一眼 NULL：app.py 的 == 比较同样会漏掉 type 为 NULL 的行
SELECT COUNT(*) AS null_type_rows FROM bills WHERE type IS NULL;

-- ---------------------------------------------------------------------------
-- 步骤 2：已知同义词 → 收入 / 支出
-- 规整方式：lower(trim(both E' \t\n\r' || chr(12288) || chr(160) from type))
--   - trim 掉首尾半角空格、制表符、换行、回车；
--   - chr(12288) 就是全角空格 U+3000（等价写法 U&'\3000'），Postgres 的 E'' 里
--     直接用 \u3000 不保险，所以用 chr() 拼进去；
--   - chr(160) 是不换行空格 NBSP U+00A0（从 Excel / 网页复制粘贴的常见脏字符），
--     Python 的 str.strip() 会吃掉它，SQL 这里必须同步吃掉，否则会出现
--     “Python 判收入、SQL 判支出”的永久性改写不一致；
--   - 再 lower() 统一大小写。
-- 已经是 "收入"/"支出" 的行会被映射成它们自己（幂等），且加了 IS DISTINCT FROM
-- 判断，不会产生无意义的空更新。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_changed integer := 0;
BEGIN
    WITH norm AS (
        SELECT id,
               CASE
                   WHEN k IN ('收入','收','income','in','revenue','salary','credit')
                       THEN '收入'
                   WHEN k IN ('支出','支','expense','expenditure','out','spend','debit','cost')
                       THEN '支出'
                   ELSE NULL
               END AS target
        FROM (
            SELECT id,
                   lower(trim(both E' \t\n\r' || chr(12288) || chr(160) from coalesce(type, ''))) AS k
            FROM bills
        ) t
    )
    UPDATE bills b
       SET type = n.target
      FROM norm n
     WHERE b.id = n.id
       AND n.target IS NOT NULL
       AND b.type IS DISTINCT FROM n.target;

    GET DIAGNOSTICS v_changed = ROW_COUNT;
    RAISE NOTICE '步骤2 同义词映射完成：本次改写 % 行（可重复执行，第二次应为 0）', v_changed;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 3a（兜底）：type 为 NULL 的行 → '支出'
-- NULL 在 app.py 的 == 过滤里同样会被漏算，按保守口径归入支出，
-- 保证“结余只会偏小、不会虚高”，不会误报“已达成目标存款”。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_cnt integer := 0;
BEGIN
    UPDATE bills SET type = '支出' WHERE type IS NULL;
    GET DIAGNOSTICS v_cnt = ROW_COUNT;
    RAISE NOTICE '步骤3a NULL 兜底：% 行 NULL 写成 支出', v_cnt;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 3b（兜底）：既不是 收入 也不是 支出 的残余行 → '支出'
-- 注意：这一步会吞掉“真·未知”的取值（例如英文分类 Food / Transport，
-- 以及 '1' / '0' / '-1' 这类没有公认约定的数字编码），
-- 所以请先看步骤 1 的分布结果人工确认；如果某类取值其实应算收入，
-- 请先把它补进步骤 2 的同义词表再执行，否则会被当成支出。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_cnt integer := 0;
BEGIN
    UPDATE bills SET type = '支出' WHERE type NOT IN ('收入', '支出');
    GET DIAGNOSTICS v_cnt = ROW_COUNT;
    RAISE NOTICE '步骤3b 残余兜底：% 行未识别取值写成 支出（执行前请对照步骤1分布确认）', v_cnt;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 4：执行后类型分布核对 —— 预期只剩 收入 / 支出 两行
-- 下面的 DO 块在核对不通过时会 RAISE EXCEPTION：整个事务自动回滚，
-- 脚本等于“完全没执行”，不会留下半成品数据。
-- ---------------------------------------------------------------------------
SELECT type, COUNT(*) FROM bills GROUP BY type ORDER BY 2 DESC;

DO $$
DECLARE
    v_bad   integer := 0;
    v_total integer := 0;
BEGIN
    SELECT count(*) INTO v_total FROM bills;
    SELECT count(*) INTO v_bad FROM bills WHERE type IS NULL OR type NOT IN ('收入', '支出');
    IF v_bad = 0 THEN
        RAISE NOTICE '核对通过：bills 共 % 行，type 只剩 收入/支出 两个值', v_total;
    ELSE
        -- 用 EXCEPTION 而不是 WARNING：让整个事务中止并回滚，不留“改了一半”的半成品
        RAISE EXCEPTION '核对未通过：仍有 % 行 type 不是 收入/支出，事务已回滚，未写入任何变更，请手工排查', v_bad;
    END IF;
END $$;

COMMIT;
