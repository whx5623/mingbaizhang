-- =============================================================================
-- 012_bills_category.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.bills 增加消费分类列 category —— 「明白账」的账单分类（餐饮 / 奶茶咖啡 /
--   购物 / 生活费 / 交通 / 娱乐 / 学习 / 医疗 / 其他），用于按分类汇总与图表。
--     category  VARCHAR(50) NOT NULL DEFAULT '其他'
--
--   · 为什么 NOT NULL + DEFAULT：历史账单（以及任何走旧 INSERT 的路径）不会因为
--     “新列没值”变成 NULL；NULL 会让分类饼图出现一个看不见的空白扇区、也让
--     GROUP BY category 多出一组无名数据。DEFAULT '其他' 是明确的兜底口径，
--     与 db.py 的 FALLBACK 分类一致。
--   · 为什么 VARCHAR(50)：分类是枚举式短标签（db.py 的 BILL_CATEGORIES 最长 4 个字），
--     50 留足余量也挡住“把整段描述写进分类”的误用。列级 CHECK 再兜一层长度。
--   · 索引 idx_bills_category：按分类聚合（GROUP BY category / WHERE category = ?）
--     是分类页的主查询；bills 目前小，但索引是幂等对象，先建好避免以后补。
--   · 已有行不会丢数据：PostgreSQL 11+ 的 ADD COLUMN ... DEFAULT 常量只改元数据，
--     不重写表；执行后历史账单全部读作 '其他'，这是如实反映（历史没有分类信息，
--     本迁移**不做**基于商户名的回填 —— 回填规则会在代码里演化，迁移不该固化它）。
--
--   ⚠ 本迁移只加列 / 索引 / 一条 CHECK，不改动任何已有列与已有数据。
--   ⚠ 回滚会**删掉分类数据**（不可恢复）：回滚前如需保留，请先导出 category 列。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/012_bills_category.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011 → 012（本文件）
--   ✅ 编号连续：012 是 011 之后的下一号，只依赖 001 建的 bills（以及 003 的金额口径）。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只**新增**列 / 索引 / CHECK，不改动已有列和数据，风险很低；仍建议先留结构备份：
--       pg_dump --schema-only -t public.bills "$DATABASE_URL" > schema_before_012.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE bills_backup AS SELECT ...
--      之类的库内副本 —— 它会被 PostgREST 暴露、会随主表一起被删、也会让
--      pg_dump 的 schema 里多出垃圾对象。备份一律落在库外文件。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的 CREATE TABLE
--   IF NOT EXISTS），**刻意不把 category 写进那条 CREATE TABLE**：迁移统一走本文件，
--   避免「应用启动顺手改 schema」与「迁移脚本」两条路径互相打架、也避免生产库被
--   隐式改结构。所以部署顺序是：先执行本脚本 → 再上带分类的代码。
--   反过来（012 未执行）时 db.py 会**优雅降级**：探测到 bills.category 不存在，
--   INSERT 自动去掉 category 列、只告警一次，记账照常成功（不会红屏、不会丢账单）；
--   分类页显示为空。执行 012 后无需改代码，重启应用即可写入分类。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   DROP INDEX IF EXISTS public.idx_bills_category;
--   ALTER TABLE public.bills DROP COLUMN IF EXISTS category;
--   （bills_category_len 是列级 CHECK，随 DROP COLUMN 自动删除，无需单独 DROP CONSTRAINT。）
--   （回滚后 db.py 的分类写入自动走「列不存在」降级分支，记账不受影响；
--     已有的分类数据随列一起删除，不可恢复。）
--
-- =============================================================================
-- 【幂等】
--   · 列：ADD COLUMN IF NOT EXISTS + information_schema 存在性判断（双保险）；
--   · 索引：CREATE INDEX IF NOT EXISTS；
--   · CHECK：pg_constraint 存在性判断后 ADD CONSTRAINT；
--   · 核对：每次执行都跑，失败 RAISE EXCEPTION 整体回滚。
--   · 重复执行不会改已有列类型、不动数据、不重建索引（NOTICE 跳过）。
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（bills 是否存在）
--   category 加在 bills 上；bills 不存在时 ALTER TABLE 会报 42P01 并让整个事务
--   进 aborted 状态。不如在这里给一句能直接照做的报错。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF to_regclass('public.bills') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.bills 不存在。请先执行 001~003 系列迁移（或启动一次应用让 db.init_db() 建表），并确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：public.bills 存在，继续';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：加列（幂等）
--   双保险：先查 information_schema，已存在则 NOTICE 跳过；新增时仍用
--   ADD COLUMN IF NOT EXISTS（防并发/手工已加过时撞车）。
--   不在已存在时自动修正列类型/可空性：那等于隐式改生产结构，且若历史数据
--   不满足预期会在 ALTER 中途失败；正确做法由步骤 3 的核对给出明确报错。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'bills' AND column_name = 'category'
    ) THEN
        RAISE NOTICE '步骤1：public.bills.category 已存在，跳过 ADD COLUMN（幂等）';
    ELSE
        ALTER TABLE public.bills
            ADD COLUMN IF NOT EXISTS category VARCHAR(50) NOT NULL DEFAULT '其他';
        RAISE NOTICE '步骤1：已新增 public.bills.category（VARCHAR(50) NOT NULL DEFAULT ''其他''）';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1（续）：列级 CHECK（幂等）
--   VARCHAR(50) 本身已限长，这条 CHECK 是与 db.py「分类是短标签」的口径双保险，
--   也方便以后若把类型放宽成 TEXT 时仍保留长度约束。命名约束 + 存在性判断。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.bills'::regclass AND conname = 'bills_category_len'
    ) THEN
        RAISE NOTICE '步骤1（续）：约束 bills_category_len 已存在，跳过（幂等）';
    ELSE
        ALTER TABLE public.bills
            ADD CONSTRAINT bills_category_len CHECK (char_length(category) <= 50);
        RAISE NOTICE '步骤1（续）：已新增约束 bills_category_len';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 2：分类索引（幂等）
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bills_category
    ON public.bills (category);

-- ---------------------------------------------------------------------------
-- 步骤 3：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：列存在 / 类型 character varying / 长度 50 / NOT NULL /
--           DEFAULT 含 '其他' / CHECK 定义逐字正确 / 索引存在且键列就是 category。
--   约束与索引都用「目录 + 键列」判断，而不是只看名字：只查名字挡不住
--   「名字对、定义/键列错」。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type       text;
    v_len        integer;
    v_nullable   text;
    v_default    text;
    v_constraint text;
    v_def_norm   text;
    v_idx_col    text;
    v_advice     text := '若 bills.category 已存在但结构与预期不符：本脚本刻意不自动改旧列（避免误改生产结构）。请先导出数据，再执行回滚段（DROP INDEX + DROP COLUMN）后重跑本脚本。';
BEGIN
    -- ① 列存在 + 类型 + 长度
    SELECT data_type, character_maximum_length, is_nullable, column_default
      INTO v_type, v_len, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'bills' AND column_name = 'category';
    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①a未通过：public.bills.category 不存在。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'character varying' THEN
        RAISE EXCEPTION '核对①b未通过：category 类型是 %（应为 character varying / VARCHAR(50)）。%', v_type, v_advice;
    END IF;
    IF v_len IS DISTINCT FROM 50 THEN
        RAISE EXCEPTION '核对①c未通过：category 长度是 %（应为 50）。%', coalesce(v_len::text, '<无限长>'), v_advice;
    END IF;

    -- ② NOT NULL：空分类会让 GROUP BY 多出无名组，必须禁止
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对②未通过：category is_nullable = %（应为 NO）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;

    -- ③ DEFAULT 必须含 '其他'：历史 INSERT（不带 category 的旧代码路径）靠它兜底
    --    PostgreSQL 渲染成 '''其他''::character varying'，这里判「包含」而不是逐字比对。
    IF v_default IS NULL OR position('其他' in v_default) = 0 THEN
        RAISE EXCEPTION '核对③未通过：category default = %（应含 ''其他''）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ④ CHECK：存在 + 定义逐字正确（去空白、小写后比对）
    SELECT pg_get_constraintdef(oid) INTO v_constraint
    FROM pg_constraint
    WHERE conrelid = 'public.bills'::regclass
      AND conname = 'bills_category_len'
      AND contype = 'c';
    IF v_constraint IS NULL THEN
        RAISE EXCEPTION '核对④a未通过：缺少 CHECK 约束 bills_category_len（char_length(category) <= 50）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_constraint, '\s+', '', 'g'));
    IF v_def_norm <> 'check((char_length((category)::text)<=50))' THEN
        RAISE EXCEPTION '核对④b未通过：bills_category_len 定义是 %（应为 CHECK (char_length(category) <= 50)）。%', v_constraint, v_advice;
    END IF;

    -- ⑤ 索引：存在、有效、键列恰好是 category
    SELECT a.attname INTO v_idx_col
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_namespace ns ON ns.oid = ic.relnamespace
    LEFT JOIN pg_attribute a
           ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
    WHERE ns.nspname = 'public'
      AND ic.relname = 'idx_bills_category'
      AND i.indrelid = 'public.bills'::regclass
      AND i.indisvalid;
    IF v_idx_col IS NULL THEN
        RAISE EXCEPTION '核对⑤a未通过：索引 idx_bills_category 不存在、无效或不在 public.bills 上。%', v_advice;
    END IF;
    IF v_idx_col IS DISTINCT FROM 'category' THEN
        RAISE EXCEPTION '核对⑤b未通过：idx_bills_category 的键列是 %（应恰好是 category）。%', v_idx_col, v_advice;
    END IF;

    RAISE NOTICE '核对通过：bills.category = VARCHAR(50) NOT NULL DEFAULT ''其他''，CHECK bills_category_len 与索引 idx_bills_category(category) 齐备';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 4：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表列/约束/索引/核对全部通过并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;
