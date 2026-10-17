-- =============================================================================
-- 010_login_events_retention.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 009 建的 public.login_events 加**保留期清理**：
--     ① 创建函数 public.prune_login_events(keep_days, batch, max_batches)
--        —— 分批删除「早于 now() - keep_days 天」的事件，返回实际删除行数；
--     ② 若环境里装了 pg_cron（Supabase 支持），自动创建每周日 04:00(UTC) 的清理任务；
--        没装则只打 NOTICE，不影响函数本身。
--
--   为什么需要它：登录事件是持续增长的流水表，不清理会一直涨。
--   默认保留 180 天：既能支撑半年趋势，又不至于无限膨胀（按每次登录 1 行、每天几百行估算，
--   半年也就几万行量级；真实流量请按需调小）。
--
--   ⚠ 安全：这是**删数据的函数**，且 login_events 在 public schema（PostgREST 会暴露函数）。
--     脚本会 `REVOKE ALL ... FROM PUBLIC` 并对 anon/authenticated（若存在）单独 REVOKE，
--     只留高权限角色（服务端连接串用的 postgres / cron 的执行者）能调用。
--     不要为了"方便"给 anon 授 EXECUTE，那等于把删表入口开到公网。
--
--   ⚠ 清理是**不可逆**的：函数只删 occurred_at 早于阈值的行；先确认保留期符合审计要求。
--
--   ⚠ 幂等：函数用 CREATE OR REPLACE；pg_cron 任务按 jobname 查重后再建。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/010_login_events_retention.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）。
--   ⚠ 顺序：001 → … → 009 → 010（本文件）；依赖 009 的 login_events 表。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，**不要**用 pooler 6543 跑（会话/事务状态问题）。
--
--   ✅ 没执行时不会红屏：应用不依赖本函数；只是事件表没有自动清理。
--
-- =============================================================================
-- 【手工清理（不想要定时任务时）】
-- =============================================================================
--   SELECT public.prune_login_events(180, 10000);   -- 保留 180 天
--   SELECT count(*) FROM public.login_events;       -- 看当前总量
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   DO $do$ BEGIN
--     IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
--       PERFORM cron.unschedule('prune_login_events') WHERE EXISTS (
--         SELECT 1 FROM cron.job WHERE jobname = 'prune_login_events');
--     END IF;
--   EXCEPTION WHEN OTHERS THEN NULL; END $do$;
--   DROP FUNCTION IF EXISTS public.prune_login_events(integer, integer, integer);
--
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（009 是否执行）
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF to_regclass('public.login_events') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.login_events 不存在。请先执行 migrations/009_login_events.sql。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：login_events 存在，继续';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：清理函数
--   · 分批删除（默认 1 万行/批，最多 100 批）：避免一次大 DELETE 长时间持锁；
--   · SET search_path 固定为 pg_catalog, public：函数体内全部用限定名，防 search_path 注入；
--   · 参数校验：keep_days < 1 直接抛错（拒绝"手滑传 0 清空全表"）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.prune_login_events(
    p_keep_days   integer DEFAULT 180,
    p_batch       integer DEFAULT 10000,
    p_max_batches integer DEFAULT 100
) RETURNS bigint
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $fn$
DECLARE
    v_deleted bigint := 0;
    v_batch   bigint;
    v_rounds  integer := 0;
BEGIN
    IF p_keep_days IS NULL OR p_keep_days < 1 THEN
        RAISE EXCEPTION 'p_keep_days 必须 >= 1（收到 %），拒绝执行以免误删全部事件', p_keep_days;
    END IF;
    IF p_batch IS NULL OR p_batch < 1 THEN
        RAISE EXCEPTION 'p_batch 必须 >= 1（收到 %）', p_batch;
    END IF;
    IF p_max_batches IS NULL OR p_max_batches < 1 THEN
        RAISE EXCEPTION 'p_max_batches 必须 >= 1（收到 %）', p_max_batches;
    END IF;

    LOOP
        v_rounds := v_rounds + 1;
        WITH doomed AS (
            SELECT id
            FROM public.login_events
            WHERE occurred_at < now() - make_interval(days => p_keep_days)
            ORDER BY id
            LIMIT p_batch
        )
        DELETE FROM public.login_events e
        USING doomed d
        WHERE e.id = d.id;

        GET DIAGNOSTICS v_batch = ROW_COUNT;
        v_deleted := v_deleted + v_batch;
        EXIT WHEN v_batch = 0 OR v_rounds >= p_max_batches;
    END LOOP;

    RAISE NOTICE 'prune_login_events：保留 % 天，共删除 % 行（% 轮，批大小 %）',
        p_keep_days, v_deleted, v_rounds, p_batch;
    RETURN v_deleted;
END
$fn$;

-- ---------------------------------------------------------------------------
-- 步骤 2：收紧执行权限
--   新函数默认对 PUBLIC 授 EXECUTE；login_events 在 public schema，PostgREST 能把函数
--   暴露成 RPC。这里收回 PUBLIC，并单独收回 Supabase 的 anon / authenticated（若存在）。
-- ---------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.prune_login_events(integer, integer, integer) FROM PUBLIC;

DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.prune_login_events(integer, integer, integer) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION public.prune_login_events(integer, integer, integer) FROM authenticated';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：可选 pg_cron 定时任务（每周日 04:00 UTC）
--   pg_cron 不可用（未安装、无权限）时只打 NOTICE，不影响函数本身。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    BEGIN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_cron';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_cron 不可用（%）—— 跳过定时任务，请用「手工清理」段落的 SQL 或平台自带调度', SQLERRM;
        RETURN;
    END;

    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'prune_login_events') THEN
            RAISE NOTICE 'pg_cron 任务 prune_login_events 已存在，跳过创建';
        ELSE
            PERFORM cron.schedule(
                'prune_login_events',
                '0 4 * * 0',
                'SELECT public.prune_login_events(180, 10000)'
            );
            RAISE NOTICE '已创建 pg_cron 任务 prune_login_events：每周日 04:00 UTC 清理 180 天前的事件';
        END IF;
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '创建 pg_cron 任务失败（%）：不影响清理函数，请手工调度', SQLERRM;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 4：核对
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_proc_count integer;
    v_ret_type  text;
    v_public_ok boolean;
    v_cron_job  integer := -1;
BEGIN
    -- 用 regprocedure 的规范文本比对（pg_get_function_identity_arguments 会带参数名，
    -- 例如 "p_keep_days integer, p_batch integer, ..."，不能直接和类型串比）
    SELECT count(*) INTO v_proc_count
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = 'prune_login_events'
      AND p.oid::regprocedure::text = 'prune_login_events(integer,integer,integer)';
    IF v_proc_count <> 1 THEN
        RAISE EXCEPTION '核对①未通过：prune_login_events(integer,integer,integer) 数量为 %（应为 1），事务已回滚', v_proc_count;
    END IF;

    SELECT format_type(p.prorettype, NULL) INTO v_ret_type
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'prune_login_events'
      AND p.oid::regprocedure::text = 'prune_login_events(integer,integer,integer)';
    IF v_ret_type IS DISTINCT FROM 'bigint' THEN
        RAISE EXCEPTION '核对②未通过：返回类型是 %（应为 bigint），事务已回滚', coalesce(v_ret_type, '<空>');
    END IF;

    -- PUBLIC 不再能执行：直接看 ACL（grantee = 0 即 PUBLIC；默认函数 ACL 会给 PUBLIC EXECUTE）
    SELECT bool_and(a.grantee <> 0)
      INTO v_public_ok
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
    WHERE n.nspname = 'public' AND p.proname = 'prune_login_events'
      AND p.oid::regprocedure::text = 'prune_login_events(integer,integer,integer)'
      AND a.privilege_type = 'EXECUTE';
    IF v_public_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对③未通过：PUBLIC 仍可 EXECUTE prune_login_events（请检查 REVOKE），事务已回滚';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        SELECT count(*) INTO v_cron_job FROM cron.job WHERE jobname = 'prune_login_events';
    END IF;
    RAISE NOTICE '核对通过：prune_login_events 就位、返回 bigint、PUBLIC 无 EXECUTE；pg_cron 任务数=%', coalesce(v_cron_job, -1);
END $do$;

SELECT 'OK' AS result;

COMMIT;
