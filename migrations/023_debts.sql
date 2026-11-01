-- =============================================================================
-- 023_debts.sql
-- 项目：明白账（Streamlit + pandas + SQLAlchemy + PostgreSQL/Supabase）
--
-- 【用途】
--   「AI 债务重组与还款优先级规划器」的负债台账：把花呗 / 白条 / 信用卡 / 借呗
--   等平台的**欠款金额、还款日、年化利率**落到库里，前端才能排出「还款作战日历」。
--     debts.id          SERIAL      主键
--     debts.user_id     INTEGER     所属用户（删用户级联删负债）
--     debts.platform    VARCHAR(50) 平台名，如 花呗 / 白条 / 信用卡
--     debts.amount      NUMERIC(12,2) 当前欠款（> 0）
--     debts.due_date    DATE        下一次还款日（还款作战日历按它排序）
--     debts.apr         NUMERIC(6,2) 年化利率（%，0~100；信用卡循环利息约 18.25）
--     debts.note        VARCHAR(200) 备注（可选）
--     debts.created_at  TIMESTAMP   创建时间
--
--   · 为什么不从 bills 自动推断：账单只能看到「曾经分期/还款」的流水，看不到
--     「现在还欠多少、下一次几号还、利率多少」—— 这三项只有用户自己知道。
--     因此本表是**用户主动录入**的台账，AI 只负责排期与话术，不猜金额。
--   · 为什么 due_date 用 DATE 而不是「每月几号」：日期能自然表达「本月 9 号已过、
--     下月 9 号」以及逾期状态；每月几号在跨月/大小月/闰年上反而要额外规则。
--   · 索引：按 (user_id, due_date) 建复合索引 —— 作战日历的唯一查询模式就是
--     「某用户的负债按还款日排序」。
--   · 已有数据不受影响：本迁移只新建一张表，不改 bills / users 等任何既有表。
--
--   ⚠ 回滚会**删掉负债台账数据**（不可恢复）：回滚前如需保留，请先导出 debts 表。
-- =============================================================================
-- 【执行入口】
-- =============================================================================
--   · Supabase：Dashboard → SQL Editor → New query → **整段**粘贴本文件 → Run；
--   · 本机 / 自建：psql "$DATABASE_URL" -f migrations/023_debts.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.debts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    due_date DATE NOT NULL,
    apr NUMERIC(6,2) NOT NULL DEFAULT 0,
    note VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_debts_user_due
    ON public.debts (user_id, due_date);

COMMENT ON TABLE public.debts IS
    '用户主动录入的负债台账：平台 / 欠款 / 还款日 / 年化利率，用于还款优先级排期';
COMMENT ON COLUMN public.debts.apr IS
    '年化利率（百分数，如 18.25 表示 18.25%/年）；0 表示免息或未知';

-- 回滚（确认不再需要负债台账时手动执行）：
-- DROP TABLE IF EXISTS public.debts;