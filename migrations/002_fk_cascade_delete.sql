-- =============================================================================
-- 002_fk_cascade_delete.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
-- 把 bills / chat_history 指向 users 的外键改成 ON DELETE CASCADE，
-- 让“删除用户”时其账单与聊天记录被数据库自动连带删除（用户注销所需行为）。
-- 背景：db.py 的 CREATE TABLE IF NOT EXISTS 里虽然写了 ON DELETE CASCADE，
--   但真实库的表是早期建的，IF NOT EXISTS 不会修改已存在表；
--   实测删除 users 会被 bills_user_id_fkey（NO ACTION）挡住 → ForeignKeyViolation。
--
-- 【怎么找外键：按语义，不按名字】
-- 不硬编码 bills_user_id_fkey / chat_history_user_id_fkey 这类约束名，
-- 而是去 pg_constraint 里按“语义”筛：
--   ① contype = 'f'（是外键）；
--   ② conrelid 是 public.bills 或 public.chat_history；
--   ③ confrelid = 'public.users'::regclass（必须指向 users，别把别的表算进来）；
--   ④ 约束列**正好只有 user_id**（多列/别的列都不碰）。
-- 命中的约束里，confdeltype <> 'c'（不是 CASCADE）的逐个 DROP + 按原 conname
-- 重建为 ON DELETE CASCADE（用 format/quote_ident 风格拼接，避免注入）。
-- 好处：约束被改过名、或同一张表上有多个等价外键，都能自动适配。
--
-- 【有孤儿行会直接失败】
-- ADD CONSTRAINT 默认会全表校验现有数据。如果库里已经存在“孤儿行”
-- （user_id 指向早已被删掉的用户），ADD CONSTRAINT 会报 violates foreign key
-- constraint 而失败。本脚本在 ADD 之前先跑一遍孤儿行校验，
-- 若有孤儿行则 RAISE EXCEPTION 中止整个事务，并给出数量 + 处理提示，
-- 不会静默跳过、也不会留下“改了一半”的库。
--   （想自己先看一眼等价 SQL：
--     SELECT count(*) FROM bills b LEFT JOIN users u ON u.id = b.user_id
--      WHERE b.user_id IS NOT NULL AND u.id IS NULL;  chat_history 同理）
--   清理方式：DELETE 这些孤儿行，或 UPDATE 把 user_id 置 NULL，然后重跑本脚本。
--
-- 【执行前的影响提示 —— 请务必看完再跑】
-- 加上 CASCADE 之后：DELETE FROM users WHERE id = ... 会连带删除
--   · 该用户全部 bills 记录
--   · 该用户全部 chat_history 记录
-- 这是注销场景想要的级联，但也是不可逆的：生产上执行本脚本本身不改数据，
-- 之后再删用户就真的会连数据一起删。执行前建议备份：
--   CREATE TABLE users_backup_20260101      AS SELECT * FROM users;
--   CREATE TABLE bills_backup_20260101      AS SELECT * FROM bills;
--   CREATE TABLE chat_history_backup_20260101 AS SELECT * FROM chat_history;
-- （日期换成实际执行日期）
-- 另外：ALTER TABLE ... ADD CONSTRAINT 会短暂持有 bills / chat_history 的
--   ACCESS EXCLUSIVE 锁，建议在低峰执行。
--
-- 【执行入口】
-- Supabase 控制台 → 选择本项目 → SQL Editor → 整段粘贴执行（或 psql -f 本文件）。
--
-- 【重要】
-- 1) 本脚本由 db-expert 生成，未自动执行 —— 生成时只在事务里试跑过、随后 ROLLBACK，
--    生产库上的外键约束保持原样。
-- 2) 执行前请先在演练环境跑一遍。
-- 3) 可重复执行（幂等）：已全部是 CASCADE 时只 RAISE NOTICE 跳过；第二次执行应为 0 变更。
-- 4) 末尾核对不通过时用 RAISE EXCEPTION：整个事务自动回滚，不留半成品。
-- 5) 生成时实测到的真实约束（2026 年当前库）：
--      public.bills          bills_user_id_fkey        FOREIGN KEY (user_id) REFERENCES users(id)                     confdeltype='a'  ← 就是它挡住删用户，本脚本会重建
--      public.bills          fk_bills_user              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE   confdeltype='c'  ← 重复外键，已带级联，跳过
--      public.chat_history   chat_history_user_id_fkey  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE   confdeltype='c'  ← 已带级联，跳过
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 步骤 1：打印当前真实外键（执行前基线，便于执行后对比）
-- confdeltype 取值：a=NO ACTION, r=RESTRICT, c=CASCADE, n=SET NULL, d=SET DEFAULT
-- ---------------------------------------------------------------------------
SELECT con.conname                    AS constraint_name,
       rel.relname                    AS table_name,
       con.confdeltype                AS on_delete_code,
       pg_get_constraintdef(con.oid)  AS definition
FROM pg_constraint con
JOIN pg_class rel     ON rel.oid = con.conrelid
JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname = 'public'
  AND rel.relname IN ('bills', 'chat_history')
  AND con.confrelid = 'public.users'::regclass
ORDER BY rel.relname, con.conname;

-- ---------------------------------------------------------------------------
-- 步骤 2：按语义找出「bills / chat_history.user_id → public.users」的外键，
--         把其中不是 CASCADE 的逐个重建为 ON DELETE CASCADE。
--         重建时**沿用原 conname**（不改名、不猜测名字）。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_names    text[];            -- 命中的约束名（沿用原名重建）
    v_tables   text[];            -- 对应的表名，与 v_names 一一对应
    v_deltypes text[];            -- 重建前的 ON DELETE 类型，仅用于日志
    v_orphans  integer;
    i          integer;
BEGIN
    -- (a) 孤儿行校验：放在 ADD 之前，有孤儿行就直接中止并说明怎么处理
    SELECT count(*) INTO v_orphans
    FROM public.bills b
    LEFT JOIN public.users u ON u.id = b.user_id
    WHERE b.user_id IS NOT NULL AND u.id IS NULL;
    IF v_orphans > 0 THEN
        RAISE EXCEPTION '中止：public.bills 有 % 行孤儿账单（user_id 指向不存在的 users.id）。ADD CONSTRAINT 会校验失败，请先 DELETE 这些行或把 user_id 置 NULL，然后重跑本脚本', v_orphans;
    END IF;

    SELECT count(*) INTO v_orphans
    FROM public.chat_history c
    LEFT JOIN public.users u ON u.id = c.user_id
    WHERE c.user_id IS NOT NULL AND u.id IS NULL;
    IF v_orphans > 0 THEN
        RAISE EXCEPTION '中止：public.chat_history 有 % 行孤儿聊天记录（user_id 指向不存在的 users.id）。ADD CONSTRAINT 会校验失败，请先 DELETE 这些行或把 user_id 置 NULL，然后重跑本脚本', v_orphans;
    END IF;

    -- (b) 语义查找：表 ∈ {bills, chat_history} 且 外键列正好只有 user_id 且 指向 public.users
    --     先整体取出再循环，避免边 DDL 边游标扫描系统表
    SELECT array_agg(con.conname            ORDER BY rel.relname, con.conname),
           array_agg(rel.relname::text      ORDER BY rel.relname, con.conname),
           array_agg(con.confdeltype::text  ORDER BY rel.relname, con.conname)
      INTO v_names, v_tables, v_deltypes
    FROM pg_constraint con
    JOIN pg_class rel     ON rel.oid = con.conrelid
    JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'f'
      AND ns.nspname = 'public'
      AND rel.relname IN ('bills', 'chat_history')
      AND con.confrelid = 'public.users'::regclass
      AND ARRAY(
            SELECT att.attname::text
            FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute att
              ON att.attrelid = con.conrelid AND att.attnum = k.attnum
            ORDER BY k.ord
          ) = ARRAY['user_id']
      AND con.confdeltype <> 'c';

    IF v_names IS NULL THEN
        RAISE NOTICE '跳过（幂等）：bills / chat_history 上指向 public.users 的 user_id 外键已全部是 ON DELETE CASCADE，无需变更';
    ELSE
        FOR i IN 1 .. array_length(v_names, 1) LOOP
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I',
                           v_tables[i], v_names[i]);
            EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I '
                           'FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE',
                           v_tables[i], v_names[i]);
            RAISE NOTICE '已重建 public.%.%：ON DELETE % → CASCADE（沿用原约束名）',
                         v_tables[i], v_names[i], v_deltypes[i];
        END LOOP;
        RAISE NOTICE '共重建 % 个外键', array_length(v_names, 1);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 步骤 3：执行后核对 —— 预期 bills / chat_history 上指向 users 的外键 confdeltype 全为 'c'
-- ---------------------------------------------------------------------------
SELECT con.conname                    AS constraint_name,
       rel.relname                    AS table_name,
       con.confdeltype                AS on_delete_code,
       pg_get_constraintdef(con.oid)  AS definition
FROM pg_constraint con
JOIN pg_class rel     ON rel.oid = con.conrelid
JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname = 'public'
  AND rel.relname IN ('bills', 'chat_history')
  AND con.confrelid = 'public.users'::regclass
ORDER BY rel.relname, con.conname;

-- 核对：只看指向 public.users 的外键（confrelid 过滤），不把指向别的表的外键算进来。
-- 不通过 → RAISE EXCEPTION → 整个事务回滚，不会留下半成品。
DO $$
DECLARE
    v_bad   integer := 0;
    v_total integer := 0;
BEGIN
    SELECT count(*) INTO v_total
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'f'
      AND ns.nspname = 'public'
      AND rel.relname IN ('bills', 'chat_history')
      AND con.confrelid = 'public.users'::regclass;

    SELECT count(*) INTO v_bad
    FROM pg_constraint con
    JOIN pg_class rel    ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid  = rel.relnamespace
    WHERE con.contype = 'f'
      AND ns.nspname = 'public'
      AND rel.relname IN ('bills', 'chat_history')
      AND con.confrelid = 'public.users'::regclass
      AND con.confdeltype <> 'c';

    IF v_bad = 0 THEN
        RAISE NOTICE '核对通过：bills / chat_history 上指向 users 的外键共 % 个，已全部是 ON DELETE CASCADE', v_total;
    ELSE
        RAISE EXCEPTION '核对未通过：还有 % 个指向 users 的外键不是 CASCADE，事务已回滚，请检查上方 SELECT 结果', v_bad;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 【可选清理，默认不执行】
-- 重建后 bills 上可能同时存在两个等价外键：bills_user_id_fkey（本脚本重建）
-- 与 fk_bills_user（历史遗留，本就带 CASCADE）。两个 CASCADE 外键功能上无害，
-- 只是每次写入多一次约束校验。确认无其他依赖后，可手工执行下面这条去重：
--   ALTER TABLE public.bills DROP CONSTRAINT IF EXISTS fk_bills_user;
-- ---------------------------------------------------------------------------

COMMIT;
