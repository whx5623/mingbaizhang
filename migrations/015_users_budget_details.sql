-- =============================================================================
-- 015_users_budget_details.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.users 增加一列 budget_details JSONB，保存「智能预算」的分配结果：
--     { "餐饮": 600.00, "购物": 300.00, "娱乐": 200.00, "学习": 100.00 }
--   即「这个月每个分类打算花多少钱」。db.py 的
--     get_budget_details / save_budget_details / generate_smart_budget
--   都依赖这一列（生成逻辑见 db.py 的 generate_smart_budget：读上月 bills 分类
--   支出 → 让 AI 按总预算给出方案 → 用户确认后 save_budget_details 落库）。
--
--   为什么是 JSONB 而不是「预算表」：
--     · 一个用户的预算就是**一个小对象**（分类数 ≤ 20、金额 ≤ 两位小数），
--       按 user_id 整读整写，没有按分类查询/聚合的需求；
--     · 用 JSONB 省掉一张表 + 外键 + 级联，也让「AI 给了一组分类」这种
--       结构不固定的输入不必先做 schema 映射；
--     · 它天然随 users 行一起被删除（不需要额外清理），也不会被 PostgREST
--       当作独立表暴露。
--
--   字段：
--     budget_details  JSONB NULL
--                     - NULL  = **还没设置过**（页面显示"未设置"，不是空预算）；
--                     - {}    = 已设置但没有任何分类（db.py 的 save 不允许空 dict，
--                               正常情况下不会出现，读取侧也按"未设置"处理）；
--                     - 其他  = JSON object（分类名 → 金额）；数组/标量/JSON null
--                               一律被下面的 CHECK 拒绝。
--
--   ⚠ 列必须可空、且**没有默认值**：NULL 是"未设置"的语义。若给 DEFAULT '{}'::jsonb，
--     所有历史用户会立刻"看起来设置过预算"，页面会把 0 预算当成真实配置展示。
--
--   ⚠ 本迁移**不建索引**：JSONB 对象很小，读写都是 `WHERE id = :uid`（users 主键），
--     在 budget_details 上加 B-tree/GIN 没有任何查询会用到，只会拖慢写入、多占空间。
--
--   约束（命名约束，便于回滚与核对）：
--     users_budget_details_object
--       CHECK (budget_details IS NULL OR jsonb_typeof(budget_details) = 'object')
--     说明：PostgreSQL 的 CHECK 在表达式结果为 NULL 时算通过，而 jsonb_typeof(NULL)
--     返回 SQL NULL，所以「IS NULL」这一支必须**显式写上**，否则 NULL 也会被放行
--     （结果恰好相同，但意图不清晰）；反过来，JSON 里的 null 是 jsonb 'null'，
--     jsonb_typeof 返回 'null'（不是 SQL NULL），会被拒绝。
--     数组 → 'array'、字符串 → 'string'、数字 → 'number'、布尔 → 'boolean'，全部拒绝。
--
--   ✅ 与 003 的金额上界一致：单个金额由应用层限制在 NUMERIC(12,2) 的上界
--      9999999999.99 内（db.py 的 BUDGET_DETAILS_MAX_AMOUNT），JSONB 本身不做数值约束
--      （JSON number 不限制精度/范围），所以这条规则只在 Python 侧生效。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/015_users_budget_details.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证，
--     中途失败会留下「有列无约束」或「约束没核对」的半完成状态。
--   ⚠ 迁移建议用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011
--          → 012 → 013 → 014 → 015（本文件）
--   ✅ 015 只依赖 public.users（001 已建），不读写 bills；
--      db.py 的 get_month_category_totals 另依赖 bills.category（012）与
--      bills.date 的 ISO 约束（007），与本文件的 ALTER 无关。
--      迁移仍按编号排在 014 之后执行（顺序固定）。
--
-- =============================================================================
-- 【备份建议】（执行前做）
-- =============================================================================
--   本脚本只**新增**一列（可空、无默认值，PG 11+ 只改 catalog、不重写表）和一条
--   CHECK 约束（需要对现有 13 行做一次校验扫描，users 只有几十行，代价可忽略），
--   不改动任何已有列与数据，风险很低；仍建议留一份结构备份：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > schema_before_015.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE users_budget_backup AS ... 之类的库内副本——
--     它会被 PostgREST 暴露，也会让 pg_dump 的 schema 里多出垃圾对象。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的
--   CREATE TABLE IF NOT EXISTS），**刻意不给 users 补 budget_details**：迁移统一走本文件，
--   避免「应用启动顺手改 schema」与「迁移脚本」两条路径互相打架。
--   没执行本文件时：
--     · db.py 的 get_budget_details / save_budget_details 遇到 42703（列不存在）
--       会抛 RuntimeError（中文提示先执行 015），不会静默返回 {} 或假装保存成功；
--     · generate_smart_budget 在执行到落库前就会失败（它本身不写这一列，但页面
--       的"保存预算"会走 save_budget_details）；
--     · get_month_category_totals 只读 bills，不依赖这一列，仍可正常使用；
--     · app.py 捕获 RuntimeError 后显示提示，不会红屏。
--   这些都只是"智能预算"这一个功能不可用，不影响记账/登录/日报主流程。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   按「先约束、后列」的顺序（DROP COLUMN 会自动带走列上的约束，但显式写出来
--   让回滚脚本本身可读、也可单独撤掉约束）：
--       ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_budget_details_object;
--       ALTER TABLE public.users DROP COLUMN     IF EXISTS budget_details;
--   回滚后 db.py 的 get_budget_details / save_budget_details 会重新走 42703 分支
--   抛 RuntimeError（中文提示先执行 015）；get_month_category_totals 不受影响。
--   ⚠ 回滚会**删除所有已保存的预算**，不可恢复；需要保留时先导出：
--       SELECT id, username, budget_details FROM public.users WHERE budget_details IS NOT NULL;
--
-- =============================================================================
-- 【幂等（重复执行安全）】
-- =============================================================================
--   · 加列：先查 information_schema.columns 打印实际动作，再 ALTER ... ADD COLUMN
--           IF NOT EXISTS（列已存在时是 no-op，不会报错、不会改类型）；
--   · 约束：pg_constraint 里已有同名约束 → RAISE NOTICE 跳过；没有才 ADD CONSTRAINT
--           （PostgreSQL 的 ADD CONSTRAINT 没有 IF NOT EXISTS，必须用 DO 块判断）；
--   · 核对：每次都跑，任何一条不符就 RAISE EXCEPTION → 整个事务回滚；
--   · 重复执行不会改旧数据、不重写表（ADD COLUMN 可空无默认值是 catalog-only 操作）。
--   ⚠ ALTER TABLE 需要短暂的 ACCESS EXCLUSIVE 锁；开头的 SET LOCAL lock_timeout = '5s'
--     保证拿不到锁时快速失败回滚，而不是把线上写入无限期卡住。
--   ⚠ 如果库里已有一列**同名但类型不对**的 budget_details（例如 TEXT），
--     ADD COLUMN IF NOT EXISTS 不会改它，步骤 2 会明确报错并回滚。正确做法是：
--     先确认/导出该列数据 → DROP COLUMN → 重跑本脚本（不要手改 CHECK 去迁就错误类型）。
-- =============================================================================

BEGIN;

-- 5 秒拿不到表锁就报错回滚，不无期限卡住线上写入。
SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（users 存在、users.id 是 integer）
--   budget_details 挂在 users 上；表不存在时 ALTER 会失败并留下半截事务，
--   不如在这里给一句能直接照做的报错。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type text;
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.users 不存在。请先执行 001/002 系列迁移（或启动一次应用让 db.init_db() 建表），并确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;

    SELECT data_type INTO v_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id';
    IF v_type IS DISTINCT FROM 'integer' THEN
        RAISE EXCEPTION '步骤0 中止：public.users.id 类型是 %（应为 integer，001 的 SERIAL 主键）。请先核对 001 迁移。本事务未做任何改动', coalesce(v_type, '<列不存在>');
    END IF;

    RAISE NOTICE '步骤0：public.users 存在、id 为 integer，继续';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：加列（幂等）
--   先查 information_schema 打一条 NOTICE（让人从输出里就能看出"这次是真加了
--   还是跳过了"），再执行 ADD COLUMN IF NOT EXISTS。
--   列定义必须是 JSONB、可空、**无默认值**：NULL = 未设置（见文件头）。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_has_column boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'budget_details'
    ) INTO v_has_column;

    IF v_has_column THEN
        RAISE NOTICE '步骤1：users.budget_details 已存在，跳过 ADD COLUMN（幂等）';
    ELSE
        RAISE NOTICE '步骤1：users.budget_details 不存在，新增 JSONB 列（可空、无默认值）';
    END IF;
END $do$;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS budget_details JSONB;

-- ---------------------------------------------------------------------------
-- 步骤 1（续）：加 CHECK 约束（幂等）
--   约束名固定为 users_budget_details_object，方便核对与回滚。
--   ADD CONSTRAINT 没有 IF NOT EXISTS，用 pg_constraint 判断「同名约束是否已在
--   public.users 上」。若同名约束存在但定义不对，步骤 2 会报错并整体回滚。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname  = 'users_budget_details_object'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_budget_details_object
            CHECK (budget_details IS NULL OR jsonb_typeof(budget_details) = 'object');
        RAISE NOTICE '步骤1（续）：已新增 CHECK 约束 users_budget_details_object';
    ELSE
        RAISE NOTICE '步骤1（续）：约束 users_budget_details_object 已存在，跳过（幂等）';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 2：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：
--     · 列存在、类型是 jsonb、可空（YES）、没有默认值（NULL = 未设置）；
--     · 约束存在、是 CHECK（contype='c'）、已校验（convalidated=true，不能是
--       NOT VALID —— NOT VALID 的 CHECK 对历史行不生效，等于没约束）；
--     · 约束定义与预期**逐字比对**（去空白 + 小写后的 PostgreSQL 规范化文本），
--       不用 LIKE '%jsonb_typeof%' 这种松判据 —— 松判据会把
--       `CHECK (jsonb_typeof(budget_details) <> 'object')`（写反了）也判成通过。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type       text;
    v_nullable   text;
    v_default    text;
    v_contype    text;
    v_validated  boolean;
    v_def        text;
    v_def_norm   text;
    v_advice     text := '若列/约束已存在但结构与预期不符：本脚本刻意不自动改旧结构（避免误改生产数据）。正确做法通常是先导出 budget_details 的数据，再 ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_budget_details_object; ALTER TABLE public.users DROP COLUMN IF EXISTS budget_details; 然后重跑本脚本。';
BEGIN
    -- ① 列类型 / 可空性 / 默认值
    SELECT data_type, is_nullable, column_default
      INTO v_type, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'users'
      AND column_name  = 'budget_details';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①a未通过：public.users.budget_details 不存在（步骤1 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'jsonb' THEN
        RAISE EXCEPTION '核对①b未通过：budget_details 类型是 %（应为 jsonb）。%', v_type, v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对①c未通过：budget_details is_nullable = %（应为 YES —— NULL 表示"未设置"，不允许 NOT NULL）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    -- 默认值必须是 NULL：有默认值会让所有历史用户立刻"看起来设置过预算"。
    IF v_default IS NOT NULL THEN
        RAISE EXCEPTION '核对①d未通过：budget_details 有默认值 %（应为无默认值：NULL = 未设置）。%', v_default, v_advice;
    END IF;

    -- ② 约束：存在、是 CHECK、已校验、定义逐字正确
    SELECT contype::text, convalidated, pg_get_constraintdef(oid)
      INTO v_contype, v_validated, v_def
    FROM pg_constraint
    WHERE conrelid = 'public.users'::regclass
      AND conname  = 'users_budget_details_object';

    IF v_contype IS NULL THEN
        RAISE EXCEPTION '核对②a未通过：缺少约束 users_budget_details_object（CHECK budget_details IS NULL OR jsonb_typeof(budget_details) = ''object''）。%', v_advice;
    END IF;
    IF v_contype IS DISTINCT FROM 'c' THEN
        RAISE EXCEPTION '核对②b未通过：users_budget_details_object 的约束类型是 %（应为 c / CHECK）。%', v_contype, v_advice;
    END IF;
    IF v_validated IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对②c未通过：users_budget_details_object 未校验（convalidated=false / NOT VALID），对历史行不生效，等于没有约束。%', v_advice;
    END IF;

    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    -- PostgreSQL 会把 `budget_details IS NULL OR jsonb_typeof(...) = 'object'`
    -- 规范化成 `CHECK (((budget_details IS NULL) OR (jsonb_typeof(budget_details) = 'object'::text)))`。
    IF v_def_norm <> 'check(((budget_detailsisnull)or(jsonb_typeof(budget_details)=''object''::text)))' THEN
        RAISE EXCEPTION '核对②d未通过：users_budget_details_object 定义是 %（应为 CHECK (budget_details IS NULL OR jsonb_typeof(budget_details) = ''object'')）。%', v_def, v_advice;
    END IF;

    RAISE NOTICE '核对通过：users.budget_details 类型 jsonb / 可空 / 无默认值 / CHECK users_budget_details_object 定义与校验状态全部符合预期';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表加列、约束与核对全部通过并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;
