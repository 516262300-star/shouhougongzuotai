# 模块 5「退货报废」设计验收

## 对比基准

- Source visual truth: `C:\Users\lds\.codex\generated_images\01a05ff4-9534-7610-93e6-fad2bc839a51\exec-63b755f6-cb7e-46c5-bc63-1280edb4aa3d.png`
- Implementation screenshot: `D:\desktop\codex\售后工作台\scrap-implementation-final.png`
- Full-view comparison: `D:\desktop\codex\售后工作台\scrap-design-comparison.png`
- Focused ranking comparison: `D:\desktop\codex\售后工作台\scrap-focus-ranking.png`
- Focused detail comparison: `D:\desktop\codex\售后工作台\scrap-focus-detail.png`
- Viewport: 1487 × 1057 CSS px, device scale factor 1
- Pixel dimensions: source 1487 × 1058；implementation 1487 × 1057。仅存在 1 px 高度差，不需要缩放归一化。
- State: 桌面端；模块 5 已打开；默认近 90 天；全部原因、责任归属与数据状态；首个 ERP 型号自动选中；型号明细抽屉关闭。

## Findings

- 无待处理的 P0 / P1 / P2 问题。
- 字体与排版：沿用现有 Noto Sans SC Variable，标题、指标、表格和小字层级与参考图一致；数字使用等宽数字特性，未发现截断或异常换行。
- 间距与布局：概览卡、筛选区、TOP10 表格、下方双图表和 410 px 型号诊断侧栏均与参考图保持相同的信息密度与纵向节奏。
- 色彩与视觉令牌：延续现有工作台蓝色主色，报废数量使用橙色，高风险、关注与健康报废率分别使用红、橙、绿；边框、背景和选中态与参考图一致。
- 图表与图标：图表使用 Recharts 稳定渲染，导航与操作图标使用项目既有 Phosphor 图标库；没有使用占位图片或手绘图标替代。
- 文案与内容：指标口径、ERP 只读状态、型号排名、原因分布、损失占比和侧栏诊断信息均完整；明确说明 ERP 单价不直接作为财务损失。

## Full-view comparison evidence

全屏并排对比确认：页面区块顺序、首屏信息密度、主表与下方图表的分配、固定右侧诊断区和主色层级均匹配。现有产品保留“仓库验货、售后归因”等已有导航项，这是基于既有系统结构的有意保留，不属于设计漂移。

## Focused region comparison evidence

- 型号排名：列顺序、行高、选中态、条形比例、风险色和合计行均匹配；实现额外保留了可点击行和筛选后的空状态。
- 型号诊断：基础信息、颜色分布、主要原因和底部主操作保持同一结构；实现增加了可关闭按钮，并把底部按钮连接到真实 ERP 明细和人工核定表单。

## Comparison history

### Iteration 1

- Earlier findings: 右侧栏初始宽度为 340 px，导致内容拥挤；全局表格 `min-width: 900px` 使侧栏小表横向溢出；图表动画造成截图时线条和环图尚未完成；筛选与排名区整体比参考图偏上约 30–50 px。
- Fixes made: 模块 5 专用侧栏改为 410 px；小表明确覆盖 `min-width: 0`；关闭图表入场动画；接入真实 ERP 查询、加载/异常/空状态与人工核定表单，并调整筛选区、表格行高和纵向节奏。
- Post-fix evidence: `scrap-design-comparison.png` 与 `scrap-focus-ranking.png` 显示侧栏不再截断，表格和下方图表位置与参考图对齐。

### Iteration 2

- Earlier findings: 型号诊断区缺少参考图中的整体卡片边界和关闭入口；趋势线使用平滑曲线，与参考图折线表现有轻微差异。
- Fixes made: 增加诊断卡整体边界、关闭按钮和 ERP 只读提示；趋势改为直线折线。
- Post-fix evidence: `scrap-focus-detail.png` 显示信息结构、边界、趋势与主操作均已对齐。

## Interaction and runtime verification

- 已测试：进入“退货报废”、加载真实 ERP 汇总、切换型号、打开型号报废明细、进入和取消人工核定表单。
- 当前 90 天真实汇总显示退货数量 224,230.01、报废数量 848.01、报废率 0.38%；未核定记录全部进入“待补原因”，已核定损失为 0。
- 默认选中 `2639-单孔` 后，侧栏、颜色分布和 ERP 报废明细同步更新。
- 浏览器控制台未发现 error 或 warning。

## Implementation checklist

- [x] 方案 3 的主界面结构落地
- [x] 筛选、选中、说明、下钻交互可用
- [x] ERP 真实数据、加载/异常/空状态和人工核定入口可用
- [x] 1487 px 桌面视口视觉对比完成
- [x] P0 / P1 / P2 问题清零

final result: passed
