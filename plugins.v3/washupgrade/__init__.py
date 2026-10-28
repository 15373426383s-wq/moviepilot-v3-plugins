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


def _title_has(title_upper: str, token: str) -> bool:
    """标题（大写）是否包含某 token，兼容 & / . / / 分隔符差异。"""
    if token in title_upper:
        return True
    if token.replace("&", ".") in title_upper:
        return True
    if token.replace(".", "&") in title_upper:
        return True
    return False


def _score_title(title: str) -> int:
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
    return total


class WashUpgrade(_PluginBase):
    """
    评分洗版插件（MoviePilot v3.2.1）。

    按用户定义的 9 维优先级规则（分辨率 / 片源 / 视频编码 / 音频编码 / HDR /
    色深 / 帧率 / 字幕 / 文件格式）对每个订阅的资源评分，分数越高版本越好；
    发现比当前已下版本更优的资源时，下载升级。

    对比基线保存在内存中：每个订阅只记录「已下载的最佳评分」，只有当搜索到
    更高分的资源时才触发下载，避免重复下载同一版本。
    """

    plugin_name = "洗版自用"
    plugin_desc = "评分洗版：按 9 维优先级规则给订阅资源打分，下载更优版本。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.1.0"
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

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # 配置页 + 默认配置
    # ------------------------------------------------------------------ #
    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "VForm",
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
                            "placeholder": "0 4 * * *",
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
            }
        ], {
            "enabled": False,
            "scope": _SCOPE_ALL,
            "only_best_version": True,
            "crontab": "0 4 * * *",
            "dry_run": True,
        }

    # ------------------------------------------------------------------ #
    # 详情页
    # ------------------------------------------------------------------ #
    def get_page(self) -> list[dict]:
        scope_text = {"all": "全部", "movie": "仅电影", "tv": "仅电视剧"}.get(self._scope, "全部")
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
                        f"试运行：{'是' if self._dry_run else '否'} | "
                        f"上次运行：{self._last_run}"
                    ),
                },
            }
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

            # 搜索该订阅的资源
            try:
                contexts = SearchChain().search_by_title(title=name, mtype=mtype)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[评分洗版] 搜索《{name}》失败：{e}")
                continue
            if not contexts:
                continue

            best_ctx = None
            best_score = -1
            best_title = ""
            for ctx in contexts:
                ti = getattr(ctx, "torrent_info", None)
                if not ti:
                    continue
                t = getattr(ti, "title", None) or ""
                s = _score_title(t)
                if s > best_score:
                    best_score, best_ctx, best_title = s, ctx, t

            if best_ctx is None:
                continue

            prev = self._best.get(sid, -1)
            if best_score <= prev:
                logger.debug(f"[评分洗版] 《{name}》当前最佳评分={prev}，已最优，跳过")
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
