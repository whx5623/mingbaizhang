-- =============================================================================
-- 004_password_bcrypt.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   历史遗留问题：public.users.password_hash 里存的是**明文密码**，登录时靠
--   `WHERE username = :un AND password_hash = :ph` 做明文等值比较。一次拖库
--   （备份泄露、日志、误授权、SQL 注入）就等于把全体用户的密码原文交出去，
--   而用户普遍跨站复用密码 —— 这是本项目当前最严重的安全缺陷。
--
--   本脚本做三件事：
--     1) 把存量**明文**密码**原地**转换成 bcrypt 哈希（cost=12，$2a$12$...，60 字符）：
--        转换方式是 crypt(password_hash, gen_salt('bf', 12))，即“把数据库里当前
--        这个值当作密码原文”去哈希 —— 所以**用户仍然用原来的密码登录**，
--        不需要强制重置、不需要通知用户、不会有用户因此流失。这是本脚本最关键的取舍：
--        “保住登录能力”优先于“立即换成更强的新密码体系”。
--     2) 把 NULL / 空白密码账号（无法哈希，也没有可用密码）换成**随机不可猜**的
--        bcrypt 占位哈希：这些账号从此登不进去，必须由管理员重置密码。
--        **不删除用户、不动它们的账单**（users 一行被删，bills/chat_history 会因
--        ON DELETE CASCADE 一起没）。
--     3) 加数据库级兜底 CHECK 约束 users_password_hash_bcrypt：
--        以后任何写入路径（本项目的 db.py、Supabase 控制台手工 INSERT/UPDATE、
--        别的脚本、将来的新代码）都写不进明文密码。应用层哈希可能被绕过，
--        约束绕不过去。
--
-- 【执行顺序：001 → 002 → 003 → 004（本文件），必须最后执行】
--   001_normalize_bill_type.sql               账单类型脏值清洗
--   002_fk_cascade_delete.sql                 外键改 ON DELETE CASCADE
--   003_amount_precision_and_constraints.sql  amount 精度 + 约束
--   004_password_bcrypt.sql（本文件）          密码明文 → bcrypt + 兜底约束
--   顺序理由：本脚本与前三者无数据依赖，只看 users.password_hash，理论上可以单独跑；
--   但为了保证“生产库结构 = 迁移链的最终状态”可复现，仍然排在最后执行。
--
-- =============================================================================
-- 【⚠ 上线清单：必须停写、整段执行、按下面 5 步走（顺序不可反，也不可跳步）】
--
--   ① 停写（单实例 / 维护模式）
--        只留一个应用实例在跑、或直接进维护模式挡掉注册。为什么必须停：
--        · 004 加上 CHECK 之后，**仍在运行的旧代码**会把明文写进 password_hash →
--          被约束直接拒绝 → 用户看到“注册失败”，而代码还没切换，属于纯负面体验；
--        · 更糟的是：如果窗口里刚好有旧代码写入明文，步骤 4/5 的断言会失败 →
--          整个事务回滚 → 你既没迁移成功、又白等一个窗口。
--
--   ② 部署新代码（db.py 的 bcrypt 校验 + 应用层哈希）
--        此时老用户暂时登不上（新代码 fail closed，见 db.py::verify_password），
--        所以第 ③ 步要立刻跟上。
--
--   ③ **立刻整段执行本脚本**（Supabase SQL Editor 全选粘贴 / `psql -f 本文件`）
--        · 必须**整段一次执行，不要逐句执行**：脚本依赖 BEGIN … COMMIT 这一个事务，
--          而 `SET LOCAL search_path = public, extensions;` 以及步骤 4 的
--          `ALTER TABLE` 都只在同一事务内有效。逐句执行（每条自动提交）会让
--          search_path 丢失（crypt() 解析不到）并且失去“失败整体回滚”的保证 ——
--          一旦在中途失败，库里会停留在“一半哈希、一半明文”的中间态。
--        · 耗时估算：≈ 用户行数 × 0.25s（bcrypt cost=12 逐行计算），
--          再加上步骤 4 `ADD CONSTRAINT` 校验存量行时的一次**短暂 ACCESS EXCLUSIVE
--          锁**（这段时间 users 的读写都会排队等锁，通常不到 1 秒）。
--          行数上万时请放进低峰窗口执行。
--
--   ④ 验收：用 test1 / test4 各自的**原密码**登录一次；
--      再手工跑一遍步骤 5 的断言（或重跑本脚本，应全程 0 行变更、只出 NOTICE）。
--
--   ⑤ 放开写（退出维护模式 / 恢复多实例）。
--
--   为什么顺序不可反：
--   · 先部署代码、后跑 004 → 窗口期内所有老用户都登不上（明文不在约束内，
--     而新代码拒绝非 bcrypt 值）；
--   · 先跑 004、后部署代码 → 旧代码用明文等值比较，库里已是哈希，同样全员登不上；
--   · 所以正确做法是把窗口压到最短：停写 → 部署代码 → 立刻整段跑 004 → 验收 → 放开写。
--   · 本脚本是**单事务**的：任何一步失败都会整体回滚，不会留下中间状态；
--     失败时库回到执行前的样子，只需修好报错再跑一遍。
--     唯一的例外是步骤 0.5 的硬拦截：它会 RAISE EXCEPTION 主动中止，
--     同样属于“一条不动”，请按它的提示先处理超长密码账号。
-- =============================================================================
-- 【⚠ 执行前必须备份 —— 强烈推荐“库外备份”，不要在本库里建明文备份表】
--
--   ✅ 推荐做法（库外文件，包含全部明文密码，必须按密码级机密对待）：
--       pg_dump --data-only -t public.users "$DATABASE_URL" > users_backup.sql
--       （Windows PowerShell 同理；备份文件只落本机、执行完立即删除或进加密盘）
--       注意：Supabase 的 pooler 连接串（端口 6543，transaction 模式）通常不能用于
--       pg_dump，会报 prepared statement / pooler 相关错误 —— 备份时请改用
--       **5432 直连串**（Dashboard → Database → Connection string → Direct）。
--
--   ⛔ 不要用 `CREATE TABLE users_backup_YYYYMMDD AS SELECT * FROM public.users;`
--      这种“库内备份表”写法在 Supabase 上是**新的泄露面**：
--        · public schema 默认经 PostgREST（/rest/v1/...）对外暴露；
--        · 新建表默认**不启用 RLS**；
--      两个默认叠起来 = 你刚修完明文密码，又顺手造了一份**可远程读取的明文副本**，
--      而且它比 users 更“干净”（没人会去审计一张叫 backup 的表）。
--
--   ⚠ 如果受环境限制只能用 SQL 备份表，必须把下面 4 条一条不落地做完：
--       1) 建在**非暴露** schema（不存在就先建），不要建在 public：
--            CREATE SCHEMA IF NOT EXISTS private;
--            CREATE TABLE private.users_backup_YYYYMMDD AS SELECT * FROM public.users;
--       2) 撤销匿名/登录用户的全部权限（PostgREST 用的就是 anon / authenticated）：
--            REVOKE ALL ON private.users_backup_YYYYMMDD FROM anon, authenticated;
--       3) 打开行级安全（双保险，即使将来误授权也读不到）：
--            ALTER TABLE private.users_backup_YYYYMMDD ENABLE ROW LEVEL SECURITY;
--       4) 验收通过后**立即**删除：
--            DROP TABLE private.users_backup_YYYYMMDD;
--      另外请顺手做一次只读检查，确认 users 本身有没有被 PostgREST 暴露：
--            SELECT relrowsecurity FROM pg_class WHERE relname = 'users';
--            （true = users 已启用 RLS；false = RLS 未启用，此时只要 anon /
--              authenticated 还持有该表权限，PostgREST 就能直接读写 —— 属于需要
--              单独修的 P1 项，与本脚本无关，但既然看见了就应该登记。
--              只查 RLS 不够，真正决定暴露面的是“表权限 × RLS × policy”三者，
--              本脚本不改变其中任何一项。）
--
-- 【回滚方法（严格按 ①②③④ 的顺序，顺序反了必然失败）】
--   回滚 = 把库退回“明文密码 + 无 CHECK 约束”，让旧代码能重新登录。
--   ① 先回滚**代码部署**（把 db.py 换回旧的明文比较版本）
--        必须最先做：新代码是 fail closed 的，只要代码还是新的，下面就白回滚 ——
--        旧用户照样登不上。
--   ② 再删约束
--        ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_password_hash_bcrypt;
--        必须第二步做：CHECK 还在时，第 ③ 步回灌明文会被约束拒绝
--        （报 new row violates check constraint "users_password_hash_bcrypt"），
--        整个回滚就卡死在第一步 —— 这也是上一版文档写错的地方。
--   ③ 从备份回灌 password_hash
--        · 库外文件备份（推荐）：把 users_backup.sql 里的 users 数据恢复回去，
--          例如 psql "$DATABASE_URL" -f users_backup.sql（--data-only 只含 INSERT，
--          请先确认目标表可写入/必要时先清空 users 数据）。
--        · 或用库内备份表：UPDATE public.users u SET password_hash = b.password_hash
--            FROM private.users_backup_YYYYMMDD b WHERE u.id = b.id;
--   ④ 复核登录：用 test1 / test4 的原密码在**回滚后的旧代码**上登录一次。
--
--   ⚠⚠ 回灌之后库里又是**明文密码**了：这只是一次临时抢救动作。
--       一旦服务恢复，必须立刻重新执行一遍本脚本（004），把明文重新收成 bcrypt，
--       否则数据库又回到“一次拖库全量泄露”的原始状态。并且别忘了回滚第 ① 步的
--       代码 —— 代码和库必须是同一套口径，否则会出现“代码新、库旧（或反之）”的
--       静默全员登录失败。
-- =============================================================================
-- 【执行入口】
--   Supabase 控制台 → 选择本项目 → SQL Editor → 整段粘贴执行（或 psql -f 本文件）。
--
-- 【重要】
--   1) 本脚本**未自动执行** —— 生成时只在事务里试跑过、随后 ROLLBACK，
--      生产库 users 的 2 行（test1 / test4）密码保持明文原样，一个字节都没改。
--   2) 可重复执行（幂等）：
--      · 已是 bcrypt 的行不在 UPDATE 的 WHERE 条件里 → 第二次执行 UPDATE 0 行，
--        **不会对哈希再做一次哈希**（那是本脚本最危险的失败模式：哈希两遍 = 谁都登不上）；
--      · NULL/空白占位第二次执行影响 0 行；
--      · 约束已存在且定义符合预期 → 跳过并 NOTICE；同名但定义不符 → 先 DROP 再重建
--        （绝不只按名字跳过，那会把错误的口径永久固化）。
--   3) pgcrypto 的 crypt/gen_salt 依赖 search_path 能找到 extensions schema。
--      实测（PostgreSQL 17.11 / Supabase）：本库本角色 search_path 已含 extensions，
--      裸写 crypt()/gen_salt() 可解析；但 Supabase SQL Editor 的 search_path 不保证一致，
--      所以下面显式 `SET LOCAL search_path = public, extensions;`。
--      若你的库报 "function crypt(text, text) does not exist"，把脚本里的
--      crypt( / gen_salt( 全部改成 extensions.crypt( / extensions.gen_salt( 即可。
--   4) bcrypt 的密码长度硬限制是 72 字节（约 24 个汉字）：pgcrypto 的 bf 实现会
--      **静默截断**超长输入，而 Python bcrypt 5.0.0 对 >72 字节输入直接抛 ValueError
--      （= 迁移后永久登不上，且原密码不可恢复）。所以本脚本不靠“注释提醒”，
--      而是把这件事做成**硬拦截**：见步骤 0.5，发现任何超长明文就直接 RAISE
--      EXCEPTION 中止整个脚本 —— 要么安全执行，要么一条不动。
--   5) 所有 bcrypt 正则与 db.py::_BCRYPT_PATTERN **逐字同口径**（前缀 $2[abxy]，
--      两位 cost，53 个 [./A-Za-z0-9]），共 60 字符。之所以要“逐字”对齐：任何
--      一边更宽/更窄，都会造出“库里合规、代码判非法”（或反之）的账号，
--      表现为莫名其妙的“用户名或密码错误”。
--      唯一差别是 SQL 必须写 ^...$（`~` 是搜索语义），而 Python 侧用
--      `re.fullmatch`（不用 `$`，因为 Python 的 `$` 允许串尾多一个换行，
--      会让 "hash + \n" 在两边判定相反）。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 步骤 0：前置准备
--   pgcrypto 提供 crypt() / gen_salt()；Supabase 上它通常已安装在 extensions schema。
--   这里 CREATE EXTENSION IF NOT EXISTS 是幂等的：已安装则什么都不做。
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SET LOCAL search_path = public, extensions;

-- ---------------------------------------------------------------------------
-- 步骤 1：预检（只读，不打印任何密码内容）
--   1a：总行数 / 待重哈希行数 / NULL 行数 / 空白行数 / 超 72 字节行数
--   bcrypt 正则与 db.py::_BCRYPT_PATTERN、以及步骤 4 的 CHECK 约束**完全同口径**：
--     ^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$   —— 60 字符，$2a$/$2b$/$2y$/$2x$ 都认
--   “空白”口径与 Python 的 password_hash.strip() 对齐：用 ~ '^\s*$'（\s 覆盖空格、
--   \t、\n、\r、\f、\v）而不是 btrim(=空格)—— 否则一个 E'\t' 值在 SQL 里算“非空密码”
--   会被拿去哈希，而 Python 侧却认为它是空密码，两边判定相反。
-- ---------------------------------------------------------------------------
SELECT
    count(*)                                                                    AS users_total,
    count(*) FILTER (WHERE password_hash IS NOT NULL
                       AND password_hash !~ '^\s*$'
                       AND password_hash !~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$')
                                                                                 AS plaintext_rows,
    count(*) FILTER (WHERE password_hash IS NULL)                               AS null_rows,
    count(*) FILTER (WHERE password_hash ~ '^\s*$')                              AS empty_rows,
    count(*) FILTER (WHERE password_hash ~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$')
                                                                                 AS already_bcrypt_rows,
    -- 超 72 字节的明文行数：>0 时步骤 0.5 会直接中止整个脚本
    count(*) FILTER (WHERE password_hash IS NOT NULL
                       AND octet_length(password_hash) > 72)
                                                                                 AS over72_rows
  FROM public.users;

--   1b：待重哈希的用户名样例（**只显示 username，绝不打印密码**）
SELECT id, username
  FROM public.users
 WHERE password_hash IS NOT NULL
   AND password_hash !~ '^\s*$'
   AND password_hash !~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$'
 ORDER BY id
 LIMIT 20;

-- ---------------------------------------------------------------------------
-- 步骤 0.5（硬拦截）：>72 字节的明文密码 → 直接 RAISE EXCEPTION 中止整个脚本
--   为什么必须硬拦截，而不能只在注释里提醒：
--     · pgcrypto 的 bf 实现把输入**静默截断**到 72 字节 → 迁移本身不会报错，
--       脚本照样打印 OK，看起来“迁移成功”；
--     · 而 Python bcrypt 5.0.0 对 >72 字节输入**直接抛 ValueError**
--       （db.py::verify_password 捕获后按“校验失败”处理 = fail closed）；
--     · 结果：用户用完整原密码登录 → 永远失败；用前 72 字节 → 也不行。
--       原密码不可恢复（我们只存了截断后的哈希），只能管理员重置。
--   所以：只要发现有超长行，一个字节都不改，整脚本 abort，先人工处理这批账号。
--   放在预检之后、步骤 2 之前 —— 此时事务里还什么都没写过。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    over72_count integer;
    over72_names text;
BEGIN
    SELECT count(*),
           string_agg(format('%s(%s 字节)', username, octet_length(password_hash)), ', ' ORDER BY id)
      INTO over72_count, over72_names
      FROM public.users
     WHERE password_hash IS NOT NULL
       AND octet_length(password_hash) > 72;

    IF over72_count > 0 THEN
        RAISE EXCEPTION
            '[004] 已中止（本次未修改任何数据）：% 个账号的密码超过 bcrypt 的 72 字节上限，无法安全迁移：%'
            '。处理办法：管理员先给这些账号重置密码（重置后 password_hash 应为 60 字符的 bcrypt 串），再重跑本脚本；'
            '不要直接哈希 —— pgcrypto 会静默截断到 72 字节，迁移后原密码永久不可用。',
            over72_count, over72_names;
    END IF;

    RAISE NOTICE '[004] 步骤0.5：没有超过 72 字节的明文密码，继续';
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 2：原地重哈希（核心步骤；幂等：已是 bcrypt 的行不在 WHERE 里）
--   把“库里当前这个值”当密码原文 → crypt(value, gen_salt('bf', 12))
--   → 用户原来的密码依然能通过 bcrypt.checkpw 校验，登录能力保住。
--   每行独立随机盐（gen_salt 逐行求值），所以同样的密码得到的哈希互不相同。
--   空白判定用 ~ '^\s*$'（与 Python .strip() 同口径，见步骤 1 注释）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n integer;
BEGIN
    UPDATE public.users
       SET password_hash = crypt(password_hash, gen_salt('bf', 12))
     WHERE password_hash IS NOT NULL
       AND password_hash !~ '^\s*$'
       AND password_hash !~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$';
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE '[004] 步骤2：% 行明文密码已原地转换为 bcrypt(cost=12)，用户可用原密码登录', n;
    ELSE
        RAISE NOTICE '[004] 步骤2：没有待转换的明文密码（幂等复跑：0 行）';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 3：NULL / 空白密码账号 → 随机不可猜的 bcrypt 占位
--   这些账号没有可用密码，无法“保留原密码登录”。处理方式：
--     crypt(gen_random_uuid()::text, gen_salt('bf', 12))
--   uuid v4 有 122 位随机熵，猜不出来；转换后这些账号**登不进去**，
--   需要管理员在控制台手工重置密码（UPDATE 成新密码的 bcrypt 哈希）。
--   **不 DELETE 用户**：删一行 users 会级联删掉它的 bills / chat_history。
--   幂等：转换后 password_hash 非空且是 bcrypt，第二次执行影响 0 行。
--   注：本项目生产库 password_hash 是 NOT NULL（NULL 分支实际 0 行，SQL 仍保留兜底）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n     integer;
    names text;
BEGIN
    SELECT string_agg(username, ', ' ORDER BY id)
      INTO names
      FROM public.users
     WHERE password_hash IS NULL OR password_hash ~ '^\s*$';

    UPDATE public.users
       SET password_hash = crypt(gen_random_uuid()::text, gen_salt('bf', 12))
     WHERE password_hash IS NULL OR password_hash ~ '^\s*$';
    GET DIAGNOSTICS n = ROW_COUNT;

    IF n > 0 THEN
        RAISE NOTICE '[004] 步骤3：% 个 NULL/空白密码账号已写入随机占位哈希，需管理员重置密码：%',
                     n, names;
    ELSE
        RAISE NOTICE '[004] 步骤3：没有 NULL/空白密码账号，跳过（幂等复跑：0 行）';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 4：加数据库级兜底约束 users_password_hash_bcrypt
--   目的：把“password_hash 必须是 bcrypt”从**应用层约定**升级成**库级事实**。
--   今后任何路径（db.py、控制台手工 SQL、别的服务、将来的新代码）写明文都会被拒。
--   代价：ADD CONSTRAINT 会全表校验并短暂持有 ACCESS EXCLUSIVE 锁（见文件头耗时说明）。
--   幂等策略与 003 一致：同名约束已存在**且定义符合预期**才跳过；
--   NULL 语义说明：CHECK 对 password_hash IS NULL 求值为 NULL，SQL 把 NULL 视为“通过”，
--   所以这条约束**管不住 NULL**（本次不额外加 NOT NULL，因为生产库该列本就是 NOT NULL，
--   且 NULL 账号在应用层被 verify_password fail closed 拒绝，不存在登录风险）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    def text;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
      INTO def
      FROM pg_constraint c
     WHERE c.conrelid = 'public.users'::regclass
       AND c.conname  = 'users_password_hash_bcrypt';

    IF def IS NULL THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_password_hash_bcrypt
            CHECK (password_hash ~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$');
        RAISE NOTICE '[004] 步骤4：已创建约束 users_password_hash_bcrypt';
    ELSIF position('password_hash' IN def) > 0
          AND position('2[abxy]' IN def) > 0
          AND position('{53}' IN def) > 0 THEN
        RAISE NOTICE '[004] 步骤4：约束 users_password_hash_bcrypt 已存在且定义符合预期，跳过';
    ELSE
        -- 同名但口径不符（例如只判长度、或漏了 $2x$）→ 先删后建，绝不按名字跳过
        ALTER TABLE public.users DROP CONSTRAINT users_password_hash_bcrypt;
        ALTER TABLE public.users
            ADD CONSTRAINT users_password_hash_bcrypt
            CHECK (password_hash ~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$');
        RAISE NOTICE '[004] 步骤4：旧约束定义不符（%），已 DROP 并按预期重建', def;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 5：末尾核对（任一不满足 → RAISE EXCEPTION → 整个事务回滚）
--   ① 非 bcrypt 行数 = 0
--   ② 约束存在且定义正确
--   ③ pgcrypto 可用（crypt('probe', gen_salt('bf',12)) 形如 $2[abxy]$）
--   ④ 没有 >72 字节的残留值（步骤 0.5 之后不该再出现，这里再兜一次）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    bad       integer;
    def       text;
    pgc_ok    boolean;
    total     integer;
    over72    integer;
BEGIN
    SELECT count(*) INTO total FROM public.users;

    SELECT count(*) INTO bad
      FROM public.users
     WHERE password_hash IS NOT NULL
       AND password_hash !~ '^\$2[abxy]\$[0-9]{2}\$[./A-Za-z0-9]{53}$';
    IF bad <> 0 THEN
        RAISE EXCEPTION '[004] 校验失败：仍有 % 行 password_hash 不是 bcrypt，事务回滚', bad;
    END IF;

    SELECT pg_get_constraintdef(c.oid)
      INTO def
      FROM pg_constraint c
     WHERE c.conrelid = 'public.users'::regclass
       AND c.conname  = 'users_password_hash_bcrypt';
    IF def IS NULL THEN
        RAISE EXCEPTION '[004] 校验失败：约束 users_password_hash_bcrypt 不存在，事务回滚';
    END IF;
    IF position('2[abxy]' IN def) = 0 OR position('{53}' IN def) = 0 THEN
        RAISE EXCEPTION '[004] 校验失败：约束定义不符合预期（%），事务回滚', def;
    END IF;

    SELECT crypt('probe', gen_salt('bf', 12)) ~ '^\$2[abxy]\$' INTO pgc_ok;
    IF pgc_ok IS NOT TRUE THEN
        RAISE EXCEPTION '[004] 校验失败：pgcrypto 不可用（crypt/gen_salt 解析不到），事务回滚';
    END IF;

    -- 迁移后所有值都是 60 字符的 bcrypt 串，理论上不可能再有 >72 字节的行。
    -- 这里再断言一次，防止“步骤 2 之前有人手工塞了超长值”之类的外部干扰。
    SELECT count(*) INTO over72
      FROM public.users
     WHERE password_hash IS NOT NULL AND octet_length(password_hash) > 72;
    IF over72 <> 0 THEN
        RAISE EXCEPTION '[004] 校验失败：仍有 % 行 password_hash 超过 72 字节，事务回滚', over72;
    END IF;

    RAISE NOTICE '[004] 校验通过：users 共 % 行、0 行非 bcrypt、0 行超 72 字节、约束在位且定义正确、pgcrypto 可用', total;
END $$;

SELECT 'OK' AS result;

COMMIT;
