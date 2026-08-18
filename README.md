<p align="center">  
  <img src="logo.png" width="120" alt="logo">
</p>

<h1 align="center">astrbot_plugin_kugoumusic</h1>

# 🎵 酷狗音乐点歌/解析插件（AstrBot）

> 基于 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) 的 AstrBot 酷狗音乐插件。

点歌、播放、歌词/逐字歌词、热搜、排行榜、歌手/专辑/歌单/评论、相似/版本、新歌速递、每日推荐、私人 FM、扫码登录、7 档音质自适配与试听降级、语音/文件发送、酷狗链接自动解析 —— 一站式酷狗体验。

---

## ✨ 功能特性

- **点歌播放**：关键词搜索 → 列表卡片 → `#kg听N` 选歌，或 `#kg播放` 直接播第一首
- **音质自适配**：`auto` 自动匹配歌曲最高可用音质并逐级降级；蝰蛇母带2.0 / 蝰蛇超清 / 蝰蛇HiFi / Hi-Res / 无损 / 320K / 128K 共 7 档；未登录 VIP 歌曲自动 60s 试听降级
- **音频投递**：语音（silk 转码）+ 群/好友文件双通道，互不阻塞，失败自动回退
- **多平台适配**：QQ 官方（合并消息规避额度、大文件分片上传、ffmpeg 压缩兜底、纯文本兜底）、个人微信 weixin_oc（语音自动降级为文件）、Telegram / 钉钉 / 飞书 / KOOK / Discord 原生支持语音与文件
- **卡片渲染**：10 套酷狗蓝主题 HTML 卡片（列表/详情/歌词/热搜/评论/榜单/歌单/帮助/状态/设置），自动裁剪白边
- **链接自动解析**：发送 `kugou.com/song/#hash=...` 分享链接，自动识别歌曲并播放
- **扫码登录**：`#kg登录` 生成二维码，轮询自动写入 Cookie，全群共享账号
- **账号扩展**：我的歌单、最近播放、听歌排行、云盘、已购、听歌等级、关注/取关歌手、关注歌手新歌；播放后自动上报听歌历史、登录后自动刷新 token
- **临时文件自清理**：卡片图、二维码、音频文件发出后自动延时清除，`keepFileSec=0` 即时清除

---

## 📦 依赖与前提

| 依赖 | 说明 |
| --- | --- |
| **AstrBot** | `>=4.16, <5`（推荐 4.26+） |
| **API 服务** | [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（默认 `http://127.0.0.1:3000`） |
| **node** | api运行时环境（建议V22的LTS版本以上） |
| **pnpm** | api依赖更新使用 |

> ⚠️ 本插件不内置 API，需自行部署 KuGouMusicApi 服务端。插件通过 HTTP 调用其接口，所有数据来自酷狗音乐。（或者用我frp的API）

### 部署 API 服务（默认有node和pnpm，没有去下载安装）

```bash
git clone https://github.com/MakcRe/KuGouMusicApi.git
cd KuGouMusicApi
npm install
npm run dev   # 默认监听 http://localhost:3000；自定义端口：PORT=XXXX npm run dev
```

---

## 🚀 安装方式

AstrBot WebUI → 插件管理 → 搜索 `astrbot_plugin_kugoumusic` → 安装。

---

## ⚙️ 配置项

WebUI → 插件管理 → 本插件 → 设置面板。也可用指令热改部分项。

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `apiBase` | string | `http://127.0.0.1:3000` | KuGouMusicApi 服务地址（与插件同机部署用 127.0.0.1；自定义端口改对应地址） |
| `enable` | bool | `true` | 插件总开关 |
| `enableSongRequest` | bool | `true` | 点歌功能开关 |
| `enableResolve` | bool | `true` | 酷狗链接自动解析开关 |
| `maxList` | int | `10` | 点歌列表最大显示条数（1–20） |
| `quality` | string | `auto` | 最高播放音质，可选 `auto/viper_tape/viper_clear/super/high/flac/320/128`（蝰蛇与 Hi-Res 档需登录 VIP） |
| `trialFallback` | bool | `true` | 匿名时 VIP/付费歌曲自动发送 60s 试听片段 |
| `sendVocal` | bool | `true` | 以语音消息方式发送音频 |
| `uploadFile` | bool | `true` | 以群/好友文件方式发送音频 |
| `tempDir` | string | `temp/kugou` | 临时下载目录（相对插件目录） |
| `downloadTimeout` | int | `90000` | 音频下载超时（毫秒） |
| `keepFileSec` | int | `60` | 临时文件保留秒数（`0` 表示发出后立即删除，作用于音频+卡片图） |
| `ffmpegCompress` | bool | `true` | 大文件/FLAC 无法作为文件发送时（QQ 官方无分片上传/发送失败），用 ffmpeg 压成紧凑 mp3 兜底发送 |
| `compressBitrate` | int | `128` | 压缩兜底 mp3 码率（kbps） |
| `identifyPrefix` | string | `识别：` | 识别提示前缀 |
| `qrLoginEnable` | bool | `true` | 允许 `#kg登录` 扫码登录 |
| `qqofficialAdapt` | bool | `true` | QQ 官方机器人专用适配（合并消息/语音回退文件/跳过原生卡片） |
| `qqofficialChunkedUpload` | bool | `true` | QQ 官方大文件分片上传（需 AstrBot ≥ 4.27.3）：FLAC/>10MB 文件以分片方式发送；关闭则保留旧守卫（大文件仅发语音） |
| `renderListCard` | bool | `true` | 列表/搜索结果渲染为图片卡片（关闭则纯文本） |
| `sendNativeCard` | bool | `false` | OneBot 原生音乐卡片（type=kugou，仅 aiocqhttp；QQ 官方自动跳过） |
| `sendTextInfo` | bool | `true` | 发送音频时附带歌曲信息文案 |
| `defaultCookie` | string | `""` | 酷狗登录 Cookie（`token=...;userid=...`），可手填或扫码自动写入 |
| `defaultUid` | string | `""` | 扫码登录后自动写入的账号 userid（自动反查补全，一般留空） |

---

## 🎮 指令一览

所有指令以 `#kg` 为前缀（`#` 可省略，大小写不敏感）。会话内选歌也可用简写 `#听N`。

### 🎤 点歌播放

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `#kg点歌 关键词` | 搜索并列出歌曲 | `#kg点歌 晴天` |
| `#kg听N` / `#听N` | 播放列表第 N 首 | `#kg听1` |
| `#kg播放 关键词` | 搜索并直接播放第一首 | `#kg播放 晴天` |
| `#kg来首歌` | 随机来一首（私人 FM，未登录退每日推荐） | `#kg来首歌` |
| `#kg歌词 关键词\|hash` | 获取歌词 | `#kg歌词 晴天` |
| `#kg逐字歌词 关键词\|hash` | KRC 逐字歌词 | `#kg逐字歌词 晴天` |
| `#kg热搜` | 热搜榜 | `#kg热搜` |

### 🌐 发现音乐

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `#kg排行 [榜单名]` | 排行榜列表 / 查看具体榜单 | `#kg排行 TOP500` |
| `#kg歌手 关键词` | 歌手热门歌曲 | `#kg歌手 周杰伦` |
| `#kg专辑 关键词` | 专辑曲目 | `#kg专辑 叶惠美` |
| `#kg歌单 关键词` | 歌单曲目（VIP 歌单需登录） | `#kg歌单 华语` |
| `#kg评论 关键词` | 歌曲热评 | `#kg评论 晴天` |
| `#kg版本 关键词` | 同一首歌的其他版本（翻唱/remix 等） | `#kg版本 晴天` |
| `#kg新歌` | 新歌速递 | `#kg新歌` |
| `#kg精品歌单` | 精选歌单 | `#kg精品歌单` |
| `#kg搜索建议 关键词` | 关键词补全 | `#kg搜索建议 晴天` |
| `#kg歌单分类` | 歌单分类列表 | `#kg歌单分类` |
| `#kgMV 关键词` | MV 详情与播放链接 | `#kgMV 晴天` |

### 🧭 发现扩展

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `#kg新碟 [地区]` | 新碟上架（华语/欧美/日本/韩国） | `#kg新碟 华语` |
| `#kg好歌 [卡片]` | 好歌精选（精选/怀旧/热门/小众/VIP） | `#kg好歌 热门` |
| `#kg主题歌单 [序号]` | 主题歌单列表 / 主题曲目 | `#kg主题歌单 1` |
| `#kg乐库` | 乐库各区块概览 | `#kg乐库` |
| `#kg编辑精选` | 编辑精选专题 | `#kg编辑精选` |
| `#kg排行推荐` | 推荐的排行榜 | `#kg排行推荐` |
| `#kg历史日推 [序号]` | 历史每日推荐 | `#kg历史日推 1` |
| `#kg高潮 关键词` | 歌曲高潮片段时间 | `#kg高潮 晴天` |
| `#kgAI推荐 关键词` | AI 相似推荐 | `#kgAI推荐 晴天` |
| `#kg收藏 关键词` | 歌曲收藏数 | `#kg收藏 晴天` |
| `#kg歌手专辑 歌手` | 歌手的专辑列表 | `#kg歌手专辑 周杰伦` |
| `#kg歌手列表 [分类]` | 歌手列表（华语/欧美/日韩等） | `#kg歌手列表 华语` |
| `#kg歌单评论 关键词` | 歌单热评 | `#kg歌单评论 华语` |
| `#kg专辑评论 专辑` | 专辑热评 | `#kg专辑评论 叶惠美` |
| `#kg评论数 关键词` | 歌曲评论数 | `#kg评论数 晴天` |

### 💎 推荐 / 账号（部分需登录）

| 指令 | 说明 | 备注 |
| --- | --- | --- |
| `#kg推荐` / `#kg日推` | 每日推荐 | — |
| `#kgFM` | 私人 FM 歌曲列表 | — |
| `#kg我的歌单` | 我创建/收藏的歌单 | 需登录（主人） |
| `#kg最近` | 最近播放歌曲 | 需登录（主人） |
| `#kg听歌排行` | 听歌排行 | 需登录（主人） |
| `#kg云盘` | 我的云盘歌曲 | 需登录（主人） |
| `#kg已购` | 已购单曲/专辑 | 需登录（主人） |
| `#kg等级` | 听歌等级 | 需登录（主人） |
| `#kg关注 歌手` / `#kg取关 歌手` | 关注/取关歌手 | 需登录（主人） |
| `#kg关注列表` | 我关注的歌手 | 需登录（主人） |
| `#kg关注新歌` | 关注歌手的上新 | 需登录（主人） |

### 👤 账号状态

| 指令 | 说明 |
| --- | --- |
| `#kg登录` | 扫码登录（生成二维码，轮询自动写入 Cookie，仅主人） |
| `#kg状态` / `#kgs` | 查看登录状态 |
| `#kg登出` | 登出并清除本地 Cookie（仅主人） |

### 🛠️ 管理（仅主人）

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `#kg设置` | 设置面板（登录态/音质/开关/脱敏 API） | `#kg设置` |
| `#kg音质 <档位>` | 修改音质 | `#kg音质 high` |
| `#kg api <地址>` | 修改 API 地址 | `#kg api http://127.0.0.1:3000` |
| `#kg测试` | 测试 API 连通性 | `#kg测试` |
| `#kg帮助` | 帮助卡片 | `#kg帮助` |

### 🔗 自动解析

直接发送酷狗歌曲分享链接（`kugou.com/song/#hash=...`），自动识别：

- **单曲** `hash=...` → 详情卡片 + 播放

---

## 🔊 音频投递说明

插件按 `sendVocal`（语音）和 `uploadFile`（文件）配置双通道投递，两者互不阻塞：

1. **语音**：本地音频经 silk 转码以语音消息发送
2. **文件**：以原始音质文件（mp3/flac 等）作为群/好友文件发送

### 试听降级（`trialFallback`）

酷狗对 VIP/付费歌曲在未登录时只提供 60s 试听流（URL 含 `/yp/p_0_` 标记）。插件自动识别并标注「试听 60s」；`#kg登录` 后即可播放全曲。

### QQ 官方机器人适配（`qqofficialAdapt`）

QQ 官方机器人接口与 OneBot 差异较大，插件做了专项适配：

- **合并消息**：文案与首个媒体合并发送，规避被动回复额度限制
- **大文件分片上传**：AstrBot ≥ 4.27.3 的 QQ 官方适配器对本地 >10MB 文件自动走分片上传，无损 FLAC 也可作为文件发送（`qqofficialChunkedUpload` 开关，默认开）。旧版 AstrBot 或关闭该开关时保留守卫：按大小（>10MB）或后缀（`.flac`）拦截文件上传——此时开启 `ffmpegCompress`（默认开）会用 ffmpeg 压成紧凑 mp3 发送；ffmpeg 缺失/压缩失败才退回仅发语音（silk）
- **纯文本兜底**：媒体发送失败时，文本走 `msg_type=0` 纯文本，规避 `40034011 无效 markdown` 报错
- **原生卡片跳过**：QQ 官方无 OneBot `send_api`，原生音乐卡片自动跳过

### 个人微信（`weixin_oc`）适配

微信开放平台 ilink 通道（手机扫码登录个人微信）。weixin_oc 适配器出站**不支持语音（Record）**，`sendBySession` 只接收 Plain/Image/Video/File：

- **语音自动降级**：`sendVocal` 开启时，语音自动改为文件发送（无需改配置，图片卡片/二维码/文件均正常）
- **原生卡片跳过**：weixin_oc 无 OneBot `send_api`，原生音乐卡片自动跳过

### Telegram / 钉钉 / 飞书 / KOOK / Discord

五个平台适配器出站均**原生支持语音与文件**，插件双通道投递无需调整：

- **Telegram**：`send_voice`，用户隐私设置拒绝语音时适配器自动回退发文档
- **钉钉**：`_prepare_voice_for_dingtalk` 自动转码语音格式
- **飞书**：`convert_audio_to_opus` 转 opus 发送
- **KOOK**：上传资源后以 AUDIO 卡片发送
- **Discord**：语音转 wav 作为附件发送
- 五个平台均无 OneBot `send_api`，原生音乐卡片自动跳过

### aiocqhttp（OneBot）增强

- 可选 `sendNativeCard`：发送 OneBot 原生音乐卡片（type=kugou），需协议端支持 `send_api`


---

## 📁 目录结构

```
astrbot_plugin_kugoumusic/
├── main.py                  # Star 主类：#kg 指令 handler + 辅助方法
├── api.py                   # KugouApiClient：aiohttp HTTP 客户端 + 设备/登录 Cookie + 归一化
├── quality.py               # 酷狗音质阶梯（蝰蛇/Hi-Res/无损/320/128）与标签
├── delivery.py              # 音频下载 → Record/File 投递（含 QQ 官方适配）
├── cards.py                 # SessionStore + 卡片数据构建 + 文本兜底格式化
├── tpl_adapter.py           # art-template → Jinja2 模板适配
├── _conf_schema.json        # 配置项 schema
├── metadata.yaml            # 插件元数据
├── CHANGELOG.md             # 更新日志
├── requirements.txt         # Python 依赖
├── __init__.py
└── resources/html/          # 10 套 HTML 卡片模板（酷狗蓝主题）
    ├── kg-list/             # 列表卡片
    ├── kg-detail/           # 歌曲详情
    ├── kg-lyric/            # 歌词
    ├── kg-hot/              # 热搜榜
    ├── kg-comment/          # 评论
    ├── kg-generic/          # 通用榜单/歌手/专辑列表
    ├── kg-playlist/         # 歌单
    ├── kg-help/             # 帮助
    ├── kg-status/           # 登录状态
    └── kg-settings/         # 设置面板
```

---

## ❓ 常见问题

**Q：提示「未登录」或个人化接口失败？**
A：部分接口（我的歌单、最近播放、云盘、已购、等级等）需登录。发送 `#kg登录` 扫码登录，或手动在配置 `defaultCookie` 填入 `token=...;userid=...` Cookie。酷狗为共享账号，登录后全群使用该账号。

**Q：VIP 歌曲播放失败/只有 60s 试听？**
A：酷狗未登录时 VIP 歌曲只能获取试听流，插件会标注「试听 60s」并发送。`#kg登录` 后即可播放全曲；`trialFallback` 可关闭试听降级（改为提示登录）。

**Q：播放的音频突然全变成 60s 试听？**
A：酷狗按设备 dfid 动态限流。删除 `data/plugin_data/astrbot_plugin_kugoumusic/device_cookies.json` 后重载插件，重新注册设备即可恢复。

**Q：QQ 官方机器人发不出音频文件？**
A：AstrBot ≥ 4.27.3 起 QQ 官方适配器支持大文件分片上传（`qqofficialChunkedUpload` 默认开），FLAC/>10MB 也可正常发送。若分片不可用或仍发送失败，开启 `ffmpegCompress`（默认开）会用 ffmpeg 压成紧凑 mp3 兜底——请确认本机已安装 ffmpeg。

**Q：自动解析不生效？**
A：确认 `enableResolve` 开启，且消息中含完整的酷狗歌曲链接（`kugou.com/song/#hash=...`）。插件指令消息不会被误解析。

**Q：临时文件堆积？**
A：默认 60 秒自动清理。如仍堆积，检查 `keepFileSec` 是否被设为过大值；设为 `0` 即时清理。

---
## 📮 用户群

QQ 群（申请frp的api和插件讨论）：[点击加入](https://qm.qq.com/q/8sOZdZTnaw)

---

## 🙏 致谢

- [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) — 酷狗音乐 API 服务
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 多平台聊天机器人框架

---

## 📄 许可

本项目仅供学习交流使用。所有音乐版权归属酷狗音乐及相应权利人，使用本插件产生的任何后果由使用者自行承担。

---

<div align="center">

如果觉得这个插件对你有帮助，欢迎 Star 一下哈哈

</div>
