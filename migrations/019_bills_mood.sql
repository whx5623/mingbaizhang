-- =============================================================================
-- 019_bills_mood.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.bills 增加消费情绪列 mood —— 「情绪消费气象站」用它把账单和消费时的
--   情绪（😊开心 / 😢Emo / 😠焦虑 / 😴疲惫 / 🤩激动）关联起来，做情绪消费分析。
--     mood  VARCHAR(20) NULL
--
--   · 为什么 NULL 而不是 NOT NULL + DEFAULT：情绪是**用户可选**的附加信息，
--     「没有标注」和「标注为开心」是两种不同的语义。历史账单、AI 识别不出情绪、
--     用户不想说，都应如实读作 NULL；用 DEFAULT 填一个假情绪会污染统计。
--   · 为什么 VARCHAR(20)：情绪是枚举式短标签（db.py 的 MOOD_OPTIONS 最长 3 个字符，
--     加 emoji 也在 20 以内），列级 CHECK 再兜一层长度，挡住「把整段描述写进情绪」。
--   · 本迁移**不加索引**：bills 目前数据量小，情绪分析按月 + 用户过滤，
--     user_id 上的既有索引足够；等真有必要再单独加，避免为低频查询背写入成本。
--   · 已有行不会丢数据：ADD COLUMN 只加一列可空列，不重写表、不动任何已有值。
--     历史账单 mood 一律读作 NULL（页面显示为「未标注」），这是如实反映 ——
--     本迁移**不做**任何基于商户名/金额的猜测性回填。
--
--   ⚠ 本迁移只加一列 mood（以及一条长度 CHECK），不改动任何已有列与已有数据。
--   ⚠ 回滚会**删掉情绪数据**（不可恢复）：回滚前如需保留，请先导出 mood 列。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/019_bills_mood.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → … → 017 → 019（本文件）
--   ✅ 018 号由其它并行功能占用，本文件按 019 编号；只依赖 001 建的 bills。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只**新增**一列，不改动已有列和数据，风险很低；仍建议先留结构备份：
--       pg_dump --schema-only -t public.bills "$DATABASE_URL" > schema_before_019.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE bills_backup AS SELECT ...
--      之类的库内副本 —— 它会被 PostgREST 暴露、会随主表一起被删、也会让
--      pg_dump 的 schema 里多出垃圾对象。备份一律落在库外文件。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的 CREATE TABLE
--   IF NOT EXISTS），**刻意不把 mood 写进那条 CREATE TABLE**：迁移统一走本文件，
--   避免「应用启动顺手改 schema」与「迁移脚本」两条路径互相打架。
--   部署顺序是：先执行本脚本 → 再上带情绪标签的代码。
--   反过来（019 未执行）时 db.py 会**优雅降级**：探测到 bills.mood 不存在，
--   INSERT 自动去掉 mood 列、只告警一次，记账照常成功（不会红屏、不会丢账单）；
--   情绪气象站显示「功能暂不可用」。执行 019 后无需改代码，重启应用即可写入情绪。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   ALTER TABLE public.bills DROP COLUMN IF EXISTS mood;
--   （bills_mood_len 是列级 CHECK，随 DROP COLUMN 自动删除，无需单独 DROP CONSTRAINT。）
--   （回滚后 db.py 的情绪写入自动走「列不存在」降级分支，记账不受影响；
--     已有的情绪数据随列一起删除，不可恢复。）
--
-- =============================================================================
-- 【幂等】
--   · 列：ADD COLUMN IF NOT EXISTS + information_schema 存在性判断（双保险）；
--   · CHECK：pg_constraint 存在性判断后 ADD CONSTRAINT；
--   · 核对：每次执行都跑，失败 RAISE EXCEPTION 整体回滚。
--   · 重复执行不会改已有列类型、不动数据。
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 1：加列（可空，无默认值）
-- ---------------------------------------------------------------------------
ALTER TABLE public.bills
    ADD COLUMN IF NOT EXISTS mood VARCHAR(20) NULL;

COMMENT ON COLUMN public.bills.mood IS
    '消费时的情绪标签：开心/Emo/焦虑/疲惫/激动；NULL = 未标注（019 迁移新增）';

-- ---------------------------------------------------------------------------
-- 步骤 2：长度 CHECK（只挡长度，不枚举取值 —— 标签集合由 db.py MOOD_OPTIONS 维护，
--         将来扩标签不需要再改库）
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.bills'::regclass
          AND conname = 'bills_mood_len'
    ) THEN
        ALTER TABLE public.bills
            ADD CONSTRAINT bills_mood_len CHECK (mood IS NULL OR char_length(mood) <= 20);
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：核对（列存在 / 类型 / 长度 / 可空 / CHECK 齐备）
--     任何一项不符 → RAISE EXCEPTION → 整个事务回滚，不会留下半成品。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type TEXT;
    v_len INTEGER;
    v_nullable TEXT;
    v_check INTEGER;
    v_advice TEXT := '请检查本次执行是否被部分回滚，然后整段重新执行本文件。';
BEGIN
    SELECT data_type, character_maximum_length, is_nullable
      INTO v_type, v_len, v_nullable
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'bills'
       AND column_name = 'mood';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①未通过：public.bills.mood 不存在。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'character varying' THEN
        RAISE EXCEPTION '核对②未通过：mood 类型是 %（应为 character varying）。%', v_type, v_advice;
    END IF;
    IF v_len IS DISTINCT FROM 20 THEN
        RAISE EXCEPTION '核对③未通过：mood 长度是 %（应为 20）。%', v_len, v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对④未通过：mood 应为可空（is_nullable=YES）。%', v_advice;
    END IF;

    SELECT COUNT(*)
      INTO v_check
      FROM pg_constraint
     WHERE conrelid = 'public.bills'::regclass
       AND conname = 'bills_mood_len';
    IF v_check = 0 THEN
        RAISE EXCEPTION '核对⑤未通过：CHECK 约束 bills_mood_len 不存在。%', v_advice;
    END IF;

    RAISE NOTICE '核对通过：bills.mood = VARCHAR(20) NULL，CHECK bills_mood_len 齐备';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 4：成功标记
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;