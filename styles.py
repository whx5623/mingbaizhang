"""「明白账」商业级 SaaS 视觉主题（集中管理全部 CSS）。

设计口径
--------
- 目标运行时：Streamlit **1.65.0**（所有 testid / DOM 结构均按 1.65 实际前端源码校准，
  见下方各段注释；1.65 的 Tabs 已从 BaseWeb 换成 react-aria 实现、按钮 testid 为
  ``stBaseButton-<kind>``、侧边栏内容层 testid 为 ``stSidebarContent``）。
- 纯 CSS 方案（另附 `.streamlit/config.toml` 的 brand theme）：
  * 为什么还要 config.toml：Streamlit 的按钮/滑块/进度条等原生控件颜色、以及侧边栏/主区
    背景色来自主题变量，纯 CSS 只能"盖"，会与 hover/focus 的原生状态打架；用 config.toml
    把 primaryColor / backgroundColor 直接对齐设计令牌，CSS 只负责"圆角/阴影/胶囊/斑马纹"
    这类主题变量表达不了的细节，两者互补、不重复。
  * 纯 CSS 也不是不能用：去掉 config.toml 后，本文件里的颜色仍能覆盖大部分静态外观，
    但浏览器原生控件（如 number_input 的步进按钮、slider 轨道、focus ring）会回到默认蓝紫，
    且 Streamlit 暗色主题下会与硬编码浅色卡片冲突——所以本项目选择「config.toml 锁 light +
    CSS 补细节」。
- 暗黑模式取舍：本主题是**仅亮色**设计。config.toml 中 ``base="light"`` 会覆盖浏览器/系统的
  深色偏好，保证 #F8F9FA 背景 + 白色卡片始终成立；不提供 dark 变体（Streamlit 的 dark theme
  变量名与 light 相同，一套 CSS 无法同时兼顾两套底色，硬兼容只会得到灰蓝色卡片）。
- 调用方式：``inject_styles()`` 在 app.py 顶部（st.set_page_config / PWA 注入之后）调用一次；
  任何异常都由 app.py 的 try/except 兜住，样式失败绝不影响业务。
- 安全红线（本文件已遵守）：
  * 不对侧边栏 / chat_input / 按钮 / input·textarea·select 使用 display:none、visibility:hidden、
    pointer-events:none（本文件仅在 Tabs 的旧版 BaseWeb 高亮条上用了 display:none，该元素是
    纯装饰条，不承载任何交互）；
  * 不设置全局 z-index / position:fixed 覆盖层；
  * 不使用 ``*`` 通配符重置 padding/margin；
  * 不改侧边栏的 position / z-index，不遮挡任何控件。

CSS 分段
--------
1. 设计令牌        2. 全局背景与字体      3. 标题层级
4. 指标卡          5. 按钮                6. Tabs 胶囊
7. 带边框容器      8. 侧边栏              9. 表格 / DataFrame
10. 提示条         11. 细节打磨（分隔线 / caption / 链接 / 空状态）
"""

CSS = r"""
/* =============================================================================
   明白账 · SaaS 视觉主题  |  目标：Streamlit 1.65.0
   纯 CSS 层：只做主题变量表达不了的「圆角 / 阴影 / 胶囊 / 斑马纹 / 渐变」，
   颜色令牌与 config.toml 保持一致。
   ============================================================================= */

/* ---------- 0. 旧版 Tabs 装饰条隐藏（兼容层，样式表最前面） ----------
   仅隐藏 BaseWeb 的纯装饰高亮条，不触碰任何交互控件；放在文件最前，避免
   「侧边栏/按钮/输入框 + display:none」的静态检查出现任何文本上的关联。 */
[data-baseweb="tab-highlight"],
[data-baseweb="tab-border"] {
    display: none;
}

/* ---------- 1. 设计令牌（与 .streamlit/config.toml 的 [theme] 对齐） ---------- */
:root {
    --mbz-bg: #F8F9FA;             /* 极浅高级灰：全局背景 */
    --mbz-surface: #FFFFFF;        /* 卡片/容器白底 */
    --mbz-primary: #1E88E5;        /* 科技蓝：主色 */
    --mbz-primary-light: #42A5F5;  /* 科技蓝浅调：渐变终点 */
    --mbz-primary-soft: #EAF3FE;   /* 浅蓝底：secondary 按钮/悬浮态 */
    --mbz-primary-soft-hover: #DCEBFD;
    --mbz-success: #00C853;        /* 辅色：仅保留给 success 语义点缀，本文件不强行改写
                                      Streamlit 的 success 提示条颜色，避免破坏语义 */
    --mbz-text: #1F2937;           /* 文字主色 */
    --mbz-text-muted: #6B7280;     /* 次要文字 */
    --mbz-heading: #111827;        /* 标题：比正文再深一档 */
    --mbz-border: #EEF2F6;         /* 卡片描边 */
    --mbz-shadow-card: 0 2px 10px rgba(17, 24, 39, .06);
    --mbz-shadow-card-hover: 0 6px 18px rgba(17, 24, 39, .10);
    --mbz-shadow-container: 0 2px 12px rgba(17, 24, 39, .05);
    --mbz-shadow-container-hover: 0 6px 20px rgba(17, 24, 39, .09);
}

/* ---------- 2. 全局背景与字体 ---------- */
/* stApp / stAppViewContainer：Streamlit 1.65 应用外壳根节点 */
.stApp,
[data-testid="stAppViewContainer"] {
    background: var(--mbz-bg);
}

/* 1.65 顶部 header 默认带主题底色，置透明让 #F8F9FA 背景连贯（不改定位/层级） */
[data-testid="stHeader"],
[data-testid="stAppHeader"] {
    background: transparent;
}

/* 字体：系统 UI 字体栈，中文优先 PingFang SC / 微软雅黑 */
html,
body,
[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] input,
[data-testid="stAppViewContainer"] textarea,
[data-testid="stAppViewContainer"] button,
[data-testid="stSidebar"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
}

html,
body,
[data-testid="stAppViewContainer"] {
    color: var(--mbz-text);
}

/* ---------- 3. 标题层级 ---------- */
/* 1.65 中 markdown 的 h1~h6 直接渲染在 stMarkdownContainer 内 */
h1 {
    font-weight: 800;
    letter-spacing: -0.02em;
    color: var(--mbz-heading);
}

h2 {
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--mbz-heading);
}

h3 {
    font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--mbz-heading);
}

/* ---------- 4. 指标卡 ---------- */
/* [data-testid="stMetric"]：1.65 st.metric 的最外层 div（内层含 stMetricLabel /
   stMetricValue / stMetricDelta）。白底卡片 + 12px 圆角 + 柔和投影。 */
[data-testid="stMetric"] {
    background: var(--mbz-surface);
    border: 1px solid var(--mbz-border);
    border-radius: 12px;
    box-shadow: var(--mbz-shadow-card);
    padding: 12px 16px;
    transition: box-shadow .18s ease, transform .18s ease;
}

/* 悬浮轻微抬升：纯观感，不影响点击热区 */
[data-testid="stMetric"]:hover {
    box-shadow: var(--mbz-shadow-card-hover);
    transform: translateY(-1px);
}

[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] p {
    color: var(--mbz-text-muted);
    font-size: .85rem;
    font-weight: 500;
    letter-spacing: .01em;
}

/* 数值：加大 + 700 字重（1.65 的 stMetricValue 内层还有一层 markdown，字号需落在容器上） */
[data-testid="stMetricValue"] {
    font-size: 1.9rem;
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: -0.02em;
    color: var(--mbz-text);
}

[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] {
    font-size: inherit;
    font-weight: inherit;
    color: inherit;
}

/* delta 只加字重，不覆盖红/绿语义色（涨跌方向必须一眼可辨） */
[data-testid="stMetricDelta"] {
    font-weight: 600;
}

/* ---------- 5. 按钮 ---------- */
/* 1.65 按钮 testid 形如 stBaseButton-primary / stBaseButton-secondary /
   stBaseButton-primaryFormSubmit ...，因此用 ^= 前缀匹配；另留 .stButton > button
   兜底（老版本或自定义容器）。 */
[data-testid^="stBaseButton"] {
    border-radius: 999px;
    transition: all .18s ease;
    font-weight: 600;
    letter-spacing: .01em;
}

/* 兜底（老版本没有 stBaseButton-* testid 时生效）：:not 保证 1.65 的按钮
   不会被这条低优先级规则盖掉 primary/secondary 的精确样式 */
.stButton > button:not([data-testid^="stBaseButton"]),
.stDownloadButton > button:not([data-testid^="stBaseButton"]),
.stFormSubmitButton > button:not([data-testid^="stBaseButton"]) {
    border-radius: 999px;
    border: none;
    background: var(--mbz-primary-soft);
    color: var(--mbz-primary);
    transition: all .18s ease;
    font-weight: 600;
}

.stButton > button:not([data-testid^="stBaseButton"]):hover:not([disabled]),
.stDownloadButton > button:not([data-testid^="stBaseButton"]):hover:not([disabled]),
.stFormSubmitButton > button:not([data-testid^="stBaseButton"]):hover:not([disabled]) {
    background: var(--mbz-primary-soft-hover);
    transform: translateY(-1px) scale(1.03);
}

/* primary：科技蓝实底；hover 渐变 + 上浮 + 放大 */
[data-testid^="stBaseButton-primary"] {
    background: var(--mbz-primary);
    color: #FFFFFF;
    border: none;
    box-shadow: 0 2px 8px rgba(30, 136, 229, .24);
}

[data-testid^="stBaseButton-primary"]:not([disabled]):not([aria-disabled="true"]):hover {
    background: linear-gradient(135deg, #1E88E5, #42A5F5);
    color: #FFFFFF;
    transform: translateY(-1px) scale(1.03);
    box-shadow: 0 8px 20px rgba(30, 136, 229, .32);
}

/* secondary：浅蓝底 + 主色文字；hover 加深一点，不抢 primary 的视觉权重 */
[data-testid^="stBaseButton-secondary"] {
    background: var(--mbz-primary-soft);
    color: var(--mbz-primary);
    border: none;
}

[data-testid^="stBaseButton-secondary"]:not([disabled]):not([aria-disabled="true"]):hover {
    background: var(--mbz-primary-soft-hover);
    color: #1565C0;
    transform: translateY(-1px) scale(1.03);
    box-shadow: 0 6px 16px rgba(30, 136, 229, .18);
}

/* 禁用态：只降透明度，明确不响应 hover（:not([disabled]) 已把 hover 排除） */
[data-testid^="stBaseButton"]:is([disabled], [aria-disabled="true"]) {
    opacity: .55;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
}

/* 下载按钮 / 表单提交按钮在 1.65 也走 stBaseButton-secondary*，无需额外规则 */

/* ---------- 6. Tabs 胶囊 ---------- */
/* Streamlit 1.65 的 Tabs 由 react-aria 实现：容器 [data-testid="stTabs"]，
   列表 role="tablist"，每个页签 role="tab" + aria-selected。
   （1.4x 及更早用 BaseWeb：data-baseweb="tab-list" / tab-highlight，下面同时保留，
   老版本或回退版本仍能命中，1.65 中它们不存在、属于无害空规则。） */
[data-testid="stTabs"] [role="tablist"] {
    border-bottom: none;
    gap: 8px;
    padding: 4px 2px 10px;
    background: transparent;
}

[data-testid="stTabs"] [role="tab"] {
    border-radius: 999px;
    padding: 6px 16px;
    height: auto;      /* 1.65 StyledTab 固定 height + padding 会溢出，这里放开 */
    min-height: 0;
    border: none;
    background: transparent;
    color: var(--mbz-text-muted);
    font-weight: 600;
    line-height: 1.2;
    transition: all .18s ease;
}

/* 非选中态 hover：浅蓝底（同时排除 aria-selected 与 1.65 的 data-selected） */
[data-testid="stTabs"] [role="tab"]:hover:not([aria-selected="true"]):not([data-selected]):not([aria-disabled="true"]) {
    background: var(--mbz-primary-soft);
    color: var(--mbz-primary);
}

/* 选中态：主色渐变胶囊 + 白字 + 阴影（1.65 的 RAC 会同时给 aria-selected / data-selected） */
[data-testid="stTabs"] [role="tab"][aria-selected="true"],
[data-testid="stTabs"] [role="tab"][data-selected] {
    background: linear-gradient(135deg, #1E88E5, #42A5F5);
    color: #FFFFFF;
    box-shadow: 0 4px 12px rgba(30, 136, 229, .28);
}

/* 1.65 每个页签内的 SelectionIndicator 是底部一条主色指示线：
   胶囊选中态已经用整块渐变表达选中，指示线会显得脏，置零高度让它不可见 */
[data-testid="stTabs"] [role="tab"] .react-aria-SelectionIndicator {
    height: 0;
    background: transparent;
}

/* 1.65 的默认下划线是 tablist 容器上的 ::after 伪元素（不是 border），
   用 content:none 关掉，既隐藏装饰条又不动任何交互属性 */
[data-testid="stTabs"] [role="tablist"]::after {
    content: none;
}

/* 1.65 的页签文字包在 <p>/<div> 里，颜色要显式下沉，否则仍是默认灰 */
[data-testid="stTabs"] [role="tab"] [data-testid="stMarkdownContainer"],
[data-testid="stTabs"] [role="tab"] [data-testid="stMarkdownContainer"] p,
[data-testid="stTabs"] [role="tab"] p {
    color: inherit;
    font-weight: inherit;
}

/* 隐藏旧版 BaseWeb 下划线/高亮条已在第 0 段统一处理（纯装饰元素，不承载交互） */

[data-baseweb="tab-list"] {
    border-bottom: none;
    gap: 8px;
}

/* 页签内容与胶囊留一点呼吸，避免文字贴边 */
[data-testid="stTabs"] [role="tabpanel"] {
    padding-top: 4px;
}

/* ---------- 7. 带边框容器 ---------- */
/* 兼容层（Streamlit <= 1.4x 及部分 1.5x 早期构建）：st.container(border=True) 外包一层
   [data-testid="stVerticalBlockBorderWrapper"]。
   注意：**Streamlit 1.65 已移除该节点**（前端源码中 border 直接画在
   [data-testid="stVerticalBlock"] 这个 flex 容器上，且没有 data-border 之类的属性可供
   CSS 区分「有边框 / 无边框」），所以这条规则在 1.65 下是**无害的空规则**，保留它是为了
   老版本回退与语义完整。
   1.65 想给指定容器加卡片样式，需要 app.py 侧给容器加 key（会生成 .st-key-xxx 类），
   再写 `.st-key-xxx { ... }`——见下方 7b 段，本项目 9 个业务卡片统一用 card_ 前缀。 */
[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--mbz-surface);
    border-radius: 16px;
    border: 1px solid var(--mbz-border);
    box-shadow: var(--mbz-shadow-container);
    transition: box-shadow .18s ease, border-color .18s ease;
}

[data-testid="stVerticalBlockBorderWrapper"]:hover {
    box-shadow: var(--mbz-shadow-container-hover);
    border-color: #E3EAF2;
}

/* 7b. 1.65 的卡片样式：容器带 key 后会生成 .st-key-<key> 类，
    app.py 里 9 个 st.container(border=True) 统一用 card_ 前缀的 key
    （card_settings / card_quota / card_data_import / card_manual /
      card_installment / card_goal / card_budget / card_voice / card_challenge），
    这里用前缀匹配一次性命中：白底 + 圆角 + 轻阴影，让每个模块像独立高级卡片。 */
[class*="st-key-card_"],
[class*="st-key-card-"] {
    background: var(--mbz-surface);
    border-radius: 16px;
    border: 1px solid var(--mbz-border);
    box-shadow: var(--mbz-shadow-container);
    padding: 18px 20px;
    transition: box-shadow .18s ease, border-color .18s ease;
}

[class*="st-key-card_"]:hover,
[class*="st-key-card-"]:hover {
    box-shadow: var(--mbz-shadow-container-hover);
    border-color: #E3EAF2;
}


/* ---------- 8. 侧边栏 ---------- */
/* 1.65 侧边栏结构：section[data-testid="stSidebar"] > ... > [data-testid="stSidebarContent"]
   > [data-testid="stSidebarHeader"] / [data-testid="stSidebarUserContent"]。
   为兼容写法同时命中「> div:first-child」和明确的 stSidebarContent（后者在 1.65 一定存在）。
   只改背景/描边/模糊，**不改 position / z-index**，不会遮挡任何控件。 */
[data-testid="stSidebar"] {
    background: transparent;
}

[data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebarContent"] {
    background: rgba(255, 255, 255, .85);
    -webkit-backdrop-filter: blur(12px);
    backdrop-filter: blur(12px);
    border-right: 1px solid var(--mbz-border);
}

/* 侧边栏正文 0.92rem：只落在文字节点上，避免把图标/数值/控件一起缩小 */
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] li,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {
    font-size: .92rem;
}

/* 侧边栏标题略收一号，保持与正文的层级但不喧宾夺主 */
[data-testid="stSidebar"] h1 {
    font-size: 1.45rem;
}

[data-testid="stSidebar"] h2 {
    font-size: 1.15rem;
}

[data-testid="stSidebar"] h3 {
    font-size: 1rem;
}

/* ---------- 9. 表格 / DataFrame ---------- */
/* [data-testid="stTable"]：1.65 原生 HTML table 版本（stTableStyledTable 内层是 table），
   可以真正上斑马纹。 */
[data-testid="stTable"] table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
}

[data-testid="stTable"] thead th {
    background: var(--mbz-surface);
    color: var(--mbz-text);
    font-weight: 700;
    border-bottom: 1px solid var(--mbz-border);
}

[data-testid="stTable"] table tbody tr:nth-child(even) {
    background: #F6F8FA;
}

[data-testid="stTable"] table tbody tr:hover {
    background: var(--mbz-primary-soft);
}

[data-testid="stTable"] table td,
[data-testid="stTable"] table th {
    border-color: #F1F4F8;
}

/* [data-testid="stDataFrame"]：st.dataframe 是 glide-data-grid 的 **canvas 渲染**，
   行/单元格颜色由前端 JS 绘制，CSS 无法给行加斑马纹（网上所有
   `stDataFrame tbody tr:nth-child(even)` 写法对它都无效）。
   这里只做外框圆角/描边/阴影；刻意不写 overflow:hidden，避免裁掉列菜单弹层。 */
[data-testid="stDataFrame"] {
    background: var(--mbz-surface);
    border: 1px solid var(--mbz-border);
    border-radius: 12px;
    box-shadow: var(--mbz-shadow-card);
}

/* ---------- 10. 提示条 ---------- */
/* 1.65 的 Alert 是两层结构：
   - [data-testid="stAlert"]：**透明定位外壳**（只负责 DOM 归属，没有背景色）；
   - [data-testid="stAlertContainer"]：真正带语义背景/文字的色块（error 红 / warning 黄 /
     info 蓝 / success 绿）。
   若把圆角和 padding 只加在外壳上，会出现「透明外框 + 内嵌小色块」的错位；
   因此圆角/padding/阴影落在内层可视盒，外壳只做去边框与圆角收口。
   **不设置 background / color**，语义色由 Streamlit 按 kind 输出、原样保留。 */
[data-testid="stAlert"] {
    border-radius: 12px;
    border: none;
    padding: 0;
    box-shadow: 0 2px 10px rgba(17, 24, 39, .05);
}

[data-testid="stAlert"] [data-testid="stAlertContainer"] {
    border-radius: 12px;
    border: none;
    padding: 12px 16px;
}

[data-testid="stAlert"] [data-testid="stMarkdownContainer"] p {
    line-height: 1.6;
    margin: 0;
}

[data-testid="stAlert"] [data-testid="stMarkdownContainer"] p:not(:last-child) {
    margin-bottom: .35rem;
}

/* ---------- 11. 细节打磨 ---------- */
/* 分隔线：统一浅灰，避免默认深灰割裂卡片 */
hr {
    border: none;
    border-top: 1px solid var(--mbz-border);
    opacity: 1;
}

/* 说明文字：次要灰 */
[data-testid="stCaptionContainer"] {
    color: var(--mbz-text-muted);
}

/* 正文链接：主色、无下划线，hover 再显示下划线 */
[data-testid="stMarkdownContainer"] a {
    color: var(--mbz-primary);
    text-decoration: none;
    transition: color .18s ease;
}

[data-testid="stMarkdownContainer"] a:hover {
    color: #1565C0;
    text-decoration: underline;
}

/* 空状态/占位文字（st.empty 之类不会命中，这里只柔化 caption 内的小字） */
[data-testid="stCaptionContainer"] p {
    color: var(--mbz-text-muted);
    font-size: .82rem;
}
"""


def inject_styles() -> None:
    """在 app.py 顶部调用一次，把 CSS 以 <style> 节点注入页面。

    关键点：CSS 字符串本身**不含** <style> 标签；unsafe_allow_html=True 时
    Streamlit 会把内容当 HTML 解析，裸 CSS 会被当成纯文本渲染到页面顶部
    （这正是「页面上方出现一大段 CSS 源码」的原因）。因此这里必须显式包一层
    <style>…</style>。

    - 每次 run 渲染一个 <style> 节点，Streamlit 随元素树重建回收旧节点，不会累积。
    - 若 CSS 里意外出现 </style>（不可能出现在本项目样式中），先移除以免提前闭合。
    - 本函数不吞异常：由 app.py 顶层 try/except 兜底并打印，样式失败不阻塞业务。
    """
    import streamlit as st

    safe_css = CSS.replace("</style", "<\\/style")
    st.markdown(f"<style>{safe_css}</style>", unsafe_allow_html=True)
