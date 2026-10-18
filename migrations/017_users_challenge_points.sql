-- =============================================================================
-- 017_users_challenge_points.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   「省钱挑战」的积分账本。给 public.users 增加两列：
--     challenge_points    INTEGER NOT NULL DEFAULT 0
--         -- 用户累计挑战积分（只增不减，单调递增；本迁移不提供扣减入口）。
--         -- NOT NULL + DEFAULT 0：历史用户立刻是 0 分，不需要回填脚本。
--     last_challenge_week VARCHAR(8) NULL
--         -- 最近一次**成功发放积分**的 ISO 周，形如 '2026-W42'（年份-大写 W-两位周号）。
--         -- NULL = 从没领过。
--         -- 这一列是**防刷分的唯一凭据**：db.py 的 award_challenge_points 用
--         --   WHERE id = :uid AND (last_challenge_week IS NULL OR last_challenge_week <> :w)
--         -- 做条件更新，同一周第二次 UPDATE 影响 0 行 → 不发分。
--         -- 于是「刷新页面 / 并发双击 / 重放请求」都不会重复加分；
--         -- 而"周"由应用按 ISO 周计算，跨周自然放行。
--     CHECK users_challenge_points_nonneg：challenge_points >= 0。
--         这是**第二道闸**：应用层已保证只加正数（1~100），CHECK 防的是将来
--         任何绕过 db.py 的写入（手工 SQL / 新脚本 / 误把加减搞反）把余额写成负数。
--
--   ⚠ 积分是**只增的累计值**，没有"流水表"。若将来需要审计"哪一周加了多少"
--     （撤销作弊积分、对账），必须新建 challenge_point_events(user_id, week, points,
--     created_at) 表并加唯一索引 (user_id, week) —— 而不是在 users 上再加列。
--     唯一索引 (user_id, week) 同时是防重复发放的更强保证；当前单列方案靠
--     条件 UPDATE + 行锁做到"同周一次"，够用且不需要新表。
--
-- -----------------------------------------------------------------------------
-- 【为什么用「条件 UPDATE」而不是「先查后写」】（防刷分口径，改 db.py 前必读）
-- -----------------------------------------------------------------------------
--   db.py 的发分只有**一条** UPDATE 语句：
--       UPDATE users
--          SET challenge_points = challenge_points + :p,
--              last_challenge_week = :w
--        WHERE id = :uid
--          AND (last_challenge_week IS NULL OR last_challenge_week <> :w)
--     RETURNING challenge_points;
--   · 判定与写入在同一条语句、同一个行锁里完成，不存在
--     "SELECT 时还没领 → 两条请求都以为能领 → 都加分"的 TOCTOU 竞态；
--   · 同一用户并发两次请求时，后到的那条会等行锁，锁释放后 PostgreSQL 会
--     用**最新的行版本**重新检查 WHERE（EvalPlanQual），发现 last_challenge_week
--     已经是本周 → 不更新、RETURNING 返回 0 行 → 调用方得到 awarded=False；
--   · 「先 SELECT 判断、再 UPDATE」的写法在任何隔离级别下都有窗口，**禁止**改回去。
--   ⚠ 本表不建索引：发分/读分都是 `WHERE id = :uid`，走 users 主键，加索引只会
--     增加写入负担。将来若按 last_challenge_week 统计，再单独评估。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/017_users_challenge_points.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚
--     保证，中途失败会留下「有列无约束」的半完成状态。
--   ⚠ 迁移建议用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011
--          → 012 → 013 → 014 → 015 → 016 → 017（本文件）
--   ✅ 017 只依赖 public.users（001 已建），不读不写 bills / login_events /
--      savings_goals / user_notifications / chat_history，也不依赖 015 / 016 的列。
--      迁移仍按编号排在 016 之后执行（顺序固定，便于排障与回滚到某个编号）。
--   ⚠ 014 这个编号已被 014_user_notifications.sql 占用，所以本迁移是 **017**。
--
-- =============================================================================
-- 【备份建议】（执行前做）
-- =============================================================================
--   本脚本只**新增**两列（一列 NOT NULL DEFAULT 0 —— PostgreSQL 11+ 的 fast default
--   只改 catalog、不重写表；一列可空无默认值）和一条 CHECK 约束（对现有行做一次
--   校验扫描，代价可忽略），不改动任何已有列与数据，风险很低；仍建议留结构备份：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > schema_before_017.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE users_challenge_backup AS ... 之类的库内
--     副本 —— 它会被 PostgREST 暴露，也会让 pg_dump 的 schema 里多出垃圾对象。
--   ⚠ 积分数据没有历史、只有当前值：如果 users 上已经有 challenge_points 列（旧版本
--     手工加的），执行前先 `SELECT count(*) FROM public.users WHERE challenge_points <> 0;`
--     确认没有非零数据，或先导出该列；否则回滚 = 永久丢分。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的
--   CREATE TABLE IF NOT EXISTS），**刻意不给 users 补 challenge_points /
--   last_challenge_week**：迁移统一走本文件，避免「应用启动顺手改 schema」与
--   「迁移脚本」两条路径互相打架。
--   没执行本文件时：
--     · db.py 的 get_challenge_state / award_challenge_points 遇到 42703
--       （列不存在）或 42P01（表不存在）会抛 RuntimeError（中文提示先执行 017），
--       不会静默返回 0 分、更不会静默"发分成功"；
--     · 页面据此提示"省钱挑战功能未启用"，普通记账 / 日报 / AI 主流程完全不受影响。
--     · 不存在"应用启动时偷偷补列"的路径，线上 schema 只由迁移改变。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   按「先约束、后列」的顺序（DROP COLUMN 会自动带走列上的约束，但显式写出来让
--   回滚脚本本身可读、也可单独撤掉其中一项）：
--       ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_challenge_points_nonneg;
--       ALTER TABLE public.users DROP COLUMN IF EXISTS challenge_points;
--       ALTER TABLE public.users DROP COLUMN IF EXISTS last_challenge_week;
--   回滚后 db.py 的两个函数会重新走 42703 分支抛 RuntimeError（中文提示先执行 017）；
--   记账 / AI / 报表主流程不受影响。
--   ⚠ 回滚会**永久删除所有用户的积分与本周发放记录**：删除后同一周可以重新领一次
--     （last_challenge_week 没了，防重闸随之失效）。需要保留数据就先导出：
--       \copy (SELECT id, challenge_points, last_challenge_week FROM public.users)
--         TO 'challenge_points_rollback.csv' CSV HEADER
--
-- =============================================================================
-- 【幂等（重复执行安全）】
-- =============================================================================
--   · 加列：先查 information_schema.columns 打一条 NOTICE（从输出就能看出
--           "这次是真加了还是跳过了"），再 ALTER … ADD COLUMN IF NOT EXISTS
--           （列已存在时是 no-op，不会报错、不会改类型、不会重置已有积分 ——
--           这很关键：重复执行**绝不能**把用户积分归零）；
--   · 约束：pg_constraint 里已有同名约束 → RAISE NOTICE 跳过；没有才 ADD CONSTRAINT
--           （PostgreSQL 的 ADD CONSTRAINT 没有 IF NOT EXISTS，必须用 DO 块判断）；
--   · 核对：每次都跑，任何一条不符就 RAISE EXCEPTION → 整个事务回滚；
--   · 重复执行不会改旧数据、不重写表（ADD COLUMN 带 DEFAULT 0 在 PG 11+ 是
--     catalog-only 操作，历史行按需读取默认值）。
--   ⚠ ALTER TABLE 需要短暂的 ACCESS EXCLUSIVE 锁；开头的 SET LOCAL lock_timeout = '5s'
--     保证拿不到锁时快速失败回滚，而不是把线上写入无限期卡住。
--   ⚠ 如果库里已有一列**同名但类型不对**的 challenge_points / last_challenge_week
--     （例如 BIGINT / TEXT / VARCHAR(10)），ADD COLUMN IF NOT EXISTS 不会改它，
--     步骤 2 会明确报错并回滚。正确做法是：
--       先确认/导出该列数据 → DROP COLUMN → 重跑本脚本
--     （不要手改核对去迁就错误类型，那会把"类型漂移"永久固化下来）。
--   ⚠ 如果已有同名但定义不同的 CHECK 约束（例如 `> 0` 或没校验的 NOT VALID），
--     步骤 2 同样会报错回滚；先 DROP 掉错误对象再重跑，不要绕过核对。
--
-- =============================================================================

BEGIN;

-- 5 秒拿不到表锁就报错回滚，不无期限卡住线上写入。
SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（users 存在、users.id 是 integer）
--   两列挂在 users 上；表不存在时 ALTER 会失败并留下半截事务，不如在这里给一句
--   能直接照做的报错。
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
-- 步骤 1：加两列（幂等）
--   先查 information_schema 打 NOTICE（区分"真加了"还是"已存在跳过"），
--   再执行 ADD COLUMN IF NOT EXISTS。
--   · challenge_points：INTEGER NOT NULL DEFAULT 0 —— 历史行立刻是 0 分。
--     刻意用 INTEGER 而不是 BIGINT：积分上限 100/周，INTEGER 够用几十年；
--     列宽窄、索引/比较都更省。类型必须与 db.py 的 int 语义一致。
--   · last_challenge_week：VARCHAR(8) 可空、无默认值 —— NULL 表示"从没领过"，
--     不能给默认值（否则历史用户会被当成"某一周已领"，白白少领一次）。
--     长度 8 恰好容纳 'YYYY-Www'（4+1+1+2 = 8），多一位都是脏值。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_has_points boolean;
    v_has_week   boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'challenge_points'
    ) INTO v_has_points;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'last_challenge_week'
    ) INTO v_has_week;

    IF v_has_points AND v_has_week THEN
        RAISE NOTICE '步骤1：users.challenge_points / last_challenge_week 均已存在，跳过 ADD COLUMN（幂等，已有积分不会被重置）';
    ELSE
        RAISE NOTICE '步骤1：challenge_points 存在=% / last_challenge_week 存在=%，补齐缺失列（INTEGER NOT NULL DEFAULT 0 / VARCHAR(8) NULL）', v_has_points, v_has_week;
    END IF;
END $do$;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS challenge_points INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS last_challenge_week VARCHAR(8);

-- ---------------------------------------------------------------------------
-- 步骤 1（续）：加 CHECK 约束（幂等）
--   约束名固定为 users_challenge_points_nonneg，方便核对与回滚。
--   语义：challenge_points >= 0。NOT NULL 已挡住 NULL，CHECK 挡住负数 ——
--   两层合起来，任何写入路径都不可能造出"负积分"。
--   ADD CONSTRAINT 没有 IF NOT EXISTS，用 pg_constraint 判断同名约束是否已在
--   public.users 上。若同名约束存在但定义不对（例如 > 0 / NOT VALID），
--   步骤 2 会报错并整体回滚。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname  = 'users_challenge_points_nonneg'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_challenge_points_nonneg
            CHECK (challenge_points >= 0);
        RAISE NOTICE '步骤1（续）：已新增 CHECK 约束 users_challenge_points_nonneg (challenge_points >= 0)';
    ELSE
        RAISE NOTICE '步骤1（续）：约束 users_challenge_points_nonneg 已存在，跳过（幂等）';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 2：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：
--     · challenge_points 存在、integer、NOT NULL（is_nullable='NO'）、
--       default 恰为 0（DB 里默认值不存在 = 历史行 0 分，但列定义与 db.py 不符）；
--     · last_challenge_week 存在、character varying(8)、可空（YES）、无默认值；
--     · 约束存在、是 CHECK（contype='c'）、已校验（convalidated=true，不能是
--       NOT VALID —— NOT VALID 的 CHECK 只约束新行，等于给历史行/旧路径留后门）；
--     · 约束定义与预期**逐字比对**（去空白 + 小写后的 PostgreSQL 规范化文本）。
--       不用 LIKE '%>=%' 这种松判据：把 `>= 0` 写成 `> 0`、`<= 0` 也能命中松判据，
--       等于没核对。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type      text;
    v_len       integer;
    v_nullable  text;
    v_default   text;
    v_contype   text;
    v_validated boolean;
    v_def       text;
    v_def_norm  text;
    v_advice    text := '若列/约束已存在但结构与预期不符：本脚本刻意不自动改旧结构（避免误改生产数据）。正确做法通常是先导出 challenge_points 的数据（积分不可重建）、DROP CONSTRAINT IF EXISTS users_challenge_points_nonneg; DROP COLUMN IF EXISTS challenge_points; DROP COLUMN IF EXISTS last_challenge_week; 然后重跑本脚本。';
BEGIN
    -- ①-a challenge_points：integer / NOT NULL / default 0
    SELECT data_type, character_maximum_length, is_nullable, column_default
      INTO v_type, v_len, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'users'
      AND column_name  = 'challenge_points';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①a未通过：public.users.challenge_points 不存在（步骤1 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'integer' THEN
        RAISE EXCEPTION '核对①b未通过：challenge_points 类型是 %（应为 integer —— 与 db.py 的 int 语义一致，积分 1~100/周）。%', v_type, v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对①c未通过：challenge_points is_nullable = %（应为 NO —— 积分必须有值，NULL 会被 db.py 的 int() 炸掉）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS DISTINCT FROM '0' THEN
        RAISE EXCEPTION '核对①d未通过：challenge_points 默认值是 %（应为 0 —— 历史用户从 0 分开始，且数据库层给出"未设置即 0"的语义）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ①-b last_challenge_week：character varying(8) / 可空 / 无默认值
    SELECT data_type, character_maximum_length, is_nullable, column_default
      INTO v_type, v_len, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'users'
      AND column_name  = 'last_challenge_week';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①e未通过：public.users.last_challenge_week 不存在（步骤1 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'character varying' THEN
        RAISE EXCEPTION '核对①f未通过：last_challenge_week 类型是 %（应为 character varying —— 存 ''YYYY-Www'' 文本，不做日期类型转换）。%', v_type, v_advice;
    END IF;
    IF v_len IS DISTINCT FROM 8 THEN
        RAISE EXCEPTION '核对①g未通过：last_challenge_week 长度上限是 %（应为 8 —— ''YYYY-Www'' 恰好 8 字符）。%', coalesce(v_len::text, '<NULL>'), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对①h未通过：last_challenge_week is_nullable = %（应为 YES —— NULL 表示"从没领过"，不允许 NOT NULL）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS NOT NULL THEN
        RAISE EXCEPTION '核对①i未通过：last_challenge_week 有默认值 %（应为无默认值：NULL = 从没领过）。%', v_default, v_advice;
    END IF;

    -- ② 约束：存在、是 CHECK、已校验、定义逐字正确
    SELECT contype::text, convalidated, pg_get_constraintdef(oid)
      INTO v_contype, v_validated, v_def
    FROM pg_constraint
    WHERE conrelid = 'public.users'::regclass
      AND conname  = 'users_challenge_points_nonneg';

    IF v_contype IS NULL THEN
        RAISE EXCEPTION '核对②a未通过：缺少约束 users_challenge_points_nonneg。%', v_advice;
    END IF;
    IF v_contype IS DISTINCT FROM 'c' THEN
        RAISE EXCEPTION '核对②b未通过：users_challenge_points_nonneg 的约束类型是 %（应为 c / CHECK）。%', v_contype, v_advice;
    END IF;
    IF v_validated IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对②c未通过：users_challenge_points_nonneg 未校验（convalidated=false / NOT VALID），等于给历史行与旧写入路径留后门。%', v_advice;
    END IF;

    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    -- PostgreSQL 17 会把上面的 CHECK 规范化成（去空白 + 小写后）：
    --   check((challenge_points>=0))
    -- 若换了 PG 大版本导致 deparse 形式变化（例如多一层括号），
    -- 请先人工核对 pg_get_constraintdef 再更新本字符串，**不要**放宽成 LIKE。
    IF v_def_norm <> 'check((challenge_points>=0))' THEN
        RAISE EXCEPTION '核对②d未通过：users_challenge_points_nonneg 定义是 %（应为 CHECK (challenge_points >= 0)）。%', v_def, v_advice;
    END IF;

    RAISE NOTICE '核对通过：challenge_points integer NOT NULL DEFAULT 0；last_challenge_week varchar(8) 可空、无默认值；CHECK users_challenge_points_nonneg 已校验且定义为 challenge_points >= 0';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表加列、约束与核对全部通过并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;
