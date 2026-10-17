-- =============================================================================
-- 009_login_events.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   新建 public.login_events —— 记录每一次登录尝试（成功 / 失败），用途有两个：
--     ① 管理员面板的「近 7 个自然日每日登录用户」趋势图（真实 DAU 代理指标）；
--         users.last_login 只是「最后一次登录」的覆盖式快照，画不出趋势；
--     ② 登录失败限流：同一用户名 15 分钟内失败 5 次且其间没有成功登录 → 暂时拒绝。
--         db.py 的 login_is_throttled() / record_login_event() 依赖本表。
--
--   表结构：
--     id          BIGSERIAL   主键（**排序/限流都用 id 而不是 occurred_at**：now() 是事务开始时刻，
--                             并发可能同微秒、时钟回拨会让「先后」错乱；id 单调且无歧义）
--     user_id     INTEGER     可空（用户名不存在的失败尝试没有 user_id），
--                             外键 → users(id) ON DELETE **SET NULL**
--                             （保留审计记录：删号后历史登录事件仍在，趋势图不会凭空回缩；
--                               趋势统计按 COALESCE(user_id::text, 'name:'||username) 去重）
--     username    TEXT        可空；CHECK 长度 ≤ 50（与 users.username 的 VARCHAR(50) 对齐）。
--                             记录**输入的用户名**（strip 后），失败尝试也记 —— 这样限流不会因为
--                             「账号不存在」而失效，也不给账号枚举提供额外信号
--     event       TEXT NOT NULL  CHECK (event IN ('success','failure'))
--     occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()   -- 绝对时刻，与 008 口径一致
--
--   索引：
--     (username, occurred_at DESC)  限流查询：按用户名取最近 N 分钟
--     (user_id,  occurred_at DESC)  留存/趋势：按用户取历史
--     (occurred_at)                 按天分桶 + 将来的保留期清理
--
--   ⚠ 隐私与暴露面：表在 public schema，Supabase 会经 PostgREST 暴露表名。
--     本脚本对 login_events 执行 `ENABLE ROW LEVEL SECURITY` 且**不建任何 policy**，
--     于是 anon / authenticated 角色读不到任何行（服务端用连接串里的 postgres 用户直连，
--     不受影响）。不要为了"方便调试"去加放行 policy。
--
--   ⚠ 保留期：事件表会随时间增长，建议定期清理（默认不自动执行，按需手工跑）：
--       DELETE FROM public.login_events WHERE occurred_at < now() - interval '180 days';
--     一次性迁移少则几十行、多则几十万行；清理请放在低峰、分批做。
--
--   ⚠ 不回填历史：login_events 从本脚本执行时刻开始记录，历史登录无法还原
--     （users.last_login 只有最后时刻）。因此趋势图执行后从"0 / 只有新登录"开始涨，
--     这是如实反映，不要用 last_login 伪造历史点。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/009_login_events.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证。
--
--   ⚠ 顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009（本文件）
--     依赖 005（users 表已有 last_login，本脚本的比较口径与之无关，但 005 之后表结构才稳定）。
--
--   ✅ 没执行时不会红屏：db.py 的登录事件写入/限流/趋势查询对
--      "表不存在（42P01）" 都 fail-open —— 限流不生效、埋点静默、趋势图显示为空。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只**新增**对象（表/索引/RLS 开关），不改动已有表与数据，风险很低。
--   仍建议先留一份结构备份：
--       pg_dump --schema-only -t public.users -t public.login_events "$DATABASE_URL" > schema_before_009.sql
--   ⚠ Supabase pooler（6543 / transaction 模式）不能用于 pg_dump，请用 5432 直连串。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   DROP TABLE IF EXISTS public.login_events;
--   （回滚后 db.py 自动 fail-open：限流不生效、趋势图空、登录照常。）
--
-- =============================================================================
-- 【幂等】
--   · 表 / 索引：IF NOT EXISTS；
--   · RLS：ENABLE 本身幂等；
--   · 核对：每次执行都跑，失败 RAISE EXCEPTION 整体回滚。
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.users 不存在。请先启动一次应用让 db.init_db() 建表，或确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：public.users 存在，继续';
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 1：建表（幂等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.login_events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NULL REFERENCES public.users(id) ON DELETE SET NULL,
    username    TEXT NULL,
    event       TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT login_events_event_check CHECK (event IN ('success', 'failure')),
    CONSTRAINT login_events_username_len_check CHECK (username IS NULL OR char_length(username) <= 50)
);

DO $$
BEGIN
    IF to_regclass('public.login_events') IS NULL THEN
        RAISE EXCEPTION '步骤1 未通过：public.login_events 创建后仍不存在，事务已回滚';
    END IF;
    RAISE NOTICE '步骤1：login_events 表已就绪（或已存在）';
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 2：索引（幂等）
--   username 索引用 DESC：限流查询是「按用户名取最近 15 分钟」，倒序索引更顺。
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_login_events_username_time
    ON public.login_events (username, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_user_time
    ON public.login_events (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_time
    ON public.login_events (occurred_at);

-- ---------------------------------------------------------------------------
-- 步骤 3：RLS（幂等）
--   public schema 会被 PostgREST 暴露；开 RLS + 不建 policy = anon/authenticated 读不到。
--   服务端用连接串里的高权限角色直连，不受影响。
-- ---------------------------------------------------------------------------
ALTER TABLE public.login_events ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 步骤 4：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_type          text;
    v_nullable      text;
    v_default       text;
    v_event_check   text;
    v_username_chk  text;
    v_rls           boolean;
    v_idx_count     integer;
    v_user_id_null  text;
    v_fk_action     "char";
    v_event_type    text;
    v_username_type text;
    v_policy_count  integer;
    v_advice        text := '若表已存在但结构与预期不符：本脚本刻意不自动改旧表（避免误改生产结构），请把数据导出后 DROP TABLE public.login_events; 再重跑本脚本；或按报错手工 ALTER 对齐。';
BEGIN
    -- ① occurred_at：类型 / 非空 / 默认值
    SELECT data_type, is_nullable, column_default
      INTO v_type, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'login_events' AND column_name = 'occurred_at';
    IF v_type IS DISTINCT FROM 'timestamp with time zone' THEN
        RAISE EXCEPTION '核对①a未通过：occurred_at 类型是 %（应为 timestamptz）。%', coalesce(v_type, '<列不存在>'), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对①b未通过：occurred_at is_nullable = %（应为 NO）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS NULL OR v_default NOT LIKE '%now()%' THEN
        RAISE EXCEPTION '核对①c未通过：occurred_at default = %（应含 now()）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ② user_id 必须可空（用户名不存在的失败事件没有 user_id）
    SELECT is_nullable INTO v_user_id_null
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'login_events' AND column_name = 'user_id';
    IF v_user_id_null IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对②未通过：user_id is_nullable = %（应为 YES，否则未知用户名的失败事件插不进来、限流对它们失效）。%', coalesce(v_user_id_null, '<列不存在>'), v_advice;
    END IF;

    -- ③ 外键删除动作必须是 SET NULL('n')：CASCADE 会在删号时抹掉审计、并让历史趋势回缩
    SELECT con.confdeltype INTO v_fk_action
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE ns.nspname = 'public' AND rel.relname = 'login_events' AND con.contype = 'f'
      AND con.confrelid = 'public.users'::regclass
    LIMIT 1;
    IF v_fk_action IS NULL THEN
        RAISE EXCEPTION '核对③a未通过：login_events 上没有指向 users(id) 的外键。%', v_advice;
    END IF;
    IF v_fk_action <> 'n' THEN
        RAISE EXCEPTION '核对③b未通过：外键删除动作是 %（应为 SET NULL / ''n''）。CASCADE 会删掉审计记录。%', v_fk_action, v_advice;
    END IF;

    -- ④ 列类型
    SELECT data_type INTO v_event_type FROM information_schema.columns
    WHERE table_schema='public' AND table_name='login_events' AND column_name='event';
    SELECT data_type INTO v_username_type FROM information_schema.columns
    WHERE table_schema='public' AND table_name='login_events' AND column_name='username';
    IF v_event_type IS DISTINCT FROM 'text' OR v_username_type IS DISTINCT FROM 'text' THEN
        RAISE EXCEPTION '核对④未通过：event/username 类型为 %/%（都应为 text）。%', coalesce(v_event_type,'?'), coalesce(v_username_type,'?'), v_advice;
    END IF;

    -- ⑤ 两条 CHECK：事件取值 + 用户名长度
    SELECT pg_get_constraintdef(oid) INTO v_event_check
    FROM pg_constraint
    WHERE conrelid = 'public.login_events'::regclass AND conname = 'login_events_event_check';
    IF v_event_check IS NULL
       OR v_event_check NOT LIKE '%success%' OR v_event_check NOT LIKE '%failure%' THEN
        RAISE EXCEPTION '核对⑤a未通过：login_events_event_check 缺失或定义不含 success/failure。%', v_advice;
    END IF;
    SELECT pg_get_constraintdef(oid) INTO v_username_chk
    FROM pg_constraint
    WHERE conrelid = 'public.login_events'::regclass AND conname = 'login_events_username_len_check';
    IF v_username_chk IS NULL THEN
        RAISE EXCEPTION '核对⑤b未通过：缺少 login_events_username_len_check（username 长度 ≤ 50）。%', v_advice;
    END IF;

    -- ⑥ RLS 开启，且**没有任何 policy**（有 policy 就可能对 anon/authenticated 放行）
    SELECT relrowsecurity INTO v_rls FROM pg_class WHERE oid = 'public.login_events'::regclass;
    IF v_rls IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对⑥a未通过：login_events 未启用 RLS（relrowsecurity=false）。%', v_advice;
    END IF;
    SELECT count(*) INTO v_policy_count
    FROM pg_policies WHERE schemaname = 'public' AND tablename = 'login_events';
    IF v_policy_count <> 0 THEN
        RAISE EXCEPTION '核对⑥b未通过：login_events 上有 % 条 RLS policy（应为 0 条 —— 本表只给服务端直连用）。请先 DROP POLICY 再重跑。%', v_policy_count, v_advice;
    END IF;

    -- ⑦ 三个业务索引
    SELECT count(*) INTO v_idx_count
    FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'login_events'
      AND indexname IN ('idx_login_events_username_time', 'idx_login_events_user_time', 'idx_login_events_time');
    IF v_idx_count <> 3 THEN
        RAISE EXCEPTION '核对⑦未通过：预期 3 个业务索引，实际 % 个。%', v_idx_count, v_advice;
    END IF;

    RAISE NOTICE '核对通过：login_events 结构/约束/RLS(无 policy)/索引齐备（occurred_at=timestamptz NOT NULL DEFAULT now()，FK ON DELETE SET NULL）';
END $$;

SELECT 'OK' AS result;

COMMIT;
