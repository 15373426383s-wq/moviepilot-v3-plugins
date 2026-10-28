import random
import re
import time
from datetime import datetime
from threading import Thread
from typing import Any
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.schemas.types import MediaType
from app.db.oper.subscribe import SubscribeOper
from app.chain.subscribe.facade import SubscribeChain
from app.chain.search import SearchChain
from app.chain.download import DownloadChain

# 洗版范围
_SCOPE_ALL = "all"
_SCOPE_MOVIE = "movie"
_SCOPE_TV = "tv"

# 洗版模式
_MODE_NATIVE = "native"   # 复用 MoviePilot 原生最佳版本搜索（全局评分规则 + 订阅目标）
_MODE_CUSTOM = "custom"   # 自定义类别打分，覆盖 MP 原生规则

# 电视剧洗版策略（分集 / 全集）
_TV_AUTO = "auto"          # 分集也洗；一旦出现全集/整季包，自动转全集洗版（推荐）
_TV_PACK_ONLY = "pack_only"  # 只等全集/整季包，分集一律不洗
_TV_EPISODE = "episode"    # 只按分集洗，不区分全集（等同旧行为）

# 9 维评分维度（顺序 = 优先级 #1 → #9，与 MP「洗版评分规则」页一致）
_DIMENSIONS = [
    ("resolution", ["8K", "4K", "1080P", "1080I", "720P", "SD"]),
    ("source", ["UHD-BLURAY-REMUX", "UHD-BLURAY", "BLURAY-REMUX", "REMUX", "BLURAY",
                "WEB-DL", "WEBRIP", "UHDTV", "HDTV", "DVD", "DVDRIP", "HDRIP", "BDMV",
                "SCR", "HDTC", "TC", "TELESYNC", "HDCAM", "CAM"]),
    ("video_codec", ["HEVC", "AV1", "AVC", "VP9", "MPEG2", "VC1", "XVID", "DIVX", "AVS3"]),
    ("audio_codec", ["TRUEHD7.1", "TRUEHD5.1", "TRUEHD", "DTS-HD.MA7.1", "DTS-HD.MA", "DTS-X",
                     "DTS", "DDP5.1", "DDP", "EAC3", "AC3", "AAC2.0", "AAC", "FLAC", "LPCM", "OPUS", "ATMOS"]),
    ("hdr", ["DV", "HDR10+", "HDR10", "HDR-VIVID", "HLG", "SDR", "EDR", "3D", "IMAX", "REPACK", "PROPER", "RERIP", "DIY", "HQ"]),
    ("color_depth", ["12BIT", "10BIT", "8BIT"]),
    ("frame_rate", ["120FPS", "60FPS", "50FPS", "30FPS", "24FPS"]),
    ("subtitle", ["CHS&CHT", "CHS&ENG", "CHS", "CHT", "MULTISUB", "MUXED-SUB", "EXT-SUB",
                  "HARDSUB", "RAW", "NO-SUB", "NO-CHS"]),
    ("file_extension", ["MKV", "MP4", "AVI", "MOV", "WMV", "FLV", "RMVB", "ISO", "M4V",
                       "MPG", "MPEG", "3GP", "TS", "WEBM", "RM"]),
]
_RES_RANK = {"8K": 6, "4K": 5, "1080P": 4, "1080I": 3, "720P": 2, "SD": 1}
# 各维度的中文名 + 一句通俗说明（用于配置页，避免英文术语看不懂）
_DIM_ZH = {
    "resolution":     ("分辨率",   "画面清晰度，如 8K / 4K / 1080P"),
    "source":         ("来源",     "资源出处，如 蓝光原盘 / REMUX / WEB-DL"),
    "video_codec":    ("视频编码", "压缩格式，如 HEVC / H.264 / AV1"),
    "audio_codec":    ("音频编码", "声音格式，如 TrueHD / DTS / AAC"),
    "hdr":            ("画质特效", "如 杜比视界 / HDR10 / SDR"),
    "color_depth":    ("色深",     "色彩细腻度，如 10bit / 8bit"),
    "frame_rate":     ("帧率",     "流畅度，如 60fps / 24fps"),
    "subtitle":       ("字幕",     "如 简繁双语 / 简英 / 无字幕"),
    "file_extension": ("文件格式", "如 MKV / MP4 / ISO"),
}
# 标题里常见的等价写法归一化
_SYN = {"2160P": "4K", "2160": "4K", "CHS.CHT": "CHS&CHT", "CHT.CHS": "CHS&CHT", "CHS/CHT": "CHS&CHT"}
_SYN_CACHE: dict[str, set[str]] = {}


def _variants(tok: str) -> set[str]:
    """某个类别在标题里的所有等价写法（如 4K ↔ 2160P、CHS&CHT ↔ CHS.CHT）。

    PT 站点大多写 2160P 而不是 4K，必须双向映射，否则分辨率维度会整体失配。
    """
    cached = _SYN_CACHE.get(tok)
    if cached is not None:
        return cached
    out = {tok}
    canon = _SYN.get(tok)
    if canon:
        out.add(canon)
    for k, v in _SYN.items():
        if v == tok:
            out.add(k)
    _SYN_CACHE[tok] = out
    return out


class WashUpgrade(_PluginBase):
    """
    评分洗版插件（MoviePilot v3.2.1）。

    两种洗版模式：
    - 原生最佳版本（默认，推荐）：定时/手动对目标订阅触发 MoviePilot 原生的「最佳版本」搜索，
      评分规则、画质/特效目标、分集/全集处理全部由宿主完成（即「洗版评分规则」页 + 订阅「最佳版本」）。
    - 自定义类别打分：插件按你在配置页勾选的类别（分辨率/来源/视频编码…）自行打分，
      搜索并下载更优版本，覆盖 MP 原生规则，做精细控制。

    电视剧分集/全集：默认「分集也洗，出全集后自动转全集」——追更期间按分集洗画质，
    站点一旦出现全集/整季包就自动转洗全集，洗到全集即收工。

    洗版封顶：达到目标画质、洗满次数、连续多轮没有更优资源、或已洗到全集，
    该订阅即判定「洗版完成」，不再检测、不再访问站点，并从详情页列表移除（可一键重置）。

    资源来源：MoviePilot 已配置的 PT 站点（由宿主搜索/下载链负责）。

    日志：写入宿主插件日志（logs/plugins/washupgrade.log）与内存活动缓存
    （在插件「数据」页可见，避免日志文件暂不可读时看不到运行记录）。
    """

    plugin_name = "洗版自用"
    plugin_desc = "评分洗版：原生触发或自选类别打分；电视剧分集也洗、出全集自动转全集；洗到目标即停并从首页移除。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.5.0"
    plugin_author = "your-name"
    author_url = "https://github.com/your-name"
    plugin_config_prefix = "washupgrade_"
    plugin_order = 50
    auth_level = 1

    # 运行态（init_plugin 填充，必须允许重复调用）
    _enabled = False
    _scope = _SCOPE_ALL
    _only_best_version = True
    _crontab = "0 4 * * *"
    _dry_run = True
    _run_once = False
    _wash_mode = _MODE_NATIVE
    # 每个维度：是否启用 + 勾选的类别（按内置优先级 #1→#9 自动排序）
    _dim_enabled: dict[str, bool] = {}
    _dim_values: dict[str, list[str]] = {}
    _min_resolution = ""
    _exclude_keywords = ""
    _bonus_keywords = ""
    _score_threshold = 0
    # 请求频率控制（防止站点因访问过于密集而封禁）
    _interval_seconds = 60      # 每处理完一个订阅后等待的秒数（含随机抖动）
    _batch_limit = 10           # 每轮最多处理多少个订阅（0 = 不限），其余下轮轮转
    _cooldown_hours = 12        # 同一订阅的冷却小时数，冷却内跳过（0 = 不限）
    _tv_mode = _TV_AUTO         # 电视剧：分集也洗，出全集后自动转全集
    # 洗版封顶：达到这些条件后该订阅不再检测、不再洗版
    _max_wash_times = 2         # 同一订阅最多洗几次（0 = 不限）
    _target_resolution = "4K"   # 洗到这个分辨率即封顶（"" = 不按分辨率封顶）
    _target_source = ""         # 洗到这个来源即封顶（"" = 不按来源封顶）
    _stop_on_complete_pack = True   # 电视剧洗到全集即视为完成
    _give_up_rounds = 3         # 连续这么多轮没找到更优版本就收工（0 = 不放弃）
    _hide_done = True           # 已完成的订阅从详情页移除
    _done: dict[str, dict] = {}  # 已完成洗版的订阅（持久化）
    _cursor = 0                 # 分批轮转起点
    _last_wash_at: dict[int, float] = {}   # 订阅 id -> 上次洗版时间戳
    _ver_cache: dict[int, tuple[str, float]] = {}   # 订阅 id -> (当前版本标题, 缓存时间)
    _last_run = "尚未运行"

    # 内存活动缓存（在「数据」页展示）
    _activities: list[str] = []
    # 自定义模式下，每个订阅已下载的最佳分/标题（内存基线，重启清空，靠宿主去重兜底）
    _best_scores: dict[int, int] = {}
    _best_titles: dict[int, str] = {}
    # 防止手动/定时并发重叠执行
    _running = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict | None = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._scope = str(config.get("scope") or _SCOPE_ALL)
        self._only_best_version = bool(config.get("only_best_version", True))
        self._crontab = str(config.get("crontab") or "0 4 * * *")
        self._dry_run = bool(config.get("dry_run", True))
        self._wash_mode = str(config.get("wash_mode") or _MODE_NATIVE)
        # 9 维类别选择（仅自定义模式生效）：每个维度 启用开关 + 勾选类别
        enabled: dict[str, bool] = {}
        values: dict[str, list[str]] = {}
        for dim, all_vals in _DIMENSIONS:
            enabled[dim] = bool(config.get(f"{dim}_enabled", True))
            values[dim] = self._normalize_values(config.get(f"{dim}_values", all_vals), all_vals)
        self._dim_enabled = enabled
        self._dim_values = values
        self._min_resolution = str(config.get("min_resolution") or "")
        self._exclude_keywords = str(config.get("exclude_keywords") or "")
        self._bonus_keywords = str(config.get("bonus_keywords") or "")
        try:
            self._score_threshold = int(config.get("score_threshold") or 0)
        except (TypeError, ValueError):
            self._score_threshold = 0
        # 请求频率控制
        self._interval_seconds = self._to_int(config.get("interval_seconds"), 60, 0, 3600)
        self._batch_limit = self._to_int(config.get("batch_limit"), 10, 0, 500)
        self._cooldown_hours = self._to_int(config.get("cooldown_hours"), 12, 0, 720)
        # 电视剧分集/全集策略（兼容旧配置里的 full_season_only 布尔开关）
        raw_tv_mode = config.get("tv_mode")
        if raw_tv_mode in (_TV_AUTO, _TV_PACK_ONLY, _TV_EPISODE):
            self._tv_mode = str(raw_tv_mode)
        elif "full_season_only" in config:
            legacy = bool(config.get("full_season_only"))
            self._tv_mode = _TV_PACK_ONLY if legacy else _TV_EPISODE
        else:
            self._tv_mode = _TV_AUTO
        # 洗版封顶（到达后不再检测、不再洗版）
        self._max_wash_times = self._to_int(config.get("max_wash_times"), 2, 0, 100)
        # 注意：区分「键不存在」（用默认 4K）与「显式选了不封顶」（空串）
        raw_target_res = config.get("target_resolution", "4K")
        self._target_resolution = str(raw_target_res if raw_target_res is not None else "").strip().upper()
        raw_target_src = config.get("target_source", "")
        self._target_source = str(raw_target_src if raw_target_src is not None else "").strip().upper()
        self._stop_on_complete_pack = bool(config.get("stop_on_complete_pack", True))
        self._give_up_rounds = self._to_int(config.get("give_up_rounds"), 3, 0, 100)
        self._hide_done = bool(config.get("hide_done", True))
        # 冷却记录/轮转游标/版本缓存/完成状态：跨配置重载保留，但每个实例独立一份
        if "_last_wash_at" not in self.__dict__:
            self._last_wash_at = {}
        if "_cursor" not in self.__dict__:
            self._cursor = 0
        if "_ver_cache" not in self.__dict__:
            self._ver_cache = {}
        if "_best_scores" not in self.__dict__:
            self._best_scores = {}
        if "_best_titles" not in self.__dict__:
            self._best_titles = {}
        self._load_done()
        self._record("插件已加载 / 重新配置")
        # 「立即运行一次」开关：保存配置时若为开，则触发一次并自动复位为关，避免重启重复执行
        self._run_once = bool(config.get("run_once"))
        if self._run_once:
            self._run_once = False
            try:
                reset_cfg = dict(config)
                reset_cfg["run_once"] = False
                self.update_config(reset_cfg)
            except Exception as e:  # noqa: BLE001
                self._record(f"复位「立即运行一次」失败：{e}")
            if self._enabled:
                self._trigger_background("立即运行一次")

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # 工具：记录活动（同时写宿主日志 + 内存缓存）
    # ------------------------------------------------------------------ #
    def _record(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts} {msg}"
        self._activities.append(line)
        if len(self._activities) > 200:
            self._activities.pop(0)
        logger.info(f"[评分洗版] {msg}")

    @staticmethod
    def _norm_title(title: str) -> str:
        return (title or "").upper()

    @staticmethod
    def _to_int(raw: Any, default: int, low: int = 0, high: int = 10 ** 6) -> int:
        """把配置里的数字安全转成 int，并夹到 [low, high] 区间。"""
        try:
            v = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, v))

    @staticmethod
    def _is_complete_pack(title: str, total_episode: int = 0) -> bool:
        """判断资源是否为「全集 / 整季包」（用于电视剧分集转全集）。"""
        if not title:
            return False
        t = title.upper()
        if any(k in title for k in ("全集", "合集", "全剧", "整季", "完结")):
            return True
        if re.search(r"全\s*\d+\s*集", title):
            return True
        if total_episode and re.search(rf"全\s*{total_episode}\s*集", title):
            return True
        if "COMPLETE" in t or "SEASON PACK" in t or "COMPLETE PACK" in t or "全集" in t:
            return True
        return False

    @staticmethod
    def _normalize_values(raw: Any, all_vals: list[str]) -> list[str]:
        """把配置里的类别选择规整成「按内置优先级排序」的有效列表。

        兼容前端可能存成数组或逗号字符串；非法值丢弃；空则回退为全部类别。
        """
        if isinstance(raw, str):
            chosen = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, (list, tuple)):
            chosen = [str(s).strip() for s in raw if str(s).strip()]
        else:
            chosen = []
        picked = set(chosen)
        ordered = [v for v in all_vals if v in picked]
        return ordered or list(all_vals)

    # ------------------------------------------------------------------ #
    # 洗版封顶：达到目标程度后不再检测、不再洗版
    # ------------------------------------------------------------------ #
    @classmethod
    def _dim_index(cls, dim: str, title: str) -> int:
        """返回标题在该维度里命中的最优类别下标（越小越优），未命中返回 -1。"""
        if not title:
            return -1
        vals = dict(_DIMENSIONS).get(dim) or []
        tu = cls._norm_title(title)
        ext = tu.rsplit(".", 1)[-1] if "." in tu else ""
        for idx, tok in enumerate(vals):
            vs = _variants(tok)
            if dim == "file_extension":
                if ext in vs:
                    return idx
            elif any(
                v in tu or v.replace("&", ".") in tu or v.replace(".", "&") in tu for v in vs
            ):
                return idx
        return -1

    def _reached_target(self, title: str) -> bool:
        """当前版本是否已经洗到目标档位（分辨率/来源）。用于「到顶就不再洗」。"""
        if not title:
            return False
        res_all = dict(_DIMENSIONS)["resolution"]
        if self._target_resolution:
            if self._target_resolution in res_all:
                need = res_all.index(self._target_resolution)
                got = self._dim_index("resolution", title)
                if got < 0 or got > need:
                    return False
            # 目标值不在候选表里就忽略这一项，避免配置写错导致永不封顶
        if self._target_source:
            src_all = dict(_DIMENSIONS)["source"]
            if self._target_source in src_all:
                need = src_all.index(self._target_source)
                got = self._dim_index("source", title)
                if got < 0 or got > need:
                    return False
        return True

    def _load_done(self) -> None:
        """载入「已完成洗版」记录（持久化在插件数据里）。"""
        if "_done" not in self.__dict__:
            data = None
            try:
                data = self.get_data("done")
            except Exception:  # noqa: BLE001
                data = None
            self._done = data if isinstance(data, dict) else {}

    def _save_done(self) -> None:
        try:
            self.save_data("done", self._done)
        except Exception as e:  # noqa: BLE001
            self._record(f"保存洗版完成状态失败：{e}")

    @staticmethod
    def _done_key(sub: Any) -> str:
        name = str(getattr(sub, "name", "") or "")
        year = str(getattr(sub, "year", "") or "")
        stype = str(getattr(sub, "type", "") or "")
        return f"{stype}|{name}|{year}"

    def _may_finish_early(self, sub: Any, st: dict | None) -> bool:
        """电视剧在「等全集」阶段，即使画质到顶也不算完成——还要等全集来转。

        否则分集先洗到 4K 就会立刻收工，永远等不到自动转全集。
        """
        st = st or {}
        if self._stop_on_complete_pack and self._tv_mode == _TV_AUTO and not st.get("pack"):
            if self._to_mtype(getattr(sub, "type", None)) is MediaType.TV:
                return False
        return True

    def _done_reason(self, sub: Any, cur_title: str = "") -> str:
        """若该订阅已洗到封顶，返回原因（非空即不再检测）；否则返回空串。"""
        st = self._done.get(self._done_key(sub)) or {}
        if st.get("reason"):
            return str(st["reason"])
        times = self._to_int(st.get("count", 0), 0, 0, 10000)
        if self._max_wash_times > 0 and times >= self._max_wash_times:
            return f"已洗版 {times} 次，达到次数上限"
        if self._stop_on_complete_pack and st.get("pack"):
            return "已洗到全集/整季包"
        # 「已达目标画质」只在插件确实洗过之后才算数，否则重置/手动达标会立刻又被判完成
        if (times > 0 and cur_title and self._may_finish_early(sub, st)
                and self._reached_target(cur_title)):
            return f"当前版本已达目标画质（{self._target_resolution or '—'}）"
        return ""

    def _mark_wash(self, sub: Any, cur_title: str, is_pack: bool = False) -> str:
        """记录一次洗版，返回是否就此封顶（返回原因字符串，空串表示还能继续洗）。"""
        key = self._done_key(sub)
        st = self._done.get(key)
        if not isinstance(st, dict):
            st = {}
        st["count"] = self._to_int(st.get("count", 0), 0, 0, 10000) + 1
        if is_pack:
            st["pack"] = True
        st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["title"] = cur_title
        reason = ""
        if self._stop_on_complete_pack and st.get("pack"):
            reason = "已洗到全集/整季包"
        elif self._max_wash_times > 0 and st["count"] >= self._max_wash_times:
            reason = f"已洗版 {st['count']} 次，达到次数上限"
        elif (cur_title and self._may_finish_early(sub, st)
                and self._reached_target(cur_title)):
            reason = f"已洗到目标画质（{self._target_resolution or '—'}）"
        st["reason"] = reason
        self._done[key] = st
        self._save_done()
        return reason

    def reset_done(self) -> dict[str, Any]:
        """清空「已完成洗版」记录，让所有订阅重新参与洗版。"""
        n = len(self._done)
        self._done = {}
        self._save_done()
        self._record(f"已重置洗版完成状态，{n} 个订阅重新参与洗版")
        return {"success": True, "message": f"已重置 {n} 个订阅的完成状态。"}

    def _set_done(self, sub: Any, reason: str, title: str = "") -> None:
        """直接标记完成（不增加洗版次数），用于「已达目标画质」这类收工判定。"""
        key = self._done_key(sub)
        st = self._done.get(key)
        if not isinstance(st, dict):
            st = {}
        st["reason"] = reason
        st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if title:
            st["title"] = title
        self._done[key] = st
        self._save_done()

    def _count_miss(self, sub: Any, cur_title: str = "") -> str:
        """累计「本轮没找到更优版本」的次数，连续太多次就判定洗版结束。"""
        if self._give_up_rounds <= 0:
            return ""
        # 电视剧还在等全集时不能放弃，否则永远等不到「自动转全集」
        if not self._may_finish_early(sub, self._done.get(self._done_key(sub))):
            return ""
        key = self._done_key(sub)
        st = self._done.get(key)
        if not isinstance(st, dict):
            st = {}
        st["miss"] = self._to_int(st.get("miss", 0), 0, 0, 10000) + 1
        st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if cur_title:
            st["title"] = cur_title
        if st["miss"] >= self._give_up_rounds:
            reason = f"连续 {st['miss']} 轮没有更优资源，判定已洗到顶"
            st["reason"] = reason
            self._done[key] = st
            self._save_done()
            return reason
        self._done[key] = st
        self._save_done()
        return ""

    @classmethod
    def _score_title(
        cls,
        title: str,
        dim_enabled: dict[str, bool] | None = None,
        dim_values: dict[str, list[str]] | None = None,
        bonus: list[str] | None = None,
    ) -> int:
        """按维度优先级给资源标题打分（无需用户填数字）。

        - 维度越靠前（#1 分辨率 → #9 文件格式）权重越高，越靠前的维度命中加分越多；
        - 同维度内，勾选列表里越靠前的类别得分越高；
        - 未启用的维度不参与打分。
        """
        if not title:
            return 0
        dim_enabled = dim_enabled or {}
        dim_values = dim_values or {}
        tu = cls._norm_title(title)
        ext = tu.rsplit(".", 1)[-1] if "." in tu else ""
        bonus = bonus or []
        total = 0
        n_dims = len(_DIMENSIONS)
        for idx, (dim, all_vals) in enumerate(_DIMENSIONS):
            if not dim_enabled.get(dim, True):
                continue
            chosen = dim_values.get(dim) or all_vals
            dim_weight = n_dims - idx  # #1 最重，逐级递减
            matched_rank: int | None = None
            for tok in sorted(chosen, key=len, reverse=True):
                vs = _variants(tok)
                if dim == "file_extension":
                    if ext in vs:
                        matched_rank = chosen.index(tok)
                        break
                else:
                    if any(
                        v in tu or v.replace("&", ".") in tu or v.replace(".", "&") in tu
                        for v in vs
                    ):
                        matched_rank = chosen.index(tok)
                        break
            if matched_rank is not None:
                total += dim_weight * (len(chosen) - matched_rank)
        # 优先词加分
        for b in bonus:
            if b and cls._norm_title(b) in tu:
                total += 25
        return total

    def _passes_filters(self, title: str) -> bool:
        """自定义模式下对资源标题做基础过滤（排除词 + 最低分辨率）。"""
        tu = self._norm_title(title)
        # 排除关键词
        for kw in [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]:
            if self._norm_title(kw) in tu:
                return False
        # 最低分辨率门槛
        if self._min_resolution:
            res_rank = 0
            for tok in _DIMENSIONS[0][1]:
                tgt = _SYN.get(tok, tok)
                if tgt in tu:
                    res_rank = max(res_rank, _RES_RANK.get(tok, 0))
            if res_rank < _RES_RANK.get(self._min_resolution, 0):
                return False
        return True

    # ------------------------------------------------------------------ #
    # 配置页 + 默认配置
    # ------------------------------------------------------------------ #
    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        # 每个维度一组：开关（#n 中文名）+ 类别多选框（参照 MP「洗版评分规则」页的交互）
        dim_items: list[dict] = []
        for i, (dim, all_vals) in enumerate(_DIMENSIONS):
            zh, _desc = _DIM_ZH.get(dim, (dim, ""))
            dim_items.append({
                "component": "VSwitch",
                "props": {
                    "model": f"{dim}_enabled",
                    "label": f"#{i + 1} {zh}（{dim}）",
                    "color": "primary",
                    "hide-details": True,
                },
            })
            dim_items.append({
                "component": "VSelect",
                "props": {
                    "model": f"{dim}_values",
                    "label": f"点开选择需要的「{zh}」类别（可多选，越靠前越优先）",
                    "items": [{"title": v, "value": v} for v in all_vals],
                    "multiple": True,
                    "chips": True,
                    "closable-chips": True,
                    "variant": "outlined",
                    "hide-details": True,
                },
            })
            dim_items.append({"component": "VDivider", "props": {"class": "my-3"}})
        content: list[dict] = [
                    # ===== 分区一：基础设置（不再放标题提示条） =====
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用评分洗版"}},
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "wash_mode",
                            "label": "洗版模式",
                            "items": [
                                {"title": "原生最佳版本（推荐，复用 MP 评分规则）", "value": _MODE_NATIVE},
                                {"title": "自定义类别打分（自己勾选类别，覆盖 MP 原生规则）", "value": _MODE_CUSTOM},
                            ],
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "scope",
                            "label": "洗版范围（对哪些订阅生效）",
                            "items": [
                                {"title": "全部订阅", "value": _SCOPE_ALL},
                                {"title": "仅电影", "value": _SCOPE_MOVIE},
                                {"title": "仅电视剧", "value": _SCOPE_TV},
                            ],
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "only_best_version",
                            "label": "仅对开启了「最佳版本」的订阅洗版",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "tv_mode",
                            "label": "电视剧洗版方式（分集 / 全集）",
                            "items": [
                                {"title": "分集也洗，出全集后自动转全集（推荐）", "value": _TV_AUTO},
                                {"title": "只等全集，分集不洗", "value": _TV_PACK_ONLY},
                                {"title": "只按分集洗，不区分全集", "value": _TV_EPISODE},
                            ],
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "crontab",
                            "label": "扫描周期（定时触发，如 0 4 * * * = 每天 04:00）",
                            "placeholder": "0 4 * * *",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "dry_run",
                            "label": "试运行（只记录将要洗版的订阅，不真正搜索/下载）",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "run_once",
                            "label": "立即运行一次（保存后马上按当前配置执行一次，执行完自动关闭）",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "「原生最佳版本」模式：评分规则与画质/特效目标请在 MoviePilot「洗版评分规则」页和订阅的「最佳版本」里设置，本插件只负责按时触发它们。",
                        },
                    },
                    # ===== 分区：请求频率（防止站点封禁） =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "error",
                            "variant": "tonal",
                            "text": "请求频率（防止站点封禁）：一次洗版会对每个订阅遍历所有 PT 站点，"
                                    "短时间打太多请求容易被判定为爬虫而临时封禁。"
                                    "下面三项在每次运行内部生效，建议先用默认值。",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "interval_seconds",
                            "label": "每个订阅之间的间隔（秒）",
                            "type": "number",
                            "placeholder": "60",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "batch_limit",
                            "label": "每轮最多处理订阅数（分批轮转，0 = 不限）",
                            "type": "number",
                            "placeholder": "10",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "cooldown_hours",
                            "label": "同一订阅冷却（小时，冷却内跳过；0 = 不限）",
                            "type": "number",
                            "placeholder": "12",
                        },
                    },
                    # ===== 分区：洗到什么时候停（防止无限洗版） =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success",
                            "variant": "tonal",
                            "text": "洗到什么时候停：不设上限会一直反复检测、反复洗版。"
                                    "满足下面任意一条就算「洗版完成」，该订阅不再检测、不再访问站点，"
                                    "并从详情页列表移除。想重洗时点详情页的「重置完成状态」。",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "max_wash_times",
                            "label": "同一订阅最多洗几次（默认 2：一次分集/电影 + 一次全集；0 = 不限）",
                            "type": "number",
                            "placeholder": "2",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "target_resolution",
                            "label": "洗到这个分辨率就算到顶（不再洗）",
                            "items": [
                                {"title": "8K", "value": "8K"},
                                {"title": "4K（推荐）", "value": "4K"},
                                {"title": "1080P", "value": "1080P"},
                                {"title": "720P", "value": "720P"},
                                {"title": "不按分辨率封顶", "value": ""},
                            ],
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "target_source",
                            "label": "洗到这个来源就算到顶（可选，一般不用设）",
                            "items": [
                                {"title": "不按来源封顶（默认）", "value": ""},
                                {"title": "REMUX / 原盘级", "value": "REMUX"},
                                {"title": "BLURAY（蓝光）", "value": "BLURAY"},
                                {"title": "WEB-DL（网页）", "value": "WEB-DL"},
                            ],
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "stop_on_complete_pack",
                            "label": "电视剧洗到「全集/整季」即视为完成（不再洗单集）",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "give_up_rounds",
                            "label": "连续几轮没找到更优版本就收工（默认 3；0 = 不放弃）",
                            "type": "number",
                            "placeholder": "3",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "hide_done",
                            "label": "洗版完成的订阅从详情页移除（只显示还在洗的）",
                        },
                    },
                    # ===== 分区二：自定义类别选择 =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "自定义类别（仅当上方「洗版模式 = 自定义类别打分」时生效）"
                                    "。像 MP「洗版评分规则」一样：每个维度用开关决定是否启用，"
                                    "点开下拉勾选需要的类别即可（可多选）。"
                                    "同一维度内，列表越靠前的类别越优先；不勾选的类别不会命中。",
                        },
                    },
                    *dim_items,
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "min_resolution",
                            "label": "最低接受分辨率（低于此清晰度的不洗）",
                            "items": [
                                {"title": "不限", "value": ""},
                                {"title": "8K", "value": "8K"},
                                {"title": "4K", "value": "4K"},
                                {"title": "1080P", "value": "1080P"},
                                {"title": "720P", "value": "720P"},
                            ],
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "exclude_keywords",
                            "label": "排除关键词（逗号分隔，标题含这些词就不洗）",
                            "placeholder": "国配,德语",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "bonus_keywords",
                            "label": "优先关键词（逗号分隔，命中后额外加分）",
                            "placeholder": "DIY,REMUX",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "score_threshold",
                            "label": "评分提升阈值（新版本需比旧的高出这么多分才下载）",
                            "type": "number",
                            "placeholder": "0",
                        },
                    },
                ]
        # 加大行与行的间距（Vuetify 间距类，MP 表单会把 props 透传给组件）
        for _it in content:
            _comp = _it.get("component")
            _pr = _it.setdefault("props", {})
            if _comp in ("VSelect", "VTextField"):
                _pr.setdefault("class", "mb-5")
            elif _comp == "VSwitch":
                _pr.setdefault("class", "mb-4")
            elif _comp == "VAlert":
                _pr.setdefault("class", "mb-3")
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "wash_mode": _MODE_NATIVE,
            "scope": _SCOPE_ALL,
            "only_best_version": True,
            "tv_mode": _TV_AUTO,
            "crontab": "0 4 * * *",
            "dry_run": True,
            "run_once": False,
            "interval_seconds": 60,
            "batch_limit": 10,
            "cooldown_hours": 12,
            "max_wash_times": 2,
            "target_resolution": "4K",
            "target_source": "",
            "stop_on_complete_pack": True,
            "give_up_rounds": 3,
            "hide_done": True,
            **{f"{dim}_enabled": True for dim, _v in _DIMENSIONS},
            **{f"{dim}_values": list(vals) for dim, vals in _DIMENSIONS},
            "min_resolution": "",
            "exclude_keywords": "",
            "bonus_keywords": "",
            "score_threshold": 0,
        }

    # ------------------------------------------------------------------ #
    # 详情页（数据 + 手动触发）
    # ------------------------------------------------------------------ #
    def get_page(self) -> list[dict]:
        """详情页：海报 + 当前版本的一览式卡片，尽量少文字。"""
        mode_text = "原生最佳版本" if self._wash_mode == _MODE_NATIVE else "自定义类别打分"
        nodes: list[dict] = [self._trigger_card()]
        nodes.append({
            "component": "VCardText",
            "props": {"class": "pa-0 px-1 pb-3 text-caption text-medium-emphasis"},
            "text": (
                f"{'运行中' if self._enabled else '已停用'} · {mode_text} · "
                f"{'试运行' if self._dry_run else '正式'} · 间隔 {self._interval_seconds}s · "
                f"每轮 {self._batch_limit or '不限'} · 冷却 {self._cooldown_hours}h · "
                f"洗到 {self._target_resolution or '不限'} 停 · 最多洗 {self._max_wash_times or '不限'} 次"
            ),
        })

        subs: list[Any] = []
        err = ""
        try:
            subs = list(SubscribeOper().list() or [])
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if err:
            nodes.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": f"读取订阅失败：{err}"},
            })
        else:
            pending: list[Any] = []
            finished: list[Any] = []
            for s in subs:
                if not getattr(s, "name", None):
                    continue
                reason = self._done_reason(s, self._current_version_cached(s))
                (finished if reason else pending).append(s)
            shown = pending if self._hide_done else (pending + finished)
            cards = [self._sub_card(s) for s in shown[:30]]
            if cards:
                nodes.extend(cards)
                if len(shown) > 30:
                    nodes.append({
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-1 text-caption text-medium-emphasis"},
                        "text": f"共 {len(shown)} 个订阅，此处只显示前 30 个",
                    })
            else:
                nodes.append({
                    "component": "VCardText",
                    "props": {"class": "pa-0 px-1 text-caption text-medium-emphasis"},
                    "text": "暂无待洗版订阅" if finished else "暂无订阅",
                })
            if finished:
                nodes.append({
                    "component": "VCardText",
                    "props": {"class": "pa-0 px-1 text-caption text-medium-emphasis"},
                    "text": (
                        f"已完成洗版 {len(finished)} 个"
                        + ("（已从列表移除）" if self._hide_done else "（仍在列表中）")
                    ),
                })
                nodes.append(self._reset_card(len(finished)))

        nodes.append({
            "component": "VCardText",
            "props": {"class": "pa-0 px-1 pt-3 text-caption text-medium-emphasis"},
            "text": f"上次运行：{self._last_run}",
        })
        recent = list(reversed(self._activities[-8:]))
        nodes.append({
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mt-2"},
            "content": [
                {
                    "component": "VList",
                    "props": {"density": "compact"},
                    "content": [
                        {"component": "VListItem", "props": {"title": line, "class": "text-caption"}}
                        for line in (recent or ["暂无运行记录"])
                    ],
                }
            ],
        })
        return nodes

    def _trigger_card(self) -> dict:
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "variant": "elevated",
                        "block": True,
                        "prepend-icon": "mdi-refresh",
                        "text": "立即洗版一次",
                    },
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/run",
                            "method": "POST",
                            "params": {},
                        }
                    },
                }
            ],
        }

    def _reset_card(self, count: int) -> dict:
        """已完成洗版的订阅可以一键重置，重新参与洗版。"""
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "color": "surface-variant", "class": "mb-3 mt-1 pa-1"},
            "content": [
                {
                    "component": "VBtn",
                    "props": {
                        "color": "secondary",
                        "variant": "text",
                        "block": True,
                        "size": "small",
                        "prepend-icon": "mdi-restore",
                        "text": f"重置完成状态（{count} 个）",
                    },
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/reset_done",
                            "method": "POST",
                            "params": {},
                        }
                    },
                }
            ],
        }

    def _sub_card(self, sub: Any) -> dict:
        """单个订阅卡片：左海报 + 右标题/当前版本/集数。"""
        name = str(getattr(sub, "name", "") or "")
        year = str(getattr(sub, "year", "") or "")
        stype = str(getattr(sub, "type", "") or "")
        cur = self._current_version_cached(sub)
        bits = [b for b in (stype, year) if b]
        total_ep = self._to_int(getattr(sub, "total_episode", 0), 0, 0, 10000)
        if total_ep:
            lack = self._to_int(getattr(sub, "lack_episode", 0), 0, 0, 10000)
            bits.append(f"{max(0, total_ep - lack)}/{total_ep} 集")
        info = " · ".join(bits) or "—"
        cur_text = f"当前版本：{self._shorten(cur, 40)}" if cur else "当前版本：未记录"
        return {
            "component": "VCard",
            "props": {"variant": "flat", "class": "mb-3 pa-2 d-flex align-center"},
            "content": [
                self._poster_node(str(getattr(sub, "poster", "") or ""), name),
                {
                    "component": "VCardText",
                    "props": {"class": "py-0 px-3 flex-grow-1", "style": "min-width:0"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {"class": "pa-0 text-body-1 font-weight-bold text-truncate"},
                            "text": name,
                        },
                        {
                            "component": "VCardSubtitle",
                            "props": {"class": "pa-0 text-caption text-medium-emphasis text-truncate"},
                            "text": cur_text,
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "pa-0 pt-1 text-caption text-medium-emphasis"},
                            "text": info,
                        },
                    ],
                },
            ],
        }

    def _poster_node(self, poster: str, name: str) -> dict:
        """海报，取不到就画一个带首字的占位块，避免破图。"""
        w, h = 68, 98
        url = self._poster_url(poster)
        if url:
            return {
                "component": "VImg",
                "props": {
                    "src": url,
                    "width": w,
                    "height": h,
                    "cover": True,
                    "class": "rounded flex-shrink-0",
                    "style": "background:#eee",
                },
            }
        return {
            "component": "VSheet",
            "props": {
                "width": w,
                "height": h,
                "class": "rounded flex-shrink-0 bg-grey-lighten-3 d-flex align-center justify-center",
            },
            "content": [
                {
                    "component": "VCardText",
                    "props": {"class": "pa-0 text-h6 text-medium-emphasis"},
                    "text": (name[:1] or "?"),
                }
            ],
        }

    @staticmethod
    def _poster_url(raw: str) -> str:
        """把订阅海报字段转成浏览器能直接加载的地址。"""
        u = (raw or "").strip()
        if not u:
            return ""
        if u.startswith("//"):
            return "https:" + u
        low = u.lower()
        if not low.startswith(("http://", "https://")):
            # 本机静态资源（如 /static/poster/xxx.jpg）可直接用；其他未知格式不渲染，避免破图
            return u if u.startswith("/") else ""
        # 豆瓣图有防盗链，走后端图片代理
        if "doubanio.com" in low:
            return "api/v1/system/img/0?imgurl=" + quote(u, safe="")
        return u

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        t = (text or "").strip()
        return t if len(t) <= limit else t[: limit - 1] + "…"

    def _current_version_cached(self, sub: Any) -> str:
        """当前版本标题，带 5 分钟缓存，避免每次打开详情页都批量查库。"""
        try:
            sid = int(getattr(sub, "id", 0) or 0)
        except (TypeError, ValueError):
            sid = 0
        now = time.time()
        hit = self._ver_cache.get(sid)
        if hit and (now - hit[1]) < 300:
            return hit[0]
        val = self._current_version_title(sub)
        if sid:
            self._ver_cache[sid] = (val, now)
        return val

    # ------------------------------------------------------------------ #
    # 对外 API：手动触发
    # ------------------------------------------------------------------ #
    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.run_now,
                "methods": ["POST"],
                "summary": "立即洗版一次",
                "allow_anonymous": True,
            },
            {
                "path": "/reset_done",
                "endpoint": self.reset_done,
                "methods": ["POST"],
                "summary": "重置洗版完成状态",
                "allow_anonymous": True,
            },
        ]

    def get_command(self) -> list[dict[str, Any]]:
        return []

    def _trigger_background(self, reason: str) -> bool:
        """后台线程执行一次洗版，避免阻塞前端或配置保存。返回是否真正触发。"""
        if self._running or not self.get_state():
            return False
        self._running = True

        def _worker() -> None:
            try:
                self._record(f"手动触发：{reason}")
                self.wash()
            finally:
                self._running = False

        Thread(target=_worker, daemon=True).start()
        return True

    def run_now(self) -> dict[str, Any]:
        """手动触发入口：后台线程执行，避免前端请求超时。"""
        if self._running:
            return {"success": False, "message": "上一次洗版仍在执行中，请稍后再试。"}
        if not self.get_state():
            return {"success": False, "message": "插件未启用，无法触发。"}
        self._trigger_background("详情页按钮")
        return {"success": True, "message": "已触发洗版，详见「数据」页运行记录。"}

    # ------------------------------------------------------------------ #
    # 定时服务：按配置的 Cron 周期触发
    # ------------------------------------------------------------------ #
    def get_service(self) -> list[dict]:
        if not self.get_state():
            return []
        return [
            {
                "id": "WashUpgrade.Score",
                "name": "评分洗版扫描",
                "trigger": CronTrigger.from_crontab(self._crontab),
                "func": self.wash,
                "kwargs": {},
            }
        ]

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_mtype(stype: Any) -> MediaType | None:
        s = str(stype or "")
        if s in ("电影", "movie", MediaType.MOVIE.value):
            return MediaType.MOVIE
        if s in ("电视剧", "tv", MediaType.TV.value):
            return MediaType.TV
        return None

    # ------------------------------------------------------------------ #
    # 核心逻辑
    # ------------------------------------------------------------------ #
    def wash(self) -> None:
        if not self.get_state():
            return
        mode_text = "原生最佳版本" if self._wash_mode == _MODE_NATIVE else "自定义类别打分"
        scope_text = {"all": "全部", "movie": "电影", "tv": "电视剧"}.get(self._scope, "全部")
        self._record(f"开始扫描（模式：{mode_text}，范围：{scope_text}，试运行：{self._dry_run}）")
        try:
            subs = SubscribeOper().list()
        except Exception as e:  # noqa: BLE001
            self._last_run = f"读取订阅失败：{e}"
            self._record(self._last_run)
            return

        targets: list[tuple[int, Any]] = []
        done_skipped = 0
        for sub in subs:
            try:
                sid = int(sub.id)
                name = str(sub.name or "")
                stype = getattr(sub, "type", None)
                best_ver = getattr(sub, "best_version", 0) or 0
            except Exception:  # noqa: BLE001
                continue
            if not name:
                continue
            if self._only_best_version and not best_ver:
                continue
            mtype = self._to_mtype(stype)
            if self._scope == _SCOPE_MOVIE and mtype is not MediaType.MOVIE:
                continue
            if self._scope == _SCOPE_TV and mtype is not MediaType.TV:
                continue
            # 已洗到封顶的订阅直接跳过，不再访问站点（避免无限检测）
            cur = self._current_version_cached(sub) if self._wash_mode == _MODE_CUSTOM else ""
            reason = self._done_reason(sub, cur)
            if reason:
                done_skipped += 1
                continue
            targets.append((sid, sub))

        if done_skipped:
            self._record(f"已完成洗版：{done_skipped} 个订阅已到顶，本轮跳过（不访问站点）")

        if not targets:
            self._last_run = "本次无可洗版订阅"
            self._record(self._last_run)
            return

        # 1) 冷却：最近洗过的先跳过，避免反复打同一批站点
        targets = self._apply_cooldown(targets)
        # 2) 分批：每轮只处理 N 个，从上次结束处继续（轮转，不总是洗前几个）
        targets = self._apply_batch(targets)

        if not targets:
            self._last_run = "本轮全部订阅都在冷却期内，跳过（未访问任何站点）"
            self._record(self._last_run)
            return

        if self._dry_run:
            self._last_run = f"[试运行] 计划处理 {len(targets)} 个订阅（未访问任何站点）"
            self._record(self._last_run)
            for _sid, sub in targets:
                self._record(f"[试运行] 将对《{str(getattr(sub, 'name', '') or '')}》执行{mode_text}洗版")
            return

        total = len(targets)
        gap = self._interval_seconds
        if gap > 0:
            self._record(
                f"请求节奏：每处理 1 个订阅后等待约 {gap} 秒（含随机抖动），"
                f"本轮 {total} 个，预计 {max(1, round(gap * (total - 1) / 60))} 分钟内完成"
            )

        for i, (sid, sub) in enumerate(targets):
            if self._wash_mode == _MODE_NATIVE:
                self._native_wash_one(sid, sub)
            else:
                self._custom_wash_one(sid, sub)
            self._last_wash_at[sid] = time.time()
            if i < total - 1:
                self._pace(i + 1, total)

        self._last_run = f"{mode_text}洗版完成，处理 {total} 个订阅"
        self._record(self._last_run)

    # ------------------------------------------------------------------ #
    # 请求频率控制
    # ------------------------------------------------------------------ #
    def _apply_cooldown(self, targets: list[tuple[int, Any]]) -> list[tuple[int, Any]]:
        """冷却期内的订阅直接跳过，不产生任何站点请求。"""
        if self._cooldown_hours <= 0:
            return targets
        limit = self._cooldown_hours * 3600
        now = time.time()
        kept: list[tuple[int, Any]] = []
        skipped = 0
        for sid, sub in targets:
            last = self._last_wash_at.get(sid)
            if last and (now - last) < limit:
                skipped += 1
                left_min = int((limit - (now - last)) / 60)
                self._record(
                    f"跳过《{str(getattr(sub, 'name', '') or '')}》："
                    f"距上次洗版不足 {self._cooldown_hours} 小时（约 {left_min} 分钟后再试）"
                )
                continue
            kept.append((sid, sub))
        if skipped:
            self._record(f"冷却过滤：跳过 {skipped} 个，剩余 {len(kept)} 个")
        return kept

    def _apply_batch(self, targets: list[tuple[int, Any]]) -> list[tuple[int, Any]]:
        """每轮最多处理 N 个订阅，按轮转游标取，保证长期都能轮到。"""
        limit = self._batch_limit
        if limit <= 0 or len(targets) <= limit:
            return targets
        start = self._cursor % len(targets)
        rotated = targets[start:] + targets[:start]
        picked = rotated[:limit]
        self._cursor = (start + limit) % len(targets)
        self._record(
            f"分批限流：共 {len(targets)} 个订阅，本轮只处理 {limit} 个（下次从第 {self._cursor + 1} 个继续）"
        )
        return picked

    def _pace(self, done: int, total: int) -> None:
        """两个订阅之间的等待，带 0~25% 随机抖动，避免固定周期特征。"""
        base = self._interval_seconds
        if base <= 0:
            return
        wait = base * (1 + random.uniform(0, 0.25))
        self._record(f"等待 {wait:.0f} 秒后继续（进度 {done}/{total}，降低站点请求频率）")
        self._sleep(wait)

    @staticmethod
    def _sleep(seconds: float) -> None:
        """集中在一处的等待实现，便于测试替换。"""
        try:
            time.sleep(seconds)
        except Exception:  # noqa: BLE001
            pass

    def _native_wash_one(self, sid: int, sub: Any) -> None:
        """原生模式：一次只触发一个订阅，便于控制节奏（原来是整批一次性触发）。"""
        name = str(getattr(sub, "name", "") or "")
        cur = self._current_version_cached(sub)
        if cur and self._reached_target(cur):
            self._set_done(sub, f"当前版本已达目标画质（{self._target_resolution or '—'}）", cur)
            self._record(
                f"《{name}》已达目标画质（{self._target_resolution or '—'}），跳过触发，"
                f"后续不再检测，已从首页移除"
            )
            return
        try:
            SubscribeChain().search(sids=(sid,), manual=True)
            self._record(f"《{name}》已触发原生最佳版本搜索")
        except Exception as e:  # noqa: BLE001
            self._record(f"《{name}》原生搜索失败：{e}")
            return
        reason = self._mark_wash(sub, cur, is_pack=False)
        if reason:
            self._record(f"《{name}》洗版完成 → {reason}，后续不再检测，已从首页移除")

    # ------------------------------------------------------------------ #
    # 自定义权重模式：搜索 + 打分 + 下载更优版本
    # ------------------------------------------------------------------ #
    def _current_version_title(self, sub: Any) -> str:
        """取该订阅「当前已下载版本」的种子名，用于日志展示与基线推算。取不到返回空串。"""
        name = str(getattr(sub, "name", "") or "")
        year = str(getattr(sub, "year", "") or "")
        mt = self._to_mtype(getattr(sub, "type", None))
        if mt is MediaType.MOVIE:
            mtype = "电影"
        elif mt is MediaType.TV:
            mtype = "电视剧"
        else:
            mtype = None
        try:
            from app.db.oper.downloadhistory import DownloadHistoryOper

            recs = DownloadHistoryOper().get_last_by(
                mtype=mtype, title=name, year=year or None
            ) or []
            if recs:
                r = recs[0]
                return str(getattr(r, "torrent_name", "") or getattr(r, "title", "") or "")
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _custom_wash_one(self, sid: int, sub: Any) -> None:
        name = str(getattr(sub, "name", "") or "")
        year = getattr(sub, "year", None)
        stype = getattr(sub, "type", None)
        mtype = self._to_mtype(stype)
        # 已洗到封顶的订阅不再搜索（不产生任何站点请求）
        early = self._done_reason(sub, self._current_version_cached(sub))
        if early:
            self._record(f"《{name}》已完成洗版（{early}），跳过检测")
            return
        search_title = name
        try:
            if year and int(year) > 0:
                search_title = f"{name} {int(year)}"
        except (TypeError, ValueError):
            pass
        try:
            contexts = SearchChain().search_by_title(search_title, mtype=mtype)
        except Exception as e:  # noqa: BLE001
            self._record(f"《{name}》搜索失败：{e}")
            return

        bonus = [k.strip() for k in self._bonus_keywords.split(",") if k.strip()]
        is_tv = mtype is MediaType.TV
        total_ep = self._to_int(getattr(sub, "total_episode", 0), 0, 0, 10000)

        # 候选分成「全集/整季包」与「分集」两堆
        packs: list[tuple[int, Any, str]] = []
        singles: list[tuple[int, Any, str]] = []
        for ctx in (contexts or []):
            ti = getattr(ctx, "torrent_info", None)
            t = getattr(ti, "title", None) if ti else getattr(ctx, "title", None)
            if not t or not self._passes_filters(t):
                continue
            s = self._score_title(t, self._dim_enabled, self._dim_values, bonus)
            if is_tv and self._is_complete_pack(t, total_ep):
                packs.append((s, ctx, t))
            else:
                singles.append((s, ctx, t))

        # 当前版本（分集/全集判断与基线都依赖它）
        cur_title = self._current_version_title(sub) or self._best_titles.get(sid, "")
        cur_is_pack = bool(is_tv and cur_title and self._is_complete_pack(cur_title, total_ep))
        if cur_title:
            cur_score = self._score_title(cur_title, self._dim_enabled, self._dim_values, bonus)
        else:
            cur_score = self._best_scores.get(sid, -1)
        cur_text = cur_score if cur_score >= 0 else "未知"

        # ---- 按「电视剧分集/全集策略」决定候选池 ----
        if is_tv and self._tv_mode == _TV_PACK_ONLY:
            if not packs:
                self._record(f"《{name}》暂无全集/整季资源，继续等全集（设置：只等全集）")
                return
            pool = packs
        elif is_tv and self._tv_mode == _TV_AUTO:
            if packs:
                pool = packs
                if not cur_is_pack:
                    self._record(f"《{name}》发现 {len(packs)} 个全集/整季资源 → 自动转全集洗版")
            else:
                if cur_is_pack:
                    self._record(f"《{name}》当前已是全集版本，站点暂无更优全集，本次不洗")
                    return
                pool = singles
                if singles:
                    self._record(
                        f"《{name}》暂无全集，按分集洗版（追更中，共 {len(singles)} 个分集候选）"
                    )
        else:
            pool = packs + singles

        # ---- 选最优候选 ----
        best_score, best_ctx, best_title = -1, None, ""
        for s, ctx, t in pool:
            if s > best_score:
                best_score, best_ctx, best_title = s, ctx, t

        self._record(f"《{name}》当前版本：{cur_title or '未记录'}（评分 {cur_text}）")

        if best_ctx is None:
            # 没找到任何可用资源：累计空转轮次，连续太多次就收工，别一直扫
            self._record(f"《{name}》未找到符合过滤条件的资源，跳过")
            miss_reason = self._count_miss(sub, cur_title)
            if miss_reason:
                self._record(f"《{name}》洗版结束 → {miss_reason}，后续不再检测")
            return

        best_is_pack = bool(is_tv and self._is_complete_pack(best_title, total_ep))

        # 已是全集时不再用分集覆盖（防止把全集洗成单集）
        if cur_is_pack and not best_is_pack:
            self._record(f"《{name}》当前已是全集版本，不接受分集覆盖，本次不洗")
            return

        if best_is_pack and not cur_is_pack:
            # 分集 → 全集：这是「转全集洗版」，必须执行，不比较分数
            self._record(
                f"《{name}》出现全集资源：{best_title}（评分 {best_score}）→ 自动转全集洗版"
            )
            improved = True
        else:
            improved = best_score > cur_score and (best_score - cur_score) >= self._score_threshold
            if improved:
                self._record(
                    f"《{name}》对比后发现更高版本：{best_title}（评分 {best_score}）"
                )

        if not improved:
            self._record(
                f"《{name}》对比后未发现更高版本：最佳候选 {best_title}"
                f"（评分 {best_score}），保持当前版本"
            )
            if (cur_title and self._reached_target(cur_title)
                    and self._may_finish_early(sub, self._done.get(self._done_key(sub)))):
                self._set_done(
                    sub, f"当前版本已达目标画质（{self._target_resolution or '—'}）", cur_title
                )
                self._record(
                    f"《{name}》洗版结束 → 已达目标画质（{self._target_resolution or '—'}），"
                    f"后续不再检测，已从首页移除"
                )
                return
            miss_reason = self._count_miss(sub, cur_title)
            if miss_reason:
                self._record(f"《{name}》洗版结束 → {miss_reason}，后续不再检测")
            return

        # ---- 进行评分洗版 ----
        if self._dry_run:
            self._record(f"《{name}》[试运行] 将进行评分洗版（不实际下载）")
            return
        self._record(f"《{name}》进行评分洗版：开始下载")
        try:
            DownloadChain().download_single(best_ctx)
        except Exception as e:  # noqa: BLE001
            self._record(f"《{name}》下载失败：{e}")
            return
        self._best_scores[sid] = best_score
        self._best_titles[sid] = best_title
        self._record(f"《{name}》评分洗版已提交下载（评分 {best_score}）")
        reason = self._mark_wash(sub, best_title, is_pack=best_is_pack)
        if reason:
            self._record(f"《{name}》洗版完成 → {reason}，后续不再检测，已从首页移除")

    # ------------------------------------------------------------------ #
    # 停用：释放后台资源
    # ------------------------------------------------------------------ #
    def stop_service(self) -> None:
        self._enabled = False
