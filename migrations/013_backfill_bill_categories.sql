-- =============================================================================
-- 013_backfill_bill_categories.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给历史账单批量补分类：只把 category = '其他' 的行，按商户名关键词回填成
--   奶茶咖啡 / 餐饮 / 购物 / 生活费 / 交通 / 娱乐 / 学习 / 医疗 之一。
--
--   · 只补「其他」，**绝不覆盖**任何已有分类：已经有分类的行（人工选的、上一轮
--     回填写入的）一律不动。UPDATE 的 WHERE 里始终带 category = '其他'。
--   · 规则与 db.py 的 CATEGORY_RULES **完全一致**（关键词、分类、顺序都一致）：
--     顺序就是优先级，先匹配先赢；后面的 UPDATE 只作用在仍是 '其他' 的行上，
--     所以同一条 SQL 跑出来的结果与之后 Python 自动分类的口径不会互相打架。
--   · 本迁移是**数据回填**，不新增/不修改任何表结构（category 列由 012 负责）。
--   · 匹配范围只有 merchant：bills 表没有 description 列，db.py 的
--     auto_categorize 在 CSV/示例数据路径上还会把 description 拼进来一起匹配，
--     这是 SQL 侧无法复刻的部分；纯商户名的结果是一致的，描述里的长尾留给
--     以后新记的账走 Python 规则/AI。
--
--   ⚠ 本脚本**只前进、不可逆**：UPDATE 之后无法区分「原本就是其他」和「被回填
--     成其他」的行（本次也不会把任何行改成其他，但仍无法凭 category 反推出
--     回填前的值）。所以没有自动回滚段，执行前请按下方【备份建议】留一份快照。
--
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/013_backfill_bill_categories.sql
--   ⚠ 必须**整段一次执行**（BEGIN … COMMIT 是一个事务）；逐句执行会失去整体
--     回滚保证，中途失败会留下「补了一半」的分类。
--   ⚠ 迁移请用 SQL Editor 或 5432 直连，不要用 pooler 6543 跑
--     （transaction 模式下多语句事务虽能跑，但 DDL/长事务在 pooler 上排障困难）。
--
--   顺序：001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011
--         → 012（bills.category）→ 013（本文件）
--   ✅ 013 只依赖 012：必须在 012 之后执行（下面「步骤 0」会检查 category 列）。
--
-- =============================================================================
-- 【备份建议】（重要，执行前做）
-- =============================================================================
--   `pg_dump --schema-only` **没用**：它只导出表结构，不导出 category 的**值**，
--   回填错了也恢复不了任何数据。数据回填要备份的是「行 → 分类」的映射：
--
--   方式 A（推荐，库内快照表，放在 private schema —— 不要放 public）：
--       CREATE SCHEMA IF NOT EXISTS private;
--       CREATE TABLE private.bills_bak_20261018 AS
--           SELECT id, category FROM public.bills;
--     · private schema 默认不被 PostgREST 暴露，不会变成外部可读的 API 表；
--     · **不要**把备份表建在 public：public 下的表会被 PostgREST 暴露，也会被
--       pg_dump 的 schema 一起带走，属于"库内垃圾对象"；
--     · 只存 id + category 两列（不含金额/商户），内容敏感度最低；
--     · 快照会随时间变旧：请在**执行本脚本前一刻**再建。
--
--   方式 B（库外文件，Supabase 可用 Table Editor 导出 bills 的 id/category 两列）：
--       psql "$DATABASE_URL" -c "\copy (SELECT id, category FROM public.bills) TO 'bills_category_bak.csv' CSV HEADER"
--
--   需要恢复时：用备份表/文件里的 id → category 映射人工回写；不要把整表恢复
--   覆盖（那会把回填之后用户新改的分类一起冲掉）。
--
-- =============================================================================
-- 【本迁移不会被自动执行】
-- =============================================================================
--   db.py 的 init_db() 只建 users / bills / chat_history，不执行 migrations/ 下
--   的任何脚本；应用启动、登录、记账都不会顺手跑本文件。回填必须由人手工执行，
--   所以本脚本可以放心写成"整段事务 + 详细 NOTICE"，不会被应用隐式触发。
--
-- =============================================================================
-- 【可重复执行（幂等）】
-- =============================================================================
--   · 每条 UPDATE 都限定 category = '其他'：第一遍补过的行不再是 '其他'，
--     第二遍自然不再命中 → 重复执行不会产生新的变化（第二次每个分类应报 0 行）；
--   · 不会新增列/索引/约束，不会失败重跑后留下半成品（整段是一个事务）；
--   · ⚠ 唯一的口径注意：如果**执行之后**用户又手工把某笔匹配关键词的账改回
--     '其他'，再跑一次仍会把它补成规则分类 —— 这是"只补其他"语义的自然结果，
--     不是 bug；不想改就别重跑，或重跑前先确认没有人工改回的其他行。
--   · 幂等验证方法：连跑两遍，第二遍的 8 条 NOTICE 应全部是 0 行，且前后两次
--     分类分布 SELECT 完全一致。
--
-- =============================================================================

BEGIN;

-- 5 秒拿不到表锁就报错回滚，不无限期卡住线上写入。
SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 步骤 0：前置检查（bills 存在 + category 列存在）
--   category 由 012 添加；没跑 012 就 UPDATE 会报 42703 并让事务进入 aborted
--   状态。这里提前给一句能直接照做的报错。
-- ---------------------------------------------------------------------------
DO $do$
BEGIN
    IF to_regclass('public.bills') IS NULL THEN
        RAISE EXCEPTION '步骤0 中止：public.bills 不存在。请先执行 001~003 系列迁移（或启动一次应用建表），并确认连接串指向正确的库/schema。本事务未做任何改动';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'bills' AND column_name = 'category'
    ) THEN
        RAISE EXCEPTION '步骤0 中止：public.bills.category 不存在。请先执行 migrations/012_bills_category.sql，确认成功后再跑本回填（013）。本事务未做任何改动';
    END IF;
    RAISE NOTICE '步骤0：public.bills 与 category 列均存在，继续回填';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 1：回填前分布（留档对照；执行后再看同一查询）
-- ---------------------------------------------------------------------------
SELECT category, count(*) FROM bills GROUP BY 1 ORDER BY 2 DESC;

-- ---------------------------------------------------------------------------
-- 步骤 2：按 CATEGORY_RULES 顺序回填
--   顺序与 db.py 完全一致：奶茶咖啡 → 餐饮 → 购物 → 生活费 → 交通 →
--   娱乐 → 学习 → 医疗。先匹配先赢；每条 UPDATE 只改 category = '其他' 的行，
--   所以后面的规则不会覆盖前面刚写进去的分类。
--   关键词表与 db.py 的 CATEGORY_RULES 逐字相同，改这里必须同步改 db.py。
--   ILIKE：大小写不敏感（KTV / ktv 都命中）；中文关键词不受大小写影响。
-- ---------------------------------------------------------------------------
DO $do$
DECLARE
    v_count integer;
BEGIN
    -- ① 奶茶咖啡（必须排在餐饮前：蜜雪/瑞幸/星巴克等名字里常带"茶/咖啡"）
    UPDATE public.bills SET category = '奶茶咖啡'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%蜜雪%' OR merchant ILIKE '%瑞幸%' OR merchant ILIKE '%星巴克%'
         OR merchant ILIKE '%库迪%' OR merchant ILIKE '%奶茶%' OR merchant ILIKE '%咖啡%'
         OR merchant ILIKE '%茶百道%' OR merchant ILIKE '%喜茶%' OR merchant ILIKE '%霸王茶姬%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-1 奶茶咖啡：本次补标 % 行', v_count;

    -- ② 餐饮
    UPDATE public.bills SET category = '餐饮'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%食堂%' OR merchant ILIKE '%外卖%' OR merchant ILIKE '%美团%'
         OR merchant ILIKE '%饿了么%' OR merchant ILIKE '%餐厅%' OR merchant ILIKE '%饭店%'
         OR merchant ILIKE '%快餐%' OR merchant ILIKE '%麦当劳%' OR merchant ILIKE '%肯德基%'
         OR merchant ILIKE '%面馆%' OR merchant ILIKE '%米饭%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-2 餐饮：本次补标 % 行', v_count;

    -- ③ 购物
    UPDATE public.bills SET category = '购物'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%京东%' OR merchant ILIKE '%淘宝%' OR merchant ILIKE '%天猫%'
         OR merchant ILIKE '%拼多多%' OR merchant ILIKE '%唯品会%' OR merchant ILIKE '%超市%'
         OR merchant ILIKE '%便利店%' OR merchant ILIKE '%商场%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-3 购物：本次补标 % 行', v_count;

    -- ④ 生活费
    UPDATE public.bills SET category = '生活费'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%水电%' OR merchant ILIKE '%电费%' OR merchant ILIKE '%水费%'
         OR merchant ILIKE '%网费%' OR merchant ILIKE '%话费%' OR merchant ILIKE '%房租%'
         OR merchant ILIKE '%物业%' OR merchant ILIKE '%住宿%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-4 生活费：本次补标 % 行', v_count;

    -- ⑤ 交通
    UPDATE public.bills SET category = '交通'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%地铁%' OR merchant ILIKE '%公交%' OR merchant ILIKE '%打车%'
         OR merchant ILIKE '%滴滴%' OR merchant ILIKE '%高铁%' OR merchant ILIKE '%火车%'
         OR merchant ILIKE '%机票%' OR merchant ILIKE '%共享单车%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-5 交通：本次补标 % 行', v_count;

    -- ⑥ 娱乐
    UPDATE public.bills SET category = '娱乐'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%电影%' OR merchant ILIKE '%游戏%' OR merchant ILIKE '%会员%'
         OR merchant ILIKE '%KTV%' OR merchant ILIKE '%演出%' OR merchant ILIKE '%门票%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-6 娱乐：本次补标 % 行', v_count;

    -- ⑦ 学习（"书"是单字，范围宽；前面购物/餐饮已先匹配，能挡住"京东图书"这类）
    UPDATE public.bills SET category = '学习'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%书%' OR merchant ILIKE '%教材%' OR merchant ILIKE '%文具%'
         OR merchant ILIKE '%打印%' OR merchant ILIKE '%考试%' OR merchant ILIKE '%课程%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-7 学习：本次补标 % 行', v_count;

    -- ⑧ 医疗
    UPDATE public.bills SET category = '医疗'
    WHERE category = '其他'
      AND (
            merchant ILIKE '%药%' OR merchant ILIKE '%医院%' OR merchant ILIKE '%挂号%'
         OR merchant ILIKE '%体检%' OR merchant ILIKE '%诊所%'
      );
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '步骤2-8 医疗：本次补标 % 行', v_count;

    RAISE NOTICE '步骤2 完成：以上 8 条 NOTICE 是本次各分类的补标行数；重复执行时应逐条为 0';
END $do$;

-- ---------------------------------------------------------------------------
-- 步骤 3：回填后分布（与步骤 1 对照，确认"其他"变少、目标分类变多）
-- ---------------------------------------------------------------------------
SELECT category, count(*) FROM bills GROUP BY 1 ORDER BY 2 DESC;

-- ---------------------------------------------------------------------------
-- 步骤 4：成功标记
--   前面任何一步失败都会 RAISE EXCEPTION → 整个事务回滚，走不到这里；
--   看到这一行 'OK' 才代表 8 条回填全部执行并已提交。
-- ---------------------------------------------------------------------------
SELECT 'OK';

COMMIT;
