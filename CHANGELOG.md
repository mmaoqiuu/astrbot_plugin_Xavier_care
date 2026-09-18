# 变更日志

## v1.6.2
* 新增：
  - README 补充「升级与数据迁移」章节，说明数据独立存放、旧插件名目录自动迁移与新增配置项生效方式。
  - README 异常监测规则表补充「深夜实时熬夜」规则（深夜窗口 + 心率高于静息倍数）及其独立冷却说明。
  - README 配置表补充 `late_night_start_time` / `late_night_end_time` / `late_night_hr_ratio` / `fallback_care` 四项。
* 修改：
  - README 顶部标注当前插件版本；`retention_days` 默认值由 `30` 更正为实际的 `180`。
  - metadata 版本号同步至 v1.6.2。
* 修复：
  - 无。
* 原因：
  - 文档落后于实现：实时熬夜规则、兜底文案与数据保留默认值均未在 README 中体现。

## v1.6.1
* 修复：
  - 数据迁移时，若旧目录中的同名文件更新（例如重启前手机又上报过一次），改为覆盖迁移，避免丢当天最新数据。
* 原因：
  - 迁移逻辑最初只复制「目标不存在」的文件，存在覆盖不到最新数据的窗口。

## v1.6.0
* 新增：
  - 启动时自动迁移旧插件名遗留的数据目录，改名后历史数据不丢。
* 修改：
  - 插件 ID 与目录更名为 `astrbot_plugin_Xavier_care`（插件列表显示名仍为「小狗健康」）。
  - 数据目录改为 `data/plugin_data/astrbot_plugin_Xavier_care/`，配置文件改为 `astrbot_plugin_Xavier_care_config.json`。
  - 日志前缀由 `[health_bridge]` 统一改为 `[Xavier_care]`。
  - metadata 的 repo 字段补全为仓库地址；README 与测试导入路径同步更新。
* 修复：
  - 无。
* 原因：
  - 与仓库名 `astrbot_plugin_Xavier_care` 保持一致。

## v1.5.0
* 移除：
  - `prune_old_events()` 及配套配置项 `events_keep_days`。
  - `store_report()` 中对 `events` / `event` / `app_name` / `source` 的合并入库逻辑，这些字段现在一律丢弃。
  - 摘要与日详情里的事件渲染分支（「最近动态」「事件：」两处）。
  - 未完成的「每日独白」相关代码：`load_prev_day_report()`、`save_daily_thought()`，以及 `daily_thought` / `daily_thought_at` 两个字段。
* 修改：
  - 存储层不再保存任何 App 使用记录；历史数据文件中残留的 `events` 字段一并清除。
  - 历史数据文件中残留的 `daily_thought` / `daily_thought_at` 字段一并清除。
* 修复：
  - 测试中两个期望「非法日期抛异常」的用例与实现（兜底成今天）不符，已按当前契约重写。
  - 修正 `normalize_payload` 内「不合法即抛」的过时注释。
* 原因：
  - App 使用记录已由 astrbot_plugin_event_sensor（小狗雷达）独立采集，健康插件不必再承担该职责，也不应留存相关数据。
## v1.4.0
* 新增：
  - 统一指令入口 `/health`，原 `/health_monitor` 与 `/health_period` 合并为一条指令。
* 修改：
  - 动作词精简为 `on` / `off` / `test` / `base` / `log` / `mark` / `end`，`baseline` 与 `history` 仍作别名兼容。
  - 不带参数时输出总览：主动关怀状态 + 经期 / 周期状态合成一条消息。
  - 关怀记录默认只展示最近 3 条，避免长输出刷屏。
  - README、`_conf_schema.json` 提示文案与代码注释内的指令名同步更新。
* 修复：
  - 无。
* 原因：
  - 原指令名长、动作词分散、历史记录全量输出，日常查看成本高。

## v1.3.1
* 新增：
  - `/health_monitor baseline` 指令支持，输出当前历史基线健康数据统计算法结果。
  - `_check_late_night_realtime` 实时熬夜判定规则，当深夜时段（默认 23:00~05:00）检测到当前心率明显高于静息心率时主动关怀。
* 修改：
  - `_conf_schema.json` 补充 `rule_late_night_realtime` 及时间窗口与比例配置。
* 修复：
  - 增强异常检测鲁棒性，基线不足时优雅回退并保留绝对阈值检测。
* 原因：
  - 支持即时熬夜感知，提升健康主动关怀的实时性与灵敏度。
