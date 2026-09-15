# 更新日志

## v2.0.3 (2026-09-15)

### 🐛 缺陷修复

- **卸载时后台任务未等待收尾**：`terminate()` 取消 `_bg_tasks` 后直接关闭 aiohttp 会话，任务收尾阶段仍可能引用已关闭的会话（Windows 下报 `Event loop is closed`/会话已关闭警告）。取消后先 `await asyncio.gather(*tasks, return_exceptions=True)` 再关会话，与 ncm/qq 版同口径。
- **下载前预清理存在同步 IO 漏网**：`delivery.py` 的 `os.path.exists`/`os.remove` 未入线程池，与全局异步卫生标准不一致；已包 `asyncio.to_thread`。

## v2.0.2 (2026-09-12)

### 🐛 缺陷修复

- **拆包后所有播放路径运行时死亡**：`core/api/_song.py` 内 `from .quality import ...` 在拆包后指向不存在的 `core.api.quality`（函数内导入，加载不报错、首次播放才炸 `ModuleNotFoundError` 且 handler 接不住、用户零回复）。改为 `..quality`。
- **每次播放/每张卡片抛 `NameError`**：`delivery.py` 用了 `safe_int` 却没导入——音频发出后抛异常，且清理定时器永不登记，temp 目录永不清理。已补导入。
- **语音/文件通道的文案被写成元组**：`('', True)` 字面量直接发给协议端；现按元组解包。
- **`#kgqq登录` 永远失败**：设备 cookie 覆盖了会话 cookie（`qrsig` 丢失必然 502），改为合并而非覆盖。
- **上报听歌历史发错参数名**（`ot` → `time`）；单曲投递失败不再上报（与连播按真实结果计数同口径）。
- **`#kg听所有` 成功数虚高**：投递层如实返回双通道是否发出，连播按真实结果计数。
- **6 处裸 `int(cfg.get(...))`**：WebUI 清空字段或填非数字时抛 `TypeError`（handler 接不住，零回复）；统一改走 `safe_int`/`cfg_int`。
- **QQ 官方下载失败丢弃挂起的歌曲信息文案**：现把挂起文案拼在失败提示前一并发送。
- **临时文件清理治理**：`keepFileSec` 清空抛 `TypeError` 且音频永不登记清理、卸载只取消不补删、`keepFileSec=0` 过早删除、定时器句柄无登记——统一为登记表 + 取消补删 + 5 秒下限。
- **`send_chain` 只捕获 `AttributeError`**：平台侧失败穿透 handler；现默认不穿透并保留「失败即降级」链。
- **API 总超时穿透 handler**：`asyncio.TimeoutError` 不是 `aiohttp.ClientError`，用户零回复且消息继续流向后续管道；转为带超时秒数的 `ApiError`。
- **个人微信收不到歌曲信息文案**：wxoc 分支把文案直接置空；现与其他平台同路径发送。
- **配置写盘「假成功」**：旧版 `await None` 吞异常仍回复成功、新版返回值未校验；统一 `MusicService.save_config()` 并如实提示。
- **会话 TTL 锚定首次写入**：`updatedAt` 被旧值覆盖，600 秒后必失效；改让新时间戳胜出。
- **`#kg主题歌单`/`#kg历史日推` 冲掉点歌会话**：共用同一 KV 键互相覆盖；`SessionStore` 按 `kind` 分桶。
- **aiocqhttp 文件发送失败走错通道重试**：普通 `File` 组件跨容器必然再失败，改 base64 直发。
- **`#kg评论数`/`#kg排行推荐` 被 `#kg评论`/`#kg排行` 吞掉**：正则重叠同优先级，补 priority。
- **设备 dfid 注册 100% 失败**：`StarTools.get_data_dir()` 反查不到插件名必然抛错；显式传插件名。
- **上游错误文案全部丢失**：失败响应 `{status, msg}` 的 `msg` 不在候选链里，统一降级成「HTTP 502」；新增 `_upstream_msg()` 透出。
- **原生音乐卡片发不出去**：改 `event.bot.call_action` 直发 OneBot `music` 段。
- **零碎修正**：`#kg排行` 榜单名空串恒真；评论数按「任意 32 位 key」取值张冠李戴；帮助卡片假统计数字（`45+`/`1.0.0`）；fire-and-forget 任务未持引用；`_deliver_local_audio` 拆分后 `finally` 变量预置恢复；`ffmpeg_compress` 推导收敛透传；帮助表补 `#kg帮助`；Jinja 渲染移入线程池；启动扫地单文件竞态。

### 🧩 其他改动

- `core/api.py` 拆分为按域包 `core/api/`（逐符号比对零丢失）；帮助卡片与纯文本两份手写清单合并为单一数据源 `core/help_data.py`。
- 10 套模板直接改写标准 Jinja2（`tpl_adapter.py` 删除），按路径缓存已编译模板、首次读盘/编译/渲染在线程池；`wrap_card_data()` 缺键安全包装。
- 临时目录固定到 `data/plugin_data/astrbot_plugin_kugoumusic/temp`（`tempDir` 配置移除），启动时清理 1 小时前的崩溃残留。
- 移除 40 余处历史导入脚手架与多处死代码；魔法数字提具名常量；重复文案收敛 `core/messages.py`；ffmpeg 探测进程内缓存并启动预热。
- 复用常驻 Chromium 与模块级 HTTP 会话；分享解析前置子串判断；配置写盘/版本号读取等阻塞 IO 移出事件循环。
- 文档：KuGouMusicApi 默认端口更正为 4000（以 `server.js` 代码为准，上游 README 写 3000）；`enable` 描述、`#kg api` 示例、目录树（api 包 + help_data）、别名与 `keepFileSec` 说明同步修正。

## v2.0.1 (2026-08-31)

### ⚡ 内存与资源治理

- **会话缓存容量上限**：`SessionStore` 增加 `MAX_MEM=512` 上限与 `_evict_if_needed()` 淘汰策略（按更新时间淘汰最旧，数据已持久化不丢失），修复类级字典慢泄露。
- **复用 HTTP 会话**：`api.py` 新增模块级 `_get_session()`/`close_session()` 复用 `ClientSession`（请求、重定向探测、QQ 官方跳转全部复用），`terminate()` 改为异步并关闭会话。

### 🚀 流式下载

- **音频下载改流式**：`download_audio` 由 `res.read()` 整读改为 `iter_chunked(256KB)` 分块 + 定时 `asyncio.to_thread` 追加落盘，显著降低内存峰值。

### 🔧 多插件协作

- **`#听N` 跨插件归属标记**：三插件共享「最近活跃归属」标记 `_music_session_owner.json`，裸 `#听N` 仅最近活跃插件响应，带前缀（`#kg听N` 等）始终直接响应，避免多音乐插件同装时抢占。

### 📝 文档

- `requirements.txt` 补充 `jinja2>=3.0.0` 声明。

---

## v2.0.0 (2026-08-28)

### 🏗️ 架构升级 · 全面模块化

- **声明式路由解耦**：`main.py` 精简为纯净生命周期入口，指令系统按领域拆分为 `play`（播放/连播）、`explore`（探索/榜单）、`detail`（详情/歌词）、`auth`（登录/认证）、`system`（系统/设置）、`share`（卡片与链接解析）六大独立处理器。
- **业务服务下沉**：核心逻辑全面下沉至 `core/service.py`（`MusicService`），`api`、`cards`、`delivery`、`quality`、`render`、`tpl_adapter` 统一收拢至 `core/` 目录。

### ⚡ 性能与纯异步加固

- **纯异步非阻塞调度**：所有磁盘写入、Base64 编码、音频转码全面通过 `asyncio.to_thread` / 异步子进程调度，网络请求采用 `aiohttp` 上下文闭环管理，确保 0 事件循环阻塞。
- **零内存泄露**：完善生命周期管理，插件销毁 `terminate()` 时自动取消所有后台扫码轮询任务；卡片图片与临时媒体文件均设有时效延时清理。

### 🐛 缺陷修复

- **修复版本指令正则越界**：修复 `#kg版本` 指令中正则表达式捕获组索引越界引发的 `IndexError`。

---

## v1.0.8 (2026-08-27)

### ⚡ 异步与性能加固

- **全面非阻塞文件 IO**：音频大文件下载（`download_audio`）、卡片渲染 PNG 落盘（`reply_card_or_text`）、登录二维码（`save_qr_image`）全面接入 `asyncio.to_thread(_write_bytes, ...)` 异步写入，彻底消除主事件循环因磁盘写入导致的阻塞。
- **并发与作用域安全**：修正登录轮询与卡片延迟清理中的循环事件作用域引用，全面提升高并发稳定性。

### 🐛 异常与防御完善

- **全量 AST 静态审查**：排查并修复所有潜在的命名作用域、高潮片段格式化以及参数边界问题，确保 0 语法缺陷与纯净运行。

---
## v1.0.7 (2026-08-23)

### 🔒 安全合规

- **移除渲染环境的全部自动安装行为**（插件市场安全审查整改）：删除 render.py 中的 apt 源改写、`apt-get update`、`pip install playwright`、Chromium 二进制自动下载与 `install-deps` 自动执行——插件不再执行任何未经用户确认的系统级安装/网络下载。
- **渲染环境缺失优雅降级**：playwright 包 / Chromium 内核 / 系统库缺失时，所有指令自动回退纯文本，点歌播放不受影响。
- **日志内置完整教程**：渲染环境缺失时日志一次性输出手动安装教程——① `pip install playwright`（含清华镜像写法）；② `python -m playwright install chromium`（含 npmmirror 加速与 Windows PowerShell 写法）；③ 仅 Linux 容器缺系统库时 `playwright install-deps chromium` 或手动 apt-get 安装库列表，附可选的阿里 apt 镜像源换源命令；之后仅简短提示不刷屏。
- **README 教程化**：「卡片渲染环境安装教程」章节扩写为四步完整教程（装包 → 下载内核 → 系统运行库 / 可选换阿里源 → 重载插件），并声明插件绝不自动执行任何系统级安装

### ✨ 新功能

- **▶ 新增 `#kg听所有`**：对当前会话歌曲列表（点歌/专辑/歌单展开等）依次发送全部歌曲的语音+音频文件（上限 30 首，防误触发刷屏）；单曲失败不中断，结束汇报成功/失败数；帮助卡片同步更新。

### 🐛 修复与优化

- **语音修复（1秒时长/手机无法播放）**：aiocqhttp 语音不再插件侧预编码 silk。此前自编的裸 `#!SILK_V3`（无 `\x02` 前缀、且 PyPI `pysilk` 为空壳包导致该路径长期失效）会被协议端按魔数透传——非标码流导致手机无法播放、时长探测失败钳到 1 秒。改为把紧凑 mp3 以 OneBot v11 标准 `record` 段 base64 直发，由协议端（NapCat / SnowLuma 等）自带 ffmpeg addon 统一转成标准 Tencent silk（`\x02#!SILK_V3`、24kHz 单声道），时长按原始音频精确探测。
- **歌词完整显示**：移除 36 行截断，超长歌词自动按每页 36 行分页成多张卡片发送；KRC 逐字歌词同样解除截断。
- **逐字歌词回退**：无 KRC 逐字歌词时自动改为显示普通歌词，不再只提示「暂无」。
- **分享卡片解析修复**：解析入口从 `@filter.regex` 改为全量事件监听——OneBot `json` 段（音乐分享卡片）不会写入 `message_str`，regex 过滤器永远匹配不到导致群里发卡片无反应；`_collect_message_text` 显式读取 Json 组件数据。

## v1.0.6 (2026-08-20)

### ✨ 新功能

- **渲染模块独立 `render.py`**：渲染逻辑收敛进独立 `render.py`，`main.py` 仅负责调用与落盘。
- **跨平台 Playwright 三步运行时就绪**：渲染前主动按序完成——① 确保 playwright Python 包（缺失自动 pip 装清华镜像，装不到位终止）→ ② 仅 Linux：切阿里 apt 源并 `playwright install-deps` 装系统运行库 → ③ 下载 Chromium 二进制（npmmirror 加速）。幂等，首次执行一次后跳过。

### 🐛 修复与优化

- **Linux 识别**：apt 换源 / install-deps 仅在 `platform.system()=="Linux"` 时执行，Windows/macOS 一律跳过，不触碰系统配置。

## v1.0.5 (2026-08-20)

### ✨ 新功能

- **先选歌再操作**：`#kg歌词` / `#kg逐字歌词` / `#kg评论` / `#kgMV` / `#kg收藏` / `#kg相似(版本)` 带关键词先出候选列表，`#kg听N` 再执行对应动作（一次性，用完恢复播放）；不带关键词复用当前会话候选列表。`#kg播放` / `#kg点歌` 保持原有行为。
- **aiocqhttp（OneBot/napcat）语音/文件直发适配**：跨容器不共享文件系统时，语音自动经 ffmpeg→24kHz wav→pysilk 编成标准 silk（几 MB）直发、文件以 base64 内联直发，无需共享挂载；无损/大文件先压成紧凑 mp3 控制载荷。

### 🐛 修复与优化

- **Playwright 缺系统库自愈 + 阿里源**：容器缺 Chromium 系统库（`libnspr4.so` 等）时自动把官方 apt 源切换为阿里镜像并执行 `playwright install-deps chromium`（幂等、备份 .bak、仅动官方域名）；失败时日志给出可直接执行的安装命令。
- `metadata.yaml` 补充 `category` 分类字段。

## v1.0.4 (2026-08-18)

### 🐛 修复与优化
- **市场规范元数据补全**：`metadata.yaml` 补充 `social_link` 与 `tags` 分类标签。

## v1.0.3 (2026-08-18)

### ✨ 新功能

- **新增 QQ 扫码登录支持（`#kgqq登录`）**：对齐 KuGouMusicApi 新增的 QQ 扫码授权链路（`/login/qq/qr/create` + `/login/qq/qr/check`），支持通过手机 QQ 扫码完成酷狗账号授权与绑定，自动换取并持久化 `token` / `userid` 到插件配置 `defaultCookie`。

### 🐛 修复与优化

- **Playwright Chromium 自动安装与镜像加速**：
  - 渲染卡片时若检测到未安装 Chromium 浏览器二进制，自动使用当前 Python 环境（`sys.executable`）在后台子线程中静默安装并恢复渲染。
  - 自动通过国内高速镜像源（`PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/`）下载加速，避免无外网加速时下载慢或超时失败。
  - 为 Chromium 补充 `--no-sandbox`、`--disable-setuid-sandbox`、`--disable-dev-shm-usage` 等 Docker / Linux 环境防崩溃启动参数。

## v1.0.2 (2026-08-15)

### ✨ 新功能

- **ffmpeg 压缩兜底**：大文件/FLAC 无法作为文件发送时（QQ 官方无分片上传、或文件发送失败），用 ffmpeg 压成紧凑 mp3（`compressBitrate`，默认 128k）再发送，不再丢失文件通道。新增 `ffmpegCompress` 配置（默认开）；ffmpeg 缺失或压缩失败时退回旧行为（跳过文件仅发语音）
- 守卫拦截时压缩成功 → 文件名带 `.mp3`；原文件与压缩文件都按 `keepFileSec` 调度清理

### 🧹 质量

- `_deliver_local_audio` 新增 `_ffmpeg_path` / `_compress_to_mp3` 助手；本地投递单测扩至 36 用例（压缩成功/失败/无 ffmpeg、守卫拦截压缩兜底、发送失败压缩重试）；该测试脚本未随仓库提供
- 第二轮重构：`_send_file_payload` 扁平化（提前 return 消除深嵌套）；`download_audio` 改流式写入磁盘（大 FLAC 不再整块读入内存），失败时清理残留文件；本地单测扩至 **42 用例**（新增 download_audio 流式/过小/HTML/HTTP 错误清理 + 压缩重试失败文案兜底）；该测试脚本未随仓库提供

## v1.0.1 (2026-08-15)

### ✨ 新功能

- **QQ 官方大文件分片上传**：AstrBot ≥ 4.27.3 的 QQ 官方适配器对本地 >10MB 文件自动走分片上传（修复大文件无法发送的问题）。插件新增 `qqofficialChunkedUpload` 配置（默认开）：开启时放行 FLAC/>10MB 文件发送，不再降级为仅语音；关闭或旧版 AstrBot（< 4.27.3）保留原守卫（大文件仅发语音 silk）。语音/文件双通道互不阻塞逻辑不变。

### 🧹 质量

- 守卫逻辑抽成可单测的 `_should_block_qqofficial_file` / `_qq_official_chunked_upload_supported(version)`；修复拦截提示在语音未开启时误导（"改发语音"不成立）→ 改为如实提示"音频文件未发送"
- `deliver_song` 拆分出可单测的 `_deliver_local_audio`（语音/文件双通道投递 + wxoc 降级 + 文件守卫 + 文案兜底 + 清理调度），`deliver_song` 只负责文案/卡片/下载后委托
- 新增本地投递单测（31 用例，mock 无网络）：版本检测 + 守卫决策矩阵 + 本地音频投递端到端（双发/单发/文件拦截回退语音/全部失败文案兜底/微信降级/清理调度）；该测试脚本未随仓库提供

## v1.0.0 (2026-08-10)

### 首次发布

**核心功能**
- 点歌播放：`#kg点歌` → `#kg听N`/`#听N`、`#kg播放`、歌词 / KRC 逐字歌词、热搜
- 发现音乐：排行榜、歌手、专辑、歌单、评论、更多版本、新歌速递、精品歌单、歌单分类、搜索建议、MV
- 发现扩展：新碟上架、好歌精选卡片、主题歌单、乐库、编辑精选、排行推荐、历史日推、歌曲高潮、AI 推荐、收藏数、歌手专辑、歌手列表、歌单/专辑评论、评论数
- 推荐：每日推荐、随机来一首、私人 FM
- 账号：扫码登录、状态、登出、我的歌单、最近播放、听歌排行、云盘、已购、听歌等级、关注/取关歌手、关注歌手新歌
- 管理（主人）：设置面板、音质切换、API 地址、连通测试
- 自动：酷狗链接解析、播放后上报听歌历史、登录后刷新 token

**音质体系**
- 7 档音质阶梯：蝰蛇母带2.0 → 蝰蛇超清 → 蝰蛇HiFi(DSD) → Hi-Res → 无损 FLAC → 高品 320K → 标准 128K
- hash 音质体系：每音质专属 32 位文件 hash，按档取链并逐级降级
- 匿名 VIP 歌曲自动 60s 试听降级（URL `p_0_` 标记识别）

**投递与适配**
- 语音（silk 转码）/ 文件双通道发送，可配置
- QQ 官方、个人微信（weixin_oc）、Telegram、钉钉、飞书、KOOK、Discord 平台适配（语音/文件降级）
- OneBot 原生音乐卡片（type=kugou，可选）

**质量**
- 首发版本 53 个指令 handler（当前 55 个），ruff 全过；另有本地 pytest mock 单测与真实 API 冒烟测试（测试脚本未随仓库提供）
- 10 个 HTML 卡片模板（酷狗蓝主题，经 `tpl_adapter` 转换后用 Jinja2 渲染）

### 已知限制
- 歌单曲目未登录时返回 20010，需登录 Cookie
- 酷狗按设备 dfid 动态限流（dfid 配额波动可能导致免费歌也返回试听流）
