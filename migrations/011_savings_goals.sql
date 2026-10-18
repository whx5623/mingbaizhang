-- =============================================================================
-- 011_savings_goals.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   新建 public.savings_goals —— 「心愿存钱罐」：每个用户**一条**存钱目标
--   （一人一个心愿，靠 UNIQUE (user_id) 保证），记录目标金额、已存金额、目标日期。
--     id             BIGSERIAL    主键
--     user_id        INTEGER NOT NULL  外键 → users(id) ON DELETE CASCADE
--                    （删号即删心愿：这是用户的私有数据，不留残余；
--                      与 login_events 的 SET NULL 不同 —— 那里留的是审计，这里留的是隐私）
--     goal_name      TEXT NOT NULL     目标名（如「换一台 MacBook」）
--     target_amount  NUMERIC(12,2) NOT NULL  目标金额，必须 > 0
--     current_amount NUMERIC(12,2) NOT NULL DEFAULT 0   已存金额，必须 >= 0
--     target_date    DATE NULL          目标日期（可空 = 没定 deadline）
--     created_at     TIMESTAMPTZ NOT NULL DEFAULT now()  创建时刻（绝对时刻，跨时区稳定）
--
--   约束（4 条命名约束 + 主键 + 外键）：
--     savings_goals_target_positive  CHECK (target_amount > 0)
--     savings_goals_current_nonneg   CHECK (current_amount >= 0)
--     savings_goals_name_len         CHECK (char_length(btrim(goal_name)) BETWEEN 1 AND 50)
--     savings_goals_user_unique      UNIQUE (user_id)   -- 一人一个心愿目标
--     savings_goals_user_id_fkey     FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
--
--   ⚠ 为什么 UNIQUE (user_id) 是业务核心：db.py 的 set_goal 用
--     INSERT ... ON CONFLICT (user_id) DO UPDATE 实现「有则更新、无则新建」，
--     这个 upsert 依赖的就是这条唯一约束；没有它 ON CONFLICT 直接报 42P10
--     （there is no unique or exclusion constraint matching the ON CONFLICT spec）。
--   ⚠ 金额上界 9999999999.99 = NUMERIC(12,2) 的容量上界，db.py 在 Python 侧
--     提前拦（MAX_ABS_AMOUNT 同口径），避免数据库抛 numeric field overflow。
--
--   ⚠ 隐私与暴露面：表在 public schema，Supabase 会经 PostgREST 暴露表名。
--     本脚本对 savings_goals 执行 ENABLE ROW LEVEL SECURITY 且**不建任何 policy**，
--     于是 anon / authenticated 角色读不到/写不进任何行（服务端用连接串里的高权限
--     角色直连，不受 RLS 影响 —— 表属于该角色时默认绕过 RLS）。
--     不要为了「方便调试」或「让前端能读」去加放行 policy：心愿目标里的金额就是
--     用户的财务隐私，一旦放行 anon 读，等于把全站用户的存钱目标公开。
--
--   ⚠ 本脚本只建表，**不会**给现有用户预置任何心愿目标（用户第一次调用 set_goal
--     时才产生第一行）。savings_goals 从执行时刻开始为空表，这是如实反映。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/011_savings_goals.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011（本文件）
--   ✅ 编号连续：011 是 010 之后的下一号，只依赖 001 建的 users（以及更早的列/类型规范化）。
--   ✅ 没执行本脚本时不会红屏：db.py 的 set_goal / get_goal / add_to_goal 遇到
--      「表不存在（42P01）」会向上抛 RuntimeError（中文提示先执行本迁移），
--      app.py 捕获后显示提示，**不会**静默返回假数据。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只**新增**一张表，不改动已有表与数据，风险很低；仍建议先留一份结构备份：
--       pg_dump --schema-only -t public.users -t public.savings_goals "$DATABASE_URL" > schema_before_011.sql
--   （savings_goals 还不存在时 -t 找不到对象会失败，只备 users 也可：-t public.users）
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE savings_goals_backup AS SELECT ...
--      之类的库内副本 —— 它会被 PostgREST 暴露、会随主表一起被删、也会让
--      pg_dump 的 schema 里多出垃圾对象。备份一律落在库外文件。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的 CREATE TABLE
--   IF NOT EXISTS），**刻意不建 savings_goals**：迁移统一走本文件，避免「应用启动
--   顺手改 schema」与「迁移脚本」两条路径互相打架、也避免生产库被隐式改结构。
--   所以部署顺序是：先执行本脚本 → 再上带 set_goal 的代码。反过来时页面会提示
--   「心愿存钱罐功能未启用：请先执行 migrations/011_savings_goals.sql」。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   DROP TABLE IF EXISTS public.savings_goals;
--   （表里有用户心愿数据，回滚 = 这些数据一起删掉，不可恢复；回滚前先导出。）
--   （回滚后 db.py 的三个函数会自动走 42P01 分支抛 RuntimeError，页面提示未启用。）
--
-- =============================================================================
-- 【幂等】
--   · 表：CREATE TABLE IF NOT EXISTS；
--   · RLS：ENABLE 本身幂等；
--   · 核对：每次执行都跑，失败 RAISE EXCEPTION 整体回滚。
--   · 重复执行不会改旧表结构、不动数据、不重置序列。
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（users 是否存在）
--   savings_goals.user_id 的外键指向 users(id)；users 不存在时建表会失败并留下
--   半截事务，不如在这里给一句能直接照做的报错。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.users 不存在。请先执行 001/002 系列迁移（或启动一次应用让 db.init_db() 建表），并确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：public.users 存在，继续';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：建表（幂等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.savings_goals (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    goal_name      TEXT NOT NULL,
    target_amount  NUMERIC(12,2) NOT NULL,
    current_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    target_date    DATE NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT savings_goals_target_positive CHECK (target_amount > 0),
    CONSTRAINT savings_goals_current_nonneg  CHECK (current_amount >= 0),
    CONSTRAINT savings_goals_name_len        CHECK (char_length(btrim(goal_name)) BETWEEN 1 AND 50),
    CONSTRAINT savings_goals_user_unique     UNIQUE (user_id)   -- 一人一个心愿目标
);

DO $do$
BEGIN
    IF to_regclass('public.savings_goals') IS NULL THEN
        RAISE EXCEPTION '步骤1 未通过：public.savings_goals 创建后仍不存在，事务已回滚';
    END IF;
    RAISE NOTICE '步骤1：savings_goals 表已就绪（或已存在）';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1（续）：RLS（幂等，建表收尾）
--   public schema 会被 PostgREST 暴露；开 RLS + 不建 policy = anon/authenticated
--   读不到也写不进。服务端直连（表 owner / 高权限角色）不受影响。
-- ---------------------------------------------------------------------------
ALTER TABLE public.savings_goals ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 步骤 2：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：列类型 / NOT NULL / 默认值 / 4 条命名约束的定义 / UNIQUE 的键列 /
--          FK 删除动作 CASCADE('c') / RLS=true / policy 数=0。
--   约束定义用「去空白 + 小写」后与 PostgreSQL 规范化文本**逐字比对**，而不是
--   用 LIKE '%target_amount%' 这种松判据：松判据会把
--   `CHECK (target_amount < 0)`（写反了）也判成通过。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type          text;
    v_nullable      text;
    v_default       text;
    v_precision     integer;
    v_scale         integer;
    v_def           text;
    v_def_norm      text;
    v_fk_action     "char";
    v_fk_def        text;
    v_unique_key    smallint[];
    v_user_attnum   smallint;
    v_rls           boolean;
    v_policy_count  integer;
    v_advice        text := '若表已存在但结构与预期不符：本脚本刻意不自动改旧表（避免误改生产结构）。savings_goals 是本次新建的表，正确做法通常是导出数据后 DROP TABLE public.savings_goals; 再重跑本脚本。';
BEGIN
    -- ① target_amount / current_amount：类型必须是 numeric(12,2)、NOT NULL
    SELECT data_type, is_nullable, numeric_precision, numeric_scale
      INTO v_type, v_nullable, v_precision, v_scale
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'target_amount';
    IF v_type IS DISTINCT FROM 'numeric' THEN
        RAISE EXCEPTION '核对①a未通过：target_amount 类型是 %（应为 numeric(12,2)）。%', coalesce(v_type, '<列不存在>'), v_advice;
    END IF;
    IF v_precision IS DISTINCT FROM 12 OR v_scale IS DISTINCT FROM 2 THEN
        RAISE EXCEPTION '核对①b未通过：target_amount 是 numeric(%,%)（应为 numeric(12,2)）。%', coalesce(v_precision, -1), coalesce(v_scale, -1), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对①c未通过：target_amount is_nullable = %（应为 NO）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;

    SELECT data_type, is_nullable, numeric_precision, numeric_scale
      INTO v_type, v_nullable, v_precision, v_scale
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'current_amount';
    IF v_type IS DISTINCT FROM 'numeric' THEN
        RAISE EXCEPTION '核对①d未通过：current_amount 类型是 %（应为 numeric(12,2)）。%', coalesce(v_type, '<列不存在>'), v_advice;
    END IF;
    IF v_precision IS DISTINCT FROM 12 OR v_scale IS DISTINCT FROM 2 THEN
        RAISE EXCEPTION '核对①e未通过：current_amount 是 numeric(%,%)（应为 numeric(12,2)）。%', coalesce(v_precision, -1), coalesce(v_scale, -1), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对①f未通过：current_amount is_nullable = %（应为 NO）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    -- DEFAULT 0 是 add_to_goal 的初始值：没有它，新建心愿的已存金额会是 NULL，
    -- 后续 current_amount + :amt 得到 NULL，进度条永远算不出来。
    SELECT column_default INTO v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'current_amount';
    IF v_default IS NULL OR btrim(v_default) NOT IN ('0', '0.00', '0::numeric') THEN
        RAISE EXCEPTION '核对①g未通过：current_amount default = %（应为 0）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ② target_date：date、可空
    SELECT data_type, is_nullable
      INTO v_type, v_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'target_date';
    IF v_type IS DISTINCT FROM 'date' THEN
        RAISE EXCEPTION '核对②a未通过：target_date 类型是 %（应为 date）。%', coalesce(v_type, '<列不存在>'), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对②b未通过：target_date is_nullable = %（应为 YES，没定 deadline 时存 NULL）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;

    -- ③ created_at：timestamptz、NOT NULL、DEFAULT now()
    SELECT data_type, is_nullable, column_default
      INTO v_type, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'created_at';
    IF v_type IS DISTINCT FROM 'timestamp with time zone' THEN
        RAISE EXCEPTION '核对③a未通过：created_at 类型是 %（应为 timestamptz）。%', coalesce(v_type, '<列不存在>'), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对③b未通过：created_at is_nullable = %（应为 NO）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS NULL OR v_default NOT LIKE '%now()%' THEN
        RAISE EXCEPTION '核对③c未通过：created_at default = %（应含 now()）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ④ goal_name / user_id：TEXT / INTEGER，均 NOT NULL
    SELECT data_type, is_nullable INTO v_type, v_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'goal_name';
    IF v_type IS DISTINCT FROM 'text' OR v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对④a未通过：goal_name 类型/可空性为 %/%（应为 text/NO）。%', coalesce(v_type,'?'), coalesce(v_nullable,'?'), v_advice;
    END IF;
    SELECT data_type, is_nullable INTO v_type, v_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'savings_goals' AND column_name = 'user_id';
    IF v_type IS DISTINCT FROM 'integer' OR v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对④b未通过：user_id 类型/可空性为 %/%（应为 integer/NO）。%', coalesce(v_type,'?'), coalesce(v_nullable,'?'), v_advice;
    END IF;

    -- ⑤ 三条 CHECK：存在 + 定义逐字正确（去空白、小写后比对）
    SELECT pg_get_constraintdef(oid) INTO v_def
    FROM pg_constraint
    WHERE conrelid = 'public.savings_goals'::regclass AND conname = 'savings_goals_target_positive';
    IF v_def IS NULL THEN
        RAISE EXCEPTION '核对⑤a未通过：缺少约束 savings_goals_target_positive（CHECK target_amount > 0）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    IF v_def_norm <> 'check((target_amount>(0)::numeric))' THEN
        RAISE EXCEPTION '核对⑤b未通过：savings_goals_target_positive 定义是 %（应为 CHECK (target_amount > 0)）。%', v_def, v_advice;
    END IF;

    SELECT pg_get_constraintdef(oid) INTO v_def
    FROM pg_constraint
    WHERE conrelid = 'public.savings_goals'::regclass AND conname = 'savings_goals_current_nonneg';
    IF v_def IS NULL THEN
        RAISE EXCEPTION '核对⑤c未通过：缺少约束 savings_goals_current_nonneg（CHECK current_amount >= 0）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    IF v_def_norm <> 'check((current_amount>=(0)::numeric))' THEN
        RAISE EXCEPTION '核对⑤d未通过：savings_goals_current_nonneg 定义是 %（应为 CHECK (current_amount >= 0)）。%', v_def, v_advice;
    END IF;

    SELECT pg_get_constraintdef(oid) INTO v_def
    FROM pg_constraint
    WHERE conrelid = 'public.savings_goals'::regclass AND conname = 'savings_goals_name_len';
    IF v_def IS NULL THEN
        RAISE EXCEPTION '核对⑤e未通过：缺少约束 savings_goals_name_len（CHECK char_length(btrim(goal_name)) BETWEEN 1 AND 50）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    -- PostgreSQL 会把 BETWEEN 展成 >= 1 AND <= 50（等价改写），所以比对展开形式。
    IF v_def_norm <> 'check(((char_length(btrim(goal_name))>=1)and(char_length(btrim(goal_name))<=50)))' THEN
        RAISE EXCEPTION '核对⑤f未通过：savings_goals_name_len 定义是 %（应为 char_length(btrim(goal_name)) BETWEEN 1 AND 50）。%', v_def, v_advice;
    END IF;

    -- ⑥ UNIQUE (user_id)：约束类型必须是 u，且键列就是 user_id
    --    set_goal 的 ON CONFLICT (user_id) 依赖它；判据必须同时看 contype 和 conkey，
    --    只看「存在叫这个名字的约束」挡不住「名字对、键错」。
    SELECT con.conkey INTO v_unique_key
    FROM pg_constraint con
    WHERE con.conrelid = 'public.savings_goals'::regclass
      AND con.conname = 'savings_goals_user_unique'
      AND con.contype = 'u';
    IF v_unique_key IS NULL THEN
        RAISE EXCEPTION '核对⑥a未通过：缺少 UNIQUE 约束 savings_goals_user_unique（一人一个心愿目标；set_goal 的 ON CONFLICT (user_id) 依赖它）。%', v_advice;
    END IF;
    SELECT attnum INTO v_user_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.savings_goals'::regclass AND attname = 'user_id' AND NOT attisdropped;
    IF array_length(v_unique_key, 1) IS DISTINCT FROM 1 OR v_unique_key[1] IS DISTINCT FROM v_user_attnum THEN
        RAISE EXCEPTION '核对⑥b未通过：savings_goals_user_unique 的键列是 %（应恰好是 user_id，attnum=%）。%', v_unique_key, v_user_attnum, v_advice;
    END IF;

    -- ⑦ 外键：指向 users(id)，删除动作必须是 CASCADE('c')
    --    ON DELETE CASCADE 是隐私口径：删号后心愿目标（含金额）不该继续躺在库里。
    SELECT con.confdeltype, pg_get_constraintdef(con.oid)
      INTO v_fk_action, v_fk_def
    FROM pg_constraint con
    WHERE con.conrelid = 'public.savings_goals'::regclass
      AND con.contype = 'f'
      AND con.confrelid = 'public.users'::regclass
    LIMIT 1;
    IF v_fk_action IS NULL THEN
        RAISE EXCEPTION '核对⑦a未通过：savings_goals 上没有指向 users(id) 的外键。%', v_advice;
    END IF;
    IF v_fk_action <> 'c' THEN
        RAISE EXCEPTION '核对⑦b未通过：外键删除动作是 %（应为 CASCADE / ''c''）。定义：% 。%', v_fk_action, v_fk_def, v_advice;
    END IF;
    IF lower(regexp_replace(v_fk_def, '\s+', '', 'g')) NOT LIKE '%ondeletecascade%' THEN
        RAISE EXCEPTION '核对⑦c未通过：外键定义里没有 ON DELETE CASCADE：%。%', v_fk_def, v_advice;
    END IF;

    -- ⑧ RLS 开启，且没有任何 policy
    SELECT relrowsecurity INTO v_rls FROM pg_class WHERE oid = 'public.savings_goals'::regclass;
    IF v_rls IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对⑧a未通过：savings_goals 未启用 RLS（relrowsecurity=false）。public schema 会被 PostgREST 暴露，不开 RLS 等于把用户心愿金额公开。%', v_advice;
    END IF;
    SELECT count(*) INTO v_policy_count
    FROM pg_policies WHERE schemaname = 'public' AND tablename = 'savings_goals';
    IF v_policy_count <> 0 THEN
        RAISE EXCEPTION '核对⑧b未通过：savings_goals 上有 % 条 RLS policy（应为 0 条 —— 本表只给服务端直连用）。请先 DROP POLICY 再重跑。%', v_policy_count, v_advice;
    END IF;

    RAISE NOTICE '核对通过：savings_goals 列类型/4 条命名约束/UNIQUE(user_id)/FK ON DELETE CASCADE/RLS(无 policy) 全部符合预期';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表结构与核对全部通过并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;
