-- =============================================================================
-- 006_user_admin_flag.sql
-- 注：2026-10-17 仅更新注释（函数改名），SQL 未改动，本脚本已于生产执行。
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.users 加一列 is_admin BOOLEAN NOT NULL DEFAULT FALSE，
--   把"谁是管理员"落成**数据**，而不是靠代码里的猜测。
--
--   背景（一次真实的口径漏洞）：
--     · app.py 里的管理面板（图表 + 漏斗 + 反馈列表）代码一直都在、
--       db.py 的 get_admin_operate_data() / get_admin_funnel_data() 也一直在查数据；
--     · 但 app.py 只对 st.session_state.is_admin 赋过 False，**从来没有任何一处赋 True**，
--       于是面板永远进不去，等于一大块死代码；
--     · 于是也没有任何"增量风险"可言 —— 谁都能看出来它没在用它，
--       也就没人去核对它里面那些只在"面板可达"时才会暴露的问题
--       （重复定义、DATE() 包列、date 对象直接塞给 echarts）。
--   本脚本负责"定义管理员"这一半；另一半在 db.py（同一次发布的代码）：
--     · 新增 get_user_auth_state(user_id) -> (存在, 是否管理员)  鉴权查询的唯一入口（默认无权限）；
--     · 修 get_admin_operate_data()          删掉重复定义（后者覆盖前者、返回 3 个 int，
--                                            而 app.py 按 DataFrame 用 ["count"] → TypeError）
--                                            并把 SQL 改成可索引、不依赖逐行 cast 的写法。
--   本脚本**只加列、不改任何业务数据**，也**不新增任何"设为管理员"的应用接口**：
--   指定管理员是**一次性运营动作**，走下面那段手工 SQL（见【如何指定管理员】），
--   不放进产品页面 —— 一个能被点出来的"把某人升成管理员"按钮，等于给自己开一个
--   提权后门，而本项目连 CSRF/审计日志都没有。
--
-- =============================================================================
-- 【⚠ 默认所有人 FALSE —— 升级后必须手工指定管理员】
-- =============================================================================
--   ADD COLUMN ... DEFAULT FALSE 会把**已经存在的每一行**都填成 FALSE（含 id=1 这类老账号）。
--   也就是说：本脚本执行完之后，系统里**没有任何管理员**，管理面板对所有人都不可见。
--   这是**刻意**的（默认最小权限：鉴权失败必须落成"无权限"，不能落成"有权限"）。
--
--   也**不要**把规则改成"第一个注册的用户 / id 最小的用户自动当管理员"：
--     · 本项目历史上 id=1 是测试期随手注册的 test1（见当前库），把一个测试账号
--       自动升成管理员，等于把后台数据交给一个谁知道密码在哪儿的账号；
--     · "谁先注册谁是管理员"在多环境（测试库/生产库）里必然选错人，
--       而且它是一个**隐式**规则 —— 半年后没人记得管理员是怎么来的；
--     · 反过来想：真管理员是运营/开发明确指定的那一个人，一条 UPDATE 就够了，
--       何苦写一条会自动选错人的规则。
--
-- =============================================================================
-- 【如何指定管理员（本脚本不含，需你手工执行）】
-- =============================================================================
--   只认 username，一次一条，别写 WHERE 1=1。**用带断言的 DO 块，不要裸 UPDATE**：
--   UPDATE 命中 0 行不会报错，只会**静默**什么都不做 —— 打错一个字母就以为设好了。
--
--       -- 指定管理员（用户名不存在会直接报错，而不是静默成功）
--       DO $$
--       BEGIN
--           UPDATE public.users SET is_admin = TRUE WHERE username = '你希望当管理员的用户名';
--           IF NOT FOUND THEN
--               RAISE EXCEPTION '用户名不存在，设置未生效：%', '你希望当管理员的用户名';
--           END IF;
--       END $$;
--
--       -- 撤销管理员（同样带断言；要撤销一个本来就不是管理员的账号会报错，属预期）
--       DO $$
--       BEGIN
--           UPDATE public.users SET is_admin = FALSE WHERE username = '不再需要的用户名';
--           IF NOT FOUND THEN
--               RAISE EXCEPTION '用户名不存在，撤销未生效：%', '不再需要的用户名';
--           END IF;
--       END $$;
--
--   （`FOUND` 是 PL/pgSQL 的内置变量：上一条 SQL 影响行数 > 0 时为真，不改 0 行时为假。
--     所以这两段既能防"用户名打错"，也能防"该用户其实不在这个库/这个 schema 里"。）
--   执行前先确认目标账号存在（裸 UPDATE 的静默陷阱就出在这一步被跳过）：
--       SELECT id, username, is_admin FROM public.users WHERE username = '你希望当管理员的用户名';
--   ⚠ 执行后**必须**验证名单（这一行就是唯一权威的"当前管理员名单"）：
--       SELECT id, username FROM public.users WHERE is_admin ORDER BY id;
--     看到那个 username 且只有它，才算设置成功；空的就说明没生效，回去查库/schema/拼写。
--
--   ⚠ 不要把 password_hash 之类带进任何巡检/导出的 SELECT —— 管理名单只需要 username。
--   ⚠ 撤销之后什么时候生效，取决于 app.py：它每次 rerun 都会重新问一次 get_user_auth_state
--     （db.py 这一侧不缓存任何管理员身份）；所以撤销对"下一次交互/刷新"生效，
--     而不是立刻掐断一个正在浏览的会话。要立刻踢人，请让对方重新登录。
--
-- =============================================================================
-- 【执行顺序：001 → 002 → 003 → 004 → 005 → 006（本文件），必须最后执行】
-- =============================================================================
--   001_normalize_bill_type.sql               账单类型脏值清洗
--   002_fk_cascade_delete.sql                 外键改 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql  amount 精度 + 约束
--   004_password_bcrypt.sql                   密码明文 → bcrypt + 兜底 CHECK 约束
--   005_last_login.sql                        users.last_login 列 + 索引
--   006_user_admin_flag.sql（本文件）          users.is_admin 列
--   理由：本脚本与 001–005 **没有数据依赖**（不读不写业务行，只加一列），
--   单独跑也不会冲突；排在最后只是为了让"生产库结构 = 迁移链的最终状态"可复现。
--   与 005 的关系：两列互不相干；006 不引用 last_login，005 也不引用 is_admin。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/006_user_admin_flag.sql
--                  （PowerShell：psql $env:DATABASE_URL -f migrations\006_user_admin_flag.sql）
--   ⚠ 必须**整段一次执行**，不要逐句执行：脚本依赖 BEGIN … COMMIT 这一个事务。
--     逐句执行（每条自动提交）会失去"失败整体回滚"的保证。
--   ⚠ 本库的 DATABASE_URL 走 Supabase pooler（端口 6543 / transaction 模式）：
--     显式 BEGIN … COMMIT 的整段脚本会被固定到同一个后端连接上，事务语义正常；
--     但**不要**在中间手工断开/重连，也不要把断点调试夹在 BEGIN 与 COMMIT 之间。
--
-- =============================================================================
-- 【⚠ 本脚本不会被自动执行（未自动执行声明）】
-- =============================================================================
--   项目里没有任何代码会在启动时执行它：
--     · db.py 的 init_db() 只跑 CREATE TABLE IF NOT EXISTS，**不会**给已存在的表补列
--       （对老库来说 CREATE TABLE ... is_admin 那半句等于什么都没做）；
--     · migrations/ 下的 .sql 全靠人手工执行（001–005 也都是这样跑过来的）。
--   因此：加列必须由人执行本文件。
--
-- 【没执行时的行为 —— 优雅降级，不是红屏】
--   db.py 的 get_user_auth_state() 捕获 ProgrammingError（列不存在），记一条 warning
--   （"is_admin 列不存在，请先执行 migrations/006_user_admin_flag.sql"）并返回 (存在, False)：
--     · 页面不会报错、不会红屏；管理面板对所有人不可见（等价于"没有管理员"）；
--     · 普通记账/统计功能完全不受影响（它们不引用 is_admin）。
--   这是刻意的取舍：鉴权失败只能落成"无权限"，绝不能落成"有权限"。
--   （对比 005：那次不执行会让注册 100% 失败，所以顺序更紧张；本次是安静的降级。）
--
-- =============================================================================
-- 【备份建议】
-- =============================================================================
--   本脚本只做 ADD COLUMN，**不写、不删、不改任何业务数据**，所以数据级备份不是必需的。
--   仍然建议留一份执行前的**结构**快照，只用于回滚结构：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > users_schema_before_006.sql
--       （Supabase 的 pooler 连接串端口 6543 / transaction 模式通常不能用于 pg_dump，
--        请改用 5432 直连串：Dashboard → Database → Connection string → Direct）
--   如果你已经手工指定过管理员，撤销/重启前**先留一份名单**（只导 username，不导哈希）：
--       psql "$DATABASE_URL" -c "SELECT id, username FROM public.users WHERE is_admin ORDER BY id;"
--   ⛔ 不要用 `CREATE TABLE public.users_backup_YYYYMMDD AS SELECT * FROM public.users;`
--      这种"库内备份表"：public schema 默认经 PostgREST 对外暴露、新建表默认**不开 RLS**，
--      等于顺手造了一份可远程读取的用户表副本（含全部 password_hash）。
--
-- =============================================================================
-- 【为什么 NOT NULL DEFAULT FALSE，以及"不重写整表"到底成不成立】
-- =============================================================================
--   · NOT NULL + DEFAULT FALSE：这一列从诞生起就有确定的值，读的地方不需要
--     `COALESCE(is_admin, FALSE)` 这种兜底（db.py 里仍然写了 COALESCE，是第二层保险）；
--     FALSE 是**安全的默认**：默认无人有权限。
--   · 性能：PG 11+ 对**常量**默认值做的是**元数据操作**（只把缺省值写进
--     pg_attribute.attmissingval），**不重写整张表、不逐行写入**；
--     所以"加大表要小心 DEFAULT 会重写全表"是 PG 10 及更早的旧说法，在这里不成立。
--     （反例才需要小心：DEFAULT now() / random() 这类**易变（volatile）**默认值，
--       必须逐行求值，才可能触发重写。）
--   · 本库 users 只有 2 行，这一点在本项目里甚至无关紧要；写清楚是为了别让下一个人
--     抄着"加列会锁表很久"的旧结论去做无谓的停机窗口。
--   · 锁：本事务里的 ADD COLUMN 取 ACCESS EXCLUSIVE 锁，**持有到 COMMIT**；
--     配合 BEGIN 之后第一句的 lock_timeout = '5s'，等锁最多 5 秒就报错回滚。
--
-- 【可重复执行（幂等）】
--   · 步骤 0：前置检查 public.users 是否存在（to_regclass，只读）；不存在就直接
--            RAISE EXCEPTION 给出人话提示，整个事务回滚，不再往下跑；
--   · 步骤 1：纯只读预检，永远执行；
--   · 步骤 2：先查 information_schema.columns，已有 is_admin → 只 NOTICE 跳过；
--            否则 ADD COLUMN **IF NOT EXISTS**（第二层保险，防并发会话同时加列）；
--   · 步骤 3：核对**永远执行**，任何一条不满足就 RAISE EXCEPTION → 整个事务回滚。
--   所以跑第二遍只会看到"跳过（幂等）"的 NOTICE，库结构不会变、数据不会动。
--   注意：幂等指的是"结构"，**不是"数据"** —— 重跑**不会**把 is_admin 重置成 FALSE，
--   你手工指定的管理员会原样保留（这是刻意的：迁移脚本没有资格撤销授权）。
--
-- =============================================================================
-- 【回滚（结构级）】
-- =============================================================================
--   ALTER TABLE public.users DROP COLUMN IF EXISTS is_admin;
--   （自动提交即可；会连带丢掉已经指定的管理员名单 —— 回滚前先想清楚要不要留名单。）
--   DROP 之后 db.py 的 get_user_auth_state() 会走 ProgrammingError 分支返回 (存在, False)（管理面板隐藏），
--   不需要同步改代码，也不会红屏。
-- =============================================================================

BEGIN;

-- 第一句就设锁等待上限：任何 DDL 卡在等锁上都最多等 5 秒，然后**报错并回滚整个事务**。
-- 拿到 "canceling statement due to lock timeout" 时的正确做法是稍后重试；
-- 调大超时只会让你的会话更久地锁着表、让排队的读写更多。
-- 用 SET LOCAL 而不是 SET：只在当前事务内生效，事务结束自动失效，
-- 不会把 lock_timeout 粘到连接池（Supabase pooler）里的后续会话上。
SET LOCAL lock_timeout = '5s';


-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查 —— public.users 到底在不在
--   为什么要有这一步：本脚本所有语句都建在 public.users 之上。如果表根本不存在
--   （新克隆的库还没启动过应用、或连到了别的库/schema），裸跑本脚本会得到一串
--   底层报错（`relation "public.users" does not exist` /
--   `relation "users" does not exist`），看不出"我该先干什么"。
--   这里提前用 to_regclass 探一下（表不存在时返回 NULL 而**不报错**，这是它比
--   'public.users'::regclass 更安全的地方），给出人话提示后回滚，不再往下跑。
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION 'public.users 不存在，请先启动应用或用 000/初版 DDL 建表';
    END IF;
    RAISE NOTICE '步骤0：public.users 存在，继续';
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 1（A3）：执行前预检 —— 纯只读，不改任何东西
--   ① users 总行数                  → 知道这次 ADD COLUMN 的规模
--   ② is_admin 列是否已存在          → 判断这是首跑还是重跑
--   ③ 当前管理员名单（**只列 username**，绝不显示 password_hash）
--      首跑：列还不存在，本块会 NOTICE 出来（不能直接 SELECT is_admin，否则整段编译失败）；
--      重跑：给出实际名单。
--   ⚠ 一次 Run 多条语句时，SQL Editor 通常只展示**最后一个**结果集，
--     所以前两个 SELECT 很可能"看不见"；用 psql -f 执行，或把本段单独另开一个 query 跑。
--     名单本身同时写进 NOTICE，保证不至于完全看不到。
-- ---------------------------------------------------------------------------

SELECT COUNT(*) AS users_total FROM public.users;

SELECT COUNT(*) AS is_admin_column_exists
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'users'
  AND column_name  = 'is_admin';

DO $$
DECLARE
    v_has_column boolean;
    v_admin_cnt  bigint;
    v_admins     text;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'is_admin'
    ) INTO v_has_column;

    IF NOT v_has_column THEN
        RAISE NOTICE '步骤1 预检：public.users 目前**没有** is_admin 列（首次执行的正常状态），本次将新增它；升级后默认全员 FALSE，需要手工指定管理员';
        RETURN;
    END IF;

    -- 列存在时才能引用它，所以必须动态 SQL（否则本块在首次执行时就无法编译）。
    -- 只取 username：管理名单不需要、也不该碰 password_hash。
    EXECUTE $q$
        SELECT COUNT(*), COALESCE(string_agg(username, ', ' ORDER BY id), '（无）')
        FROM public.users
        WHERE is_admin
    $q$ INTO v_admin_cnt, v_admins;

    RAISE NOTICE '步骤1 预检：public.users.is_admin 列已存在（重跑），当前管理员 % 人：%', v_admin_cnt, v_admins;
END $$;

-- 重跑时想直接看到名单（列已存在才能执行；只取 username / id，不含密码）：
--   SELECT id, username FROM public.users WHERE is_admin ORDER BY id;


-- ---------------------------------------------------------------------------
-- 步骤 2（A1）：幂等加列
--   判据：information_schema.columns 里有没有 public.users.is_admin。
--     · 不存在 → ALTER TABLE ... ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
--     · 已存在 → 只 NOTICE，**不动它**（不改类型、不重置数据）。
--       如果它被人手工加成了别的类型 / 可空 / 别的默认值，本脚本**不擅自替你改**，
--       而是由步骤 3 的核对 RAISE EXCEPTION 拦下，让人来决定怎么处置 ——
--       改类型或补 NOT NULL 都可能悄悄改动已有授权数据（比如把 NULL 折成 FALSE 就等于
--       撤销了一个人的权限），不该由迁移脚本自动做。
--   为什么 NOT NULL DEFAULT FALSE：见头部【为什么 NOT NULL DEFAULT FALSE】——
--   常量默认值在 PG 11+ 是元数据操作，不重写整表。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_has_column boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'is_admin'
    ) INTO v_has_column;

    IF v_has_column THEN
        RAISE NOTICE '步骤2 跳过（幂等）：public.users.is_admin 已存在，不重复 ADD COLUMN，也不改动已有值';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE public.users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE';

    -- 报告"真实结果"，而不是"我以为做了什么"：
    -- 并发场景下 IF NOT EXISTS 可能什么也没加（另一个会话刚好同时加成功）。
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'is_admin'
    ) INTO v_has_column;

    IF v_has_column THEN
        RAISE NOTICE '步骤2：已新增列 public.users.is_admin BOOLEAN NOT NULL DEFAULT FALSE（老行一律 FALSE，默认无人是管理员）';
    ELSE
        RAISE EXCEPTION '步骤2 失败：执行 ADD COLUMN 之后仍查不到 public.users.is_admin，表结构异常，事务已回滚';
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 3（A4）：执行后核对（任何一条不满足就 RAISE EXCEPTION → 整个事务回滚）
--   ① 列存在；
--   ② 类型恰为 boolean（information_schema.data_type = 'boolean'）——
--      不接受 smallint / text('true'/'false'/'1'/'0'/'yes')：那类"伪布尔"会让
--      `WHERE is_admin` 静默变成"非零即真"，'false' 这种字符串也是真值 → 人人都是管理员；
--   ③ NOT NULL（is_nullable = 'NO' 且 pg_attribute.attnotnull = true）——
--      NULL 的三值逻辑会让 `WHERE is_admin` 直接漏掉这些行，"没有权限"和"不知道"
--      混在一起，鉴权查询要靠 COALESCE 兜底，迟早有人漏写；
--   ④ 默认值确实是 false（column_default 文本里含 false）——
--      否则以后任何 `INSERT INTO users(username, password_hash)` 都会失败或落成 NULL；
--   ⑤ 没有 NULL 值（COUNT(*) WHERE is_admin IS NULL = 0）——
--      双重确认：本列刚加、不可能有 NULL；这里是给"列本来是别人手工加的、
--      曾可空、历史行有 NULL"那种情况兜底。鉴权绝不接受"未知"这个第三态。
--   末尾 SELECT 'OK' AS result：看到 OK 才算前面每一步都过了（失败时事务已回滚）。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_type       text;
    v_nullable   text;
    v_attnotnull boolean;
    v_default    text;
    v_null_cnt   bigint;
BEGIN
    -- ① + ② + ③ + ④：列存在性、类型、可空性、默认值一并取出
    SELECT c.data_type, c.is_nullable, a.attnotnull, c.column_default
      INTO v_type, v_nullable, v_attnotnull, v_default
    FROM information_schema.columns c
    JOIN pg_attribute a
      ON a.attrelid = to_regclass('public.users')
     AND a.attname  = c.column_name
     AND a.attnum   > 0
    WHERE c.table_schema = 'public'
      AND c.table_name   = 'users'
      AND c.column_name  = 'is_admin';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①未通过：public.users.is_admin 列不存在，事务已回滚';
    END IF;

    IF v_type <> 'boolean' THEN
        RAISE EXCEPTION '核对②未通过：public.users.is_admin 的实际类型是 %（应为 boolean），事务已回滚。text/''true''、''1''/''0''、yes/no 这类"伪布尔"会让 `WHERE is_admin` 判真出错（非零非空即真），请人工改成真 boolean 后重跑', v_type;
    END IF;

    IF v_nullable <> 'NO' THEN
        RAISE EXCEPTION '核对③未通过：public.users.is_admin 可空（information_schema.is_nullable=%），事务已回滚。NULL 会让 `WHERE is_admin` 漏掉这些行，鉴权只剩"未知"这一态', coalesce(v_nullable, '<列不存在>');
    END IF;

    IF v_attnotnull IS DISTINCT FROM true THEN
        RAISE EXCEPTION '核对③未通过：pg_attribute.attnotnull 应为 true（实际 %），is_admin 仍未声明 NOT NULL，事务已回滚', coalesce(v_attnotnull::text, '<列不存在>');
    END IF;

    IF v_default IS NULL OR v_default !~* 'false' THEN
        RAISE EXCEPTION '核对④未通过：public.users.is_admin 的默认值不是 false（column_default = %），事务已回滚。没有默认值会让后续 `INSERT INTO users(username, password_hash)` 失败或落成 NULL', coalesce(v_default, '<无默认值>');
    END IF;

    -- ⑤ 任何一行是 NULL 都算失败（本列刚加时必然 0；这条防的是"列早被别人手工加过"）
    EXECUTE 'SELECT COUNT(*) FROM public.users WHERE is_admin IS NULL' INTO v_null_cnt;
    IF v_null_cnt <> 0 THEN
        RAISE EXCEPTION '核对⑤未通过：public.users.is_admin 有 % 行为 NULL（期望 0），事务已回滚。请人工确认这些行应该有权限还是无权限，先补成 TRUE/FALSE 再重跑本脚本', v_null_cnt;
    END IF;

    RAISE NOTICE '核对通过：public.users.is_admin 存在、类型 boolean、NOT NULL、默认值 %、无 NULL 行。默认全员 FALSE —— 需要管理员请按文件头【如何指定管理员】手工执行 UPDATE', v_default;
END $$;

-- 最后一行给人肉眼确认：看到 OK 就说明前面每一步都过了（否则事务已因 EXCEPTION 回滚）
SELECT 'OK' AS result;

COMMIT;
