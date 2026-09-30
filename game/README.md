# 游戏配置与迁移

默认配置是本目录的 `translation.toml`，所有相对路径以本目录为基准。DeepSeek 后端使用 `deepseek-flash`、`medium` 推理强度和 `DEEPSEEK_API_KEY` 环境变量；框架不配置 Agent 的上下文窗口。

## 获取范围

- 日常 `sync`：检查 CDN 目录和 master 版本；只有某个所选语言缺少剧情文件时才获取该剧情。已经交付的历史剧情不因缓存丢失而全量重抓。
- `sync --source-id 12345`：明确获取指定剧情，即使它已有译文；参数可重复。master 始终按版本获取，选择只限制剧情范围。
- 历史检查：在 `[adapter]` 中设置 `check_existing = true`，或设置环境变量 `GIRLSCREATION_CHECK_EXISTING=true` 后同步。它只在获取阶段生效，由游戏适配器负责；关闭后恢复日常范围。
- 离线导入：设置 `[adapter].input` 指向已经解析的资料目录，包含 `index.json`、`master.json`、`novels/<ID>.json`。可以直接读取旧获取缓存；目录必须完整且没有 `.fetching` 标记。该模式不访问 CDN。

框架 `plan` 总是不联网，不调用适配器。同步失败不会替换上一次有效快照。缺席原文不会导致已交付译文被删除。

## 输入与交付

`master-fields.json` 明确列出需要提取的游戏表和字段：`string` 直接读取，`array` 从语言数组读取，`strings` 从 `|` 分隔值读取；`source_index` 默认 0。输出字段后缀仍分别为原字段名、`[]`、`|`。

`title`、`message`、`msgvoicesync` 的解析保留游戏既有逗号位置语义，不额外处理 CSV 引号。剧情使用 dialogue 资源，保留顺序、重复台词和原始说话人；标题单独作为 text 资源写入同一剧情文件，无标题时不补造上下文。名称只从此次实际选中的剧情提取并去重。

`term_sources` 从 `names.json`、`master.json` 的 `mUnits.ml_name[]` 和 `mSubunits.ml_name[]` 读取全部标准译名，包括此次未重抓的历史角色。glossary 只补充术语和说明，`館長` 的 note 直接跟随名称字典的译法。

标题仍保存在原来的剧情和 master 位置，不新增客户端未使用的 `titles.json`。框架按输出字典位置规划待译项，保留已有标题差异；不跨位置重写或统一旧标题。翻译缺失标题时，Agent 可以检索实际标题资源和历史译文。标题不作为全局角色名称，避免同文异义冲突。

## 从本仓库旧版本升级

- 配置从根目录迁到 `game/translation.toml`；自定义命令、外部 CI 调用和书签需更新 `--config` 路径。
- `fetch/prepare/merge` 对应 `sync/plan/publish`；去掉 `catalog/setup/finalize` 和 `update --dry-run`，离线查看待译项直接用 `plan`。
- CI 的 `dry_run` 改为 `plan_only`；本游戏仍保留 `check_existing` 和原来的定时获取安排。
- `--limit` 按仍有缺失译文的资源数计算，已完成的名称或 master 字段不占上限。master 各字段资源共享一个输出文件，规划时不会拆开；上限无法容纳任何含待译项的输出文件时会报错。
- `translations/`、客户端地址和原有 manifest 均不迁移、不改写。旧 `.cache/` 保留；Entry 计划和旧版临时答案文件不能直接用于当前 Resource 计划，需要重新 `sync`、`plan`。后续重新规划可保留仍适用的已接受答案。
- 原始抓取缓存现由适配器维护在 `.cache/translation/girlscreation/adapter-cache/`；不可变快照和 Agent 工作目录由框架自动隔离在同级缓存目录中。

框架契约见 [docs/adapters.md](../docs/adapters.md)。
