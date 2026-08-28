from __future__ import annotations

# 酷狗音质档位（song/url 的 quality 参数 + hash 音质体系）
# 注意：酷狗每首歌、每个音质一个专属 32 位文件 hash，
# 要拿某档 URL 必须用该档 hash（128→FileHash/hash_128，320→HQ.Hash/hash_320，
# flac→SQ.Hash/hash_flac，high→hash_high/Res.Hash，super→hash_super）
# 档位名（docs/README.md 与 privilege_lite.js 核对）：
#   128 标准 · 320 高品 · flac 无损 · high Hi-Res(无损)
#   viper_atmos 蝰蛇全景声 · viper_tape 蝰蛇母带 · viper_clear 蝰蛇超清 · super 蝰蛇HiFi(DSD)
KUGOU_QUALITY_LIST = [
    {"label": "自动（自适配最高可用）", "value": "auto"},
    {"label": "蝰蛇母带2.0", "value": "viper_tape"},
    {"label": "蝰蛇超清", "value": "viper_clear"},
    {"label": "蝰蛇HiFi", "value": "super"},
    {"label": "Hi-Res", "value": "high"},
    {"label": "无损 FLAC", "value": "flac"},
    {"label": "高品 320K", "value": "320"},
    {"label": "标准 128K", "value": "128"},
]

# 从高到低完整阶梯（auto 时从最高档起试；VIP 档匿名会 502/status2 自动降级）
QUALITY_LADDER = ["viper_tape", "viper_clear", "super", "high", "flac", "320", "128"]

QUALITY_LABEL = {"auto": "自动适配"}
QUALITY_LABEL.update({item["value"]: item["label"] for item in KUGOU_QUALITY_LIST if item["value"] != "auto"})


def trial_label(play: dict) -> str:
    """取链结果 → 展示用音质标签（试听流追加「试听 60s」）。"""
    ql = play.get("qualityLabel") or QUALITY_LABEL.get(play.get("quality") or "", "") or ""
    if play.get("trial"):
        return f"{ql}（试听 60s）" if ql else "试听 60s"
    return ql


def quality_candidates(preferred: str = "auto") -> list[str]:
    """从偏好音质起向下返回候选阶梯（auto → 全部从高到低）。"""
    q = (preferred or "auto").lower()
    if q in ("auto", "adaptive", "best"):
        return list(QUALITY_LADDER)
    idx = QUALITY_LADDER.index(q) if q in QUALITY_LADDER else 0
    return QUALITY_LADDER[idx:]


# 各音质对应的 hash 取值优先级（歌曲字典来自不同来源，字段名有差异）
def _first(*vals) -> str:
    for v in vals:
        if v:
            return str(v)
    return ""


def hash_for_quality(song: dict, quality: str) -> str:
    """从归一化歌曲字典里取某音质的专属 hash。

    蝰蛇系（viper_tape/viper_clear/viper_atmos）与 super 没有独立 hash 字段，
    由最高无损源（hash_super → hash_high → hash_flac）派生，属尽力而为。
    """
    q = (quality or "").lower()
    if q in ("viper_tape", "viper_clear", "viper_atmos", "super"):
        return _first(
            song.get("hash_super"),
            song.get("hash_high"),
            (song.get("Res") or {}).get("Hash"),
            song.get("hash_flac"),
            (song.get("SQ") or {}).get("Hash"),
        )
    if q == "high":
        return _first(
            song.get("hash_high"),
            (song.get("Res") or {}).get("Hash"),
            song.get("hash_flac"),
            (song.get("SQ") or {}).get("Hash"),
        )
    if q == "flac":
        return _first(song.get("hash_flac"), (song.get("SQ") or {}).get("Hash"))
    if q == "320":
        return _first(song.get("hash_320"), (song.get("HQ") or {}).get("Hash"))
    return _first(song.get("hash_128"), song.get("hash"), song.get("FileHash"))
