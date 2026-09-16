<div align="center">

# WeChat Download API

### 微信公众号文章获取 · RSS 订阅 · 多格式导出

**完全开源 | 免费部署 | RSS 订阅 | 文章抓取 | 多格式导出 | 反风控**

[![GitHub stars](https://img.shields.io/github/stars/tmwgsicp/wechat-download-api?style=for-the-badge&logo=github)](https://github.com/tmwgsicp/wechat-download-api/stargazers)
[![License](https://img.shields.io/badge/License-AGPL%203.0-blue?style=for-the-badge)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/tmwgsicp/wechat-download-api?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/tmwgsicp/wechat-download-api)
[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

> **100% 开源，100% 免费。** 代码完全公开，私有化部署无任何限制，不搞"开源"之名行收费之实。

</div>

---

## 功能特性

- **RSS 订阅** — 订阅任意公众号，自动定时拉取新文章（**包含完整文章内容和图片**），生成标准 RSS 2.0 源，接入 FreshRSS / Feedly 等阅读器即可使用；支持**批量添加**（粘多个公众号名称一次性订阅）
- **MCP · AI 客户端接入** — 内置 MCP 服务，Claude / Codex / Cline / Cursor 等 AI 客户端可**直接搜索、订阅、读文章**（6 个工具，静态 Token 鉴权，单用户自托管无需 OAuth）
- **文章内容获取** — 通过 URL 获取文章完整内容（标题、作者、正文 HTML / 纯文本、图片列表）
- **多格式导出** — 单篇 markdown 增量同步（带 YAML frontmatter，导入 Obsidian / Logseq）；整号文章一键打包成 **Markdown / HTML / Word / PDF / EPUB / Excel / JSON** 7 种格式（Word/PDF/EPUB 图片内嵌离线可看），纯读本地库、不触发抓取
- **不需要微信公众号** — 数据源是[微信读书](https://weread.qq.com)：扫码登录后，把你在微信读书里关注的公众号一键导入成订阅即可（见 [微信读书通道](#微信读书通道)）
- **扫码登录 + 自动续期** — 微信扫一下完成登录；`wr_skey` 过期自动用 `wr_rt` 换新，不用反复扫码
- **从书架发现公众号** — 微信读书 App 里关注谁，这里就能列出谁，一键批量导入
- **文章列表 & 搜索** — 获取公众号文章列表，支持分页和关键词过滤
- **链路诊断** — `GET /api/weread/diagnose` 把登录态、书架、列表、正文逐步跑一遍，指出卡在哪
- **图片代理** — 代理微信 CDN 图片，解决防盗链问题
- **Webhook 通知** — 登录过期提醒（提前24h/6h预警+已过期通知）、触发验证等事件自动推送（支持企业微信机器人）
- **API 文档** — 自动生成 Swagger UI / ReDoc，在线调试所有接口

<div align="center">
  <img src="assets/dashboard.jpg" width="800" alt="管理面板">
  <p><em>管理面板 — 登录状态、内容管理、系统诊断、在线测试一站式管理</em></p>
  <br>
  <img src="assets/rss.jpg" width="800" alt="RSS 订阅管理">
  <p><em>RSS 订阅管理 — 搜索公众号一键订阅，复制地址接入 RSS 阅读器</em></p>
</div>

---

## 📦 整号导出，随身带走

订阅只是第一步。**把整个公众号的存量文章一键打包成你想要的格式**——喂给 AI、导入笔记、离线存档、换设备迁移，全都行。

- ❌ 一篇一篇复制粘贴，图片还丢失
- ❌ 想喂给 AI，却拿不到干净的 Markdown
- ❌ 换阅读器 / 换设备，历史内容带不走
- ✅ **一个公众号一键打包**，7 种格式任选，**纯读本地库、不触发任何微信抓取**（零风控风险）

| 格式 | 后缀 | 适合场景 | 图片 | 单次上限 |
|------|------|----------|------|----------|
| 📝 Markdown 合集 | `.zip` | 归档 / 喂 AI / 导入 Obsidian、Logseq | 引用式 | 3000 |
| 🌐 HTML 合集 | `.html` | 单文件、带目录、暗色适配，浏览器直接看 | 引用式 | 3000 |
| 📊 Excel | `.xlsx` | 文章清单表（标题 / 作者 / 时间 / 链接） | — | 3000 |
| 🔧 JSON | `.json` | 二次开发 / 数据分析 | — | 3000 |
| 📄 Word | `.docx` | 可编辑文档，图去重压缩 | **内嵌** | 500 |
| 📕 PDF | `.pdf` | 固定排版、内置中文字体免装 | **内嵌** | 200 |
| 📱 EPUB | `.epub` | 手机 / 电子书阅读器读合集 | **内嵌** | 500 |

> **文本/表格类**（Markdown / HTML / Excel / JSON）图片引用式、秒出、零抓图带宽；**离线自包含类**（Word / PDF / EPUB）会把微信图现抓内嵌进文件、断网也能看图。全部只导已抓到正文的文章，支持按时间范围（全部 / 最近 N 天 / 时间窗 / 增量）筛选。

在 **RSS 管理页**（`/rss.html`）每个订阅右侧点「**下载文章**」即可可视化选格式与时间范围；也可直接调接口 `GET /api/export/account/{fakeid}.{格式}`，详见下方 [整号文章导出（多格式）](#整号文章导出多格式)。

---

## Docker 部署 🐳

> **镜像从哪来**：本仓库的 GitHub Actions 会把镜像推到 GHCR（`ghcr.io/always1ov/wechat-download-api`），用内置 `GITHUB_TOKEN`，**不需要配置任何 secret**。
> 打 `v*` tag 出正式版（amd64 + arm64），推 `main` 出 `:latest`，推 `claude/**` 分支出开发版（amd64）。
> GHCR 的包默认是私有的，想让别人免登录拉取，需要到仓库 Packages 页把它改成 Public。
> ⚠️ **fork 仓库默认关闭 Actions**，需先到 `Settings → Actions → General` 打开，工作流才会跑。
> 也可以本地构建：`docker build -t wechat-download-api .`


**最快速的部署方式**，无需配置 Python 环境，一键启动：

```bash
# 方式一：docker compose（推荐）—— 只要 docker-compose.yml 一个文件
git clone https://github.com/always1ov/wechat-download-api.git
cd wechat-download-api
docker compose up -d --build

# 方式二：直接 docker run（先本地构建出镜像）
docker build -t wechat-download-api:local .
docker run -d \
  -p 5000:5000 \
  -v $(pwd)/data:/app/data \
  -e SITE_URL=http://192.168.1.10:5000 \
  -e TZ=Asia/Shanghai \
  --name wechat-api \
  wechat-download-api:local
```

> 配置**不需要**额外的 `.env`：所有选项都写在 `docker-compose.yml` 的 `environment:` 里，
> 改那里即可。记得把 `SITE_URL` 改成实际访问地址，否则 RSS 图片显示不出来。
> 习惯用 `.env` 的话也支持 —— 同目录放一个，会自动覆盖 compose 里的默认值
> （`cp env.example .env` 可作为起点，里面有全部变量的详细说明）。

> compose 默认用 **CI 构建好的镜像** `ghcr.io/always1ov/wechat-download-api:latest`，
> 粘贴即可部署，不用克隆仓库。**别换成 `tmwgsicp/wechat-download-api`** —— 那是上游镜像，
> 没有本仓库的改动（整条微信读书通道都在这边），拉了等于没更新。
> 想从源码构建，把 `image:` 那两行换成 `build: .`。

**更新到最新版**：`docker compose pull && docker compose up -d`

**确认到底更没更新**：`curl http://localhost:5000/api/health`，看 `build.short_sha`；
管理页最底下也会显示，形如 `claude/kind-bell-ian46x @ 1a2b3c4`。
拉完发现页面没变、`short_sha` 也没变，就是没拉到新镜像。

服务启动后访问 `http://localhost:5000/admin.html`，按卡片上的三步走：

1. 扫码登录微信读书（跳 `/login.html`）
2. 从书架导入公众号（跳 `/rss.html`，在「添加订阅」里）
3. RSS 订阅管理

**支持多架构**：`linux/amd64` / `linux/arm64`（Apple Silicon、树莓派、ARM 服务器）

---

## SaaS 托管版 — 已上线 🚀

**不想折腾部署？30 秒注册即可使用** 👉 **[wechatrss.waytomaster.com](https://wechatrss.waytomaster.com)**

搜索公众号名称，拿到 RSS 链接，丢进你的阅读器——Feedly、Inoreader、NetNewsWire 全部兼容。

---

## 使用前提

**不需要微信公众号。** 本服务的数据全部来自微信读书：

1. 在**微信读书 App** 里关注你想订阅的公众号
2. 部署后打开 `/login.html`，用微信扫码登录微信读书
3. 打开 `/rss.html`，在「添加订阅」里点「从微信读书书架导入」，一键变成 RSS 订阅

登录态存在 `data/` 目录，重启不丢；`wr_skey` 过期会自动用 `wr_rt` 续期，一般不用反复扫码。

> **本地电脑可以直接用**，不需要公网服务器 —— 本地起服务后通过 `localhost` 访问即可。
> 只有当你要从其他设备（如手机 RSS 阅读器）远程访问时，才需要公网服务器或内网穿透。

---

## 本地落盘与离线阅读

一句话：**采到的东西只向微信/微信读书要一次，之后全部吃本地。**

| 内容 | 落在哪 | 之后还会不会再去要 |
|------|--------|--------------------|
| 文章正文 | SQLite（`data/rss.db`） | **不会** —— 轮询只抓库里没有正文的那几篇 |
| 文章图片 | `data/imgcache/` | **不会** —— 阅读器/导出/各设备都吃本地副本 |
| 登录态 | `data/.weread.json` | 过期自动续期，不用反复扫码 |

轮询器每轮仍会问一次**列表**接口（否则不知道有没有新文章），但正文和图片
都只在「这篇是新的」时才抓。实测：3 篇文章连轮 4 轮，正文请求恒定 3 次；
发一篇新的才 +1。

> 这里修掉过一个隐蔽的浪费：列表接口每轮都把最近 N 篇原样返回，而解析出来的
> 文章对象里本来就没有正文字段，所以「这篇还没有正文」永远成立 —— 每一轮都在
> 把同样几篇正文重抓一遍。20 个订阅号 × 每轮 10 篇 = 每小时 200 次重复请求，
> 内容一个字都没变。现在会先查库跳过。

**正文**存在本地库里（`RSS_FETCH_FULL_CONTENT=true`，默认开），阅读器直接读，
不用回头访问 `mp.weixin.qq.com`。

**图片**默认指向本服务的 `/api/image` 代理，并带**本地磁盘缓存**：

```
采集时预热 ──┐
             ├─→ data/imgcache/  ←─ RSS 阅读器 / 导出内嵌 / 各设备
微信 CDN ────┘        （只在第一次回源）
```

实测：一张图，采集预热 1 次 + 10 次读者取图 + PDF/Word/EPUB 三种导出，
**微信总共只被请求 1 次**；重启后仍是 0 次（缓存落盘）。

> 这里改掉了一个旧行为：以前 RSS 输出会把图**还原成微信直链**（图快一点点）。
> 代价是每个订阅者 × 每次刷新 × 每台设备都直接打微信 CDN —— 请求量成倍放大，
> 正是最容易惹风控的部分；而且读者访问不到微信时整篇文章的图全挂。
> 现在默认走自家代理，想回到旧行为设 `RSS_DIRECT_IMAGES=true`。

| 变量 | 说明 | 默认 |
|------|------|------|
| `IMAGE_CACHE` | 是否启用图片缓存 | `true` |
| `IMAGE_CACHE_MAX_MB` | 缓存上限（MB），超了按最久未用淘汰 | `512` |
| `IMAGE_CACHE_DIR` | 缓存目录 | `data/imgcache` |
| `IMAGE_PREFETCH` | 采集正文时顺手把图抓进缓存 | `true` |
| `RSS_DIRECT_IMAGES` | RSS 正文里的图还原成微信直链 | `false` |

缓存状态看 `GET /api/health` 的 `image_cache` 字段。删掉 `data/imgcache/`
只是下次重抓，不会丢文章。

**完全离线**（连本服务都访问不到）的场景用导出：Word / PDF / EPUB 会把图
内嵌进文件，详见 [整号文章导出](#整号文章导出多格式)。

---

## 微信读书通道

公众号后台那条路有三个绕不开的坑：**凭证约 4 天过期**、`appmsgpublish` **有频率风控**、正文页直抓**会触发验证码**。任意一个踩中，轮询器和文章接口就会静默失联——表现就是「RSS 不更新了」「文章拿不到正文」。而且它要求你**本人是公众号管理员**，大多数人根本进不去后台。

所以本项目把那条链路整个删掉了，数据全部走 [微信读书](https://weread.qq.com)：Web 端能读到同样的公众号文章，用的是独立的一套登录态（`wr_skey` / `wr_vid` / `wr_rt`），不需要任何公众号身份。

> 方案来源：[rachelos/we-mp-rss#442](https://github.com/rachelos/we-mp-rss/issues/442)

### 配置

**方式一：扫码登录（推荐）**

打开 `/login.html`（管理页「快捷操作 → ① 扫码登录微信读书」也是这个页），用微信扫一下就好。凭证会自动验证并保存到 `data/.weread.json`。

同一页底部还有「校验登录态」「手动续期」「手动填 Cookie」三个维护按钮，平时用不到。

命令行同样可以：

```bash
# 取二维码（data.qr_image 是 PNG 的 data URI，可直接在浏览器打开）
curl -X POST http://localhost:5000/api/weread/qrcode

# 扫完轮询状态，state 走 waiting → scanned → confirmed
curl http://localhost:5000/api/weread/qrcode/status
```

**方式二：手动粘 Cookie**

1. 浏览器登录 <https://weread.qq.com>
2. 按 `F12` 打开开发者工具 → `Network` 面板 → 刷新页面，点开任意一个请求
3. 在 `Request Headers` 里复制完整的 `Cookie`（需要包含 `wr_skey` / `wr_vid` / `wr_rt`）
4. 粘进 `/login.html` 底部的「手动填 Cookie」，或用下面的环境变量 / 接口写入

```bash
# 环境变量（优先级高于管理页写入）
# .env
WEREAD_COOKIE=wr_vid=...; wr_skey=...; wr_rt=...

# 或接口写入，存到 data/.weread.json，不用重启
curl -X POST http://localhost:5000/api/weread/cookie \
  -H "Content-Type: application/json" \
  -d '{"cookie": "wr_vid=...; wr_skey=...; wr_rt=..."}'
```

配好后查一下状态：

```bash
curl http://localhost:5000/api/weread/status
# {"success":true,"data":{"configured":true,"enabled":true,"vid":"...","keepalive":{...}}}
```

### 登录态自动维护

部署完扫一次码，之后基本不用再管：

| 机制 | 什么时候动 | 干什么 |
|------|-----------|--------|
| **被动续期** | 某个请求撞上 `-2012` / `-2041` | 当场用 `wr_rt` 换新 `wr_skey`，重试这次请求 |
| **主动守护** | 启动 30 秒后一次，之后每 6 小时（±15% 抖动） | 探活；坏了就续期，续完再验一次确认真的活了 |

只有被动续期是不够的：容器刚起或长时间没人调接口时，第一发请求必然先失败一次；
轮询器一小时才跑一轮，这一次失败就是一小时的空窗。守护把失败挡在用户看见之前。

#### 频率是怎么定的（别随手改小）

公开资料里 `wr_skey` 的有效期约 **5400 秒（1.5 小时）**，过期后接口判 401 / `-2012`。
照这个数字，「让 skey 永不过期」需要每小时续一次、一天 24 次 —— 这个方向是错的：

1. **没必要。** 续期本来就按需触发，任何请求撞上 `-2012` 都会当场续期并重试，用户无感。
   守护要抓的是 **`wr_rt` 也死了**这种只能人工重新扫码的情况，这种事一个月未必遇到一次，
   6 小时内发现完全够。
2. **反而更危险。** 微信读书的风控除了看请求频率，还看**请求规律性** —— 严格等间隔本身
   就是机器特征。所以这里不但不压缩间隔，还给它加了 ±15% 抖动。
3. **重复。** 轮询器默认一小时一轮，本来就在持续证明登录态是活的。所以守护加了一条：
   **最近 1 小时内有真实请求成功过，就跳过这一轮探活**。正常运行时守护几乎不产生额外流量
   （实测：轮询器活跃时，守护连跑 3 轮一次接口都没发）。

作为参照，同类项目（如 weread2notion-pro）的同步频率是 2~3 小时一次且长期在跑；
本项目守护一次只打一个 `/web/shelf/sync`，比那轻得多。
`WEREAD_KEEPALIVE_INTERVAL` 低于 **1800 秒会被抬到 1800** —— 查得再勤也不会更早发现
`wr_rt` 失效，只会平白多打接口。

> **真正的封号风险不在守护这儿**，而在两个地方：
> 1. **采集频率** —— `RSS_POLL_INTERVAL`（轮询间隔）、`WEREAD_CONTENT_INTERVAL`（正文最小间隔，
>    建议 ≥ 2 秒）、`WEREAD_MAX_PAGES`（单次翻页数）。订阅号多的时候这三个才是大头。
> 2. **多端同时登录同一账号** —— 微信读书对此限制很严，手机 App、网页、本服务同时在线时
>    登录态容易互相踢掉。表现就是「刚扫完码没多久就失效」。

**`wr_rt` 也过期了怎么办**（长期不用 / 微信读书侧主动失效）—— 这时续期救不回来，
只能重新扫码。守护会连续失败两次后认定这种情况，然后：

- 发 webhook 报警（配了 `WEBHOOK_URL` 的话），告诉你去 `/login.html` 重新扫
- 管理页挂一条红色横幅，登录状态卡同步改成「登录已失效」

恢复正常后还会再发一条，免得你以为一直挂着。

```bash
# 不想等下一轮，立刻查一次
curl -X POST http://localhost:5000/api/weread/keepalive/check

# 看守护状态（上次检查/续期时间、连续失败次数、要不要重新扫码）
curl http://localhost:5000/api/weread/status   # 看 data.keepalive
```

| 变量 | 说明 | 默认 |
|------|------|------|
| `WEREAD_KEEPALIVE` | 是否开启守护 | `true` |
| `WEREAD_KEEPALIVE_INTERVAL` | 检查间隔（秒），低于 1800 会被抬到 1800；实际等待带 ±15% 抖动 | `21600`（6 小时） |

#### 续期是怎么做的

微信读书的 `wr_skey` 是**短效令牌**（扫码下发的往往只有 8 个字符），过期后接口返回 `-2012`；`wr_rt` 才是长期 refreshToken。

所以遇到 `-2012` / `-2041` 时，本项目会自动 `POST /web/login/renewal`，用 `wr_rt` 换一个新的 `wr_skey` 并重试本次请求 —— **不用重新扫码，也不用重新粘 Cookie**。续期结果会写回 `data/.weread.json`，重启后继续有效（`WEREAD_COOKIE` 环境变量托管时只在内存中生效，因为程序不会去改你的部署配置）。

续期在进程内串行并带 30 秒冷却，多个轮询任务同时撞到过期时只会打一次续期接口。想关掉设 `WEREAD_AUTO_RENEW=false`；想手动触发用 `POST /api/weread/renew`。

> 扫码登录本身也依赖这条：微信读书刚下发的短 `wr_skey` 在不少环境下会被直接判 `-2012`，登录流程会先续期拿到可用的 `wr_skey`，验证通过才算登录成功。

### 两个域：网页域与 App 域

微信读书有两套接口，本项目都用：

| | 网页域 `weread.qq.com` | App 域 `i.weread.qq.com` |
|---|---|---|
| 认证 | 浏览器 UA + Cookie | **App UA** + 已续期的 `wr_skey`（未续期直接 401） |
| 文章列表 | `/web/mp/articles` | `/book/articles` |
| 正文 | `/web/mp/content` | — |
| **搜公众号** | 没有 | **`/store/search`** |

App 域多出来的「搜公众号」能力，让本项目**连搜索都不必依赖公众号后台**了 —— 这是此前唯一一个绕不开后台的环节。

App 域按「能用就用」处理：不通（401/风控）就记一笔，10 分钟内只走网页域，功能自动退回到没有 App 域时的样子。想彻底关掉设 `WEREAD_APP_API=false`。

> App 域对登录态要求更严，正好吃我们自建的 `/web/login/renewal` 续期结果。**这条路尚未在真实账号上验证过**，验不通也不会让现有功能变坏。可以用 `GET /api/weread/search?query=xxx` 单独试。

### 取数路径

| 环节 | 走哪个接口 | 不通时 |
|------|-----------|--------|
| 找公众号 | App 域 `/store/search` | 退回在**自己书架**里按名字匹配 |
| 文章列表 | 网页域 `/web/mp/articles` | → App 域 `/book/articles` → `/api/mp/cover`（只有最新一篇） |
| 文章正文 | 网页域 `/web/mp/content` | 元数据仍入库，RSS 里可点开原文 |
| 登录态 | 扫码 / `WEREAD_COOKIE` | `wr_skey` 过期自动用 `wr_rt` 续期 |

> **关于书架**：微信读书只对**书架上**的公众号返回文章列表，所以采集前会自动把公众号加入书架（相当于在微信读书里关注它）。不想改动自己的书架就设 `WEREAD_AUTO_ADD_SHELF=false`，然后手动在微信读书 App 里关注这些号。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/weread/status` | 通道状态（是否已配置 / 是否启用 / 取数策略），不回传完整 Cookie |
| `POST` | `/api/weread/qrcode` | 获取登录二维码（PNG data URI） |
| `GET` | `/api/weread/qrcode/status` | 查询扫码状态（waiting / scanned / confirmed / expired / error） |
| `DELETE` | `/api/weread/qrcode` | 取消当前扫码会话 |
| `POST` | `/api/weread/cookie` | 手动配置 Cookie，保存后立刻校验一次 |
| `DELETE` | `/api/weread/cookie` | 清除已保存的 Cookie |
| `POST` | `/api/weread/verify` | 校验当前 Cookie 是否还有效 |
| `POST` | `/api/weread/renew` | 手动用 `wr_rt` 续期 `wr_skey` |
| `POST` | `/api/weread/shelf` | 手动把公众号加入微信读书书架 |
| `GET` | `/api/weread/shelf/accounts` | 列出微信读书书架上的公众号（**不需要公众号后台**） |
| `POST` | `/api/weread/shelf/import` | 把书架上的公众号一键导入为 RSS 订阅并立即采集 |
| `GET` | `/api/weread/diagnose?fakeid=xxx` | **排障用**：把整条链路逐步跑一遍，指出卡在哪一步 |
| `GET` | `/api/weread/search?query=xxx` | 直连微信读书搜公众号（App 域，不经过后台） |
| `GET` | `/api/weread/articles?fakeid=xxx` | 直连微信读书取文章列表（完全不经过后台） |
| `GET` | `/api/weread/content?review_id=xxx` | 直连微信读书取正文，也可传 `url` + `fakeid` |

### 没有公众号？走书架这条路

公众号后台的 `searchbiz` 是搜 fakeid 的唯一官方入口，没有公众号就搜不了。
但微信读书的书架能替代它：**你在微信读书 App 里关注的公众号，这里直接能列出来**。

```bash
# 1. 打开 /login.html 扫码登录微信读书（或配 WEREAD_COOKIE）
# 2. 看看书架上有哪些公众号
curl http://localhost:5000/api/weread/shelf/accounts

# 3. 一键全部导入成 RSS 订阅，并立即采集
curl -X POST http://localhost:5000/api/weread/shelf/import \
  -H "Content-Type: application/json" -d '{}'

# 只导入其中几个：
#   -d '{"fakeids": ["MzI5NjM4MjExMg==", "..."]}'
```

页面上就是 `/rss.html` 「添加订阅」里的**「从微信读书书架导入」**按钮。

书架用的是 `/web/shelf/sync` —— 和登录态校验同一个接口，扫码有效就一定能出结果，
是这套里最稳的一环。想订阅新号，先去微信读书 App 关注它，再回来点一次导入。

> 顺带：`/api/public/searchbiz` 在后台不可用时，也会先试 App 域搜索，
> 再退回「从书架里按名字匹配」，所以搜索接口本身也不会因为没有公众号而彻底失效。

### 拉不到文章怎么办

先跑诊断，它会把 Cookie、登录态、续期、书架、三条取列表的路、正文逐个试一遍，
告诉你到底断在哪：

```bash
curl "http://localhost:5000/api/weread/diagnose?fakeid=你的公众号fakeid" | python3 -m json.tool
```

几个常见原因：

| 现象 | 原因 | 处理 |
|------|------|------|
| 搜索框搜不到某个号 | 它不在你的微信读书书架上，而全网搜索（App 域 `/store/search`）对登录态要求很严、经常不可用 | 先在微信读书 App 里关注它，公众号会自动进书架，再回来搜；或直接用「从微信读书书架导入」 |
| 刚订阅就去看，是空的 | 旧版本只写库、等下一轮轮询（默认 1 小时） | 已修：订阅会立即在后台抓一次；也可手动 `POST /api/rss/poll` |
| 诊断显示登录态 `-2012` | `wr_skey` 过期 | 有 `wr_rt` 会自动续期；诊断里续期也失败就重新扫码 |
| 「加入书架」失败 | 微信读书只对**书架上**的公众号返回文章 | 手动在微信读书 App 里关注该号，或检查 Cookie 是否含 `wr_vid` |
| 只拿得到一篇 | 列表接口被风控，退到了 `/api/mp/cover` | 该接口一次只返回最新一篇，属预期降级 |
| App 域步骤全红 | i 域对登录态更严 | 不影响网页域采集，只是搜公众号得靠后台 |

### 已知限制

- **`wr_rt` 本身也有寿命**：`wr_skey` 过期能自动续期，但 `wr_rt` 失效后（长期不用 / 微信读书侧主动失效）续期也会失败，这时需要重新扫码。
- **拿不到「号内搜索」**：微信读书能搜公众号（`/store/search`），但没有「在某个号内搜文章」的接口。`/api/public/articles` 带 `keyword` 时，只能对已拉回的列表做标题/摘要过滤，召回范围受 `WEREAD_MAX_PAGES` 限制。
- **App 域未实测**：`i.weread.qq.com` 那几个接口（搜索、列表）是按公开可见的调用形态实现的，尚未在真实账号上跑通。全部挂在回退结构下，不通就退回网页域（`weread.qq.com`），不会让现有功能变坏。
- **正文接口限流较严**：`WEREAD_CONTENT_INTERVAL` 建议保持 ≥ 2 秒。
- **只认短链**：`reviewId` 由 `bookId` + 文章短链 token 拼成，所以 `/api/article` 只支持 `https://mp.weixin.qq.com/s/<token>` 形式的链接；长链（`/s?__biz=...`）没有 token，推不出 `reviewId`。
- **列表接口偶发不可用**：微信读书曾一度停掉 `/web/mp/articles`。遇到这种情况会自动退到 `/api/mp/cover`，但那个接口一次只返回**最新一篇**，补不了历史。

---

## 快速开始

### 方式一：Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/tmwgsicp/wechat-download-api.git
cd wechat-download-api

# 2. 配置环境变量
cp env.example .env
# 编辑 .env，设置 SITE_URL 为实际访问地址（如 http://your-domain.com）

# 3. 启动服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f
```

### 方式二：一键脚本部署

**第一步：克隆项目**

```bash
git clone https://github.com/tmwgsicp/wechat-download-api.git
cd wechat-download-api
```

**第二步：一键启动**

```bash
bash start.sh  # Linux/macOS
# 或
start.bat      # Windows
```

脚本会自动完成环境检查、虚拟环境创建、依赖安装和服务启动。

**第三步：扫码登录**

访问 `http://localhost:5000/login.html`，用微信扫码登录**微信读书**（不需要公众号管理员身份）。
登录完去 `/rss.html` 「添加订阅 → 从微信读书书架导入」。

---

## API 使用

### 访问地址

| 地址 | 说明 |
|------|------|
| http://localhost:5000 | 管理面板 |
| http://localhost:5000/login.html | 扫码登录微信读书 · 凭证维护 |
| http://localhost:5000/rss.html | RSS 订阅管理 · 书架导入 |
| http://localhost:5000/api/docs | Swagger API 文档 |
| http://localhost:5000/api/health | 健康检查 |

---

## 服务器部署

### Linux 生产环境（systemd）

`start.sh` 脚本在 Linux 上以 `sudo` 运行时，会自动注册 systemd 服务并启用开机自启：

```bash
sudo bash start.sh
```

之后可通过以下命令管理服务：

```bash
# 查看运行状态
bash status.sh

# 停止服务
bash stop.sh

# 手动操作
sudo systemctl restart wechat-download-api
sudo systemctl status wechat-download-api
```

### 配置反向代理（可选）

如需通过域名或 HTTPS 访问，配置 Nginx 反向代理到 `localhost:5000`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### 环境变量

复制 `env.example` 为 `.env` 并按需修改：

```bash
cp env.example .env
```

主要配置项参见 `env.example` 中的注释说明。

---

## MCP — AI 客户端接入

内置 **MCP（Model Context Protocol）服务**，让 Claude、Codex、Cline、Cursor 等 AI 客户端**直接搜索公众号、订阅、读文章**，不用切网页、不用手动调接口——对 AI 说"帮我订阅 XX 公众号、把最新几篇总结一下"即可。

**6 个工具：**

| 工具 | 作用 |
|------|------|
| `search_accounts` | 按名称搜索公众号，拿 fakeid |
| `subscribe_account` | 订阅公众号（传 fakeid） |
| `unsubscribe_account` | 取消订阅 |
| `list_subscriptions` | 列出已订阅的公众号 |
| `get_recent_articles` | 拉最新文章（支持时间游标增量、按公众号过滤） |
| `read_article` | 读某篇文章的完整正文 |

**启用（`.env`）：** 单用户自托管，鉴权走**静态 Bearer Token**，无需 OAuth。`ENABLE_MCP` 和 `MCP_TOKEN` **两者都要设置**，缺一则 MCP 不启用。

```bash
ENABLE_MCP=1
MCP_TOKEN=设一个足够长的随机串           # 客户端凭它鉴权（必填，留空则不启用）
# MCP_RESOURCE_URL=https://rss.example.net/mcp  # 部署到公网域名时设（DNS-rebinding 白名单）
```

服务挂载在 `/mcp`（streamable-http）。

**客户端配置：**

```bash
# Claude Code
claude mcp add --transport http wechatrss https://rss.example.net/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

```jsonc
// Cursor / Cline 等（JSON 配置）
{
  "mcpServers": {
    "wechatrss": {
      "url": "https://rss.example.net/mcp",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

> 本地自测：`http://localhost:5000/mcp`。启用后未带正确 Token 会返回 401。

---

## API 接口

> 以下 HTTP 接口**无需鉴权**：调用方不用传任何 Token 或 `Authorization` 头。微信登录态由服务端扫码登录后内部持有并自动使用（前提是管理页面已扫码登录）。`MCP_TOKEN` / `Authorization: Bearer` 仅用于上面的 MCP 客户端接入，与这些 HTTP 接口无关。
>
> 文章解析与公众号搜索/文章列表接口（`/api/article`、`/api/public/searchbiz`、`/api/public/articles`、`/api/public/articles/search`）统一返回 `{ "success": bool, "data": {...}, "error": null }`，**业务数据都在 `data` 字段下**；业务失败（如登录态失效）返回 HTTP 200 且 `success: false`，请以 `success` 字段判断成败。（增量同步接口 `/api/feed/articles.json` 与 `/api/health` 直接返回数据对象、不带此包装；RSS 接口返回 XML；`/api/feed/article/{id}.md` 返回 markdown 文本。）

### 获取文章内容

`POST /api/article` — 解析微信公众号文章，返回标题、正文、图片等结构化数据

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 微信文章链接（`https://mp.weixin.qq.com/s/...`） |

请求示例：

```bash
curl -X POST http://localhost:5000/api/article \
  -H "Content-Type: application/json" \
  -d '{"url": "https://mp.weixin.qq.com/s/xxxxx"}'
```

返回字段（均在 `data` 下）：`title` 标题、`content` HTML 正文、`plain_content` 纯文本正文、`images` 图片 URL 列表、`author` 作者、`publish_time` 发布时间戳（秒）、`publish_time_str` 可读发布时间（如 `2026-02-24 09:00:00`）

### 搜索公众号

`GET /api/public/searchbiz` — 按关键词搜索微信公众号，获取 FakeID

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索关键词（公众号名称） |

请求示例：

```bash
curl "http://localhost:5000/api/public/searchbiz?query=公众号名称"
```

返回示例：

```json
{
  "success": true,
  "data": {
    "list": [
      {
        "fakeid": "MzI5NjM4MjExMg==",
        "nickname": "示例公众号",
        "alias": "example_wx",
        "round_head_img": "http://你的部署地址/api/image?url=...",
        "service_type": 1
      }
    ],
    "total": 1
  },
  "error": null
}
```

返回字段（公众号列表在 `data.list` 下）：
- `fakeid` — 公众号唯一 ID（后续获取文章、订阅时使用）
- `nickname` — 公众号名称
- `alias` — 微信号
- `round_head_img` — 头像地址（已转为服务器图片代理链接）
- `service_type` — 类型（`0`=订阅号 / `1`=服务号 / `2`=企业号）
- `data.total` — 匹配数量（已过滤黑名单后的条数）

### 获取文章列表

`GET /api/public/articles` — 获取指定公众号的文章列表，支持分页

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `fakeid` | string | 是 | 目标公众号的 FakeID（从搜索接口获取） |
| `begin` | int | 否 | 偏移量，默认 `0` |
| `count` | int | 否 | 获取数量，默认 `10`，最大 `100` |
| `keyword` | string | 否 | 在该公众号内搜索关键词 |

请求示例：

```bash
# 获取前 50 篇
curl "http://localhost:5000/api/public/articles?fakeid=YOUR_FAKEID&begin=0&count=50"

# 获取第 51-100 篇
curl "http://localhost:5000/api/public/articles?fakeid=YOUR_FAKEID&begin=50&count=50"
```

返回示例：

```json
{
  "success": true,
  "data": {
    "articles": [
      {
        "aid": "2650000000_1",
        "title": "示例文章标题",
        "link": "https://mp.weixin.qq.com/s/AbCdEfGhIj",
        "update_time": 1700000000,
        "create_time": 1699999000,
        "digest": "文章摘要",
        "cover": "http://mmbiz.qpic.cn/cover/0",
        "author": "作者名"
      }
    ],
    "total": 42,
    "begin": 0,
    "count": 1,
    "keyword": null
  },
  "error": null
}
```

返回字段（文章列表在 `data.articles` 下）：
- `aid` — 文章 ID
- `title` — 标题
- `link` — 文章链接
- `update_time` / `create_time` — 更新 / 创建时间戳
- `digest` — 摘要
- `cover` — 封面图地址
- `author` — 作者
- `data.total` — 该公众号文章总数（微信侧）；`data.count` — 本次返回条数；`data.begin` — 本次偏移量；`data.keyword` — 本次搜索关键词（未传为 `null`）

### 搜索公众号文章

`GET /api/public/articles/search` — 在指定公众号内按关键词搜索文章

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `fakeid` | string | 是 | 目标公众号的 FakeID |
| `query` | string | 是 | 搜索关键词 |
| `begin` | int | 否 | 偏移量，默认 `0` |
| `count` | int | 否 | 获取数量，默认 `10`，最大 `100` |

请求示例：

```bash
curl "http://localhost:5000/api/public/articles/search?fakeid=YOUR_FAKEID&query=关键词"
```

返回结构与「获取文章列表」一致（文章列表在 `data.articles` 下，`data.keyword` 为本次搜索词）。

### RSS 订阅

`GET /api/rss/{fakeid}` — 获取指定公众号的 RSS 2.0 订阅源

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `fakeid` | string（路径） | 是 | 公众号 FakeID |
| `limit` | int（查询） | 否 | 返回文章数量上限，默认 `20` |

使用方式：

```bash
# 1. 搜索公众号获取 fakeid
curl "http://localhost:5000/api/public/searchbiz?query=人民日报"
# 返回 fakeid: MzA1MjM1ODk2MA==

# 2. 添加订阅
curl -X POST http://localhost:5000/api/rss/subscribe \
  -H "Content-Type: application/json" \
  -d '{"fakeid": "MzA1MjM1ODk2MA==", "nickname": "人民日报"}'

# 3. 手动触发一次轮询（立即拉取文章）
curl -X POST http://localhost:5000/api/rss/poll

# 4. 获取 RSS 源（把这个地址添加到 RSS 阅读器）
curl "http://localhost:5000/api/rss/MzA1MjM1ODk2MA=="
```

也可以通过管理面板的 **RSS 订阅** 页面可视化管理，搜索公众号一键订阅并复制 RSS 地址。

> **关于 RSS 内容**: RSS 源包含**完整文章内容**（图文混排），您可以直接在 RSS 阅读器中阅读全文。
>
> 正文来自微信读书的 `/web/mp/content`，不直抓 mp.weixin.qq.com，因此没有验证码风控问题。
>
> 扫码登录后，系统会**自动**将微信凭证用于内容获取，无需手动配置。如需禁用完整内容获取（仅保留标题和摘要），可在 `.env` 中设置 `RSS_FETCH_FULL_CONTENT=false`。

#### RSS 订阅管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/rss/subscribe` | 添加 RSS 订阅 |
| `DELETE` | `/api/rss/subscribe/{fakeid}` | 取消 RSS 订阅 |
| `GET` | `/api/rss/subscriptions` | 获取订阅列表 |
| `POST` | `/api/rss/poll` | 手动触发轮询 |
| `GET` | `/api/rss/status` | 轮询器状态 |
| `GET` | `/api/rss/all` | **聚合源** — 所有订阅合成一个 RSS，阅读器里加一条就够 |
| `GET` | `/api/rss/category/{category_id}` | **分类源** — 某个分类下所有订阅合成一个 RSS |
| `GET` | `/api/rss/{fakeid}/history` | 单个公众号的历史文章 RSS |
| `GET` | `/api/rss/export` | 导出订阅列表（备份 / 迁移） |

> **分类管理**：可把订阅分组（`GET/POST /api/categories`、`PUT/DELETE /api/categories/{id}`、`POST /api/subscriptions/{fakeid}/category`），再用上面的**分类源**按主题订阅，或用**聚合源**一条读全部。

### Markdown 导出 / 文章同步

把已抓取的文章拉成 markdown（带 YAML frontmatter），可直接导入 Obsidian / Logseq 等工具。

`GET /api/feed/articles.json` — 列出本地已抓取的文章元数据（含文章 `id`），按时间游标增量同步

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `since` | int（查询） | 否 | Unix 时间戳，返回 `publish_time > since` 的文章；首次传 `0` |
| `fakeid` | string（查询） | 否 | 只看某个公众号 |
| `limit` | int（查询） | 否 | 单次返回条数（1-200），默认 `50` |

响应含 `next_since`（本批最后一篇的发布时间），作为下次 `since` 循环调用，拉到 `articles` 为空即同步完成；之后用保存的 `next_since` 做每日增量。

`GET /api/feed/article/{id}.md` — 按 `id` 获取单篇文章的 markdown 正文（带 title / author / nickname / fakeid / publish_time / date（可读时间）/ source_url 等 frontmatter）

```bash
# 1. 拉文章列表拿 id（循环 since 直到返回空）
curl "http://localhost:5000/api/feed/articles.json?since=0&limit=200"

# 2. 按 id 下载某篇 markdown（浏览器 / 下载工具会自动存成「标题.md」）
curl -OJ "http://localhost:5000/api/feed/article/1.md"
```

状态码：`200` 正文 / `404` 不存在 / `422` 内容尚未抓取完成（稍后重试）。

### 整号文章导出（多格式）

把某个公众号**已抓取入库**的文章一次性打包下载，支持 7 种格式。**纯读本地库、不触发任何微信抓取**；在 RSS 管理页（`/rss.html`）每个订阅右侧点「下载文章」即可选格式与时间范围，也可直接调接口。

`GET /api/export/account/{fakeid}.{格式}`

| 格式 | 后缀 | 说明 | 图片 |
|------|------|------|------|
| Markdown 合集 | `.zip` | 每篇一个 `.md` + `INDEX.md`，适合归档 / 喂 AI | 引用式（指向 `/api/image` 代理） |
| HTML 合集 | `.html` | 单文件，带目录、暗色适配 | 引用式 |
| Excel | `.xlsx` | 文章清单表（标题 / 作者 / 时间 / 链接） | — |
| JSON | `.json` | 文章清单数据 | — |
| Word | `.docx` | 可编辑，图去重压缩 | **内嵌**（离线可看） |
| PDF | `.pdf` | 内置中文字体，排版固定 | **内嵌** |
| EPUB | `.epub` | 每篇一章 + 目录，手机 / 阅读器读合集 | **内嵌** |

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `since` | int（查询） | 否 | 只导 `publish_time >= since` 的文章（时间窗起点 / 增量），秒级时间戳 |
| `before` | int（查询） | 否 | 只导 `publish_time <= before` 的文章（时间窗终点） |
| `limit` | int（查询） | 否 | 最多导出篇数（最近优先）。md/html/xlsx/json 上限 3000，Word/EPUB 500，PDF 200 |

```bash
# 整号导出为 Markdown 合集 zip（浏览器 / -OJ 会按「公众号名_导出_N篇.zip」自动命名）
curl -OJ "http://localhost:5000/api/export/account/MzA1MjM1ODk2MA==.zip"

# 只导最近 30 天、导成 EPUB
SINCE=$(($(date +%s) - 30*86400))
curl -OJ "http://localhost:5000/api/export/account/MzA1MjM1ODk2MA==.epub?since=$SINCE"
```

> Markdown / HTML / Excel / JSON 用图片引用式，秒出、零抓图带宽（在线打开时显示图）；Word / PDF / EPUB 会现抓微信图内嵌进文件、离线自带图，篇数多时稍慢。全部只导已抓到正文的文章。

### 其他接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/image?url=IMG_URL` | 图片代理（仅限微信 CDN 域名） |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/stats` | 限频统计 |
| `POST` | `/api/weread/qrcode` | 获取微信读书登录二维码 |
| `GET` | `/api/weread/qrcode/status` | 查询扫码状态 |
| `GET` | `/api/admin/status` | 查询登录状态 |
| `GET` | `/api/weread/status` | 微信读书通道状态（详见 [微信读书通道](#微信读书通道)） |
| `POST` | `/api/admin/logout` | 退出登录 |

完整的接口文档请访问 http://localhost:5000/api/docs

---

## 配置说明

复制 `env.example` 为 `.env`，登录后凭证会自动保存：

```bash
cp env.example .env
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `WEBHOOK_URL` | Webhook 通知地址（支持企业微信机器人） | 空 |
| `WEBHOOK_NOTIFICATION_INTERVAL` | 同一事件通知最小间隔（秒） | 300 |
| `RATE_LIMIT_GLOBAL` | 全局每分钟请求上限 | 10 |
| `RATE_LIMIT_PER_IP` | 单 IP 每分钟请求上限 | 5 |
| `RATE_LIMIT_ARTICLE_INTERVAL` | 文章请求最小间隔（秒） | 3 |
| `RSS_POLL_INTERVAL` | RSS 轮询间隔（秒） | 3600 |
| `ARTICLES_PER_POLL` | 每次轮询每个公众号拉取的文章批次数 | 10 |
| `RSS_FETCH_FULL_CONTENT` | RSS 是否获取完整内容（true/false） | true |
| `WEREAD_COOKIE` | **微信读书 Cookie（不填就走 `/login.html` 扫码登录，二选一）** | 空 |
| `WEREAD_AUTO_RENEW` | wr_skey 过期时自动用 wr_rt 续期 | true |
| `WEREAD_APP_API` | 启用微信读书 App 域接口（`i.weread.qq.com`，多一个搜公众号能力） | true |
| `WEREAD_ENABLED` | 强制开关微信读书通道（留空=配了 Cookie 就启用） | 空 |
| `WEREAD_AUTO_ADD_SHELF` | 采集前自动把公众号加入微信读书书架 | true |
| `WEREAD_CONTENT_INTERVAL` | 微信读书正文请求最小间隔（秒） | 2 |
| `WEREAD_PAGE_INTERVAL` | 微信读书列表翻页最小间隔（秒） | 1 |
| `WEREAD_MAX_PAGES` | 单次列表采集最多翻几页（每页约 50 条） | 5 |
| `SITE_URL` | **网站访问地址（用于RSS图片代理，必须配置）** | http://localhost:5000 |
| `PORT` | 服务端口 | 5000 |
| `HOST` | 监听地址 | 0.0.0.0 |
| `DEBUG` | 调试模式（开启热重载） | false |

> **⚠️ 重要**: `SITE_URL` 必须配置为实际访问地址（IP或域名），否则RSS图片无法正常显示。例如：
> - 本地开发: `http://localhost:5000`
> - 局域网部署: `http://192.168.1.100:5000`
> - 公网域名: `https://rss.example.net`

## 项目结构

```
├── app.py                # FastAPI 主应用
├── requirements.txt      # Python 依赖
├── env.example           # 环境变量示例
├── data/                 # 数据目录（运行时自动创建）
│   └── rss.db            # RSS 订阅 SQLite 数据库
├── routes/               # API 路由
│   ├── article.py        # 文章内容获取
│   ├── articles.py       # 文章列表
│   ├── account.py        # 公众号主体信息
│   ├── rss.py            # RSS 订阅管理与输出
│   ├── search.py         # 公众号搜索
│   ├── feed.py           # 本地文章增量同步（articles.json / markdown 导出）
│   ├── login.py          # 扫码登录
│   ├── admin.py          # 管理接口
│   ├── image.py          # 图片代理
│   ├── health.py         # 健康检查
│   └── stats.py          # 统计信息
├── mcp_server/           # MCP 服务（AI 客户端接入）
│   ├── server.py         # MCP 服务端（静态 Bearer Token 鉴权）
│   ├── tools.py          # MCP 工具（搜索/订阅/读文章等）
│   └── store_adapter.py  # 公众号搜索/订阅数据适配
├── utils/                # 工具模块
│   ├── auth_manager.py   # 认证管理
│   ├── helpers.py        # HTML 解析
│   ├── http_client.py    # HTTP 客户端（curl_cffi + 代理池）
│   ├── proxy_pool.py     # 代理池轮转
│   ├── rate_limiter.py   # 限频器
│   ├── rss_store.py      # RSS 数据存储（SQLite）
│   ├── rss_poller.py     # RSS 后台轮询器
│   ├── login_reminder.py # 登录过期提醒（主动检测）
│   ├── content_processor.py  # 内容处理与图片代理
│   ├── image_proxy.py    # 图片URL代理工具
│   ├── article_fetcher.py    # 批量并发获取文章
│   └── webhook.py        # Webhook 通知
└── static/               # 前端页面（含 RSS 管理）
```

---

## 内容类型与获取策略

本项目支持多种微信公众号内容类型，包括标准富文本、纯图片文章、图文消息、短内容、音频文章等。

详细说明请查看：**[CONTENT_TYPES.md](CONTENT_TYPES.md)**

**文档内容**：
- 所有支持的内容类型及 `item_show_type` 值
- 不可用状态识别（删除、违规、隐私、验证页面等）
- 反爬策略与代理配置
- 关键函数说明
- 开发贡献指南

---

## 常见问题

<details>
<summary><b>提示"服务器未登录"</b></summary>

访问 http://localhost:5000/login.html 扫码登录，凭证会自动保存到 `.env`。
</details>

<details>
<summary><b>触发微信风控 / 需要验证</b></summary>

1. 在浏览器中打开提示的文章 URL 完成验证
2. 等待 30 分钟后重试
3. 降低请求频率（系统已内置自动限频）
</details>

<details>
<summary><b>如何获取公众号的 FakeID</b></summary>

调用搜索接口：`GET /api/public/searchbiz?query=公众号名称`，从返回结果的 `fakeid` 字段获取。
</details>

<details>
<summary><b>Token 多久过期？如何提前知道？</b></summary>

Cookie 登录有效期约 4 天，系统会：
1. 前端显示到期时间（`/api/admin/status` 接口返回 `expireTime` 和 `isExpired` 字段）
2. **后台每 6 小时主动检测**，提前 24h / 6h 通过 Webhook 预警
3. 过期后立即通过 Webhook 通知

配置 `WEBHOOK_URL`（支持企业微信群机器人）可收到实时提醒，避免因凭证过期导致 RSS 轮询失败或搜索功能不可用。
</details>

<details>
<summary><b>可以同时登录多个公众号吗</b></summary>

当前版本不支持多账号。建议部署多个实例，每个登录不同公众号。
</details>

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **Web 框架** | FastAPI |
| **ASGI 服务器** | Uvicorn |
| **HTTP 客户端** | curl_cffi（Chrome TLS 指纹）/ HTTPX（降级） |
| **反风控** | 不直抓 mp.weixin.qq.com，改用微信读书接口，天然绕开验证码 |
| **RSS 存储** | SQLite（零配置，数据本地化） |
| **配置管理** | python-dotenv |
| **运行环境** | Python 3.8+ |

---

## 开源协议

本项目采用 **AGPL 3.0** 协议开源，**所有功能代码完整公开，私有化部署完全免费**。

| 使用场景 | 是否允许 |
|---------|---------|
| 个人学习和研究 | 允许，免费使用 |
| 企业内部使用 | 允许，免费使用 |
| 私有化部署 | 允许，免费使用 |
| 修改后对外提供网络服务 | 需开源修改后的代码 |

详见 [LICENSE](LICENSE) 文件。

### 免责声明

- 本软件按"原样"提供，不提供任何形式的担保
- 本项目仅供学习和研究目的，请遵守微信公众平台相关服务条款
- 使用者对自己的操作承担全部责任
- 因使用本软件导致的任何损失，开发者不承担责任

---

## 参与贡献

由于个人精力有限，目前**暂不接受 PR**，但非常欢迎：

- **提交 Issue** — 报告 Bug、提出功能建议
- **Fork 项目** — 自由修改和定制
- **Star 支持** — 给项目点 Star，让更多人看到

---

## 联系方式

<table>
  <tr>
    <td align="center">
      <img src="assets/qrcode/wechat.jpg" width="200"><br>
      <b>个人微信</b><br>
      <em>技术交流 · 商务合作</em>
    </td>
    <td align="center">
      <img src="assets/qrcode/sponsor.jpg" width="200"><br>
      <b>赞赏支持</b><br>
      <em>开源不易，感谢支持</em>
    </td>
  </tr>
</table>

- **GitHub Issues**: [提交问题](https://github.com/tmwgsicp/wechat-download-api/issues)
- **邮箱**: creator@waytomaster.com
- **SaaS 托管版**: [wechatrss.waytomaster.com](https://wechatrss.waytomaster.com)

---

## 致谢

- [FastAPI](https://fastapi.tiangolo.com/) — 高性能 Python Web 框架
- [curl_cffi](https://github.com/lexiforest/curl_cffi) — 支持浏览器 TLS 指纹模拟的 HTTP 客户端
- [HTTPX](https://www.python-httpx.org/) — 现代化 HTTP 客户端
- [gost](https://github.com/go-gost/gost) — 轻量级代理工具

---

<div align="center">

**如果觉得项目有用，请给个 Star 支持一下！**

[![Star History Chart](https://api.star-history.com/svg?repos=tmwgsicp/wechat-download-api&type=Date)](https://star-history.com/#tmwgsicp/wechat-download-api&Date)

Made with ❤️ by [tmwgsicp](https://github.com/tmwgsicp)

</div>
