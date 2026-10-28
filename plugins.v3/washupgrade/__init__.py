from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.chain.subscribe import SubscribeChain
from app.schemas.types import MediaType

# 洗版范围
_SCOPE_ALL = "all"
_SCOPE_MOVIE = "movie"
_SCOPE_TV = "tv"


class WashUpgrade(_PluginBase):
    """
    定时触发 MoviePilot 订阅刷新。

    MoviePilot 的「洗版」本质是订阅的 best_version（最佳版本）：宿主在每次刷新时
    会重新拉取站点种子并匹配，对开启最佳版本的订阅自动下载更优画质。
    本插件不重造搜索/下载/画质比对逻辑，只负责按 Cron 周期触发刷新，
    把「画质目标」交还给每个订阅自身的设置（最佳版本 + 画质/分辨率/特效）。
    """

    plugin_name = "洗版自用"
    plugin_desc = "周期性触发订阅刷新，对开启最佳版本的订阅自动下载更优画质版本。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.0"
    plugin_author = "your-name"
    author_url = "https://github.com/your-name"
    plugin_config_prefix = "washupgrade_"
    plugin_order = 50
    auth_level = 1

    # 运行态（由 init_plugin 填充，必须允许重复调用）
    _enabled = False
    _scope = _SCOPE_ALL
    _crontab = "0 4 * * *"
    _dry_run = True
    _last_run = "尚未运行"

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict | None = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._scope = str(config.get("scope") or _SCOPE_ALL)
        self._crontab = str(config.get("crontab") or "0 4 * * *")
        self._dry_run = bool(config.get("dry_run"))

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
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用洗版"},
                    },
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
                        "component": "VTextField",
                        "props": {
                            "model": "crontab",
                            "label": "扫描周期 (Cron)",
                            "placeholder": "0 4 * * *",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "dry_run", "label": "试运行（只记录不真正刷新）"},
                    },
                ],
            }
        ], {
            "enabled": False,
            "scope": _SCOPE_ALL,
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
                        f"试运行：{'是' if self._dry_run else '否'} | "
                        f"上次运行：{self._last_run}"
                    ),
                },
            }
        ]

    # ------------------------------------------------------------------ #
    # 对外 API（本插件不暴露，返回空）
    # ------------------------------------------------------------------ #
    def get_api(self) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------ #
    # 定时服务：按配置的 Cron 周期触发洗版刷新
    # ------------------------------------------------------------------ #
    def get_service(self) -> list[dict]:
        if not self.get_state():
            return []
        return [
            {
                "id": "WashUpgrade.Refresh",
                "name": "订阅洗版刷新",
                "trigger": CronTrigger.from_crontab(self._crontab),
                "func": self.wash,
                "kwargs": {},
            }
        ]

    # ------------------------------------------------------------------ #
    # 核心逻辑
    # ------------------------------------------------------------------ #
    def _mtype(self) -> str | None:
        """根据范围返回媒体类型，None 表示全部。"""
        if self._scope == _SCOPE_MOVIE:
            return MediaType.MOVIE.value
        if self._scope == _SCOPE_TV:
            return MediaType.TV.value
        return None

    def wash(self) -> None:
        """触发订阅刷新，由宿主对最佳版本订阅执行洗版（下载更优画质）。"""
        if not self.get_state():
            return
        scope_text = {"all": "全部", "movie": "电影", "tv": "电视剧"}.get(self._scope, "全部")
        logger.info(f"[洗版] 开始扫描（范围：{scope_text}）...")
        try:
            if self._dry_run:
                self._last_run = f"[试运行] 将对「{scope_text}」订阅执行洗版刷新（未实际执行）"
                logger.info(f"[洗版] {self._last_run}")
                return
            # 直接复用宿主订阅刷新能力，内部会拉取站点种子并匹配/下载更优版本。
            SubscribeChain().refresh(mtype=self._mtype(), source="manual")
            self._last_run = f"完成「{scope_text}」订阅洗版刷新"
            logger.info(f"[洗版] {self._last_run}")
        except Exception as e:  # noqa: BLE001
            self._last_run = f"运行异常：{e}"
            logger.error(f"[洗版] {e}")

    # ------------------------------------------------------------------ #
    # 停用：释放后台资源
    # ------------------------------------------------------------------ #
    def stop_service(self) -> None:
        self._enabled = False
