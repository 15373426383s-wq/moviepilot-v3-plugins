from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.schemas.types import MediaType
from app.db.oper.subscribe import SubscribeOper
from app.chain.subscribe.facade import SubscribeChain

# 洗版范围
_SCOPE_ALL = "all"
_SCOPE_MOVIE = "movie"
_SCOPE_TV = "tv"


class WashUpgrade(_PluginBase):
    """
    评分洗版插件（MoviePilot v3.2.1）。

    本插件只做一件事：定时（或手动）对目标订阅触发 MoviePilot 原生的「最佳版本」搜索。
    真正的洗版评分规则、画质/特效目标、分集/全集处理，全部由 MoviePilot 自身完成——
    也就是你在 MoviePilot「洗版评分规则」页配置的全局优先级，以及每个订阅里设置的
    「最佳版本」目标。插件不重复实现评分，避免与宿主规则冲突。

    资源来源：MoviePilot 已配置的 PT 站点（由宿主搜索链负责）。

    日志：同时写入宿主插件日志（logs/plugins/washupgrade.log）与内存活动缓存
    （在插件「数据」页可见，避免日志文件暂不可读时看不到运行记录）。
    """

    plugin_name = "洗版自用"
    plugin_desc = "定时触发 MoviePilot 原生最佳版本搜索，复用宿主评分规则做洗版。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.3.0"
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

    # 内存活动缓存（在「数据」页展示，避免依赖插件日志文件）
    _activities: list[str] = []

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
                            "label": "仅对开启「最佳版本」的订阅洗版",
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
                            "label": "试运行（只记录将触发的订阅，不真正搜索）",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "洗版评分规则与画质/特效目标请在 MoviePilot 的「洗版评分规则」页和订阅的「最佳版本」中设置；本插件只负责按周期触发它们。",
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
    # 详情页（数据）
    # ------------------------------------------------------------------ #
    def get_page(self) -> list[dict]:
        scope_text = {"all": "全部", "movie": "仅电影", "tv": "仅电视剧"}.get(self._scope, "全部")
        recent = list(reversed(self._activities[-30:]))
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
    # 对外 API / 远程命令（本插件不暴露，返回空）
    # ------------------------------------------------------------------ #
    def get_api(self) -> list[dict[str, Any]]:
        return []

    def get_command(self) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------ #
    # 定时服务：按配置的 Cron 周期触发最佳版本搜索
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
    # 核心逻辑：触发 MoviePilot 原生最佳版本搜索
    # ------------------------------------------------------------------ #
    def wash(self) -> None:
        if not self.get_state():
            return
        scope_text = {"all": "全部", "movie": "电影", "tv": "电视剧"}.get(self._scope, "全部")
        self._record(f"开始扫描（范围：{scope_text}，试运行：{self._dry_run}）")
        try:
            subs = SubscribeOper().list()
        except Exception as e:  # noqa: BLE001
            self._last_run = f"读取订阅失败：{e}"
            self._record(self._last_run)
            return

        target_sids: list[int] = []
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
            # 仅最佳版本订阅
            if self._only_best_version and not best_ver:
                continue
            # 范围过滤
            mtype = self._to_mtype(stype)
            if self._scope == _SCOPE_MOVIE and mtype is not MediaType.MOVIE:
                continue
            if self._scope == _SCOPE_TV and mtype is not MediaType.TV:
                continue
            target_sids.append(sid)
            if self._dry_run:
                self._record(f"[试运行] 将触发《{name}》的最佳版本搜索")

        if not target_sids:
            self._last_run = "本次无可洗版订阅"
            self._record(self._last_run)
            return

        if self._dry_run:
            self._last_run = f"[试运行] 计划触发 {len(target_sids)} 个订阅"
            self._record(self._last_run)
            return

        # 真正触发：复用 MoviePilot 原生最佳版本搜索（含全局评分规则 + 订阅目标）
        try:
            SubscribeChain().search(sids=tuple(target_sids), manual=True)
            self._last_run = f"已触发 {len(target_sids)} 个订阅的最佳版本搜索"
            self._record(self._last_run)
        except Exception as e:  # noqa: BLE001
            self._last_run = f"触发失败：{e}"
            self._record(self._last_run)

    # ------------------------------------------------------------------ #
    # 停用：释放后台资源
    # ------------------------------------------------------------------ #
    def stop_service(self) -> None:
        self._enabled = False
