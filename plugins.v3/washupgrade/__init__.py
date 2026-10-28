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
_MODE_CUSTOM = "custom"   # 自定义权重打分，覆盖 MP 原生规则

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
_DEFAULT_WEIGHTS = [100, 70, 50, 40, 30, 20, 12, 8, 5]
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
    - 自定义权重打分：插件自己按可配置的 9 维优先级权重对资源打分，搜索并下载更优版本，
      覆盖 MP 原生规则，做精细控制。

    资源来源：MoviePilot 已配置的 PT 站点（由宿主搜索/下载链负责）。

    日志：写入宿主插件日志（logs/plugins/washupgrade.log）与内存活动缓存
    （在插件「数据」页可见，避免日志文件暂不可读时看不到运行记录）。
    """

    plugin_name = "洗版自用"
    plugin_desc = "评分洗版：原生最佳版本触发，或自定义权重打分覆盖 MP 原生规则。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.4.1"
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
    _weights = list(_DEFAULT_WEIGHTS)
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
        # 9 维权重（仅自定义模式生效）
        weights: list[int] = []
        for i, (dim, _vals) in enumerate(_DIMENSIONS):
            try:
                weights.append(int(config.get(f"{dim}_weight", _DEFAULT_WEIGHTS[i])))
            except (TypeError, ValueError):
                weights.append(_DEFAULT_WEIGHTS[i])
        self._weights = weights
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

    @classmethod
    def _score_title(cls, title: str, weights: list[int], bonus: list[str] | None = None) -> int:
        """按 9 维权重对资源标题打分。命中维度内更靠前的（更优）取值得分更高。"""
        if not title:
            return 0
        tu = cls._norm_title(title)
        ext = tu.rsplit(".", 1)[-1] if "." in tu else ""
        bonus = bonus or []
        total = 0
        for (dim, vals), w in zip(_DIMENSIONS, weights):
            matched_idx: int | None = None
            for tok in sorted(vals, key=len, reverse=True):
                tgt = _SYN.get(tok, tok)
                if dim == "file_extension":
                    if ext == tgt:
                        matched_idx = vals.index(tok)
                        break
                else:
                    if tgt in tu or tgt.replace("&", ".") in tu or tgt.replace(".", "&") in tu:
                        matched_idx = vals.index(tok)
                        break
            if matched_idx is not None:
                total += w * (len(vals) - 1 - matched_idx)
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
        weight_items = []
        for i, (dim, _vals) in enumerate(_DIMENSIONS):
            zh, desc = _DIM_ZH.get(dim, (dim, ""))
            weight_items.append({
                "component": "VTextField",
                "props": {
                    "model": f"{dim}_weight",
                    "label": f"{zh}（重要度，默认 {_DEFAULT_WEIGHTS[i]}）",
                    "type": "number",
                    "placeholder": str(_DEFAULT_WEIGHTS[i]),
                    "hint": desc,
                    "persistent-hint": True,
                },
            })
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
                                {"title": "自定义权重打分（覆盖 MP 原生规则）", "value": _MODE_CUSTOM},
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
                    # ===== 分区二：自定义权重打分 =====
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "② 自定义权重打分（仅在上方「洗版模式 = 自定义权重打分」时生效）"
                                    "。「重要度」= 挑选更好版本时该项所占比重，数字越大越优先。"
                                    "例如分辨率 100、文件格式 5，代表更看重清晰度、不太在意格式。"
                                    "一般保持默认即可，无需修改。",
                        },
                    },
                    *weight_items,
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
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
            **{f"{dim}_weight": _DEFAULT_WEIGHTS[i] for i, (dim, _vals) in enumerate(_DIMENSIONS)},
            "min_resolution": "",
            "exclude_keywords": "",
            "bonus_keywords": "",
            "score_threshold": 0,
        }

    # ------------------------------------------------------------------ #
    # 详情页（数据 + 手动触发）
    # ------------------------------------------------------------------ #
    def get_page(self) -> list[dict]:
        mode_text = "原生最佳版本" if self._wash_mode == _MODE_NATIVE else "自定义权重打分"
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
        mode_text = "原生最佳版本" if self._wash_mode == _MODE_NATIVE else "自定义权重打分"
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
            s = self._score_title(t, self._weights, [k.strip() for k in self._bonus_keywords.split(",") if k.strip()])
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
