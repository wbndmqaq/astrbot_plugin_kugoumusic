"""用户可见文案常量。

只收敛在插件内被 **多处** 引用的文案（错误提示、tip 等），避免同一句话分散在各处
再次漂移；一次性的文案仍留在各自的调用点。文案值必须与收敛前逐字一致。

带 ``{}`` 的常量用 ``str.format(...)`` 填充。
"""

from __future__ import annotations

# ──────────── 通用错误提示 ────────────

# 配置写盘失败（旧版 AstrBot 无异步写盘 API）
CONFIG_SAVE_FAILED = "⚠ 配置写入失败（本机 AstrBot 版本可能过旧），请到 WebUI 插件配置中修改，或升级 AstrBot"

# 未登录时的统一提示
NEED_LOGIN = "需要登录后使用，请先 #kg登录"

# 接在错误文案末尾的登录指引后缀
NEED_LOGIN_SUFFIX = "\n需要先 #kg登录"

# 关键词未搜到（.format(关键词)）
NOT_FOUND = "没有搜到「{}」"
NOT_FOUND_SINGER = "没有搜到歌手「{}」"
NOT_FOUND_ALBUM = "没有搜到专辑「{}」"
NOT_FOUND_PLAYLIST = "没有搜到歌单「{}」"

# 列表序号越界（.format(总数)）
INDEX_OUT_OF_RANGE = "序号超出范围（1-{}）"

# 扫码登录被配置关闭
QR_LOGIN_DISABLED = "扫码登录已在配置中关闭"

# ──────────── 通用 tip 文案 ────────────

TIP_ALBUM_TRACKS = "发送 #kg专辑 专辑名 查看曲目"
TIP_PLAYLIST_VIEW = "发送 #kg歌单 歌单名 查看曲目"
TIP_HELP = "发送 #kg帮助 查看全部指令"
TIP_SINGER_HOT = "发送 #kg歌手 歌手名 查看热门歌曲"
TIP_RANK_BY_NAME = "发送 #kg排行 榜单名 查看歌曲"
TIP_RANK_LIST = "发送 #kg排行 榜单名 查看（如 #kg排行 TOP500）"
TIP_THEME_PLAYLIST = "发送 #kg主题歌单 序号 查看曲目（如 #kg主题歌单 1）"
TIP_YUEKU = "发送 #kg好歌 / #kg新碟 / #kg排行 查看对应内容"
TIP_HISTORY = "发送 #kg历史日推 序号 查看（如 #kg历史日推 1）"
TIP_FOLLOW_LIST = "发送 #kg歌手 歌手名 查看热门歌曲；#kg取关 歌手名 取关"

# ──────────── 内容来源标注 ────────────

LYRICS_SOURCE = "歌词来自酷狗音乐"
