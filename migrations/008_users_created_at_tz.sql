-- =============================================================================
-- 008_users_created_at_tz.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   把 public.users.created_at 从 **timestamp without time zone**（naive 墙钟）
--   改成 **timestamp with time zone**（timestamptz，存绝对时刻）。
--
--   为什么必须改（这不是"洁癖"，是一个会静默算错数的口径问题）：
--     · naive timestamp 只存一串"墙上时间"数字，**没有任何时区信息**；它到底是
--       UTC 还是北京时间，取决于写入方和读取方**约定**了什么，数据库不替我们保证。
--       本库现在的实际约定是"存 UTC 墙钟"（init_db 默认 CURRENT_TIMESTAMP，
--       而 Supabase 会话/库的 TimeZone 恰好是 UTC），但这条约定只活在注释和巧合里：
--       - 谁把会话 `SET TimeZone='Asia/Shanghai'` 再 INSERT now()，写进去的就是
--         北京时间墙钟，同一列里从此混着两种口径；
--       - 读取侧一旦把 naive 和 timestamptz 混着比较，PG 会按**会话时区**把 naive
--         那一侧解释成绝对时刻 —— 同一行 SQL 在不同时区下给出不同窗口/不同日期桶。
--     · timestamptz 存的是**绝对时刻**（内部 UTC 微秒数），读出来按会话时区渲染；
--       比较、排序、跨环境/跨时区迁移都不再依赖"大家约定好用哪个时区"。
--     · 管理员注册趋势图（db.py::get_admin_operate_data）正是踩在这上面的：
--       它的 WHERE 边界与按天分桶必须用同一个基准，否则边界附近的行会偏一天。
--
--   本脚本只改**类型与默认值**，不改任何业务数据、不改其它列、不建索引。
--   转换口径：`created_at AT TIME ZONE 'UTC'` —— 把原来的 naive 墙钟**当作 UTC**
--   贴成绝对时刻。这与现有数据的真实含义一致（实测 12 行历史值都是 UTC 墙钟）。
--   ⚠ 如果某个环境的 naive 值存的其实是本地时间（如北京时间），**不要**直接跑本脚本：
--     那会把历史时刻整体平移 8 小时。先人工确认口径（见下面【执行前自检】）。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/008_users_created_at_tz.sql
--                  （PowerShell：psql $env:DATABASE_URL -f migrations\008_users_created_at_tz.sql）
--   ⚠ 必须**整段一次执行**：脚本依赖 BEGIN … COMMIT 这一个事务，逐句执行就失去了
--     "任何一步失败 → 整体回滚"的保证。
--   ⚠ 本库 DATABASE_URL 走 Supabase pooler（6543 / transaction 模式）：整段脚本会被
--     固定到同一个后端连接，事务语义正常；但**不要**在 BEGIN 与 COMMIT 之间手工断连。
--
-- =============================================================================
-- 【执行顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008（本文件），必须最后执行】
-- =============================================================================
--   001_normalize_bill_type.sql               账单类型脏值清洗
--   002_fk_cascade_delete.sql                 外键改 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql  amount 精度 + 约束
--   004_password_bcrypt.sql                   密码明文 → bcrypt + 兜底 CHECK 约束
--   005_last_login.sql                        users.last_login 列（已是 timestamptz）
--   006_user_admin_flag.sql                   users.is_admin 列
--   007_bill_date_check.sql                   bills.date 的 ISO + 日历日 CHECK
--   008_users_created_at_tz.sql（本文件）      users.created_at 改 timestamptz
--   本脚本与 001–007 互不依赖（只动 users.created_at 的类型/默认值），
--   但必须排在 005 之后：005 之后的 users 表结构才是本脚本预检里那套。
--
-- =============================================================================
-- 【⚠ 本脚本不会被自动执行（未自动执行声明）】
-- =============================================================================
--   项目里没有任何代码会执行 migrations/ 下的 .sql：
--     · db.py 的 init_db() 只跑 CREATE TABLE IF NOT EXISTS（且它对已存在的表什么都不做），
--       不会改列类型、不会改默认值；
--     · 001–007 也全靠人手工执行，本脚本保持同一约定。
--   没执行时**不会红屏**：db.py 的新查询用的是 tz-aware UTC 边界 +
--   `(created_at AT TIME ZONE 'UTC')::date` 分桶，对 naive 列照样成立
--   （见下面【与 db.py 的兼容性】）；只是仍留着"naive 墙钟含义靠约定"的隐患。
--   执行一次之后，本脚本可以重复执行：第二遍只会看到"跳过（幂等）"的 NOTICE。
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本**不改写业务值**（只做类型转换 + 最后把值逐值核对一遍），但 DDL 没有
--   事务日志可回放，改类型前仍建议先留一份数据备份：
--       pg_dump --data-only -t public.users "$DATABASE_URL" > users_data_before_008.sql
--   ⚠ Supabase pooler（6543 / transaction 模式）**不能**用于 pg_dump，请换成 5432 直连串
--     （与 005 的备份提醒同一条限制）。
--   ⛔ 不要用 `CREATE TABLE users_backup_YYYYMMDD AS SELECT * FROM users;` 这种
--      "库内备份表"：public schema 默认经 PostgREST 对外暴露、新表默认不开 RLS，
--      等于顺手造了一份可远程读取的用户副本（含 password_hash）。
--   回滚所需的全部信息只有一句 ALTER（见下）—— 数据值在转换前后相等，
--   所以真正的备份对象是**数据行**，不是结构。
--
-- =============================================================================
-- 【回滚（三句 ALTER 必须放在同一个事务里，见下）】
-- =============================================================================
--   BEGIN;
--   SET LOCAL lock_timeout = '5s';
--   -- 1) 类型回退：timestamptz → naive timestamp，按 UTC 折掉时区（与正向转换互逆，
--   --    逐值回到原值；本脚本步骤 4 已用逐值比对证明这一点）
--   ALTER TABLE public.users
--       ALTER COLUMN created_at TYPE timestamp USING created_at AT TIME ZONE 'UTC';
--   -- 2) 默认值回退（不回退就是 timestamptz 的 now() 写进 naive 列，
--   --    会按**会话时区**隐式转换，非 UTC 会话下新行整体偏 8 小时）
--   ALTER TABLE public.users
--       ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
--   -- 3) NOT NULL 回退（本脚本步骤 3.5 会把列收紧为 NOT NULL，回滚必须一起撤销，
--   --    否则与回滚后的旧结构不一致）
--   ALTER TABLE public.users
--       ALTER COLUMN created_at DROP NOT NULL;
--   COMMIT;
--   ⚠ 两条 DDL 放进同一个事务，是为了避免"改完类型、还没改默认值"的窗口里有新注册
--     把 now() 按会话时区写进 naive 列。逐句自动提交会留下这个窗口。
--   ⚠ 精度（typmod）：若原列是 `timestamp(0)` 之类的受限精度，回滚要写成
--     `TYPE timestamp(n) USING ...`（n 取执行前 information_schema 的 datetime_precision），
--     否则会回到默认 timestamp(6)。本库原列是默认精度（6），直接 TYPE timestamp 即等价。
--   ⚠ 回滚只应发生在"008 刚执行、还没依赖新口径跑过业务"的阶段：一旦有代码依赖
--     `(created_at AT TIME ZONE 'UTC')::date` 之外的写法，回滚前请先把 db.py 一起退回。
--
-- =============================================================================
-- 【与 db.py 的兼容性（为什么 008 与代码发布的先后顺序都不会算错）】
-- =============================================================================
--   新 db.py（本轮改造后）：
--     · WHERE 用 tz-aware UTC 边界、分桶用 `(created_at AT TIME ZONE 'UTC')::date`；
--     · naive 列：tz-aware 参数与 naive 列比较时按**会话 TimeZone** 解释列，
--       本库会话 TimeZone = UTC（实测），结果与 timestamptz 列一致；
--     · timestamptz 列：两边都是绝对时刻，与会话时区无关。
--   旧 db.py（本轮改造前）：naive 边界 + `created_at::date`；
--     · naive 列：原行为不变；
--     · timestamptz 列：naive 参数按会话时区（UTC）解释成绝对时刻、`::date` 也按
--       会话时区（UTC）折日，两处基准仍然一致 → 结果不变。
--   结论：**在会话 TimeZone = UTC 的前提下**，008 先跑还是代码先发都不影响结果；
--   两者都做完之后，才真正做到与会话时区无关。若某环境的库默认时区不是 UTC，
--   请先确认会话时区再执行（步骤 1 的预检会打印当前类型与值域，便于人工核对）。
--   ✅ 本轮已把这个前提**在代码里钉死**：db.py 的 create_engine 带
--      `connect_args={"options": "-c timezone=UTC"}`，应用侧每条连接都强制 UTC，
--      不再依赖"库/角色恰好是 UTC"。所以过渡态（008 未执行 + naive 列）也是安全的。
--      若你是用别的客户端（psql / SQL Editor）跑本脚本，请先 `SHOW TimeZone;` 记下当前值；
--      步骤 1.5 的锚点校验会独立证明"naive 值确实是 UTC 墙钟"。
--
-- =============================================================================
-- 【幂等（可重复执行）】
--   · 步骤 0/1：只读检查，永远执行；
--   · 步骤 2：先查 information_schema，已是 timestamptz → NOTICE 跳过，不再 ALTER；
--   · 步骤 3：SET DEFAULT 本身就是幂等的，重复执行结果相同；
--   · 步骤 4：核对永远执行，任何一条不满足 → RAISE EXCEPTION → 整体回滚。
--   临时表 `_created_at_before` 是**会话级**（pg_temp）的，事务回滚时一起消失；
--   脚本末尾显式 DROP，也不会在库里留下任何对象。
-- =============================================================================

BEGIN;

-- 第一句就设锁等待上限：ALTER TYPE 要拿 ACCESS EXCLUSIVE（会挡读写），
-- 卡在等锁上最多等 5 秒就报错回滚，绝不把 users 锁很久。
-- 用 SET LOCAL 而不是 SET：只在当前事务内生效，不会粘到 pooler 里的后续会话上。
SET LOCAL lock_timeout = '5s';

-- 先拿表锁再取快照：ALTER TYPE 本来就要 ACCESS EXCLUSIVE，这里提前拿到，
-- 保证「快照 → 改类型 → 逐值核对」三段看到的是**同一份冻结数据**。
-- 不先锁的话，快照与 ALTER 之间的并发 INSERT 会让核对结果失去意义；
-- 更隐蔽的情况是「删一行 + 插一行」行数不变，普通 JOIN 会两边都漏掉。
LOCK TABLE public.users IN ACCESS EXCLUSIVE MODE;


-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（表在不在）
--   to_regclass 对"不存在的表"返回 NULL 而不是抛异常，所以可以自己给一句
--   能看懂的中文提示，而不是让人对着 `42P01 relation does not exist` 猜。
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.users 不存在（to_regclass 返回 NULL）。请先启动一次应用让 db.init_db() 建表，或确认连接串指向的是正确的库/schema。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：public.users 存在，继续';
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 1：预检（给人眼核对用）+ 冻结转换前的值
--   ① 当前 data_type / 行数 / MIN/MAX(created_at) —— 先看清"改之前长什么样"；
--   ② 把 (id, created_at) 冻结进**会话级临时表**：后面 ALTER 完要证明
--      "每一行的值都没变"，靠的就是这份快照。
--      临时表只活在本会话（pg_temp schema），不影响 public，也不经 PostgREST 暴露。
--   快照里多存一列 epoch_utc（绝对刻度，微秒精度），原因：
--     · naive 列改完之后，`(u.created_at AT TIME ZONE 'UTC') <> b.created_at`
--       是规格里的口径，成立的前提是"快照那一侧还是 naive"；
--     · 但本脚本要**可重复执行**：第二遍时快照里的列已经是 timestamptz，
--       再用上面那句比 timestamp vs timestamptz，PG 会按**会话时区**隐式转换
--       naive 那一侧 —— 非 UTC 会话下就会报出假差异。
--     · `extract(epoch …)` 对两种类型都给出**与会话时区无关**的同一刻度
--       （实测：UTC / Asia/Shanghai / America/New_York 三个会话下同一 naive 值都是
--        同一个 epoch），所以它才是"逐值相等"的通用判据；步骤 4 里两种判据都会跑。
-- ---------------------------------------------------------------------------

SELECT c.data_type             AS current_data_type,
       (SELECT count(*) FROM public.users)        AS users_rows,
       (SELECT min(created_at) FROM public.users) AS min_created_at,
       (SELECT max(created_at) FROM public.users) AS max_created_at
FROM information_schema.columns c
WHERE c.table_schema = 'public'
  AND c.table_name   = 'users'
  AND c.column_name  = 'created_at';

-- 防一手"同一后端会话里跑过一遍、临时表还在"：只在 pg_temp 里存在同名表时才 DROP。
-- （不用不带 schema 的 `DROP TABLE IF EXISTS _created_at_before`：那样一旦 pg_temp 里没有、
--   public 里却有人恰好建了同名表，就会把**别人的表**删掉。pg_temp 前缀把作用域钉死。）
DO $$
BEGIN
    IF to_regclass('pg_temp._created_at_before') IS NOT NULL THEN
        EXECUTE 'DROP TABLE pg_temp._created_at_before';
    END IF;
END $$;

CREATE TEMP TABLE _created_at_before AS
SELECT id,
       created_at,
       extract(epoch FROM created_at) AS epoch_utc
FROM public.users;


-- ---------------------------------------------------------------------------
-- 步骤 1.5：**自证"naive 墙钟确实是 UTC"**（本脚本唯一能证伪核心假设的地方）
--   为什么需要这一步：步骤 4 的"逐值不变"其实是**恒等式**——
--     extract(epoch FROM naive) 的定义就是"当成 UTC 算刻度"，而转换本身是
--     `AT TIME ZONE 'UTC'`，所以两者必然相等。它只能证明"DDL 没改数字"，
--     无法证明"这些数字本来就是 UTC"。
--   能证伪的独立证据是 users.last_login：注册/登录时 created_at 与 last_login
--   是同一事务里的 now()，如果 created_at 存的其实是本地时间（如 UTC+8），
--   那么 `(created_at AT TIME ZONE 'UTC')` 会**晚于** last_login（最多晚一个时区偏移），
--   且刚注册的用户会"来自未来"。
--   判定：
--     · bad > 0（created_at 比 last_login 晚超过 1 分钟）→ 中止：口径错了，不能转；
--     · future > 0（created_at 晚于 now()+1 分钟）→ 中止：同一类问题；
--     · 行数 > 0 但 anchors = 0（没有任何账号登录过，无绝对时间锚点）→ 中止，
--       要求人工确认后显式放行：先执行
--         SELECT set_config('app.ignore_anchor_check', '1', false);
--       再重跑本脚本（会话级开关，只影响当前连接）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_total     bigint;
    v_anchors   bigint;
    v_bad       bigint;
    v_min_delta numeric;
    v_future    bigint;
    v_bad_ids   text;
    v_override  text := current_setting('app.ignore_anchor_check', true);
    v_has_ll    boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'last_login'
    ) INTO v_has_ll;
    IF NOT v_has_ll THEN
        RAISE EXCEPTION '步骤1.5 中止：public.users.last_login 不存在（锚点校验需要它）。请先执行 005_last_login.sql。本事务未做任何改动';
    END IF;

    SELECT count(*) INTO v_total FROM public.users;

    SELECT count(*) FILTER (WHERE last_login IS NOT NULL),
           count(*) FILTER (WHERE last_login IS NOT NULL
                              AND (created_at AT TIME ZONE 'UTC') > last_login + interval '1 minute'),
           min(extract(epoch FROM last_login - (created_at AT TIME ZONE 'UTC')))
               FILTER (WHERE last_login IS NOT NULL)
      INTO v_anchors, v_bad, v_min_delta
    FROM public.users;

    SELECT count(*) INTO v_future
    FROM public.users
    WHERE (created_at AT TIME ZONE 'UTC') > now() + interval '1 minute';

    IF v_bad > 0 THEN
        SELECT string_agg(format('id=%s created_at=%s last_login=%s', s.id, s.created_at, s.last_login), chr(10))
          INTO v_bad_ids
        FROM (
            SELECT id, created_at, last_login
            FROM public.users
            WHERE last_login IS NOT NULL
              AND (created_at AT TIME ZONE 'UTC') > last_login + interval '1 minute'
            ORDER BY id
            LIMIT 5
        ) s;
        RAISE EXCEPTION '步骤1.5 中止：% 行的 created_at 晚于 last_login，说明 naive 值**不是** UTC 墙钟（很可能是本地时间）。按 UTC 转换会让历史时刻整体漂移，本事务已整体回滚。样本：%',
            v_bad, chr(10) || coalesce(v_bad_ids, '（样本收集失败）');
    END IF;

    IF v_future > 0 THEN
        RAISE EXCEPTION '步骤1.5 中止：% 行的 (created_at AT TIME ZONE ''UTC'') 晚于现在，naive 值不可能是 UTC 墙钟。本事务已整体回滚，请先人工确认时区口径', v_future;
    END IF;

    IF v_total > 0 AND v_anchors = 0 THEN
        IF coalesce(v_override, '') = '1' THEN
            RAISE WARNING '步骤1.5：% 行用户没有任何 last_login 锚点，已按人工放行（app.ignore_anchor_check=1）继续。请确认你已人工核实 naive 值存的是 UTC', v_total;
        ELSE
            RAISE EXCEPTION '步骤1.5 中止：% 行用户里没有任何 last_login 绝对时间锚点，数据库内无法自证 created_at 存的是 UTC。请人工确认口径；确认后先执行 SELECT set_config(''app.ignore_anchor_check'', ''1'', false); 再重跑本脚本。本事务未做任何改动', v_total;
        END IF;
    END IF;

    RAISE NOTICE '步骤1.5 锚点校验通过：last_login 锚点 % 个，异常行 % 行，最小时差 % 秒（>=0 表示 created_at 不晚于登录时刻）', v_anchors, v_bad, v_min_delta;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 2：幂等改类型
--   已是 timestamp with time zone → NOTICE 跳过（绝不重复 ALTER，重复 ALTER 虽不报错，
--     但每次都要拿 ACCESS EXCLUSIVE 锁 + 重写全表，大表上纯属自找阻塞）；
--   是 timestamp without time zone → 按 UTC 贴标签转换（本库历史值的真实含义）；
--   其它类型 → 直接中止：`AT TIME ZONE` 只对时间类型成立，硬转会报一句
--     "cannot cast type …"，不如给出"这列不是预期类型，请人工确认"的说明。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_type text;
BEGIN
    SELECT data_type INTO v_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'created_at';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '步骤2 中止：public.users.created_at 列不存在。请先确认 001–007 的执行状态与表结构';
    END IF;

    IF v_type = 'timestamp with time zone' THEN
        RAISE NOTICE '步骤2 跳过（幂等）：public.users.created_at 已是 timestamp with time zone，不再 ALTER';
    ELSIF v_type = 'timestamp without time zone' THEN
        ALTER TABLE public.users
            ALTER COLUMN created_at TYPE TIMESTAMPTZ
            USING created_at AT TIME ZONE 'UTC';
        RAISE NOTICE '步骤2：public.users.created_at 已由 timestamp without time zone 改为 timestamp with time zone（原 naive 墙钟按 UTC 解释）';
    ELSE
        RAISE EXCEPTION '步骤2 中止：public.users.created_at 的类型是 %（预期 timestamp without time zone 或 timestamp with time zone）。本脚本不会猜测如何转换这种类型，请人工确认后处理。本事务已整体回滚', v_type;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 3：幂等改默认值
--   原默认值 CURRENT_TIMESTAMP 在新类型下虽然也能用，但这里统一钉成 now()：
--   now() 与 CURRENT_TIMESTAMP 语义等价（都是事务开始时刻的 timestamptz），
--   写成 now() 是为了让"默认值"与 db.py::init_db 的建表语句**逐字同口径**。
--   SET DEFAULT 本身幂等，无需判断"已经是 now() 就跳过"。
-- ---------------------------------------------------------------------------
ALTER TABLE public.users ALTER COLUMN created_at SET DEFAULT now();


-- ---------------------------------------------------------------------------
-- 步骤 3.5：收紧为 NOT NULL（与 db.py::init_db 新库口径对齐）
--   为什么必须补：init_db 的新库建表是 `TIMESTAMPTZ NOT NULL DEFAULT now()`，
--   若 008 只改类型不改可空性，老库会永久 nullable —— 同一份代码在新库/老库行为不同，
--   schema dump 也永久漂移。先 fail-closed 查 NULL，有 NULL 就整事务回滚（不猜怎么填），
--   让人先决定补值方案。SET NOT NULL 对已 NOT NULL 的列是 no-op，天然幂等。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_null bigint;
BEGIN
    SELECT count(*) INTO v_null FROM public.users WHERE created_at IS NULL;
    IF v_null > 0 THEN
        RAISE EXCEPTION '步骤3.5 中止：% 行 created_at IS NULL。请先决定补值方案（例如回填为 created_at 的合理近似）再重跑本脚本；本事务已整体回滚', v_null;
    END IF;
    ALTER TABLE public.users ALTER COLUMN created_at SET NOT NULL;
    RAISE NOTICE '步骤3.5：created_at 已收紧为 NOT NULL（与 init_db 新库口径一致）';
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 4：执行后核对（任何一条不满足 → RAISE EXCEPTION → 整个事务回滚）
--   ① 类型是 timestamptz；
--   ② column_default 含 now()；
--   ③ **值不变**（这是本脚本最重要的断言，DDL 改类型最容易在这里出事）：
--      ③a 行数不变（没多一行、没少一行）；
--      ③b 逐值相等（epoch 口径，与会话时区无关）；
--      ③c 逐值相等（规格口径：`(created_at AT TIME ZONE 'UTC') <> b.created_at`，
--          只在"转换前是 naive"时执行 —— 重复执行时快照已是 timestamptz，
--          这个表达式会按会话时区隐式转换，失去可比性）。
--   核对通过后，RAISE NOTICE + 末尾 SELECT 'OK' 才是"真的做完了"的证据；
--   失败时事务已经回滚，改类型/改默认值都会一并撤销。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_type        text;
    v_default     text;
    v_rows        bigint;
    v_rows_before bigint;
    v_diff        bigint;
    v_diff_naive  bigint;
    v_before_type text;
    v_was_tz      boolean;
    v_nullable    text;
    v_bad_ids     text;
BEGIN
    -- 转换前快照那一列的类型，用来判断该不该跑"规格口径"的复核
    SELECT format_type(a.atttypid, a.atttypmod) INTO v_before_type
    FROM pg_attribute a
    WHERE a.attrelid = 'pg_temp._created_at_before'::regclass
      AND a.attname  = 'created_at'
      AND NOT a.attisdropped;
    v_was_tz := coalesce(v_before_type = 'timestamp with time zone', false);

    -- ① 类型
    SELECT data_type INTO v_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'created_at';
    IF v_type IS DISTINCT FROM 'timestamp with time zone' THEN
        RAISE EXCEPTION '核对①未通过：users.created_at 的类型是 %（应为 timestamp with time zone），事务已回滚', coalesce(v_type, '<列不存在>');
    END IF;

    -- ② 默认值
    SELECT column_default INTO v_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'created_at';
    IF v_default IS NULL OR v_default NOT LIKE '%now()%' THEN
        RAISE EXCEPTION '核对②未通过：users.created_at 的 column_default = %（应含 now()），事务已回滚', coalesce(v_default, '<NULL>');
    END IF;

    -- ②b 可空性：必须与 init_db 新库口径一致（NOT NULL）
    SELECT is_nullable INTO v_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'created_at';
    IF v_nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION '核对②b未通过：users.created_at 的 is_nullable = %（应为 NO），事务已回滚', coalesce(v_nullable, '<NULL>');
    END IF;

    -- ③a 行数不变
    SELECT count(*) INTO v_rows FROM public.users;
    SELECT count(*) INTO v_rows_before FROM pg_temp._created_at_before;
    IF v_rows <> v_rows_before THEN
        RAISE EXCEPTION '核对③a未通过：users 行数由 % 变成 %，事务已回滚', v_rows_before, v_rows;
    END IF;

    -- ③b 逐值相等（epoch 口径）+ 行集合对称差
    --    用 FULL OUTER JOIN 而不是 JOIN：若并发下"删一行 + 插一行"导致行数不变，
    --    普通 JOIN 会把两侧各自缺失的那行都漏掉、静默放过；对称差能抓住。
    SELECT count(*) INTO v_diff
    FROM (
        SELECT coalesce(u.id, b.id) AS id,
               extract(epoch FROM u.created_at) AS epoch_now,
               b.epoch_utc
        FROM public.users u
        FULL OUTER JOIN pg_temp._created_at_before b USING (id)
    ) s
    WHERE s.epoch_now IS DISTINCT FROM s.epoch_utc;

    IF v_diff <> 0 THEN
        SELECT string_agg(format('id=%s 快照epoch=%s 当前epoch=%s',
                                 s.id, s.epoch_utc::text, s.epoch_now::text), chr(10))
          INTO v_bad_ids
        FROM (
            SELECT coalesce(u.id, b.id) AS id,
                   extract(epoch FROM u.created_at) AS epoch_now,
                   b.epoch_utc
            FROM public.users u
            FULL OUTER JOIN pg_temp._created_at_before b USING (id)
            WHERE extract(epoch FROM u.created_at) IS DISTINCT FROM b.epoch_utc
            ORDER BY coalesce(u.id, b.id)
            LIMIT 5
        ) s;
        RAISE EXCEPTION '核对③b未通过：% 行 created_at 的值/行集合在转换前后不一致（绝对刻度变了，或行被增删），事务已回滚。样本：%.本事务已整体回滚', v_diff, chr(10) || coalesce(v_bad_ids, '（样本收集失败）');
    END IF;

    -- ③c 规格口径复核（仅转换前为 naive 时；此时 (timestamptz AT TIME ZONE 'UTC')
    --    得到的 naive 值应与快照里的 naive 值**逐微秒相等**）
    IF NOT v_was_tz THEN
        SELECT count(*) INTO v_diff_naive
        FROM public.users u
        JOIN pg_temp._created_at_before b USING (id)
        WHERE (u.created_at AT TIME ZONE 'UTC') IS DISTINCT FROM b.created_at;

        IF v_diff_naive <> 0 THEN
            RAISE EXCEPTION '核对③c未通过：% 行 (created_at AT TIME ZONE ''UTC'') 与原 naive 值不相等，事务已回滚', v_diff_naive;
        END IF;
    END IF;

    RAISE NOTICE '步骤4 通过：类型=%，default=%，行数=%（不变），逐值比对 0 行差异（epoch 口径 + AT TIME ZONE ''UTC'' 口径）', v_type, v_default, v_rows;
END $$;


-- 临时表用完必须清掉：它是会话级的，虽然事务结束也会消失，
-- 但显式 DROP 让会话里不留任何对象（尤其是 pooler 复用连接时）。
DROP TABLE IF EXISTS pg_temp._created_at_before;


-- 最后一行给人肉眼确认：看到 OK 就说明前面每一步都过了（否则事务已因 EXCEPTION 回滚）
SELECT 'OK' AS result;

COMMIT;
