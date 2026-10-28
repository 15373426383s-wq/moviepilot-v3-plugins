# 洗版自用 (WashUpgrade)

**评分洗版**插件：按自定义的 9 维优先级规则给订阅资源打分，发现比当前已下版本更优的资源时自动下载升级。

适用宿主：**MoviePilot v3.2.1+**（已针对 v3.2.1 的 `SearchChain` / `DownloadChain` / `SubscribeOper` 验证）。

## 评分规则（维度按优先级 #1 → #9）

每个维度从「好」到「差」排序，命中越靠前得分越高；各维度按优先级给权重（#1 最重）。

| # | 维度 | 取值（好 → 差） |
| --- | --- | --- |
| 1 | 分辨率 | 8K / 4K / 1080P / 1080I / 720P / SD |
| 2 | 片源类型 | UHD-BLURAY-REMUX / UHD-BLURAY / BLURAY-REMUX / REMUX / BLURAY / WEB-DL / WEBRIP / UHDTV / HDTV / DVD / DVDRIP / HDRIP / BDMV / SCR / HDTC / TC / TELESYNC / HDCAM / CAM |
| 3 | 视频编码 | HEVC / AV1 / AVC / VP9 / MPEG2 / VC1 / XVID / DIVX / AVS3 |
| 4 | 音频编码 | TRUEHD7.1 / TRUEHD5.1 / TRUEHD / DTS-HD.MA7.1 / DTS-HD.MA / DTS-X / DTS / DDP5.1 / DDP / EAC3 / AC3 / AAC2.0 / AAC / FLAC / LPCM / OPUS / ATMOS |
| 5 | HDR类型 | DV / HDR10+ / HDR10 / HDR-VIVID / HLG / SDR / EDR / 3D / IMAX / REPACK / PROPER / RERIP / DIY / HQ |
| 6 | 色深 | 12BIT / 10BIT / 8BIT |
| 7 | 帧率 | 120FPS / 60FPS / 50FPS / 30FPS / 24FPS |
| 8 | 字幕 | CHS&CHT / CHS&ENG / CHS / CHT / MULTISUB / MUXED-SUB / EXT-SUB / HARDSUB / RAW / NO-SUB / NO-CHS |
| 9 | 文件格式 | MKV / MP4 / AVI / MOV / WMV / FLV / RMVB / ISO / M4V / MPG / MPEG / 3GP / TS / WEBM / RM |

打分器是纯函数：直接在你给定的关键词列表里对资源标题做匹配（已内置 `2160P→4K` 等常见等价写法），
不依赖宿主内部属性解析，最稳。

## 工作流程

1. 读取全部订阅（默认只处理已开启「最佳版本」的订阅，可在配置里放宽）。
2. 对每个订阅调用 `SearchChain().search_by_title()` 搜索可用资源。
3. 用上面的规则给每个资源的标题打分，选出最高分。
4. 若该资源分数高于本插件记录的「已下载最佳分」，则调用 `DownloadChain().download_single()` 下载升级。
   - 对比基线保存在**内存**中：每个订阅只记已下载的最佳分，只有搜到更高分才下载，避免重复。
   - 重启后基线清空，但宿主本身的去重会兜底，不会重复下载同一资源。

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| 启用评分洗版 | 总开关（同时控制定时任务是否注册） | 关 |
| 洗版范围 | 全部订阅 / 仅电影 / 仅电视剧 | 全部 |
| 仅处理已开启「最佳版本」的订阅 | 收窄到明确要洗版的订阅 | 开 |
| 扫描周期 | Cron 表达式 | `0 4 * * *`（每天 4 点） |
| 试运行 | **只记录最佳资源、不真正下载**，首次务必保持开 | 开 |

## 使用步骤

1. 安装插件，打开「启用评分洗版」，保持「试运行」为开，保存。
2. 观察 MoviePilot 日志出现 `[评分洗版][试运行] 《片名》发现更优资源 评分=N -> 标题`，
   确认打分与筛选符合预期（分数越高版本越好）。
3. 确认无误后关闭「试运行」，到周期时就会真正下载更优资源。

## 已知限制（v1.1.0）

- **电视剧为尽力而为**：搜索按剧名检索，未逐集区分；命中的最高分资源可能是单集或合集，
  下载由宿主去重与最佳版本逻辑兜底。如需严格的逐集洗版，建议仍配合订阅自身的「最佳版本」。
- 打分依赖标题文本匹配，个别非常规命名可能漏判某个维度（漏判只影响该维度得分，不影响其它维度）。
- 对比基线仅存内存，插件重载/宿主重启后会重新评估；重复下载由宿主去重兜底。

## 发布 / 加载

1. 推到 GitHub 仓库的 `main` 分支（本仓库：`moviepilot-v3-plugins`）。
2. 在 MoviePilot 配置项 `PLUGIN_MARKET` 填入仓库地址（多个用逗号分隔）。
3. 插件市场即可发现并安装。

> 发布前确认 `__init__.py` 的 `plugin_version`、`package.v3.json` 的 `version`、`history` 顶部版本三者一致。
> 把 `WashUpgrade`、作者 `your-name` 与链接替换为你自己的标识（目录名需为类名小写 `washupgrade`）。
