-- =============================================================================
-- 020_assets.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   新建 public.assets —— 「个人资产负债表 / 数码资产折旧仪」的资产台账。
--   每条记录是用户手里的一件资产（iPad、相机、电动车……），按直线法折旧：
--
--     id                 SERIAL PRIMARY KEY
--     user_id            INTEGER NOT NULL → users(id) ON DELETE CASCADE
--     name               VARCHAR(100) NOT NULL
--     category           VARCHAR(20) NOT NULL DEFAULT '其他'   -- 数码 / 生活 / 其他
--     purchase_price     NUMERIC(12,2) NOT NULL DEFAULT 0
--     purchase_date      DATE NOT NULL
--     depreciation_rate  NUMERIC(5,4) NOT NULL DEFAULT 0.2000  -- 年折旧率，0.2 = 20%
--     created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
--
--   · 为什么 purchase_date 用 DATE（而 bills.date 是 VARCHAR）：assets 是新表，
--     没有历史包袱；DATE 让「已使用多久」由数据库保证是合法日期，也避免应用侧
--     再解析字符串。写入统一走 Python date 对象（psycopg2 原生适配）。
--   · 为什么 purchase_price 用 NUMERIC(12,2)（而 bills.amount 是 FLOAT）：钱在
--     新表上不该有二进制浮点误差；12 位整数部分足够覆盖消费级数码，2 位小数与
--     页面展示口径一致。读取侧统一转 Decimal 再定量，绝不参与 float 累加。
--   · 为什么 depreciation_rate 用 NUMERIC(5,4) + CHECK 0~1：0.2000 表示 20%/年，
--     4 位小数足够表达 12.5% 这类常见档位；CHECK 挡住 20（把百分数当小数）这种
--     会让残值瞬间变负的误用。0 表示「不折旧」（收藏品/黄金），1 表示一年归零。
--   · 为什么 category 不做枚举 CHECK：分类集合由 db.ASSET_CATEGORIES 维护，将来
--     加「家用电器」不该再发一次迁移；列级 CHECK 只兜长度，防止把整段描述塞进分类。
--   · 为什么 user_id 用 ON DELETE CASCADE：删用户必须带走资产台账（与 bills /
--     savings_goals 同口径），否则会留下孤儿行。
--   · 索引 idx_assets_user：资产页的主查询恒为 WHERE user_id = ?，先建好。
--
--   ⚠ 本迁移只**新建**一张表（+ 索引 + CHECK），不触碰任何已有表与已有数据。
--   ⚠ 回滚会**整表删除**（资产数据不可恢复）：回滚前如需保留，请先导出 assets。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/020_assets.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → … → 019（bills.mood）→ 020（本文件）
--   ✅ 020 只依赖 001 建的 users，与 019 无耦合，可单独执行。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只新建表，不改动已有对象，风险很低；仍建议先留结构备份：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > schema_before_020.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE assets_backup AS SELECT ...
--      之类的库内副本 —— 它会被 PostgREST 暴露、会随主表一起被删、也会让
--      pg_dump 的 schema 里多出垃圾对象。备份一律落在库外文件。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的 CREATE TABLE
--   IF NOT EXISTS），**刻意不把 assets 写进 init_db**：迁移统一走本文件。
--   部署顺序是：先执行本脚本 → 再上带资产台账的代码。
--   反过来（020 未执行）时 db.py 会**优雅降级**：assets_available() 返回 False，
--   资产大盘显示「暂不可用」，记账/分析等其它功能完全不受影响；
--   add_asset 会抛 AssetTableMissing（页面只提示，不红屏）。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   DROP INDEX IF EXISTS public.idx_assets_user;
--   DROP TABLE IF EXISTS public.assets;
--   （CHECK 随表一起删除；回滚后资产大盘自动走「表不存在」降级分支。）
--
-- =============================================================================
-- 【幂等】
--   · 表：CREATE TABLE IF NOT EXISTS + to_regclass 存在性判断（双保险）；
--   · 索引：CREATE INDEX IF NOT EXISTS；
--   · CHECK：pg_constraint 存在性判断后 ADD CONSTRAINT；
--   · 核对：每次执行都跑，失败 RAISE EXCEPTION 整体回滚。
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 1：建表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assets (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    name              VARCHAR(100) NOT NULL,
    category          VARCHAR(20) NOT NULL DEFAULT '其他',
    purchase_price    NUMERIC(12, 2) NOT NULL DEFAULT 0,
    purchase_date     DATE NOT NULL,
    depreciation_rate NUMERIC(5, 4) NOT NULL DEFAULT 0.2000,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT assets_user_fk
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE
);

COMMENT ON TABLE public.assets IS
    '个人资产台账：购入价 + 购入日期 + 年折旧率，按直线法计算当前残值（020 迁移新增）';
COMMENT ON COLUMN public.assets.depreciation_rate IS
    '年折旧率，0.2000 = 20%/年；0 表示不折旧，1 表示一年归零';

-- ---------------------------------------------------------------------------
-- 步骤 2：CHECK 约束（金额非负 / 折旧率 0~1 / 名称与分类长度）
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass AND conname = 'assets_price_nonneg'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT assets_price_nonneg CHECK (purchase_price >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass AND conname = 'assets_rate_range'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT assets_rate_range CHECK (depreciation_rate >= 0 AND depreciation_rate <= 1);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass AND conname = 'assets_name_len'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT assets_name_len CHECK (char_length(name) > 0 AND char_length(name) <= 100);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass AND conname = 'assets_category_len'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT assets_category_len CHECK (char_length(category) <= 20);
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：索引（资产页恒按 user_id 过滤）
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_assets_user ON public.assets (user_id);

-- ---------------------------------------------------------------------------
-- 步骤 4：核对（表存在 / 列类型与可空性 / 外键 / CHECK / 索引）
--     任何一项不符 → RAISE EXCEPTION → 整个事务回滚，不会留下半成品。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_table TEXT;
    v_price_type TEXT;
    v_rate_type TEXT;
    v_fk INTEGER;
    v_checks INTEGER;
    v_idx INTEGER;
    v_advice TEXT := '请检查本次执行是否被部分回滚，然后整段重新执行本文件。';
BEGIN
    SELECT to_regclass('public.assets')::text INTO v_table;
    IF v_table IS NULL THEN
        RAISE EXCEPTION '核对①未通过：public.assets 不存在。%', v_advice;
    END IF;

    SELECT data_type INTO v_price_type
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'assets' AND column_name = 'purchase_price';
    IF v_price_type IS DISTINCT FROM 'numeric' THEN
        RAISE EXCEPTION '核对②未通过：purchase_price 类型是 %（应为 numeric）。%', v_price_type, v_advice;
    END IF;

    SELECT data_type INTO v_rate_type
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'assets' AND column_name = 'depreciation_rate';
    IF v_rate_type IS DISTINCT FROM 'numeric' THEN
        RAISE EXCEPTION '核对③未通过：depreciation_rate 类型是 %（应为 numeric）。%', v_rate_type, v_advice;
    END IF;

    SELECT COUNT(*) INTO v_fk
      FROM pg_constraint
     WHERE conrelid = 'public.assets'::regclass
       AND contype = 'f'
       AND conname = 'assets_user_fk';
    IF v_fk = 0 THEN
        RAISE EXCEPTION '核对④未通过：外键 assets_user_fk 不存在。%', v_advice;
    END IF;

    SELECT COUNT(*) INTO v_checks
      FROM pg_constraint
     WHERE conrelid = 'public.assets'::regclass
       AND contype = 'c'
       AND conname IN ('assets_price_nonneg', 'assets_rate_range', 'assets_name_len', 'assets_category_len');
    IF v_checks < 4 THEN
        RAISE EXCEPTION '核对⑤未通过：CHECK 约束只有 % 条（应为 4 条）。%', v_checks, v_advice;
    END IF;

    SELECT COUNT(*) INTO v_idx
      FROM pg_indexes
     WHERE schemaname = 'public' AND tablename = 'assets' AND indexname = 'idx_assets_user';
    IF v_idx = 0 THEN
        RAISE EXCEPTION '核对⑥未通过：索引 idx_assets_user 不存在。%', v_advice;
    END IF;

    RAISE NOTICE '核对通过：assets 表、外键、4 条 CHECK 与 idx_assets_user 齐备';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 5：成功标记
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;