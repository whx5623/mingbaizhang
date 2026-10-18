-- =============================================================================
-- 016_users_remember_token.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   登录页「记住我」：用户勾选后，浏览器 Cookie 里保存一个随机 token（UUID 原文），
--   下次打开页面可直接恢复登录，不必重新输入密码。
--   本迁移给 public.users 增加两列，保存该 token 的**哈希**与过期时刻：
--     remember_token   VARCHAR(64) NULL   -- sha256(token) 的小写十六进制，固定 64 字符
--     token_expires_at TIMESTAMPTZ NULL   -- 绝对过期时刻（UTC 时刻，无歧义）
--   db.py 的
--     set_remember_token / get_user_by_remember_token / clear_remember_token
--   依赖这两列（见 db.py 文件末尾「登录记住我」一节）。
--
--   状态语义（只有两种合法组合）：
--     (NULL, NULL)                  = 没有「记住我」会话（未勾选 / 已退出 / 已过期清理）
--     (64 位小写 hex, 非空 timestamptz) = 有一个有效的「记住我」会话
--   两列的写入/清空永远成对发生（db.py 的 UPDATE 同时写两列、同时置 NULL）。
--
--   ⚠ 一个用户同时只保留一个 remember token：再次登录会覆盖旧 token，
--     旧浏览器里的 Cookie 随之失效。这是刻意的单会话简化；
--     若将来要支持"多设备同时记住"，应改为独立的 remember_tokens 表
--     (user_id, token_hash, expires_at)，而不是在 users 上再加列。
--
-- -----------------------------------------------------------------------------
-- 【安全口径】（比"能跑通"更重要，改本文件前先读）
-- -----------------------------------------------------------------------------
--   · **库里只存 sha256(token)，Cookie 里存 UUID 原文**。
--     拖库 / 备份泄露 / SQL 注入读到 remember_token，都不能直接拿来登录：
--     sha256 是单向的，而 token 是 UUID4（122 位随机），穷举与彩虹表都不可行。
--     注意：Cookie 本身仍是 bearer 凭据（谁拿到谁能登录），所以浏览器侧
--     必须设置 Secure / SameSite=Lax（或 Strict），只在 HTTPS 下发送。
--     ⚠ 纯 JS 写 Cookie（extra-streamlit-components 的做法）通常**无法设置
--     HttpOnly**，token 对页面脚本可读 —— 因此 XSS 防护（转义、CSP）是这套
--     方案的安全前提，不能把 HttpOnly 当成已兑现的保证。
--     本迁移只负责库结构，管不到浏览器，这些由 app.py / 反向代理侧保证。
--   · 刻意不用 bcrypt：token 是高熵随机串、不是人选的密码，不需要慢哈希；
--     记住我每次页面加载都要校验一次，bcrypt 的 ~200ms 会直接拖慢首屏。
--     不加盐也是安全的：随机 token 不存在"相同密码批量撞库"的问题。
--   · 定长 64 字符（sha256 hex）既便于 CHECK 约束，也便于建唯一索引。
--   · 有效期 7 天（应用侧写入 now() + 7 days）。过期由应用在读取时清理：
--     db.py 的 get_user_by_remember_token 发现 token_expires_at <= now()
--     会顺手把该用户两列置 NULL（best-effort，清理失败只告警、不阻断登录流程）。
--   · 校验是 `WHERE remember_token = :h` 命中唯一索引；对 64 位 hex 的等值比较
--     不存在有意义的时序侧信道（bcrypt 那套恒定时间比较是为低熵密码准备的）。
--
--   ⚠ 与需求里给出的 CHECK 有一处**刻意的强化**：
--       原式： (remember_token IS NULL AND token_expires_at IS NULL)
--              OR (char_length(remember_token) = 64 AND token_expires_at IS NOT NULL)
--     在 PostgreSQL 的三值逻辑下，当 (remember_token, token_expires_at) =
--     (NULL, 非 NULL) 时整体结果是 NULL，而 CHECK 对 NULL 是**放行**的 ——
--     于是"没有 token 却有过期时间"这种半截状态能被写进去，违背列注释的约束意图。
--     所以第二支显式补上 `remember_token IS NOT NULL`，让所有非法组合都落成 FALSE。
--     （对合法数据零影响：db.py 永远成对写/清两列。）
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/016_users_remember_token.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体回滚保证，
--     中途失败会留下「有列无约束 / 有列无索引」的半完成状态。
--   ⚠ 迁移建议用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL 在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011
--          → 012 → 013 → 014 → 015 → 016（本文件）
--   ✅ 016 只依赖 public.users（001 已建），不读写 bills / login_events /
--      savings_goals / user_notifications，也不依赖 015 的 budget_details。
--      迁移仍按编号排在 015 之后执行（顺序固定，便于排障与回滚到某个编号）。
--
-- =============================================================================
-- 【备份建议】（执行前做）
-- =============================================================================
--   本脚本只**新增**两列（均可空、无默认值；PostgreSQL 11+ 只改 catalog、不重写表）、
--   一条 CHECK 约束（对现有十几行做一次校验扫描，代价可忽略）和一个部分唯一索引
--   （13 行、且 WHERE remember_token IS NOT NULL 命不中任何历史行），
--   不改动任何已有列与数据，风险很低；仍建议留一份结构备份：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > schema_before_016.sql
--   ⚠ pooler 6543 不能用于 pg_dump，请用 5432 直连串。
--   ⚠ 禁止「库内备份表」：不要 CREATE TABLE users_remember_backup AS ... 之类的库内副本 ——
--     它会被 PostgREST 暴露，也会让 pg_dump 的 schema 里多出垃圾对象。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history（历史遗留的
--   CREATE TABLE IF NOT EXISTS），**刻意不给 users 补 remember_token /
--   token_expires_at**：迁移统一走本文件，避免「应用启动顺手改 schema」与
--   「迁移脚本」两条路径互相打架。
--   没执行本文件时：
--     · db.py 的 set_remember_token / get_user_by_remember_token /
--       clear_remember_token 遇到 42703（列不存在）会抛 RuntimeError
--       （中文提示先执行 016），不会静默成功或静默返回 None；
--     · 页面据此提示"记住我功能未启用"，普通账号密码登录 / 记账 / 日报主流程
--       完全不受影响（取消勾选「记住我」即可正常使用）；
--     · 不存在"应用启动时偷偷补列"的路径，线上 schema 只由迁移改变。
--
-- =============================================================================
-- 【回滚】
-- =============================================================================
--   按「先索引、后约束、最后列」的顺序（DROP COLUMN 会自动带走列上的约束/索引，
--   但显式写出来让回滚脚本本身可读、也可单独撤掉其中一项）：
--       DROP INDEX IF EXISTS public.uq_users_remember_token;
--       ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_remember_token_pair;
--       ALTER TABLE public.users DROP COLUMN IF EXISTS remember_token;
--       ALTER TABLE public.users DROP COLUMN IF EXISTS token_expires_at;
--   回滚后 db.py 的三个函数会重新走 42703 分支抛 RuntimeError（中文提示先执行 016）；
--   普通登录不受影响。
--   ⚠ 回滚会**让所有"记住我"会话失效**（用户需要重新输密码登录）。
--     这里没有可导出恢复的数据：库里只有不可逆的 sha256，Cookie 原文只存在
--     用户浏览器里，导出哈希没有任何恢复价值（这正是"只存哈希"的目的）。
--
-- =============================================================================
-- 【幂等（重复执行安全）】
-- =============================================================================
--   · 加列：先查 information_schema.columns 打一条 NOTICE（从输出就能看出
--           "这次是真加了还是跳过了"），再 ALTER … ADD COLUMN IF NOT EXISTS
--           （列已存在时是 no-op，不会报错、不会改类型）；
--   · 约束：pg_constraint 里已有同名约束 → RAISE NOTICE 跳过；没有才 ADD CONSTRAINT
--           （PostgreSQL 的 ADD CONSTRAINT 没有 IF NOT EXISTS，必须用 DO 块判断）；
--   · 索引：先查 pg_indexes 打 NOTICE，再 CREATE UNIQUE INDEX IF NOT EXISTS
--           （索引名已存在时是 no-op）；
--   · 核对：每次都跑，任何一条不符就 RAISE EXCEPTION → 整个事务回滚；
--   · 重复执行不会改旧数据、不重写表（ADD COLUMN 可空无默认值是 catalog-only 操作）。
--   ⚠ ALTER TABLE 需要短暂的 ACCESS EXCLUSIVE 锁；开头的 SET LOCAL lock_timeout = '5s'
--     保证拿不到锁时快速失败回滚，而不是把线上写入无限期卡住。
--   ⚠ 如果库里已有一列**同名但类型不对**的 remember_token / token_expires_at
--     （例如 TEXT / TIMESTAMP），ADD COLUMN IF NOT EXISTS 不会改它，步骤 3 会
--     明确报错并回滚。正确做法是：
--       先确认/导出该列数据 → DROP COLUMN → 重跑本脚本
--     （不要手改 CHECK 去迁就错误类型，那会把"类型漂移"永久固化下来）。
--   ⚠ 如果已有同名但定义不同的 CHECK 约束 / 索引，步骤 3 同样会报错回滚；
--     先 DROP 掉错误对象再重跑，不要绕过核对。
--
-- =============================================================================

BEGIN;

-- 5 秒拿不到表锁就报错回滚，不无期限卡住线上写入。
SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（users 存在、users.id 是 integer）
--   remember_token / token_expires_at 挂在 users 上；表不存在时 ALTER 会失败
--   并留下半截事务，不如在这里给一句能直接照做的报错。
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
--   列定义必须**可空、无默认值**：NULL = 没有「记住我」会话，不是"已过期"，
--   更不是"默认已登录"。任何 DEFAULT 都会让历史用户立刻"看起来有 token"。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_has_token   boolean;
    v_has_expires boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'remember_token'
    ) INTO v_has_token;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'token_expires_at'
    ) INTO v_has_expires;

    IF v_has_token AND v_has_expires THEN
        RAISE NOTICE '步骤1：users.remember_token / token_expires_at 均已存在，跳过 ADD COLUMN（幂等）';
    ELSE
        RAISE NOTICE '步骤1：remember_token 存在=% / token_expires_at 存在=%，补齐缺失列（均 VARCHAR(64)/TIMESTAMPTZ 可空、无默认值）', v_has_token, v_has_expires;
    END IF;
END $do$;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS remember_token VARCHAR(64);

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 步骤 1（续）：加 CHECK 约束（幂等）
--   约束名固定为 users_remember_token_pair，方便核对与回滚。
--   语义：两列必须同时为空，或同时有效（token 恰好 64 字符且过期时间非空）。
--   ⚠ 第二支的 `remember_token IS NOT NULL` 是必要的：没有它时，
--     (NULL token, 非 NULL 过期时间) 会让整个表达式求值为 NULL，而 CHECK
--     对 NULL 放行 —— 详见文件头的「安全口径」说明。
--   ADD CONSTRAINT 没有 IF NOT EXISTS，用 pg_constraint 判断同名约束是否已在
--   public.users 上。若同名约束存在但定义不对，步骤 3 会报错并整体回滚。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname  = 'users_remember_token_pair'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_remember_token_pair
            CHECK (
                (remember_token IS NULL AND token_expires_at IS NULL)
                OR (
                    remember_token IS NOT NULL
                    AND char_length(remember_token) = 64
                    AND token_expires_at IS NOT NULL
                )
            );
        RAISE NOTICE '步骤1（续）：已新增 CHECK 约束 users_remember_token_pair';
    ELSE
        RAISE NOTICE '步骤1（续）：约束 users_remember_token_pair 已存在，跳过（幂等）';
    END IF;
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 2：建部分唯一索引（幂等）
--   uq_users_remember_token (remember_token) WHERE remember_token IS NOT NULL
--   · UNIQUE：同一 token 哈希不允许出现在两个用户上（正常情况下不可能，
--     但唯一索引让"重复写入/并发覆盖"变成数据库层的硬错误，而不是静默串号）；
--   · WHERE remember_token IS NOT NULL：索引只收"真的有会话"的行。
--     普通 B-tree 唯一索引本来也允许多个 NULL，这个 predicate 主要是让索引
--     保持极小（绝大多数行是 NULL），并明确表达意图。
--   · 13 行的表用普通 CREATE INDEX 即可（锁窗口极短）；若将来 users 很大，
--     应在事务外单独用 CREATE INDEX CONCURRENTLY 建，不要直接改本文件。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname  = 'uq_users_remember_token'
    ) THEN
        RAISE NOTICE '步骤2：索引 uq_users_remember_token 已存在，跳过 CREATE INDEX（幂等）';
    ELSE
        RAISE NOTICE '步骤2：新建部分唯一索引 uq_users_remember_token (remember_token) WHERE remember_token IS NOT NULL';
    END IF;
END $do$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_remember_token
    ON public.users (remember_token)
    WHERE remember_token IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 步骤 3：核对（任何一条不满足 → RAISE EXCEPTION → 整体回滚）
--   核对项：
--     · 两列存在、类型分别是 character varying(64) / timestamp with time zone、
--       可空（YES）、无默认值（NULL = 没有会话）；
--     · 约束存在、是 CHECK（contype='c'）、已校验（convalidated=true，不能是
--       NOT VALID —— NOT VALID 的 CHECK 只约束新行，等于给历史行留后门）；
--     · 约束定义与预期**逐字比对**（去空白 + 小写后的 PostgreSQL 规范化文本）。
--       不用 LIKE '%char_length%' 这种松判据：把 `>= 64` / `IS NOT NULL` 写反
--       也能命中松判据，等于没核对；
--     · 索引存在、唯一（indisunique）、有效（indisvalid）、带 predicate、
--       键列恰好只有 remember_token 一个，且 predicate 是 remember_token IS NOT NULL。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_type          text;
    v_len           integer;
    v_nullable      text;
    v_default       text;
    v_contype       text;
    v_validated     boolean;
    v_def           text;
    v_def_norm      text;
    v_idx_unique    boolean;
    v_idx_valid     boolean;
    v_idx_has_pred  boolean;
    v_idx_pred      text;
    v_idx_pred_norm text;
    v_idx_natts     smallint;
    v_idx_col       smallint;
    v_attnum        smallint;
    v_advice        text := '若列/约束/索引已存在但结构与预期不符：本脚本刻意不自动改旧结构（避免误改生产数据）。正确做法通常是先导出 remember_token 的数据（可选，哈希不可逆）、DROP INDEX IF EXISTS public.uq_users_remember_token; DROP CONSTRAINT IF EXISTS users_remember_token_pair; DROP COLUMN IF EXISTS remember_token; DROP COLUMN IF EXISTS token_expires_at; 然后重跑本脚本。';
BEGIN
    -- ①-a remember_token：character varying(64) / 可空 / 无默认值
    SELECT data_type, character_maximum_length, is_nullable, column_default
      INTO v_type, v_len, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'users'
      AND column_name  = 'remember_token';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①a未通过：public.users.remember_token 不存在（步骤1 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'character varying' THEN
        RAISE EXCEPTION '核对①b未通过：remember_token 类型是 %（应为 character varying）。%', v_type, v_advice;
    END IF;
    IF v_len IS DISTINCT FROM 64 THEN
        RAISE EXCEPTION '核对①c未通过：remember_token 长度上限是 %（应为 64 —— sha256 hex 定长）。%', coalesce(v_len::text, '<NULL>'), v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对①d未通过：remember_token is_nullable = %（应为 YES —— NULL 表示"没有记住我会话"，不允许 NOT NULL）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS NOT NULL THEN
        RAISE EXCEPTION '核对①e未通过：remember_token 有默认值 %（应为无默认值：NULL = 没有会话）。%', v_default, v_advice;
    END IF;

    -- ①-b token_expires_at：timestamp with time zone / 可空 / 无默认值
    SELECT data_type, character_maximum_length, is_nullable, column_default
      INTO v_type, v_len, v_nullable, v_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'users'
      AND column_name  = 'token_expires_at';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①f未通过：public.users.token_expires_at 不存在（步骤1 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_type IS DISTINCT FROM 'timestamp with time zone' THEN
        RAISE EXCEPTION '核对①g未通过：token_expires_at 类型是 %（应为 timestamp with time zone —— 绝对时刻，跨时区稳定）。%', v_type, v_advice;
    END IF;
    IF v_nullable IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION '核对①h未通过：token_expires_at is_nullable = %（应为 YES）。%', coalesce(v_nullable, '<NULL>'), v_advice;
    END IF;
    IF v_default IS NOT NULL THEN
        RAISE EXCEPTION '核对①i未通过：token_expires_at 有默认值 %（应为无默认值）。%', v_default, v_advice;
    END IF;

    -- ② 约束：存在、是 CHECK、已校验、定义逐字正确
    SELECT contype::text, convalidated, pg_get_constraintdef(oid)
      INTO v_contype, v_validated, v_def
    FROM pg_constraint
    WHERE conrelid = 'public.users'::regclass
      AND conname  = 'users_remember_token_pair';

    IF v_contype IS NULL THEN
        RAISE EXCEPTION '核对②a未通过：缺少约束 users_remember_token_pair。%', v_advice;
    END IF;
    IF v_contype IS DISTINCT FROM 'c' THEN
        RAISE EXCEPTION '核对②b未通过：users_remember_token_pair 的约束类型是 %（应为 c / CHECK）。%', v_contype, v_advice;
    END IF;
    IF v_validated IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对②c未通过：users_remember_token_pair 未校验（convalidated=false / NOT VALID），等于给历史行留后门。%', v_advice;
    END IF;

    v_def_norm := lower(regexp_replace(v_def, '\s+', '', 'g'));
    -- PostgreSQL 17 会把上面的 CHECK 规范化成（去空白 + 小写后）：
    --   check((((remember_tokenisnull)and(token_expires_atisnull))or((remember_tokenisnotnull)and(char_length((remember_token)::text)=64)and(token_expires_atisnotnull))))
    -- 注意 `char_length(remember_token)` 会被 deparse 成 `char_length((remember_token)::text)`
    -- （varchar → text 的隐式转换），属预期；若换了 PG 大版本导致 deparse 形式变化，
    -- 请先人工核对 pg_get_constraintdef 再更新本字符串，**不要**放宽成 LIKE。
    IF v_def_norm <> 'check((((remember_tokenisnull)and(token_expires_atisnull))or((remember_tokenisnotnull)and(char_length((remember_token)::text)=64)and(token_expires_atisnotnull))))' THEN
        RAISE EXCEPTION '核对②d未通过：users_remember_token_pair 定义是 %（应为 (NULL,NULL) 或 (NOT NULL, char_length=64, NOT NULL)）。%', v_def, v_advice;
    END IF;

    -- ③ 部分唯一索引：存在、唯一、有效、带 predicate、键列恰好是 remember_token
    SELECT i.indisunique, i.indisvalid, (i.indpred IS NOT NULL),
           pg_get_expr(i.indpred, i.indrelid),
           i.indnkeyatts, i.indkey[0]
      INTO v_idx_unique, v_idx_valid, v_idx_has_pred,
           v_idx_pred, v_idx_natts, v_idx_col
    FROM pg_index i
    JOIN pg_class c     ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname  = 'public'
      AND c.relname  = 'uq_users_remember_token'
      AND i.indrelid = 'public.users'::regclass;

    IF v_idx_unique IS NULL THEN
        RAISE EXCEPTION '核对③a未通过：缺少索引 uq_users_remember_token（步骤2 未生效）。本事务已回滚。%', v_advice;
    END IF;
    IF v_idx_unique IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对③b未通过：uq_users_remember_token 不是唯一索引（indisunique=false）。%', v_advice;
    END IF;
    IF v_idx_valid IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对③c未通过：uq_users_remember_token 无效（indisvalid=false，可能是并发建索引失败留下的残骸）。%', v_advice;
    END IF;
    IF v_idx_has_pred IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对③d未通过：uq_users_remember_token 没有 WHERE predicate（应为部分索引 WHERE remember_token IS NOT NULL）。%', v_advice;
    END IF;
    IF v_idx_natts IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION '核对③e未通过：uq_users_remember_token 键列数是 %（应恰好 1 列：remember_token）。%', coalesce(v_idx_natts::text, '<NULL>'), v_advice;
    END IF;

    SELECT attnum INTO v_attnum
    FROM pg_attribute
    WHERE attrelid = 'public.users'::regclass
      AND attname  = 'remember_token'
      AND NOT attisdropped;
    IF v_idx_col IS DISTINCT FROM v_attnum THEN
        RAISE EXCEPTION '核对③f未通过：uq_users_remember_token 的键列 attnum=% ，不是 users.remember_token（attnum=%）。%', coalesce(v_idx_col::text, '<NULL>'), coalesce(v_attnum::text, '<NULL>'), v_advice;
    END IF;

    v_idx_pred_norm := lower(regexp_replace(v_idx_pred, '\s+', '', 'g'));
    IF v_idx_pred_norm IS DISTINCT FROM '(remember_tokenisnotnull)' THEN
        RAISE EXCEPTION '核对③g未通过：uq_users_remember_token 的 predicate 是 %（应为 (remember_token IS NOT NULL)）。%', v_idx_pred, v_advice;
    END IF;

    RAISE NOTICE '核对通过：remember_token varchar(64) / token_expires_at timestamptz 均可空、无默认值；CHECK users_remember_token_pair 已校验且定义正确；部分唯一索引 uq_users_remember_token 唯一、有效、带正确 predicate';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 4：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   所以看到这一行 'OK'，就代表加列、约束、索引与核对全部通过并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK' AS result;

COMMIT;
