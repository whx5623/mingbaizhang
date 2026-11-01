-- =============================================================================
-- 022_bills_sub_tags.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   给 public.bills 增加「副标签」列 sub_tags —— 解决传统记账「一笔钱只能算一类」的
--   痛点：主分类（category）负责“这笔钱算在哪一类钱里”，副标签负责“这笔钱是什么场景”。
--   例：给导师买教师节礼物 = category「购物」+ sub_tags ["人情","答谢"]；
--       考研资料 = category「学习」+ sub_tags ["考研","自我投资"]；
--       深夜下单的皮肤 = category「娱乐」+ sub_tags ["冲动消费"]。
--     sub_tags  TEXT NULL           -- JSON 数组字符串，如 ["人情","答谢"]；NULL = 未标注
--
--   · 为什么用 TEXT 而不强制 JSONB：本项目通过 SQLAlchemy/psycopg2 直接写字符串，
--     TEXT 在 Supabase / 自建 PG / 未来换库时都零成本；Python 侧统一用
--     db.normalize_sub_tags / db.sub_tags_to_json 做序列化与校验，
--     读出来再解析成 list，不依赖数据库的 JSON 类型能力。
--   · 为什么不加 CHECK(jsonb_valid)：历史账单与手工 SQL 可能写普通文本，
--     加约束会让迁移在脏数据上直接失败；校验放在应用层（写入前收敛 + 读取时兜底）。
--   · 为什么不建索引：副标签筛选在页面内基于已加载的当月 DataFrame 完成，
--     bills 当前数据量很小；等真正出现「按标签跨全表查询」的慢查询再加 GIN/表达式索引。
--   · 已有行不会丢数据：ADD COLUMN 只加一个可空列，不重写表、不改任何已有值；
--     历史账单 sub_tags 一律读作 NULL（页面显示为空），不做任何猜测性回填。
--
--   ⚠ 本迁移只加一列，不改动任何已有列与已有数据。
--   ⚠ 回滚会**删掉副标签数据**（不可恢复）：回滚前如需保留，请先导出 sub_tags 列。
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/022_bills_sub_tags.sql
-- =============================================================================

ALTER TABLE public.bills
    ADD COLUMN IF NOT EXISTS sub_tags TEXT;

COMMENT ON COLUMN public.bills.sub_tags IS
    '副标签 JSON 数组字符串（如 ["人情","答谢"]），用于场景化记账；NULL 表示未标注';

-- 回滚（确认不再需要副标签时手动执行）：
-- ALTER TABLE public.bills DROP COLUMN IF EXISTS sub_tags;