from datetime import datetime
from threading import Thread
from typing import Any

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


class WashUpgrade(_PluginBase):
    """
    评分洗版插件（MoviePilot v3.2.1）。

    两种洗版模式：
    - 原生最佳版本（默认，推荐）：定时/手动对目标订阅触发 MoviePilot 原生的「最佳版本」搜索，
      评分规则、画质/特效目标、分集/全集处理全部由宿主完成（即「洗版评分规则」页 + 订阅「最佳版本」）。
    - 自定义类别打分：插件按你在配置页勾选的类别（分辨率/来源/视频编码…）自行打分，
      搜索并下载更优版本，覆盖 MP 原生规则，做精细控制。

    资源来源：MoviePilot 已配置的 PT 站点（由宿主搜索/下载链负责）。

    日志：写入宿主插件日志（logs/plugins/washupgrade.log）与内存活动缓存
    （在插件「数据」页可见，避免日志文件暂不可读时看不到运行记录）。
    """

    plugin_name = "洗版自用"
    plugin_desc = "评分洗版：原生最佳版本触发，或自选类别打分覆盖 MP 原生规则。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.4.2"
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
    _wash_mode = _MODE_NATIVE
    # 每个维度：是否启用 + 勾选的类别（按内置优先级 #1→#9 自动排序）
    _dim_enabled: dict[str, bool] = {}
    _dim_values: dict[str, list[str]] = {}
    _min_resolution = ""
    _exclude_keywords = ""
    _bonus_keywords = ""
    _score_threshold = 0
    _last_run = "尚未运行"

    # 内存活动缓存（在「数据」页展示）
    _activities: list[str] = []
    # 自定义模式下，每个订阅已下载的最佳分（内存基线，重启清空，靠宿主去重兜底）
    _best_scores: dict[int, int] = {}
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
        self._record("插件已加载 / 重新配置")

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
                tgt = _SYN.get(tok, tok)
                if dim == "file_extension":
                    if ext == tgt:
                        matched_rank = chosen.index(tok)
                        break
                else:
                    if tgt in tu or tgt.replace("&", ".") in tu or tgt.replace(".", "&") in tu:
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
        return [
            {
                "component": "VForm",
                "content": [
                    # ===== 分区一：基础设置 =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "① 基础设置（通用开关与触发方式）",
                        },
                    },
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
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "「原生最佳版本」模式：评分规则与画质/特效目标请在 MoviePilot「洗版评分规则」页和订阅的「最佳版本」里设置，本插件只负责按时触发它们。",
                        },
                    },
                    # ===== 分区二：自定义类别选择 =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "② 自定义类别（仅在上方「洗版模式 = 自定义类别打分」时生效）"
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
                ],
            }
        ], {
            "enabled": False,
            "wash_mode": _MODE_NATIVE,
            "scope": _SCOPE_ALL,
            "only_best_version": True,
            "crontab": "0 4 * * *",
            "dry_run": True,
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
        mode_text = "原生最佳版本" if self._wash_mode == _MODE_NATIVE else "自定义类别打分"
        scope_text = {"all": "全部", "movie": "仅电影", "tv": "仅电视剧"}.get(self._scope, "全部")
        recent = list(reversed(self._activities[-30:]))
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-2"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "elevated",
                            "text": "立即洗版一次（手动触发）",
                            "block": True,
                        },
                        "events": {
                            "click": {
                                "api": f"plugin/{self.__class__.__name__}/run",
                                "method": "POST",
                                "params": {},
                            }
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "点击后立即按当前配置洗版一次；受「试运行」开关控制——开启试运行则只记录不实际下载。",
                        },
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        f"状态：{'运行中' if self._enabled else '已停用'} | "
                        f"模式：{mode_text} | "
                        f"范围：{scope_text} | "
                        f"仅最佳版本：{'是' if self._only_best_version else '否'} | "
                        f"试运行：{'是' if self._dry_run else '否'}"
                    ),
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
            {
                "component": "VCard",
                "props": {"title": "运行记录（最近 30 条）", "variant": "outlined"},
                "content": [
                    {
                        "component": "VList",
                        "props": {"density": "compact"},
                        "content": [
                            {
                                "component": "VListItem",
                                "props": {"title": line},
                            }
                            for line in (recent or ["暂无运行记录"])
                        ],
                    }
                ],
            },
        ]

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
            }
        ]

    def get_command(self) -> list[dict[str, Any]]:
        return []

    def run_now(self) -> dict[str, Any]:
        """手动触发入口：后台线程执行，避免前端请求超时。"""
        if self._running:
            return {"success": False, "message": "上一次洗版仍在执行中，请稍后再试。"}
        if not self.get_state():
            return {"success": False, "message": "插件未启用，无法触发。"}
        self._running = True

        def _worker() -> None:
            try:
                self.wash()
            finally:
                self._running = False

        Thread(target=_worker, daemon=True).start()
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
            targets.append((sid, sub))
            if self._dry_run:
                self._record(f"[试运行] 将对《{name}》执行{mode_text}洗版")

        if not targets:
            self._last_run = "本次无可洗版订阅"
            self._record(self._last_run)
            return

        if self._dry_run:
            self._last_run = f"[试运行] 计划处理 {len(targets)} 个订阅"
            self._record(self._last_run)
            return

        if self._wash_mode == _MODE_NATIVE:
            try:
                SubscribeChain().search(sids=tuple(sid for sid, _ in targets), manual=True)
                self._last_run = f"已触发 {len(targets)} 个订阅的原生最佳版本搜索"
                self._record(self._last_run)
            except Exception as e:  # noqa: BLE001
                self._last_run = f"触发失败：{e}"
                self._record(self._last_run)
        else:
            bonus = [k.strip() for k in self._bonus_keywords.split(",") if k.strip()]
            for sid, sub in targets:
                self._custom_wash_one(sid, sub)
            self._last_run = f"自定义洗版完成，处理 {len(targets)} 个订阅"
            self._record(self._last_run)

    # ------------------------------------------------------------------ #
    # 自定义权重模式：搜索 + 打分 + 下载更优版本
    # ------------------------------------------------------------------ #
    def _custom_wash_one(self, sid: int, sub: Any) -> None:
        name = str(getattr(sub, "name", "") or "")
        year = getattr(sub, "type", None) and getattr(sub, "year", None)
        stype = getattr(sub, "type", None)
        mtype = self._to_mtype(stype)
        search_title = name
        try:
            if year and int(year) > 0:
                search_title = f"{name} {int(year)}"
        except (TypeError, ValueError):
            pass
        try:
            contexts = SearchChain().search_by_title(search_title, mtype=mtype)
        except Exception as e:  # noqa: BLE001
            self._record(f"[自定义] 《{name}》搜索失败：{e}")
            return

        best_ctx: Any = None
        best_score = -1
        for ctx in (contexts or []):
            ti = getattr(ctx, "torrent_info", None)
            t = getattr(ti, "title", None) if ti else getattr(ctx, "title", None)
            if not t:
                continue
            if not self._passes_filters(t):
                continue
            s = self._score_title(
                t,
                self._dim_enabled,
                self._dim_values,
                [k.strip() for k in self._bonus_keywords.split(",") if k.strip()],
            )
            if s > best_score:
                best_score, best_ctx = s, ctx

        if best_ctx is None:
            self._record(f"[自定义] 《{name}》未找到符合过滤条件的更优资源")
            return

        base = self._best_scores.get(sid, -1)
        if best_score > base and (best_score - base) >= self._score_threshold:
            if self._dry_run:
                self._record(
                    f"[自定义][试运行] 《{name}》发现更优资源 评分={best_score}（基线={base}），将下载"
                )
                return
            try:
                DownloadChain().download_single(best_ctx)
                self._best_scores[sid] = best_score
                self._record(f"[自定义] 《{name}》已下载更优资源 评分={best_score}")
            except Exception as e:  # noqa: BLE001
                self._record(f"[自定义] 《{name}》下载失败：{e}")
        else:
            self._record(
                f"[自定义] 《{name}》当前最佳 评分={best_score} 未高于基线 {base}"
                f"（差 {best_score - base}，阈值 {self._score_threshold}）"
            )

    # ------------------------------------------------------------------ #
    # 停用：释放后台资源
    # ------------------------------------------------------------------ #
    def stop_service(self) -> None:
        self._enabled = False
