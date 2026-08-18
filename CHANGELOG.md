# 更新日志

## v1.0.3 (2026-08-18)

### ✨ 新功能

- **新增 QQ 扫码登录支持（`#kgqq登录`）**：对齐 KuGouMusicApi 新增的 QQ 扫码授权链路（`/login/qq/qr/create` + `/login/qq/qr/check`），支持通过手机 QQ 扫码完成酷狗账号授权与绑定，自动换取并持久化 `token` / `userid` 到插件配置 `defaultCookie`。

### 🐛 修复与优化

- **Playwright Chromium 自动下载优化**：渲染卡片时若检测到未安装 Chromium 浏览器二进制，自动通过国内高速镜像源（`PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/`）在后台子线程中静默安装并恢复渲染，避免卡片渲染报错崩溃。

## v1.0.2 (2026-08-15)

### ✨ 新功能

- **ffmpeg 压缩兜底**：大文件/FLAC 无法作为文件发送时（QQ 官方无分片上传、或文件发送失败），用 ffmpeg 压成紧凑 mp3（`compressBitrate`，默认 128k）再发送，不再丢失文件通道。新增 `ffmpegCompress` 配置（默认开）；ffmpeg 缺失或压缩失败时退回旧行为（跳过文件仅发语音）
- 守卫拦截时压缩成功 → 文件名带 `.mp3`；原文件与压缩文件都按 `keepFileSec` 调度清理

### 🧹 质量

- `_deliver_local_audio` 新增 `_ffmpeg_path` / `_compress_to_mp3` 助手；测试扩至 36 用例（压缩成功/失败/无 ffmpeg、守卫拦截压缩兜底、发送失败压缩重试）
- 第二轮重构：`_send_file_payload` 扁平化（提前 return 消除深嵌套）；`download_audio` 改流式写入磁盘（大 FLAC 不再整块读入内存），失败时清理残留文件；测试扩至 **42 用例**（新增 download_audio 流式/过小/HTML/HTTP 错误清理 + 压缩重试失败文案兜底）

## v1.0.1 (2026-08-15)

### ✨ 新功能

- **QQ 官方大文件分片上传**：AstrBot ≥ 4.27.3 的 QQ 官方适配器对本地 >10MB 文件自动走分片上传（修复大文件无法发送的问题）。插件新增 `qqofficialChunkedUpload` 配置（默认开）：开启时放行 FLAC/>10MB 文件发送，不再降级为仅语音；关闭或旧版 AstrBot（< 4.27.3）保留原守卫（大文件仅发语音 silk）。语音/文件双通道互不阻塞逻辑不变。

### 🧹 质量

- 守卫逻辑抽成可单测的 `_should_block_qqofficial_file` / `_qq_official_chunked_upload_supported(version)`；修复拦截提示在语音未开启时误导（"改发语音"不成立）→ 改为如实提示"音频文件未发送"
- `deliver_song` 拆分出可单测的 `_deliver_local_audio`（语音/文件双通道投递 + wxoc 降级 + 文件守卫 + 文案兜底 + 清理调度），`deliver_song` 只负责文案/卡片/下载后委托
- 新增 `tests/test_delivery_chunked.py`（31 用例，mock 无网络）：版本检测 + 守卫决策矩阵 + 本地音频投递端到端（双发/单发/文件拦截回退语音/全部失败文案兜底/微信降级/清理调度）

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
- 53 个指令 handler，ruff 全过、55 个 pytest mock 单测、42 项真实 API 冒烟测试
- 10 个 art-template 卡片模板（酷狗蓝主题）

### 已知限制
- 歌单曲目未登录时返回 20010，需登录 Cookie
- 酷狗按设备 dfid 动态限流（dfid 配额波动可能导致免费歌也返回试听流）
