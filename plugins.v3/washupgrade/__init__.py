import re
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.schemas.types import MediaType
from app.db.oper.subscribe import SubscribeOper
from app.chain.search.facade import SearchChain
from app.chain.download.facade import DownloadChain

# 洗版范围
_SCOPE_ALL = "all"
_SCOPE_MOVIE = "movie"
_SCOPE_TV = "tv"

# 电视剧洗版模式
_TV_MODE_EPISODE_THEN_COMPLETE = "episode_then_complete"  # 分集洗版，出全集后转全集（默认）
_TV_MODE_EPISODE_ONLY = "episode_only"                    # 仅分集洗版
_TV_MODE_COMPLETE_ONLY = "complete_only"                  # 仅全集洗版

# ---------------------------------------------------------------------------
# 评分洗版规则（由用户定义，维度 #1 优先级最高 → #9 最低）
# 每个维度按「从好到差」排序；命中靠前的得分更高。
# ---------------------------------------------------------------------------
_DIMENSIONS: list[tuple[str, list[str]]] = [
    ("resolution", ["8K", "4K", "1080P", "1080I", "720P", "SD"]),
    ("source", [
        "UHD-BLURAY-REMUX", "UHD-BLURAY", "BLURAY-REMUX", "REMUX", "BLURAY",
        "WEB-DL", "WEBRIP", "UHDTV", "HDTV", "DVD", "DVDRIP", "HDRIP", "BDMV",
        "SCR", "HDTC", "TC", "TELESYNC", "HDCAM", "CAM",
    ]),
    ("video_codec", ["HEVC", "AV1", "AVC", "VP9", "MPEG2", "VC1", "XVID", "DIVX", "AVS3"]),
    ("audio_codec", [
        "TRUEHD7.1", "TRUEHD5.1", "TRUEHD", "DTS-HD.MA7.1", "DTS-HD.MA", "DTS-X",
        "DTS", "DDP5.1", "DDP", "EAC3", "AC3", "AAC2.0", "AAC", "FLAC", "LPCM",
        "OPUS", "ATMOS",
    ]),
    ("hdr", [
        "DV", "HDR10+", "HDR10", "HDR-VIVID", "HLG", "SDR", "EDR", "3D", "IMAX",
        "REPACK", "PROPER", "RERIP", "DIY", "HQ",
    ]),
    ("color_depth", ["12BIT", "10BIT", "8BIT"]),
    ("frame_rate", ["120FPS", "60FPS", "50FPS", "30FPS", "24FPS"]),
    ("subtitle", [
        "CHS&CHT", "CHS&ENG", "CHS", "CHT", "MULTISUB", "MUXED-SUB", "EXT-SUB",
        "HARDSUB", "RAW", "NO-SUB", "NO-CHS",
    ]),
    ("file_extension", [
        "MKV", "MP4", "AVI", "MOV", "WMV", "FLV", "RMVB", "ISO", "M4V", "MPG",
        "MPEG", "3GP", "TS", "WEBM", "RM",
    ]),
]

# 各维度权重（与优先级顺序一致，#1 最重）。绝对值只决定相对重要度。
_WEIGHTS: list[int] = [100, 70, 50, 40, 30, 20, 12, 8, 5]

# 常见等价写法（提高匹配率，不改变优先级顺序）
_SYNONYMS: dict[str, str] = {
    "2160P": "4K", "2160": "4K",
    "CHS.CHT": "CHS&CHT", "CHT.CHS": "CHS&CHT", "CHS/CHT": "CHS&CHT",
}

# 单集/分集 判定正则（不区分大小写）
_EP_PATTERN = re.compile(r"S\d{1,2}E\d{1,2}|\d{1,2}X\d{2}|EP?\d{1,3}|\bE\d{2}\b", re.I)

# 优先关键词命中加分
_PREFER_BONUS = 15


def _title_has(title_upper: str, token: str) -> bool:
    """标题（大写）是否包含某 token，兼容 & / . / / 分隔符差异。"""
    if token in title_upper:
        return True
    if token.replace("&", ".") in title_upper:
        return True
    if token.replace(".", "&") in title_upper:
        return True
    return False


def _resolution_rank(title_upper: str) -> int | None:
    """返回标题命中的最佳（最靠前）分辨率索引；无命中返回 None。"""
    best = None
    for idx, tok in enumerate(_DIMENSIONS[0][1]):
        tgt = _SYNONYMS.get(tok, tok)
        if _title_has(title_upper, tgt):
            if best is None or idx < best:
                best = idx
    return best


def _hdr_tokens(title_upper: str) -> set[str]:
    return {tok for tok in _DIMENSIONS[4][1] if _title_has(title_upper, tok)}


def _video_codec_token(title_upper: str) -> str | None:
    for tok in _DIMENSIONS[2][1]:
        if _title_has(title_upper, tok):
            return tok
    return None


def _score_title(title: str, prefer: list[str] | None = None) -> int:
    """按 9 维规则给资源标题打分，分数越高代表画质/版本越好。"""
    if not title:
        return 0
    title_upper = title.upper()
    # 文件扩展名单独取末尾
    ext = ""
    if "." in title_upper:
        ext = title_upper.rsplit(".", 1)[-1]

    total = 0
    for (dim, values), weight in zip(_DIMENSIONS, _WEIGHTS):
        # 先把同义词归一
        matched_rank = None
        # 按 token 长度降序匹配，避免短 token 抢占（如 DTS 被 DTS-HD.MA 优先）
        for token in sorted(values, key=len, reverse=True):
            target = _SYNONYMS.get(token, token)
            if dim == "file_extension":
                if ext == target:
                    matched_rank = values.index(token)
                    break
            else:
                if _title_has(title_upper, target):
                    matched_rank = values.index(token)
                    break
        if matched_rank is not None:
            best = len(values) - 1
            # 越靠前（rank 越小）得分越高
            total += weight * (best - matched_rank)

    # 优先关键词加分
    if prefer:
        for kw in prefer:
            if kw and kw.upper() in title_upper:
                total += _PREFER_BONUS
    return total


def _tv_classify(title: str, complete_keywords: list[str]) -> str:
    """分类电视剧资源：complete(全集/合集) / episode(分集单集) / pack(整季文件)。"""
    tu = title.upper()
    for kw in complete_keywords:
        if kw and kw.upper() in tu:
            return "complete"
    if _EP_PATTERN.search(tu):
        return "episode"
    # 没有显式集数标记、又没有全集关键词 → 视为整季打包
    return "pack"


class WashUpgrade(_PluginBase):
    """
    评分洗版插件（MoviePilot v3.2.1）。

    按用户定义的 9 维优先级规则（分辨率 / 片源 / 视频编码 / 音频编码 / HDR /
    色深 / 帧率 / 字幕 / 文件格式）对每个订阅的资源评分，分数越高版本越好；
    发现比当前已下版本更优的资源时，下载升级。

    来源：MoviePilot 已配置的 PT 站点（通过 SearchChain 检索）。
    电视剧：支持分集洗版，并在检测到全集资源后切换为全集洗版。
    """

    plugin_name = "洗版自用"
    plugin_desc = "评分洗版：按 9 维优先级规则给 PT 订阅资源打分，下载更优版本（支持电视剧分集/全集）。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.2.0"
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

    # 评分洗版规则
    _min_resolution = ""            # 最低接受分辨率（空=不限）
    _required_hdr = ""              # 必需 HDR 类型（空=不限）
    _required_video_codec = ""      # 必需视频编码（空=不限）
    _min_improvement = 0            # 评分提升阈值，低于此值不下载
    _exclude_keywords = ""          # 排除关键词（逗号分隔）
    _prefer_keywords = ""           # 优先关键词（逗号分隔，命中加分）

    # 资源来源（PT 站点）
    _min_seeders = 0                # 最小做种人数（0=不限）
    _only_free = False              # 仅免费/促销资源

    # 电视剧洗版
    _tv_mode = _TV_MODE_EPISODE_THEN_COMPLETE
    _complete_keywords = "Complete,全季,全集,合集"

    _last_run = "尚未运行"

    # 评分洗版：每个订阅已下载的最佳评分（内存态，重启后清空，靠宿主去重兜底）
    _best: dict[int, int] = {}

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

        self._min_resolution = str(config.get("min_resolution") or "")
        self._required_hdr = str(config.get("required_hdr") or "")
        self._required_video_codec = str(config.get("required_video_codec") or "")
        self._min_improvement = int(config.get("min_improvement") or 0)
        self._exclude_keywords = str(config.get("exclude_keywords") or "")
        self._prefer_keywords = str(config.get("prefer_keywords") or "")

        self._min_seeders = int(config.get("min_seeders") or 0)
        self._only_free = bool(config.get("only_free", False))

        self._tv_mode = str(config.get("tv_mode") or _TV_MODE_EPISODE_THEN_COMPLETE)
        self._complete_keywords = str(config.get("complete_keywords") or "Complete,全季,全集,合集")

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # 配置页 + 默认配置
    # ------------------------------------------------------------------ #
    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "VCard",
                "props": {"title": "基础设置", "variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用评分洗版"}},
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "scope",
                            "label": "洗版范围",
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
                            "label": "仅处理已开启「最佳版本」的订阅",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "crontab",
                            "label": "扫描周期 (Cron)",
                            "placeholder": "0 4 * * *（每天 04:00）",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "dry_run",
                            "label": "试运行（只记录最佳资源，不真正下载）",
                        },
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"title": "评分洗版规则（9 维优先级打分）", "variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "min_resolution",
                            "label": "最低接受分辨率",
                            "clearable": True,
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
                        "component": "VSelect",
                        "props": {
                            "model": "required_hdr",
                            "label": "必需 HDR 类型",
                            "clearable": True,
                            "items": [
                                {"title": "不限", "value": ""},
                                {"title": "杜比视界 DV", "value": "DV"},
                                {"title": "HDR10+", "value": "HDR10+"},
                                {"title": "HDR10", "value": "HDR10"},
                                {"title": "HDR-VIVID", "value": "HDR-VIVID"},
                                {"title": "HLG", "value": "HLG"},
                            ],
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "required_video_codec",
                            "label": "必需视频编码",
                            "clearable": True,
                            "items": [
                                {"title": "不限", "value": ""},
                                {"title": "HEVC / H.265", "value": "HEVC"},
                                {"title": "AV1", "value": "AV1"},
                                {"title": "AVC / H.264", "value": "AVC"},
                            ],
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "min_improvement",
                            "label": "评分提升阈值（低于此分差不下载）",
                            "type": "number",
                            "placeholder": "0（任意更优即下载）",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "exclude_keywords",
                            "label": "排除关键词（逗号分隔，命中即跳过）",
                            "placeholder": "如：国配,德语,重编码",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "prefer_keywords",
                            "label": "优先关键词（逗号分隔，命中加分）",
                            "placeholder": "如：DIY,REMUX",
                        },
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"title": "资源来源（PT 站点）", "variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "洗版资源取自 MoviePilot 已配置的 PT 站点，请确保站点已启用且填写了搜索/RSS 地址。",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "min_seeders",
                            "label": "最小做种人数（0 = 不限）",
                            "type": "number",
                            "placeholder": "0",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "only_free", "label": "仅下载免费 / 促销资源"},
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"title": "电视剧洗版", "variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "tv_mode",
                            "label": "电视剧洗版模式",
                            "items": [
                                {"title": "分集洗版，出全集后转全集（推荐）", "value": _TV_MODE_EPISODE_THEN_COMPLETE},
                                {"title": "仅分集洗版", "value": _TV_MODE_EPISODE_ONLY},
                                {"title": "仅全集洗版", "value": _TV_MODE_COMPLETE_ONLY},
                            ],
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "complete_keywords",
                            "label": "全集判定关键词（逗号分隔）",
                            "placeholder": "Complete,全季,全集,合集",
                        },
                    },
                ],
            },
        ], {
            "enabled": False,
            "scope": _SCOPE_ALL,
            "only_best_version": True,
            "crontab": "0 4 * * *",
            "dry_run": True,

            "min_resolution": "",
            "required_hdr": "",
            "required_video_codec": "",
            "min_improvement": 0,
            "exclude_keywords": "",
            "prefer_keywords": "",

            "min_seeders": 0,
            "only_free": False,

            "tv_mode": _TV_MODE_EPISODE_THEN_COMPLETE,
            "complete_keywords": "Complete,全季,全集,合集",
        }

    # ------------------------------------------------------------------ #
    # 详情页
    # ------------------------------------------------------------------ #
    def get_page(self) -> list[dict]:
        scope_text = {"all": "全部", "movie": "仅电影", "tv": "仅电视剧"}.get(self._scope, "全部")
        tv_text = {
            _TV_MODE_EPISODE_THEN_COMPLETE: "分集转全集",
            _TV_MODE_EPISODE_ONLY: "仅分集",
            _TV_MODE_COMPLETE_ONLY: "仅全集",
        }.get(self._tv_mode, "分集转全集")
        filt = []
        if self._min_resolution:
            filt.append(f"分辨率≥{self._min_resolution}")
        if self._required_hdr:
            filt.append(f"需{self._required_hdr}")
        if self._required_video_codec:
            filt.append(f"需{self._required_video_codec}")
        if self._min_seeders:
            filt.append(f"做种≥{self._min_seeders}")
        if self._only_free:
            filt.append("仅免费")
        if self._min_improvement:
            filt.append(f"分差≥{self._min_improvement}")
        if self._exclude_keywords:
            filt.append(f"排除[{self._exclude_keywords}]")
        if self._prefer_keywords:
            filt.append(f"优先[{self._prefer_keywords}]")
        filt_text = "；".join(filt) if filt else "无"
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        f"状态：{'运行中' if self._enabled else '已停用'} | "
                        f"范围：{scope_text} | "
                        f"仅最佳版本：{'是' if self._only_best_version else '否'} | "
                        f"试运行：{'是' if self._dry_run else '否'}"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": f"电视剧模式：{tv_text} | 过滤规则：{filt_text}",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "secondary",
                    "variant": "tonal",
                    "text": f"上次运行：{self._last_run}",
                },
            },
        ]

    # ------------------------------------------------------------------ #
    # 对外 API / 远程命令（本插件不暴露，返回空）
    # ------------------------------------------------------------------ #
    def get_api(self) -> list[dict[str, Any]]:
        return []

    def get_command(self) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------ #
    # 定时服务：按配置的 Cron 周期触发评分洗版
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

    def _passes_filters(self, title: str, ti: Any) -> tuple[bool, str]:
        """应用评分洗版规则与 PT 来源筛选，返回 (是否通过, 原因)。"""
        tu = (title or "").upper()
        # 排除关键词
        if self._exclude_keywords:
            for kw in [k.strip() for k in self._exclude_keywords.replace("，", ",").split(",") if k.strip()]:
                if kw.upper() in tu:
                    return False, f"命中排除词[{kw}]"
        # 最低分辨率
        if self._min_resolution:
            idx_min = _DIMENSIONS[0][1].index(self._min_resolution)
            rk = _resolution_rank(tu)
            if rk is not None and rk > idx_min:
                return False, f"分辨率低于{self._min_resolution}"
        # 必需 HDR
        if self._required_hdr:
            if self._required_hdr.upper() not in _hdr_tokens(tu):
                return False, f"缺少{self._required_hdr}"
        # 必需视频编码
        if self._required_video_codec:
            if (self._required_video_codec.upper() != (_video_codec_token(tu) or "").upper()):
                return False, f"缺少{self._required_video_codec}"
        # PT 来源：最小做种
        if self._min_seeders > 0:
            seeders = getattr(ti, "seeders", 0) or 0
            if seeders < self._min_seeders:
                return False, f"做种{seeders}<{self._min_seeders}"
        # PT 来源：仅免费
        if self._only_free:
            freedate = getattr(ti, "freedate", None)
            if not freedate:
                return False, "非免费资源"
        return True, ""

    # ------------------------------------------------------------------ #
    # 核心逻辑：评分洗版
    # ------------------------------------------------------------------ #
    def wash(self) -> None:
        if not self.get_state():
            return
        scope_text = {"all": "全部", "movie": "电影", "tv": "电视剧"}.get(self._scope, "全部")
        logger.info(f"[评分洗版] 开始扫描（范围：{scope_text}，试运行：{self._dry_run}）")
        try:
            subs = SubscribeOper().list()
        except Exception as e:  # noqa: BLE001
            self._last_run = f"读取订阅失败：{e}"
            logger.error(f"[评分洗版] {self._last_run}")
            return

        prefer = [k.strip() for k in self._prefer_keywords.replace("，", ",").split(",") if k.strip()]
        complete_kw = [k.strip() for k in self._complete_keywords.replace("，", ",").split(",") if k.strip()]

        processed = 0
        upgraded = 0
        for sub in subs:
            # 立即取出 ORM 字段，避免会话关闭后访问失效
            try:
                sid = int(sub.id)
                name = str(sub.name or "")
                stype = getattr(sub, "type", None)
                best_ver = getattr(sub, "best_version", 0) or 0
            except Exception:  # noqa: BLE001
                continue
            if not name:
                continue
            # 过滤范围
            if self._only_best_version and not best_ver:
                continue
            mtype = self._to_mtype(stype)
            if self._scope == _SCOPE_MOVIE and mtype is not MediaType.MOVIE:
                continue
            if self._scope == _SCOPE_TV and mtype is not MediaType.TV:
                continue

            is_tv = mtype is MediaType.TV

            # 搜索该订阅的资源
            try:
                contexts = SearchChain().search_by_title(title=name, mtype=mtype)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[评分洗版] 搜索《{name}》失败：{e}")
                continue
            if not contexts:
                continue

            # 先过滤 + 打分，并按电视剧模式归类
            candidates: list[tuple[Any, int, str, str]] = []  # (ctx, score, title, cls)
            for ctx in contexts:
                ti = getattr(ctx, "torrent_info", None)
                if not ti:
                    continue
                t = getattr(ti, "title", None) or ""
                ok, reason = self._passes_filters(t, ti)
                if not ok:
                    logger.debug(f"[评分洗版] 《{name}》跳过 {t}：{reason}")
                    continue
                s = _score_title(t, prefer)
                cls = _tv_classify(t, complete_kw) if is_tv else "movie"
                candidates.append((ctx, s, t, cls))

            if not candidates:
                continue

            # 电视剧模式：决定使用哪类资源
            if is_tv:
                if self._tv_mode == _TV_MODE_COMPLETE_ONLY:
                    pool = [c for c in candidates if c[3] in ("complete", "pack")]
                elif self._tv_mode == _TV_MODE_EPISODE_ONLY:
                    pool = [c for c in candidates if c[3] == "episode"]
                else:  # episode_then_complete
                    completes = [c for c in candidates if c[3] in ("complete", "pack")]
                    if completes:
                        pool = completes
                        logger.info(f"[评分洗版] 《{name}》检测到全集资源，切换为全集洗版")
                    else:
                        pool = [c for c in candidates if c[3] == "episode"]
                if not pool:
                    logger.debug(f"[评分洗版] 《{name}》当前模式无匹配资源，跳过")
                    continue
            else:
                pool = candidates

            best_ctx, best_score, best_title = max(pool, key=lambda x: x[1])[0], \
                max(pool, key=lambda x: x[1])[1], max(pool, key=lambda x: x[1])[2]

            prev = self._best.get(sid, -1)
            if best_score <= prev:
                logger.debug(f"[评分洗版] 《{name}》当前最佳评分={prev}，已最优，跳过")
                continue
            # 评分提升阈值
            if prev >= 0 and (best_score - prev) < self._min_improvement:
                logger.debug(
                    f"[评分洗版] 《{name}》提升 {best_score - prev} < {self._min_improvement}，跳过"
                )
                continue

            processed += 1
            if self._dry_run:
                logger.info(
                    f"[评分洗版][试运行] 《{name}》发现更优资源 评分={best_score} -> {best_title}"
                )
            else:
                try:
                    DownloadChain().download_single(best_ctx, source="moviepilot-wash")
                    upgraded += 1
                    logger.info(
                        f"[评分洗版] 《{name}》下载更优资源 评分={best_score} -> {best_title}"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[评分洗版] 《{name}》下载失败：{e}")
            self._best[sid] = best_score

        summary = f"扫描完成：处理 {processed} 个订阅，触发下载 {upgraded} 个" + (
            "（试运行）" if self._dry_run else ""
        )
        self._last_run = summary
        logger.info(f"[评分洗版] {summary}")

    # ------------------------------------------------------------------ #
    # 停用：释放后台资源
    # ------------------------------------------------------------------ #
    def stop_service(self) -> None:
        self._enabled = False
