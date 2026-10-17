-- =============================================================================
-- 005_last_login.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   修复后台漏斗「7 天登录用户 / 留存率」永远是 0 的问题。
--
--   根因（两层，缺一不可）：
--     ① users 表**没有 last_login 列**。db.py 的 init_db() 里 CREATE TABLE 是写了这一列的，
--        但 CREATE TABLE IF NOT EXISTS 对**已存在**的表什么都不做 —— 生产库是早期只有
--        5 列（id / username / password_hash / created_at / credits）的老表，从未补列。
--     ② 登录路径**从来不写**这一列：db.py 早先只在 get_admin_funnel_data() 里读它。
--   两层叠起来的结果：登录发生了、库里没有记录，漏斗永远算不出留存。
--
--   本脚本只做两件事，**不改任何业务数据**：
--     1) 给 public.users 加一列 last_login TIMESTAMPTZ NULL（可空，即 timestamptz）；
--     2) 加一个索引 idx_users_last_login，让漏斗的
--        `WHERE last_login >= now() - interval '7 days'` 走索引。
--   **不回填历史值**（原因见步骤 4 的长注释）。
--
--   配套代码改动在 db.py（同一次发布的另一半）：
--     · verify_user()                          登录成功后写 last_login = now()（失败不影响登录）；
--     · register_user() / create_user()        注册即视为一次登录，INSERT 时写 now()；
--     · get_admin_funnel_data()                漏斗窗口整段下沉到数据库：
--                                                 SELECT COUNT(DISTINCT id) FROM users
--                                                 WHERE last_login >= now() - interval '7 days'
--                                              （不用 DATE(col)，也不再把应用机的日期当参数传进来）。
--
-- =============================================================================
-- 【部署顺序 —— 唯一安全顺序：先执行 005，再部署新 db.py】
-- =============================================================================
--   反序（先部署新 db.py、后执行 005）会造成 **注册 100% 失败**：
--     · 新 db.py 的 register_user() / create_user() 的 INSERT 显式列出了 last_login
--       （INSERT INTO users (username, password_hash, last_login) VALUES (..., now())）；
--     · 列缺失时这条 INSERT 直接抛 UndefinedColumn（SQLAlchemy 侧是 ProgrammingError），
--       而 app.py 的注册分支只把 ValueError 的文案展示给用户，其余异常走通用兜底
--       → 用户看到的就是一句“系统繁忙”，且**每一次注册都失败**，不是偶发。
--     · 登录（verify_user 的 SELECT + bcrypt 校验）**不受影响**：它不引用 last_login，
--       且写 last_login 的那一步是 try/except 兜住的（见 db.py 的 _touch_last_login）。
--   列缺失时管理面板会**红屏**，不是“恒为 0”：
--     · get_admin_funnel_data() 的漏斗 SQL 直接引用了 last_login，
--       列不存在 → 抛 UndefinedColumn（ProgrammingError）；
--     · app.py 第 188 行（st.session_state.is_admin 分支里的
--       `total_reg, bill_user_count, login_7d_user, ai_user_count = get_admin_funnel_data()`）
--       **没有 try/except**，异常会一路冒到 Streamlit → 管理面板整块报错。
--     · 普通用户页面不受影响（那段代码只在 is_admin 为 True 时执行）。
--   所以排期时把 005 放在代码发布**之前**；两者之间那几分钟里，
--   “老代码 + 有列”只是不写 last_login（漏斗偏低但不报错），是唯一无害的中间态。
--
-- 【为什么是 TIMESTAMPTZ，而不是 naive 的 TIMESTAMP】
--   naive timestamp（TIMESTAMP WITHOUT TIME ZONE）存的是“一串没有时区的墙上时间”：
--   它到底是 UTC 还是 UTC+8，取决于**写入时**那个会话的 TimeZone；读出来的含义又取决于
--   **读取时**的会话 TimeZone。于是会话 TimeZone 一变（Supabase 项目默认值被改、
--   psql 里 SET TIME ZONE 'Asia/Shanghai'、连接池换了个后端配置），所有人的历史
--   last_login 就会**整体平移 N 小时** —— 数据没变，报表口径变了，而且事后无法判定
--   “这行到底是按哪个时区写进去的”。这就是本列不用 naive 类型的真正原因。
--   timestamptz 存的是**绝对时刻**（内部按 UTC 存 epoch，与时区无关）：
--     · 写入时把会话时区当上下文换算成绝对时刻，读回时再按会话时区渲染；
--     · 所以 `last_login >= now() - interval '7 days'` 比的是两个绝对时刻的先后，
--       与写入方/读取方的时区都无关，跨环境（本机 UTC+8 / 库 UTC / 别的机器）稳定。
--   现在改成 timestamptz 是**零成本**的：本列还没有被创建、一行历史数据都没有。
--
-- 【⚠ 本脚本不会被自动执行（未自动执行声明）】
--   项目里没有任何代码会在启动时执行它：
--     · init_db() 只跑 CREATE TABLE IF NOT EXISTS，**不会**给已存在的表补列；
--     · migrations/ 下的 .sql 全靠人手工执行（001–004 也都是这样跑过来的）。
--   因此：加列必须由人执行本文件（顺序见上）。
--
-- 【执行入口】
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/005_last_login.sql
--                  （PowerShell：psql $env:DATABASE_URL -f migrations\005_last_login.sql）
--   ⚠ 必须**整段一次执行**，不要逐句执行：脚本依赖 BEGIN … COMMIT 这一个事务。
--     逐句执行（每条自动提交）会失去“失败整体回滚”的保证，可能留下
--     “加了列、没加索引”或“列加了、核对不通过”的中间态。
--
-- 【关于“一次 Run 多语句”能看到什么】
--   Supabase SQL Editor 一次 Run 多条语句，通常**只展示最后一个结果集**；
--   本文件的最后一句是 `SELECT 'OK' AS result;`，所以：
--     · **结论以那行 OK 为准**（DO 块里的 RAISE EXCEPTION 会让整段回滚并报错）；
--     · 想逐条看预检（步骤 1 的两个 SELECT）与核对（步骤 5）的 NOTICE，
--       用 psql -f 执行（NOTICE 直接打印在终端），或者把预检块单独放进一个 query 里跑；
--       不要指望 SQL Editor 的 Results 面板把它们都列出来。
--     · Supabase 对 RAISE NOTICE 的展示随版本而异（可能只在 Messages / 日志里），
--       所以本脚本把“发生了什么”同时写进 NOTICE 文本，但结论始终看 OK 那一行。
--
-- 【执行顺序：001 → 002 → 003 → 004 → 005（本文件），必须最后执行】
--   001_normalize_bill_type.sql               账单类型脏值清洗
--   002_fk_cascade_delete.sql                 外键改 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql  amount 精度 + 约束
--   004_password_bcrypt.sql                   密码明文 → bcrypt + 兜底 CHECK 约束
--   005_last_login.sql（本文件）               users.last_login 列 + 索引
--   顺序理由：本脚本与 001–004 之间**没有数据依赖**（只加列/加索引，不读不写任何行），
--   单独跑也不会冲突；但为了让“生产库结构 = 迁移链的最终状态”可复现，
--   仍然排在最后执行。反过来（先 005 后 003/004）也不会坏，只是顺序记录会乱。
--
-- 【备份建议】
--   本脚本只做 ADD COLUMN / CREATE INDEX，**不写、不删、不改任何一行业务数据**，
--   所以“数据级备份”不是必需的。仍然建议留一份执行前的**结构**快照，只用于回滚结构：
--       pg_dump --schema-only -t public.users "$DATABASE_URL" > users_schema_before_005.sql
--       （Supabase 的 pooler 连接串端口 6543 / transaction 模式通常不能用于 pg_dump，
--        请改用 5432 直连串：Dashboard → Database → Connection string → Direct）
--   如果你习惯在做结构变更前留数据快照，用库外文件：
--       pg_dump --data-only -t public.users "$DATABASE_URL" > users_data_before_005.sql
--       （该文件含 password_hash，按机密处理；验收后立即删除或放进加密盘）
--   ⛔ 不要用 `CREATE TABLE public.users_backup_YYYYMMDD AS SELECT * FROM public.users;`
--      这种“库内备份表”：public schema 默认经 PostgREST 对外暴露、新建表默认**不开 RLS**，
--      等于顺手造了一份可远程读取的用户表副本（含全部 password_hash）。
--
-- 【可重复执行（幂等）】
--   本脚本对“已经是什么样”的判定是**先查系统目录、再决定动作**，不是无条件 DDL：
--     · 步骤 2：information_schema.columns 里已有 last_login → 只 NOTICE，跳过；否则 ADD COLUMN
--              （并额外带 IF NOT EXISTS 作为第二层保险，防止并发会话刚好同时加列）；
--     · 步骤 3：pg_indexes 里已有 idx_users_last_login → 只 NOTICE，跳过；否则 CREATE INDEX
--              （同样带 IF NOT EXISTS）；
--     · 步骤 5：核对**永远执行**，任何一条不满足就 RAISE EXCEPTION → 整个事务回滚。
--   所以跑第二遍只会看到“跳过（幂等）”的 NOTICE，库结构不会变、数据不会动。
--
-- 【老用户 last_login 为 NULL 意味着什么 —— 请先跟运营对齐口径】
--   · 本脚本**不回填**，执行完 users 里所有老用户的 last_login 都是 NULL。
--   · NULL 的含义是“**迁移之后还没登录过**（或登录发生在旧代码时期，我们没有记录）”,
--     不是“从来没登录过”。本库在 005 之前根本没有这个字段，历史登录事实**不存在**。
--   · 后果：漏斗的「7 天登录用户」在 005 上线后会**先接近 0**，然后随真实登录慢慢涨起来。
--     这是“如实反映”，不是新 bug。**不要**把这个数字拿去和上线前的 0 做对比，
--     也不要在汇报里写成“留存掉了”。
--   · 老用户下次登录成功时，db.py 会立刻写入 now()，从那一刻起他就被计入漏斗。
--
-- 【可选：运营基线回填（默认不执行，请想清楚再手改）】
--   只有当运营明确需要一个“迁移当时的活跃用户基线”、并且愿意接受这个近似口径时，
--   才手工执行下面这条。它用 created_at **冒充** last_login：
--       -- ⚠ 会虚增留存：把一个从没登录过的老账号算成“最近登录过”
--       UPDATE public.users SET last_login = created_at WHERE last_login IS NULL;
--   **本脚本默认不做**，也请勿直接取消注释后跟着本文件一起跑：
--     · created_at 是“注册时间”，不是“登录时间”，两者完全不是一件事；
--     · 混进漏斗后，7 天留存会被系统性高估，而这个虚高值事后无法区分、无法纠正
--       （因为我们永远无法知道那些人到底登没登过）；
--     · created_at 还是 naive timestamp（此处按 UTC 存），把它塞进 timestamptz 列时
--       会按**当前会话时区**解释 → 又多一层时区歧义。
--   真要做基线，更诚实的做法是在报表里单列一行“历史未知（NULL）用户数”，
--   而不是把它们塞进 last_login。
--
-- 【回滚（结构级，安全且不影响数据）】
--   DROP INDEX IF EXISTS public.idx_users_last_login;
--   ALTER TABLE public.users DROP COLUMN IF EXISTS last_login;
--   这两条各自自动提交即可（都是幂等的；顺序无所谓，DROP COLUMN 会连带删掉索引）。
--   回滚前请**先**把 db.py 换回不写 last_login 的版本：新代码在列缺失时虽然不会
--   挡住登录（_touch_last_login 把它降级成一条告警），但注册会 100% 失败、
--   管理面板会报错（见上面【部署顺序】）。
--   注意：DROP COLUMN 会连带丢掉已经积累的登录时间 —— 回滚前先想清楚要不要留一份
--   库外快照（SELECT id, username, last_login FROM public.users）。
-- =============================================================================

BEGIN;

-- 第一句就上锁等待上限：任何 DDL 卡在等锁上都最多等 5 秒，然后**报错并回滚整个事务**。
-- 拿到 "canceling statement due to lock timeout" 时的正确做法是**过一会儿重试**，
-- 而不是把超时调大 —— 调大只是让你的会话更久地把表锁着、排队的读写更多，
-- 在本来就很热的表上反而会把一次几秒的窗口拖成一次事故。
-- （用 SET LOCAL 而不是 SET：只在当前事务内生效，事务结束自动失效，
--   不会把 lock_timeout 粘到连接池里的后续会话上。）
SET LOCAL lock_timeout = '5s';


-- ---------------------------------------------------------------------------
-- 步骤 1（A1）：执行前预检 —— 纯只读，不改任何东西
--   ① users 总行数                       → 知道这次 ALTER 的规模
--   ② users 当前列清单                   → 确认拿到的表和预期一致（5 列老表？还是已有 last_login？）
--   ③ last_login 是否存在 + 非 NULL 用户数
--      首次执行：应为“列不存在”（本块会 NOTICE 出来）
--      重跑：     应给出“已存在，非 NULL 用户数 = N”，且 N 不会被本脚本改动
--   ⚠ 一次 Run 多语句时，SQL Editor 通常只显示**最后一个**结果集，
--     所以这两个 SELECT 很可能“看不见”；用 psql -f 执行，或把本段单独另开一个 query 跑。
-- ---------------------------------------------------------------------------

SELECT COUNT(*) AS users_total FROM public.users;

SELECT ordinal_position, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'users'
ORDER BY ordinal_position;

DO $$
DECLARE
    v_has_column boolean;
    v_filled     bigint;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'last_login'
    ) INTO v_has_column;

    IF NOT v_has_column THEN
        RAISE NOTICE '步骤1 预检：public.users 目前**没有** last_login 列（首次执行的正常状态），本次将新增它';
    ELSE
        -- 列存在时才能引用它，所以必须动态 SQL（否则本块在首次执行时无法编译）
        EXECUTE 'SELECT COUNT(*) FROM public.users WHERE last_login IS NOT NULL' INTO v_filled;
        RAISE NOTICE '步骤1 预检：public.users.last_login 列已存在，其中非 NULL 的用户数 = %（说明这是重跑；本脚本不会改动这些值）', v_filled;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 2（A2）：幂等加列
--   判据：information_schema.columns 里有没有 public.users.last_login。
--     · 不存在 → ALTER TABLE ... ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ NULL;
--     · 已存在 → 只 NOTICE，**不动它**（不校验、不改类型）。
--       如果它被人手工加成了别的类型（naive timestamp / date / text），
--       本脚本**不擅自替你改类型**，而是由步骤 5 的核对 RAISE EXCEPTION 拦下，
--       让人来决定怎么处置 —— 改类型可能悄悄挪动已有时间值（naive timestamp → timestamptz
--       会按当前会话时区解释旧值），不该由迁移脚本自动做。
--   为什么可空、且不写 DEFAULT：
--     这是**语义**要求，不是性能考虑 —— 本列的“不知道”只有 NULL 能表达
--     （历史登录事实在 005 之前根本不存在，不写 DEFAULT now() 之类去编造一个时刻）。
--     （顺带纠正一个常见说法：PG 11+ 对**常量**默认值不再重写整张表，只把缺省值存进
--       pg_attribute.attmissingval；所以“不用 DEFAULT 是为了不重写表”这个理由在 PG 11+
--       并不成立。这里不用 DEFAULT 纯粹是因为语义。）
--   锁：这条 ALTER 在事务里取的 ACCESS EXCLUSIVE 锁**会一直持有到 COMMIT**，
--     不是执行完就放 —— 这正是下面步骤 3 要提防的地方（见步骤 3 的 ⚠）。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_has_column boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'last_login'
    ) INTO v_has_column;

    IF v_has_column THEN
        RAISE NOTICE '步骤2 跳过（幂等）：public.users.last_login 已存在，不重复 ADD COLUMN';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE public.users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ NULL';

    -- 报告“真实结果”，而不是“我以为做了什么”：
    -- 并发场景下 IF NOT EXISTS 可能什么也没加（另一个会话刚好同时加成功）。
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'users'
          AND column_name  = 'last_login'
    ) INTO v_has_column;

    IF v_has_column THEN
        RAISE NOTICE '步骤2：已新增列 public.users.last_login TIMESTAMPTZ NULL（可空、无默认值；老用户该列为 NULL）';
    ELSE
        RAISE EXCEPTION '步骤2 失败：执行 ADD COLUMN 之后仍查不到 public.users.last_login，表结构异常，事务已回滚';
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 3（A3）：幂等加索引
--   为什么建：漏斗查询是 `WHERE last_login >= now() - interval '7 days'`，
--   没有索引时 users 上万行就是全表扫（now() 是 STABLE 函数，右侧会被当常量求值，
--   B-tree 索引照样能用）。
--   为什么是普通 btree 单列索引：
--     · 只有一个过滤条件、且是范围比较（>=）→ 单列 btree 就够，不要 composite 过度设计；
--     · 不写 WHERE 部分索引：仅索引“非空值”会把 NULL 排除在外，虽然漏斗对 NULL 无意义，
--       但 `WHERE last_login IS NOT NULL` 这类运维查询也得用它，别提前做假设。
--
--   ⚠ 关于锁（请如实理解，别指望“只短暂持有”）：
--     PostgreSQL 的 DDL 锁在**事务提交前不会释放**，即使那条语句早就执行完了。
--     所以本事务里：步骤 2 的 ALTER TABLE 拿到 ACCESS EXCLUSIVE 之后，这把锁
--     **一直持有到 COMMIT**；紧随其后的 CREATE INDEX 虽然本身只需要 SHARE 锁，
--     实际上是在“你已经握着的 ACCESS EXCLUSIVE”之下执行 —— 结论是：
--     **从步骤 2 开始到 COMMIT 为止，users 的读写全部被堵**（ACCESS EXCLUSIVE 与
--     一切读写互斥），而建索引的耗时正好全部落在这个窗口里。表越大，堵得越久。
--     本库现状是 2 行 / 毫秒级，所以把三件事放在一个事务里最简单也最安全（失败全回滚）。
--
--   ⚠ 若你的 users 已经上万行、无法接受这段时间的阻塞，请**不要**直接跑本文件，
--     改成下面这三步（每步都**在事务外**执行，即 psql 里逐条回车 / 每条自动提交，
--     不要包 BEGIN，也不要包在 DO 里）：
--       (1) ALTER TABLE public.users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ NULL;
--           —— 单条 DDL 也是一个隐式事务，锁只持有到这条语句提交（毫秒级）。
--       (2) CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_last_login ON public.users (last_login);
--           —— CONCURRENTLY 不阻塞读写，代价是慢 + 不能进事务块。
--              它还会在失败时留下一个 indisvalid = false 的“残索引”：这种索引不被查询使用，
--              也不能靠 IF NOT EXISTS 修好（名字已被占），必须
--                  DROP INDEX CONCURRENTLY IF EXISTS public.idx_users_last_login;
--              再重新执行 (2)。本脚本步骤 5 会检查 indisvalid / indisready，残索引会被拦下。
--              另外：Supabase 的 6543 端口是 transaction pooler，不适合 CONCURRENTLY，
--              请用 5432 直连串（Dashboard → Database → Connection string → Direct）。
--       (3) 再单独执行步骤 5 那个核对 DO 块（它不依赖前面任何会话状态，可独立运行）。
--     走这条路时，本文件的步骤 2 / 3 会在重跑时 NOTICE “跳过（幂等）”，不会重复加。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_has_index boolean;
    v_def       text;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'users'
          AND indexname  = 'idx_users_last_login'
    ) INTO v_has_index;

    IF v_has_index THEN
        SELECT indexdef INTO v_def
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'users' AND indexname = 'idx_users_last_login';
        RAISE NOTICE '步骤3 跳过（幂等）：索引 idx_users_last_login 已存在，定义 = %', v_def;
        RETURN;
    END IF;

    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_users_last_login ON public.users (last_login)';

    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'users'
          AND indexname  = 'idx_users_last_login'
    ) INTO v_has_index;

    IF v_has_index THEN
        RAISE NOTICE '步骤3：已新增索引 idx_users_last_login ON public.users (last_login)，供漏斗 `last_login >= now() - interval ''7 days''` 使用';
    ELSE
        RAISE EXCEPTION '步骤3 失败：执行 CREATE INDEX 之后仍查不到索引 idx_users_last_login，事务已回滚';
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 步骤 4（A4）：**故意不回填 last_login**
--
--   这里没有 UPDATE，是刻意的。历史登录事实在本库里**根本不存在**：005 之前没有这个字段，
--   也就没有任何会话、日志、流水可以还原“谁在哪天登录过”。任何回填都是在编造数据。
--
--   最诱人的那个“凑数”办法是用 created_at 冒充：
--       UPDATE public.users SET last_login = created_at WHERE last_login IS NULL;   -- ❌ 默认不执行
--   它错在：created_at 是注册时间，不是登录时间。
--     · 一个注册后就再没回来的用户，会被算成“最近 7 天登录过” → 留存率**虚增**；
--     · 这种虚增事后**无法识别、无法纠正**（我们永远不知道他到底登没登过）；
--     · 而且它会污染趋势线：上线后第一批真实数据会和这批伪造值混在一起。
--   所以：老用户 = last_login IS NULL = “迁移之后还没登录过”，如实反映。
--   等你确有必要、且运营明确接受这个近似口径时，再自己手工跑上面那条 UPDATE（本文件不包含）。
--
--   给运营的替代口径（不改数据，只改报表）：
--       SELECT COUNT(*) AS 历史未知用户数 FROM public.users WHERE last_login IS NULL;
--   想区分“从没登录过”和“迁移后没登录过”，本库当前没有足够信息，只能按 NULL 统一报。
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 步骤 5（A5）：执行后核对（任何一条不满足就 RAISE EXCEPTION → 整个事务回滚）
--   ① 列存在，且类型恰为 timestamp with time zone（= timestamptz）
--     为什么只认 timestamptz：naive timestamp 的含义依赖**会话 TimeZone**，
--     时区一改历史值就整体平移；timestamptz 存绝对时刻，跨环境稳定。
--     也不接受 date/text —— date 会把时间截断成天，漏斗“最近 7×24 小时”就退化成
--     “最近 7 个自然日”。
--   ② 列可空（information_schema.is_nullable='YES' 且 pg_attribute.attnotnull=false）
--     必须可空：老用户的“不知道”只有 NULL 能表达；NOT NULL 会让 ADD COLUMN 直接失败/需要编造默认值。
--   ③ 索引存在，且**确实是 public.users(last_login) 上的、1 个键列的、有效且已就绪的**索引
--     （不只是“有个同名索引”：同名的可能建在别的列/别的表/别的 schema，
--       也可能是 CONCURRENTLY 失败留下的 indisvalid=false 残索引 —— 那种索引不被查询使用，
--       表看起来“有索引”，漏斗其实还在全表扫）。
--     indnkeyatts 需要 PG 11+（本库 PG 17，无碍）。
--   末尾 SELECT 'OK' AS result：看到 OK 才算前面每一步都过了（失败时事务已回滚）。
--   ⚠ 一次 Run 多语句时，SQL Editor 通常只展示**最后一个**结果集；
--     本块的 NOTICE 在 psql 里直接打印，在 Supabase 里可能只在 Messages / 日志里。
--     结论以最后那行 OK 为准。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_type       text;
    v_nullable   text;
    v_attnotnull boolean;
    v_attnum     smallint;
    v_idx_count  int;
    v_idx_def    text;
BEGIN
    -- ① + ②：列存在性、类型、可空性一并取出
    SELECT c.data_type, c.is_nullable, a.attnotnull
      INTO v_type, v_nullable, v_attnotnull
    FROM information_schema.columns c
    JOIN pg_attribute a
      ON a.attrelid = to_regclass('public.users')
     AND a.attname  = c.column_name
     AND a.attnum   > 0
    WHERE c.table_schema = 'public'
      AND c.table_name   = 'users'
      AND c.column_name  = 'last_login';

    IF v_type IS NULL THEN
        RAISE EXCEPTION '核对①未通过：public.users.last_login 列不存在，事务已回滚';
    END IF;

    IF v_type <> 'timestamp with time zone' THEN
        RAISE EXCEPTION '核对①未通过：public.users.last_login 的实际类型是 %（应为 timestamp with time zone / timestamptz），事务已回滚。请人工确认该列是谁、为什么加成了别的类型（naive timestamp 会让历史值随会话时区整体平移），再决定是否改动', v_type;
    END IF;

    IF v_nullable <> 'YES' THEN
        RAISE EXCEPTION '核对②未通过：public.users.last_login 不可空（information_schema.is_nullable=%），事务已回滚。老用户的“历史未知”必须能用 NULL 表达', coalesce(v_nullable, '<列不存在>');
    END IF;

    IF v_attnotnull IS DISTINCT FROM false THEN
        RAISE EXCEPTION '核对②未通过：pg_attribute.attnotnull 应为 false（实际 %），last_login 仍不是可空列，事务已回滚', coalesce(v_attnotnull::text, '<列不存在>');
    END IF;

    -- ③：索引存在 + 确实建在 users(last_login) 上 + 单键列 + 有效且就绪
    SELECT COUNT(*), MAX(indexdef)
      INTO v_idx_count, v_idx_def
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename  = 'users'
      AND indexname  = 'idx_users_last_login';

    IF v_idx_count = 0 THEN
        RAISE EXCEPTION '核对③未通过：找不到索引 idx_users_last_login，事务已回滚';
    END IF;

    SELECT a.attnum INTO v_attnum
    FROM pg_attribute a
    WHERE a.attrelid = to_regclass('public.users')
      AND a.attname  = 'last_login'
      AND a.attnum   > 0;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_class     ic ON ic.oid = i.indexrelid
        JOIN pg_namespace n  ON n.oid  = ic.relnamespace
                            AND n.nspname = 'public'      -- 同名索引可能在别的 schema
        WHERE ic.relname    = 'idx_users_last_login'
          AND i.indrelid    = to_regclass('public.users') -- 必须建在 users 上
          AND i.indkey[0]   = v_attnum                    -- 第一个键列必须是 last_login（int2vector 从 0 开始）
          AND i.indnkeyatts = 1                           -- 只能是单列索引，不接受 (a,b) 之类
          AND i.indisvalid                                -- false = CONCURRENTLY 失败留下的残索引，查询不会用它
          AND i.indisready                                -- false = 还没建完/不可插入，同样不能用
    ) THEN
        RAISE EXCEPTION '核对③未通过：索引 idx_users_last_login 存在但不是“public.users(last_login) 上的单列有效索引”（实际定义 = %）。可能原因：建在了别的列/表/schema，或是一次失败的 CREATE INDEX CONCURRENTLY 留下的 indisvalid=false 残索引。请人工 DROP 该索引后重跑本脚本', coalesce(v_idx_def, '<无名>');
    END IF;

    RAISE NOTICE '核对通过：public.users.last_login 存在、类型 timestamp with time zone、可空；索引 idx_users_last_login 存在、单键列、indisvalid/indisready 均为真且建在 public.users(last_login) 上（定义 = %）。老用户该列仍为 NULL —— 本脚本未回填', v_idx_def;
END $$;

-- 最后一行给人肉眼确认：看到 OK 就说明前面每一步都过了（否则事务已因 EXCEPTION 回滚）
SELECT 'OK' AS result;

COMMIT;
