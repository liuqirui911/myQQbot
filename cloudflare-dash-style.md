# Cloudflare Dashboard 设计语言参考

来源：2026-09-12 用户粘贴的 dash.cloudflare.com DNS 记录页原始 HTML（liuqirui911.cn 域名页）。
用途：以后做面板/UI（如 ai-draw 面板改版、mc-whitelist 面板等）时的样式参考。只取设计 token 和排版规则，不抄其业务代码。

## 字体排印
- 字体：Inter 可变字体（`inter-variable.woff2`，含斜体）+ `paper-mono-variable.woff2`（等宽，用于代码/数据）
- 基础字号 16px（html/body/button 统一）
- 行高：正文 1.5，标题 1.25
- 标题阶梯：h1 32px/**400**（注意 h1 是细体）｜ h2 24px/600 ｜ h3 20px/600 ｜ h4-h6 16px/600 ｜ small 12px
- `-webkit-font-smoothing: antialiased`；`-webkit-text-size-adjust: none`
- 段落间距：`p+p / p+ul / ul+h2+` 统一 `margin-top: 1.5em`；section 上下 2.5rem

## 布局骨架
- 顶栏高度 `--header-height: 58px`
- 侧边导航宽度 `--sidebar-nav-width: 260px`
- 全站 `box-sizing: border-box`

## 色彩系统（:root 自定义属性，每色 0-9 十级）
完整色阶在原始 HTML `#cfBaseStyles` 里（`--cf-red-0..9`、`orange`、`gold`、`green`、`cyan`、`blue`、`indigo`、`violet`、`pink`、`gray`）。常用值：

| 角色 | 变量 | 值 |
|---|---|---|
| 主色（链接/主按钮） | `--cf-blue-4` | `#0051c3` |
| 主色 hover/active | `--cf-blue-2` | `#003681` |
| 主色 focus | `--cf-blue-5` | `#086fff` |
| 浅蓝底（选中/hover 背景） | `--cf-blue-9` | `#ecf4ff` |
| 成功 | `--cf-green-5` | `#228b49`（亮：`--cf-green-6 #2db35e`） |
| 成功浅底 | `--cf-green-9` | `#e3f8eb` |
| 警告 | `--cf-orange-5` | `#c05d08`（亮 `#ee730a`） |
| 危险 | `--cf-red-5` | `#e81403`（亮 `#fc574a`） |
| 危险浅底 | `--cf-red-9` | `#ffefee` |
| 正文 | `--cf-gray-1` | `#313131` |
| 次要文字 | `--cf-gray-4/5` | `#595959` / `#797979`（placeholder 同） |
| 边框 | `--cf-gray-8` | `#d9d9d9`（hr 用 `#d5d7d8`） |
| 浅底 | `--cf-gray-9` | `#f2f2f2`（code/thead 背景） |

另有 `--cf-newGray-0..9`（#191919→#EAEAEA）、`--cf-newGreen-0..9`（新版绿）、`--cf-sequential-0..13`（图表序列色，首色 `#3E8EFF`）。
深色模式（overlay 实测值）：卡片底 `#1f2223`、主文字 `#f2f4f5`、次文字 `#a0a4a6`、边框 `rgba(255,255,255,0.12)`、遮罩 `rgba(0,0,0,0.66)`。

## 组件模式
- **链接**：`#0051c3` + 下划线（`text-underline-offset: 4px`）+ `transition: color 150ms ease`；hover/active `#003681`；focus `#086fff`；`a svg { fill: currentColor }`
- **卡片**（Medic overlay 实测）：圆角 12px；浅底 `#ffffff` + 边框 `rgba(0,0,0,0.06)`；阴影 `0 1px 2px rgba(0,0,0,0.08), 0 12px 32px -8px rgba(0,0,0,0.28)`；内边距 32-36px；内容 gap 14px
- **表格**：`border-spacing: 0`；thead 背景 `#f2f2f2`；th 600
- **code/pre**：背景 `#f2f2f2`、边框 1px `#d9d9d9`、圆角 0.5rem、字号 14px、padding 0.5rem 0.75rem
- **hr**：无边框、顶部 1px `#d5d7d8`、上下 margin 2rem
- **label**：block、0.875rem、margin-bottom 0.35938em、min-height 1.22em
- **tooltip**：13px、padding 8px 21px、圆角 3px、深底 `#222`、箭头用 border 三角（8/10px）
- **spinner 强调色**：`#f6821f`（橙色，track 用同色 18-28% 透明度）
- **列表**：`list-style-position: outside`、disc、`margin-left: 3em`

## CSS 工程手法
- 基础样式包在 `@layer cf` 里（避免被组件样式意外覆盖）
- 全部 token 放 `:root` 自定义属性
- 字体/关键资源 `<link rel="preload" as="font">`
- `@view-transition { navigation: auto }` 页面过渡
- 动效尊重 `prefers-reduced-motion`

## 一句话气质
干净、留白足、层级靠字重而非颜色：细体大 h1 + 600 小标题、蓝色只出现在可交互元素、灰阶做一切中性面。
