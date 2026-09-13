"""帮助指令清单（纯数据表）。

帮助**纯文本**（``cards.format_help_text``）与**卡片**（``cards.build_help_card_data``）
的唯一数据源：新增/改名指令只改这里，两个呈现面同步生效，不再各维护一份清单。

字段语义（与 astrbot_plugin_neteasemusic 的 help_data 同一套约定）：
    name / desc / example —— 卡片字段
    plain       —— 纯文本侧整行（合并行、自动解析说明行）
    plain_skip  —— 纯文本侧不渲染（该条目已并入前一条的 plain 合并行）
    plain_desc  —— 纯文本侧说明与卡片措辞不同时覆盖
    plain_title —— 纯文本侧分组标题覆盖（「管理」→「管理（主人）」）
    plain_no_head —— 纯文本侧不另起分组（沿用上一分组继续列举）
"""

HELP_SECTIONS: list = [
    {
        "title": "点歌播放",
        "tag": "全员可用",
        "items": [
            {"name": "#kg点歌 关键词", "desc": "搜索并列出歌曲列表", "example": "#kg点歌 晴天", "plain_desc": "搜索并列出歌曲"},
            {"name": "#kg听N", "desc": "播放列表第 N 首", "example": "#kg听1", "plain_desc": "播放列表第 N 首（可只发 #听N）"},
            {"name": "#kg听所有", "desc": "依次连播当前列表全部歌曲（上限 30 首）", "example": "#kg听所有"},
            {"name": "#kg播放 关键词", "desc": "搜索并直接播放第一首", "example": "#kg播放 晴天"},
            {"name": "#kg歌词 关键词|hash", "desc": "获取歌词", "example": "#kg歌词 晴天"},
            {"name": "#kg逐字歌词 关键词", "desc": "KRC 逐字歌词", "example": "#kg逐字歌词 晴天"},
            {"name": "#kg热搜", "desc": "热搜榜", "example": "#kg热搜"},
        ],
    },
    {
        "title": "发现音乐",
        "tag": "全员可用",
        "items": [
            {"name": "#kg排行 [榜单名]", "desc": "排行榜列表 / 具体榜单", "example": "#kg排行 TOP500", "plain_desc": "排行榜列表 / 查看具体榜单"},
            {"name": "#kg歌手 关键词", "desc": "歌手热门歌曲", "example": "#kg歌手 周杰伦"},
            {"name": "#kg专辑 关键词", "desc": "专辑曲目", "example": "#kg专辑 叶惠美"},
            {"name": "#kg歌单 关键词|id", "desc": "歌单曲目（VIP 需登录）", "example": "#kg歌单 华语", "plain_desc": "歌单曲目（VIP 歌单需登录）"},
            {"name": "#kg评论 关键词", "desc": "歌曲热评", "example": "#kg评论 晴天"},
            {"name": "#kg相似 关键词|hash", "desc": "相似歌曲", "example": "#kg相似 晴天"},
            {"name": "#kg新歌", "desc": "新歌速递", "example": "#kg新歌"},
            {"name": "#kg新碟 [地区]", "desc": "新碟上架（华语/欧美/日本/韩国）", "example": "#kg新碟 华语", "plain_name": "#kg新碟 [华语/欧美/日本/韩国]", "plain_desc": "新碟上架"},
            {"name": "#kg好歌 [卡片]", "desc": "好歌精选（精选/怀旧/热门/小众）", "example": "#kg好歌 热门", "plain_name": "#kg好歌 [精选/怀旧/热门/小众]", "plain_desc": "好歌精选卡片"},
            {"name": "#kg主题歌单 [序号]", "desc": "主题歌单列表 / 主题曲目", "example": "#kg主题歌单 1", "plain_desc": "主题歌单 / 主题曲目"},
            {"name": "#kg乐库", "desc": "乐库概览", "example": "#kg乐库"},
            {"name": "#kg编辑精选", "desc": "编辑精选专题", "example": "#kg编辑精选"},
            {"name": "#kg排行推荐", "desc": "推荐榜单", "example": "#kg排行推荐"},
            {"name": "#kg历史日推 [序号]", "desc": "历史每日推荐", "example": "#kg历史日推 1"},
            {"name": "#kg精品歌单", "desc": "精选歌单", "example": "#kg精品歌单"},
            {"name": "#kg歌单分类", "desc": "歌单分类列表", "example": "#kg歌单分类", "plain_desc": "歌单分类"},
            {"name": "#kg搜索建议 关键词", "desc": "关键词补全", "example": "#kg搜索建议 晴天"},
            {"name": "#kgMV 关键词", "desc": "MV 详情与播放链接", "example": "#kgMV 晴天"},
            {"name": "#kg高潮 关键词", "desc": "歌曲高潮片段时间", "example": "#kg高潮 晴天"},
            {"name": "#kgAI推荐 关键词", "desc": "AI 相似推荐", "example": "#kgAI推荐 晴天"},
            {"name": "#kg收藏 关键词", "desc": "歌曲收藏数", "example": "#kg收藏 晴天"},
            {"name": "#kg版本 关键词", "desc": "同一首歌的其他版本", "example": "#kg版本 晴天"},
            {"name": "#kg歌手专辑 歌手", "desc": "歌手的专辑列表", "example": "#kg歌手专辑 周杰伦"},
            {"name": "#kg歌手列表 [分类]", "desc": "歌手列表（华语/欧美/日韩等）", "example": "#kg歌手列表 华语"},
            {"name": "#kg歌单评论 / #kg专辑评论", "desc": "歌单/专辑热评", "example": "#kg专辑评论 叶惠美", "plain": "#kg歌单评论 / #kg专辑评论 / #kg评论数 关键词"},
            {"name": "#kg评论数 关键词", "desc": "歌曲评论数", "example": "#kg评论数 晴天", "plain_skip": True},
            {"name": "#kg来首歌", "desc": "随机来一首", "example": "#kg来首歌"},
            {"name": "#kgFM", "desc": "私人 FM（需登录）", "example": "#kgFM"},
        ],
    },
    {
        "title": "推荐",
        "tag": "需登录",
        "plain_no_head": True,
        "items": [
            {"name": "#kg推荐 / #kg日推", "desc": "每日推荐", "example": "#kg日推", "plain_desc": "每日推荐（需登录）"},
        ],
    },
    {
        "title": "账号",
        "tag": "需登录",
        "plain_no_head": True,
        "items": [
            {"name": "#kg我的歌单", "desc": "我创建/收藏的歌单", "example": "#kg我的歌单", "plain": "#kg我的歌单 / #kg最近 / #kg听歌排行  （需登录）"},
            {"name": "#kg最近", "desc": "最近播放歌曲", "example": "#kg最近", "plain_skip": True},
            {"name": "#kg听歌排行", "desc": "听歌排行", "example": "#kg听歌排行", "plain_skip": True},
            {"name": "#kg云盘", "desc": "我的云盘歌曲", "example": "#kg云盘", "plain": "#kg云盘 / #kg已购 / #kg等级 / #kg关注 歌手 / #kg取关 歌手 / #kg关注新歌  （需登录）"},
            {"name": "#kg已购", "desc": "已购单曲/专辑", "example": "#kg已购", "plain_skip": True},
            {"name": "#kg等级", "desc": "听歌等级", "example": "#kg等级", "plain_skip": True},
            {"name": "#kg关注 / #kg取关 歌手", "desc": "关注/取关歌手", "example": "#kg关注 周杰伦", "plain_skip": True},
            {"name": "#kg关注列表", "desc": "我关注的歌手", "example": "#kg关注列表", "plain_desc": "我关注的歌手（需登录）"},
            {"name": "#kg关注新歌", "desc": "关注歌手的上新", "example": "#kg关注新歌", "plain_skip": True},
        ],
    },
    {
        "title": "账号状态",
        "tag": "全员可用",
        "items": [
            {"name": "#kg登录", "desc": "酷狗 App 扫码登录", "example": "#kg登录", "plain_desc": "扫码登录"},
            {"name": "#kgqq登录", "desc": "QQ 扫码授权登录", "example": "#kgqq登录", "plain_desc": "通过 QQ 扫码登录绑定酷狗"},
            {"name": "#kg状态 / #kgs", "desc": "查看登录状态", "example": "#kgs", "plain_desc": "登录状态"},
            {"name": "#kg登出", "desc": "登出", "example": "#kg登出"},
        ],
    },
    {
        "title": "管理",
        "tag": "主人",
        "plain_title": "管理（主人）",
        "items": [
            {"name": "#kg设置", "desc": "设置面板", "example": "#kg设置"},
            {"name": "#kg音质 <档位>", "desc": "修改音质（auto/viper_tape/viper_clear/super/high/flac/320/128）", "example": "#kg音质 high", "plain_desc": "修改音质"},
            {"name": "#kg api <地址>", "desc": "修改 API 地址", "example": "#kg api http://127.0.0.1:4000"},
            {"name": "#kg测试", "desc": "测试 API 连通", "example": "#kg测试"},
            {"name": "#kg帮助", "desc": "查看全部指令", "example": "#kg帮助"},
        ],
    },
    {
        "title": "自动解析",
        "tag": "自动",
        "items": [
            {
                "name": "酷狗链接",
                "desc": "kugou.com 歌曲/歌单链接自动解析播放",
                "example": "https://www.kugou.com/song/#hash=xxx",
                "plain": "发送酷狗音乐分享链接（hash/mixsongid）自动解析播放",
            },
        ],
    },
]
