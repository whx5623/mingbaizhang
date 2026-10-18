-- =============================================================================
-- 014_user_notifications.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   新建 public.user_notifications —— 「日报 / 通知发送流水」：
--   记录某用户在某一天、某个渠道的通知是否发成功，用于
--     · 幂等去重：同一 (user_id, sent_date, channel) 只保留一行，
--       日报脚本重复运行不会给同一个人重复发；
--     · 可审计：status 只有 sent / failed / skipped 三种，
--       失败时把错误摘要落进 error，便于人工排查；
--     · 可重试：failed / skipped 的行可以被下一次运行覆盖成 sent
--       （ON CONFLICT DO UPDATE），不需要先删旧行。
--
--   字段：
--     id          BIGSERIAL    主键
--     user_id     INTEGER NOT NULL  外键 → users(id) ON DELETE CASCADE
--                 （删号即删通知流水：通知内容可能含用户财务摘要，按隐私数据处理）
--     sent_date   DATE NOT NULL     发送归属的**自然日**（不是时间戳）
--     channel     TEXT NOT NULL DEFAULT 'webhook'   渠道，1~32 字符
--     status      TEXT NOT NULL DEFAULT 'sent'      sent / failed / skipped
--     message     TEXT NULL         成功时的消息摘要（可为空）
--     error       TEXT NULL         失败时的错误摘要（成功/跳过着为 NULL）
--     created_at  TIMESTAMPTZ NOT NULL DEFAULT now()  最后一次写入的绝对时刻
--
--   约束与索引：
--     user_notifications_status_check  CHECK (status IN ('sent','failed','skipped'))
--     user_notifications_channel_len   CHECK (char_length(channel) BETWEEN 1 AND 32)
--     user_notifications_user_id_fkey  FOREIGN KEY (user_id) → users(id) ON DELETE CASCADE
--     uq_user_notifications_user_date_channel  UNIQUE (user_id, sent_date, channel)
--     idx_user_notifications_date             普通索引 (sent_date)
--
--   ⚠ 唯一索引是业务核心：db.py 的 record_notification 用
--     INSERT ... ON CONFLICT (user_id, sent_date, channel) DO UPDATE 实现「有则更新」，
--     依赖的正是这条**唯一索引**；没有它 ON CONFLICT 会直接报 42P10。
--   ⚠ 本表在 public schema，Supabase 会经 PostgREST 暴露表名。脚本对它执行
--     ENABLE ROW LEVEL SECURITY 且**不建任何 policy**，于是 anon / authenticated
--     角色读不到也写不进任何行；服务端用连接串里的高权限角色直连（表 owner
--     默认绕过 RLS），不受影响。不要为了「方便调试」加放行 policy：
--     message/error 里可能带用户财务摘要，一旦放行 anon 读就是公开隐私。
--   ⚠ 只建表，**不会**给现有用户预置任何通知行；从执行时刻开始为空表。
--
-- =============================================================================
-- 【时区口径】
-- =============================================================================
--   · sent_date 是 DATE（日历日），不是 timestamptz：它回答「这份日报属于哪一天」，
--     与「几点发的」无关。日报脚本应按**北京时间（UTC+8）自然日**填这个字段；
--     本库连接会话时区固定为 UTC，但 DATE 不随会话时区漂移。
--   · created_at 是 timestamptz（绝对时刻），记录最后一次 upsert 的时刻；
--     页面/脚本自行决定按哪个时区展示。
--   · pg_cron 的 '0 13 * * *' 是 **UTC** 13:00 = 北京时间 21:00；
--     此时 UTC 与北京时间的自然日相同，不会跨日。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/014_user_notifications.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证，
--     中途失败会留下「半张表 + 缺索引」的状态。
--   ⚠ 迁移建议用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--   ✅ 本文件末尾的 pg_cron 段是**独立、可跳过**的：缺 pg_cron / pg_net 扩展、
--     或端点、令牌仍是占位符时只 RAISE NOTICE，不会让建表事务失败。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011
--          → 012 → 013 → 014（本文件）
--   ✅ 014 本身只依赖 public.users（001 已建），不读写 bills；
--      db.py 的 report_user_stats 另依赖 007 对 bills.date 的 ISO 约束（与建表无关）。
--      迁移仍按编号排在 013 之后执行（顺序固定）。
--
-- =============================================================================
-- 【备份建议】（执行前做）
-- =============================================================================
--   本脚本只**新增**一张表，不改动已有表与数据，风险很低；仍建议留一份结构备份：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > schema_before_014.sql
--   （user_notifications 还不存在时不要把它写进 -t；只备 users 也足够确认外键父表。）
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE user_notifications_backup AS ...
--     之类的库内副本 —— 它会被 PostgREST 暴露，也会让 pg_dump 的 schema 里多出垃圾对象。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的 CREATE TABLE
--   IF NOT EXISTS），**刻意不建 user_notifications**：迁移统一走本文件，避免
--   「应用启动顺手改 schema」与「迁移脚本」两条路径互相打架。
--   没执行本文件时，db.py 的 record_notification / notification_already_sent
--   会抛 RuntimeError（中文提示先执行 014），不会静默返回假数据；
--   report_user_stats 只读 users/bills，不依赖本表，仍可正常使用。
--   这些新函数都不会拖垮记账/登录主流程。
--   send_daily_report 之类的脚本同样**不得**在运行时 CREATE TABLE；部署顺序必须是：
--   先执行本脚本 → 再跑日报脚本。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   先卸载可选的 pg_cron 任务（没有 cron 扩展 / 没建过任务时跳过这一段）：
--       SELECT cron.unschedule('daily_finance_report')
--       FROM cron.job
--       WHERE jobname = 'daily_finance_report';
--   （任务不存在时这条 SELECT 返回 0 行，不会报错；cron 扩展不存在时不要执行。）
--
--   再删表（表里的发送流水一起删掉，不可恢复；回滚前先导出需要保留的数据）：
--       DROP TABLE IF EXISTS public.user_notifications;
--
--   回滚后 db.py 的 report_user_stats 仍可用（它只读 users/bills）；
--   record_notification / notification_already_sent 会重新走 42P01 分支抛 RuntimeError。
--
-- =============================================================================
-- 【幂等（重复执行安全）】
-- =============================================================================
--   · 表：CREATE TABLE IF NOT EXISTS；
--   · 两个索引：CREATE [UNIQUE] INDEX IF NOT EXISTS；
--   · RLS：ENABLE 本身幂等；
--   · 核对：每次都跑，任何一条不符就 RAISE EXCEPTION → 整个事务回滚；
--   · 重复执行不会改旧表结构、不动数据、不重置序列（BIGSERIAL 序列只建一次）。
--   ⚠ CREATE TABLE IF NOT EXISTS 对**已存在的表**什么都不做：如果库里已有一张
--     结构不对的 user_notifications（少了约束/索引），本脚本不会自动补，
--     步骤 2 会明确报错并回滚；正确做法是导出数据后 DROP TABLE 再重跑。
--   ⚠ CREATE UNIQUE INDEX IF NOT EXISTS 在已有**重复行**的表上会失败并回滚，
--     这是想要的：唯一性不能靠「跳过索引」来假装满足。
-- =============================================================================

BEGIN;

-- 5 秒拿不到表锁就报错回滚，不无期限卡住线上写入。
SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（users 存在、users.id 是 integer、有主键/唯一约束）
--   user_id 的外键指向 users(id)；父表不存在时建表会失败并留下半截事务，
--   不如在这里给一句能直接照做的报错。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type          text;
    v_user_attnum   smallint;
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.users 不存在。请先执行 001/002 系列迁移（或启动一次应用让 db.init_db() 建表），并确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;

    SELECT data_type INTO v_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id';
    IF v_type IS DISTINCT FROM 'integer' THEN
        RAISE EXCEPTION '步骤0 中止：public.users.id 类型是 %（应为 integer）。user_id 外键无法指向它，请先核对 001 迁移。本事务未做任何改动', coalesce(v_type, '<列不存在>');
    END IF;

    SELECT attnum INTO v_user_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.users'::regclass AND attname = 'id' AND NOT attisdropped;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND contype IN ('p', 'u')
          AND conkey = ARRAY[v_user_attnum]
    ) THEN
        RAISE EXCEPTION '步骤0 中止：public.users.id 上没有主键/唯一约束，无法建立外键。请先核对 001 迁移。本事务未做任何改动';
    END IF;

    RAISE NOTICE '步骤0：public.users 存在、id 为 integer 且有主键/唯一约束，继续';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：建表 + 两个索引 + RLS（全部幂等）
--   status / channel 的取值与长度由命名约束兜底；db.py 在 Python 侧再校验一遍，
--   目的是把「非法状态」变成 ValueError 而不是一条 psycopg2 栈。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_notifications (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    sent_date     DATE NOT NULL,
    channel       TEXT NOT NULL DEFAULT 'webhook',
    status        TEXT NOT NULL DEFAULT 'sent',      -- sent / failed / skipped
    message       TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_notifications_status_check CHECK (status IN ('sent','failed','skipped')),
    CONSTRAINT user_notifications_channel_len  CHECK (char_length(channel) BETWEEN 1 AND 32)
);

-- 幂等去重的唯一键：db.py 的 ON CONFLICT (user_id, sent_date, channel) 依赖它。
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_notifications_user_date_channel
    ON public.user_notifications (user_id, sent_date, channel);

-- 按天查发送情况 / 清理旧流水用；普通索引，不唯一。
CREATE INDEX IF NOT EXISTS idx_user_notifications_date
    ON public.user_notifications (sent_date);

-- public schema 会被 PostgREST 暴露；开 RLS + 不建 policy = anon/authenticated
-- 读不到也写不进。服务端直连（表 owner / 高权限角色）不受影响。
ALTER TABLE public.user_notifications ENABLE ROW LEVEL SECURITY;

DO $do$
BEGIN
    IF to_regclass('public.user_notifications') IS NULL THEN
        RAISE EXCEPTION '步骤1 未通过：public.user_notifications 创建后仍不存在，事务已回滚';
    END IF;
    RAISE NOTICE '步骤1：user_notifications 表、唯一索引、日期索引、RLS 已就绪（或已存在）';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 2：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：
--     · 列类型 / 可空性 / 关键默认值；
--     · 3 条约束：status CHECK、channel_len CHECK、FK ON DELETE CASCADE；
--     · 唯一索引的键列恰好是 (user_id, sent_date, channel) 且顺序一致；
--     · 普通索引 idx_user_notifications_date 的键列是 sent_date；
--     · RLS = true 且 policy 数 = 0。
--   约束定义用「去空白 + 小写」后与 PostgreSQL 规范化文本逐字比对，不用
--   LIKE '%status%' 这种松判据 —— 松判据会把 CHECK (status <> 'sent') 也判成通过。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_col            text;
    v_expected_type  text;
    v_expected_null  text;
    v_type           text;
    v_nullable       text;
    v_default        text;
    v_def            text;
    v_def_norm       text;
    v_contype        text;
    v_user_attnum    smallint;
    v_sent_attnum    smallint;
    v_channel_attnum smallint;
    v_users_id_attnum smallint;
    v_key            smallint[];
    v_index_relid    oid;
    v_index_table    oid;
    v_is_unique      boolean;
    v_nkey           smallint;
    v_has_pred       boolean;
    v_has_expr       boolean;
    v_fk_del         text;
    v_fk_key         smallint[];
    v_fk_confkey     smallint[];
    v_fk_target      oid;
    v_rls            boolean;
    v_policy_count   integer;
    v_advice         text := '若表已存在但结构与预期不符：本脚本刻意不自动改旧表（避免误改生产结构）。user_notifications 是本次新建的表，正确做法通常是导出数据后 DROP TABLE public.user_notifications; 再重跑本脚本。';
BEGIN
    -- ① 列类型 / 可空性（逐列，逐项比对）
    FOR v_col, v_expected_type, v_expected_null IN
        SELECT * FROM (VALUES
            ('id',         'bigint',                   'NO'),
            ('user_id',    'integer',                  'NO'),
            ('sent_date',  'date',                     'NO'),
            ('channel',    'text',                     'NO'),
            ('status',     'text',                     'NO'),
            ('message',    'text',                     'YES'),
            ('error',      'text',                     'YES'),
            ('created_at', 'timestamp with time zone', 'NO')
        ) AS t(col, typ, nullable)
    LOOP
        SELECT data_type, is_nullable
          INTO v_type, v_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'user_notifications'
          AND column_name = v_col;
        IF v_type IS NULL THEN
            RAISE EXCEPTION '核对①未通过：列 % 不存在（应为 % / nullable=%）。%', v_col, v_expected_type, v_expected_null, v_advice;
        END IF;
        IF v_type IS DISTINCT FROM v_expected_type THEN
            RAISE EXCEPTION '核对①未通过：列 % 类型是 %（应为 %）。%', v_col, v_type, v_expected_type, v_advice;
        END IF;
        IF v_nullable IS DISTINCT FROM v_expected_null THEN
            RAISE EXCEPTION '核对①未通过：列 % is_nullable = %（应为 %）。%', v_col, v_nullable, v_expected_null, v_advice;
        END IF;
    END LOOP;

    -- ② channel / status 的默认值：必须是 'webhook' / 'sent'
    SELECT column_default INTO v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_notifications' AND column_name = 'channel';
    v_def_norm := lower(regexp_replace(coalesce(v_default, ''), '\s+', '', 'g'));
    IF v_def_norm NOT IN ('''webhook''::text', '''webhook''') THEN
        RAISE EXCEPTION '核对②a未通过：channel default = %（应为 ''webhook''）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    SELECT column_default INTO v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_notifications' AND column_name = 'status';
    v_def_norm := lower(regexp_replace(coalesce(v_default, ''), '\s+', '', 'g'));
    IF v_def_norm NOT IN ('''sent''::text', '''sent''') THEN
        RAISE EXCEPTION '核对②b未通过：status default = %（应为 ''sent''）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ③ created_at 默认 now()
    SELECT column_default INTO v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_notifications' AND column_name = 'created_at';
    IF v_default IS NULL OR position('now()' IN lower(v_default)) = 0 THEN
        RAISE EXCEPTION '核对③未通过：created_at default = %（应含 now()）。%', coalesce(v_default, '<NULL>'), v_advice;
    END IF;

    -- ④ status CHECK：存在、是 CHECK、定义逐字正确
    SELECT contype::text, pg_get_constraintdef(oid)
      INTO v_contype, v_def
    FROM pg_constraint
    WHERE conrelid = 'public.user_notifications'::regclass
      AND conname = 'user_notifications_status_check';
    IF v_contype IS DISTINCT FROM 'c' THEN
        RAISE EXCEPTION '核对④a未通过：缺少 CHECK 约束 user_notifications_status_check（status 只允许 sent/failed/skipped）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    IF v_def_norm <> 'check((status=any(array[''sent''::text,''failed''::text,''skipped''::text])))' THEN
        RAISE EXCEPTION '核对④b未通过：user_notifications_status_check 定义是 %（应为 status IN (sent, failed, skipped)）。%', v_def, v_advice;
    END IF;

    -- ⑤ channel 长度 CHECK：存在、是 CHECK、定义逐字正确
    SELECT contype::text, pg_get_constraintdef(oid)
      INTO v_contype, v_def
    FROM pg_constraint
    WHERE conrelid = 'public.user_notifications'::regclass
      AND conname = 'user_notifications_channel_len';
    IF v_contype IS DISTINCT FROM 'c' THEN
        RAISE EXCEPTION '核对⑤a未通过：缺少 CHECK 约束 user_notifications_channel_len（channel 长度 1~32）。%', v_advice;
    END IF;
    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    IF v_def_norm <> 'check(((char_length(channel)>=1)and(char_length(channel)<=32)))' THEN
        RAISE EXCEPTION '核对⑤b未通过：user_notifications_channel_len 定义是 %（应为 char_length(channel) BETWEEN 1 AND 32）。%', v_def, v_advice;
    END IF;

    -- ⑥ 外键：指向 users(id)、键列是 user_id、删除动作必须是 CASCADE('c')
    SELECT con.confdeltype::text, con.conkey, con.confkey, con.confrelid
      INTO v_fk_del, v_fk_key, v_fk_confkey, v_fk_target
    FROM pg_constraint con
    WHERE con.conrelid = 'public.user_notifications'::regclass
      AND con.contype = 'f'
      AND con.confrelid = 'public.users'::regclass
    LIMIT 1;
    IF v_fk_target IS NULL THEN
        RAISE EXCEPTION '核对⑥a未通过：user_notifications 上没有指向 users(id) 的外键。%', v_advice;
    END IF;

    SELECT attnum INTO v_user_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.user_notifications'::regclass AND attname = 'user_id' AND NOT attisdropped;
    SELECT attnum INTO v_users_id_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.users'::regclass AND attname = 'id' AND NOT attisdropped;
    IF v_fk_key IS DISTINCT FROM ARRAY[v_user_attnum] THEN
        RAISE EXCEPTION '核对⑥b未通过：外键本地键列是 %（应恰好是 user_id，attnum=%）。%', v_fk_key, v_user_attnum, v_advice;
    END IF;
    IF v_fk_confkey IS DISTINCT FROM ARRAY[v_users_id_attnum] THEN
        RAISE EXCEPTION '核对⑥c未通过：外键引用键列是 %（应恰好是 users.id，attnum=%）。%', v_fk_confkey, v_users_id_attnum, v_advice;
    END IF;
    IF v_fk_del <> 'c' THEN
        RAISE EXCEPTION '核对⑥d未通过：外键删除动作是 %（应为 CASCADE / ''c''）。%', v_fk_del, v_advice;
    END IF;

    -- ⑦ 唯一索引：键列恰好是 (user_id, sent_date, channel)，顺序一致，且不是部分/表达式索引
    SELECT attnum INTO v_user_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.user_notifications'::regclass AND attname = 'user_id' AND NOT attisdropped;
    SELECT attnum INTO v_sent_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.user_notifications'::regclass AND attname = 'sent_date' AND NOT attisdropped;
    SELECT attnum INTO v_channel_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.user_notifications'::regclass AND attname = 'channel' AND NOT attisdropped;

    SELECT i.indexrelid, i.indrelid, i.indisunique, i.indnkeyatts,
           string_to_array(i.indkey::text, ' ')::smallint[],
           (i.indpred IS NOT NULL), (i.indexprs IS NOT NULL)
      INTO v_index_relid, v_index_table, v_is_unique, v_nkey, v_key, v_has_pred, v_has_expr
    FROM pg_index i
    WHERE i.indexrelid = to_regclass('public.uq_user_notifications_user_date_channel');
    IF v_index_relid IS NULL OR v_index_table IS DISTINCT FROM 'public.user_notifications'::regclass THEN
        RAISE EXCEPTION '核对⑦a未通过：唯一索引 uq_user_notifications_user_date_channel 不存在或不在 user_notifications 上。%', v_advice;
    END IF;
    IF v_is_unique IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对⑦b未通过：uq_user_notifications_user_date_channel 不是 UNIQUE 索引。%', v_advice;
    END IF;
    IF v_has_pred OR v_has_expr THEN
        RAISE EXCEPTION '核对⑦c未通过：uq_user_notifications_user_date_channel 是部分索引或表达式索引（ON CONFLICT 推断会失败）。%', v_advice;
    END IF;
    IF v_nkey IS DISTINCT FROM 3
       OR v_key IS DISTINCT FROM ARRAY[v_user_attnum, v_sent_attnum, v_channel_attnum] THEN
        RAISE EXCEPTION '核对⑦d未通过：唯一索引键列是 %（应恰好是 (user_id, sent_date, channel) = %）。%', v_key, ARRAY[v_user_attnum, v_sent_attnum, v_channel_attnum], v_advice;
    END IF;

    -- ⑧ 普通索引：idx_user_notifications_date，键列就是 sent_date
    SELECT i.indexrelid, i.indrelid, i.indisunique, i.indnkeyatts,
           string_to_array(i.indkey::text, ' ')::smallint[],
           (i.indpred IS NOT NULL), (i.indexprs IS NOT NULL)
      INTO v_index_relid, v_index_table, v_is_unique, v_nkey, v_key, v_has_pred, v_has_expr
    FROM pg_index i
    WHERE i.indexrelid = to_regclass('public.idx_user_notifications_date');
    IF v_index_relid IS NULL OR v_index_table IS DISTINCT FROM 'public.user_notifications'::regclass THEN
        RAISE EXCEPTION '核对⑧a未通过：普通索引 idx_user_notifications_date 不存在或不在 user_notifications 上。%', v_advice;
    END IF;
    IF v_is_unique IS DISTINCT FROM false THEN
        RAISE EXCEPTION '核对⑧b未通过：idx_user_notifications_date 不应是 UNIQUE 索引。%', v_advice;
    END IF;
    IF v_has_pred OR v_has_expr THEN
        RAISE EXCEPTION '核对⑧c未通过：idx_user_notifications_date 是部分索引或表达式索引（不是按 sent_date 的普通 B-tree）。%', v_advice;
    END IF;
    IF v_nkey IS DISTINCT FROM 1 OR v_key IS DISTINCT FROM ARRAY[v_sent_attnum] THEN
        RAISE EXCEPTION '核对⑧d未通过：idx_user_notifications_date 键列是 %（应恰好是 sent_date，attnum=%）。%', v_key, v_sent_attnum, v_advice;
    END IF;

    -- ⑨ RLS 开启，且没有任何 policy
    SELECT relrowsecurity INTO v_rls
    FROM pg_class
    WHERE oid = 'public.user_notifications'::regclass;
    IF v_rls IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对⑨a未通过：user_notifications 未启用 RLS（relrowsecurity=false）。public schema 会被 PostgREST 暴露，不开 RLS 等于把通知流水公开。%', v_advice;
    END IF;
    SELECT count(*) INTO v_policy_count
    FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'user_notifications';
    IF v_policy_count <> 0 THEN
        RAISE EXCEPTION '核对⑨b未通过：user_notifications 上有 % 条 RLS policy（应为 0 条 —— 本表只给服务端直连用）。请先 DROP POLICY 再重跑。%', v_policy_count, v_advice;
    END IF;

    RAISE NOTICE '核对通过：user_notifications 列类型/3 条约束/唯一索引(user_id,sent_date,channel)/日期索引/RLS(无 policy) 全部符合预期';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表建表、索引、RLS 与核对全部通过。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

-- =============================================================================
-- 【可选】pg_cron 定时调用日报端点（独立、可跳过）
-- =============================================================================
--   ⚠ 必须说清楚的能力边界：
--     · pg_cron **只能发 HTTP 请求，不能直接跑 Python**。它不能 import db.py，
--       也不能执行 send_daily_report.py；真正生成日报的是被调用的 HTTP 端点。
--     · 需要 **pg_cron + pg_net** 两个扩展（pg_cron 负责定时，pg_net 负责异步 HTTP），
--       还需要一个可被公网（或 Supabase 内网）调用的 HTTP 端点，例如：
--         - Supabase Edge Function（推荐，和库同区域）
--         - 自建 API（FastAPI/Flask，暴露 /daily-report）
--         - GitHub Actions 的 workflow_dispatch（用仓库 token 触发）
--     · 端点必须自己完成「查用户 → 算日报 → 调 webhook → 写 user_notifications」，
--       并在写库时按 (user_id, sent_date, channel) 做 upsert 去重。
--     · 下面的 <EDGE_URL> / <TOKEN> 是占位符，**必须替换成自己的值**；
--       不要提交真实 token 到仓库，建议放 Supabase Vault / 端点环境变量。
--
--   手工创建（等价示例，确认扩展已装后再执行）：
--     SELECT cron.schedule('daily_finance_report', '0 13 * * *', $$
--         SELECT net.http_post(
--             url     := 'https://<EDGE_URL>/daily-report',
--             headers := jsonb_build_object('Content-Type','application/json','Authorization','Bearer <TOKEN>'),
--             body    := '{}'::jsonb
--         );
--     $$);
--     -- 查看 / 删除：
--     SELECT * FROM cron.job;
--     SELECT cron.unschedule('daily_finance_report');
--
--   下面的 DO 块是**条件执行**：缺扩展、模板占位符未替换、任务已存在、
--   或 cron.schedule 本身报错时，只 RAISE NOTICE 并跳过，绝不让 014 的建表事务失败。
--   如果你不想让迁移顺手创建定时任务，把 v_enable 设为 false 即可。
DO $do$
DECLARE
    v_enable     boolean := true;          -- ← 不想自动建任务就改成 false
    v_job_name   constant text := 'daily_finance_report';
    v_schedule   constant text := '0 13 * * *';  -- UTC 13:00 = 北京时间 21:00
    v_edge_url   constant text := 'https://<EDGE_URL>/daily-report';
    v_token      constant text := '<TOKEN>';
    v_has_cron   boolean;
    v_has_net    boolean;
BEGIN
    IF NOT v_enable THEN
        RAISE NOTICE 'pg_cron：v_enable=false，跳过定时任务创建（014 建表不受影响）';
        RETURN;
    END IF;

    SELECT to_regclass('cron.job') IS NOT NULL INTO v_has_cron;
    SELECT to_regprocedure('net.http_post(text,jsonb,jsonb,jsonb,integer)') IS NOT NULL INTO v_has_net;

    IF NOT v_has_cron THEN
        RAISE NOTICE 'pg_cron：未安装（cron.job 不存在），跳过定时任务创建。日报仍可手动触发或由外部调度器调用';
        RETURN;
    END IF;
    IF NOT v_has_net THEN
        RAISE NOTICE 'pg_cron：未安装 pg_net 或 net.http_post 签名不匹配，跳过定时任务创建。pg_cron 只能发 HTTP，不能直接跑 Python';
        RETURN;
    END IF;
    IF position('<EDGE_URL>' IN v_edge_url) > 0 OR position('<TOKEN>' IN v_token) > 0 THEN
        RAISE NOTICE 'pg_cron：端点/令牌仍是模板占位符（<EDGE_URL>/<TOKEN>），跳过定时任务创建。替换后再执行本段';
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = v_job_name) THEN
        RAISE NOTICE 'pg_cron：任务 % 已存在，跳过（幂等）', v_job_name;
        RETURN;
    END IF;

    BEGIN
        PERFORM cron.schedule(
            v_job_name,
            v_schedule,
            format($job$
                SELECT net.http_post(
                    url     := %L,
                    headers := jsonb_build_object('Content-Type','application/json','Authorization','Bearer ' || %L),
                    body    := '{}'::jsonb
                );
            $job$, v_edge_url, v_token)
        );
        RAISE NOTICE 'pg_cron：已创建任务 %（% UTC，每天调用一次日报端点）', v_job_name, v_schedule;
    EXCEPTION WHEN OTHERS THEN
        -- 调度失败不能回滚建表：这里吞掉异常，只留 NOTICE。
        RAISE NOTICE 'pg_cron：创建任务 % 失败（已跳过，建表不受影响）：%', v_job_name, SQLERRM;
    END;
END $do$;

COMMIT;
