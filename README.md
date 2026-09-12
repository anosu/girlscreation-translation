# Game Translation Workflow

《少女艺术绮谭》的简中翻译项目，使用 Codex 处理名称、剧情（含标题）和 masterdata，支持更换模型后端、接入其他游戏和多语言翻译。游戏插件：[GCMod](https://github.com/anosu/GCMod)。

## 开始使用

需要 Python 3.14+、[uv](https://docs.astral.sh/uv/) 和 Node.js 22.18+ 或 24+。

```sh
uv sync --frozen --extra girlscreation
npm ci
```

在 [translation.toml](translation.toml) 中选择后端和模型，并设置 `api_key_env` 指定的环境变量；当前配置使用 `DEEPSEEK_API_KEY`。后端须支持 Responses API 和工具调用。

先查看配置和少量待办，再执行翻译、合并与检查：

```sh
npm run workflow -- config
npm run workflow -- update --dry-run --limit 20
npm run workflow -- translate
npm run workflow -- merge
npm run workflow -- check
```

日常更新运行 `npm run workflow -- update`：只获取缺少译文文件的剧情，并从中提取 names；masterdata 每次按表和字段比对原文值，补齐没有译文的键。没有待补内容时不生成翻译任务，也不重建 manifest。

已有剧情的漏译和原文变化通过 `update --check-existing` 完整检查，可加 `--dry-run` 先查看待办。不同译法和旧输出列入 `prepare-report.json`，保留已有译文；缺失位置的复用译法有歧义时，报告为阻塞条目，需人工处理。

## 配置与选择范围

相对路径以配置文件目录为基准。`targets` 声明目标语言，默认全部处理；可用 `--config PATH` 选择配置，用 `--target zh-Hans` 选择语言，多个语言可用逗号分隔。

- 后端优先级：`--backend` > `TRANSLATION_BACKEND` > 目标的 `backend` > 项目的 `backend`。
- 模型优先级：`--model` > `TRANSLATION_MODEL` > 后端的 `model`。
- 目标可指定 `translations`、`glossary`、`state`、`work` 路径，以及 `style` 风格文件和 `rules` 校验规则。
- 后端的 `codex.effort` 指定推理强度；`codex.context_window` 指定 token 预算，需与所选模型容量匹配。

`--limit` 仅用于 `prepare` 和 `update`，限制每种语言的计划条目数。新剧情必须整份纳入计划，额度不足时需提高上限，避免发布半份文件。`translate` 执行已有计划；`--dry-run` 只获取和规划，不调用模型。各命令参数可用 `--help` 查看，例如 `npm run workflow -- update --help`。

## 维护与恢复

| 需求 | 命令（接在 `npm run workflow --` 后） |
| --- | --- |
| 查看进度与下一步 | `status` |
| 查看汇总 | `summary` |
| 查看待审阅文件 | `review --target zh-Hans` |
| 确认已审文件 | `review --target zh-Hans --ack novels/12345.json` |
| 清理过期的非当前任务缓存 | `cache --prune --days 30` |

翻译中断后可重跑 `translate`，合并中断后可重跑 `merge`。发生人工修改冲突时先核对文件。搬迁工作目录后，用 `setup --config PATH --target zh-Hans` 重新绑定路径，原文快照需保持一致。

译文、[术语表](glossary/README.md) 和 `translation-state/` 需要一起提交；`.cache/` 保存任务缓存。风格或术语变化可能使待办缓存失效，并产生历史译文审阅清单，不会自动整库重翻。

Husky 会在提交前根据暂存的配置和译文构建 manifest。`check` 检查交付结构、术语与 manifest；译文语义仍需审阅。

## GitHub Actions

在仓库 Secrets 中添加后端 `api_key_env` 对应的密钥，然后运行 [Update Translations](.github/workflows/update_translation.yml)。手动运行默认只规划，可勾选 `check_existing` 完整检查；每天北京时间 13:30 自动更新，周日同时完整检查。

可通过运行参数选择配置、语言、后端、模型和条目上限，也可设置 `TRANSLATION_BACKEND`、`TRANSLATION_MODEL`、`TRANSLATION_LIMIT` 仓库变量。所有所选语言成功后才统一发布。跨仓库调用可使用 `workflow_call` 和 `secrets: inherit`。

## 静态服务

`npm start` 将同级 `translations/` 映射到 `/translations/`，端口由 `PORT` 设置，默认为 12315。

独立部署只需 Node.js：复制 `app.ts`、`package.json`、`package-lock.json` 和 `translations/`，运行 `npm ci --omit=dev --ignore-scripts` 后启动。自定义目录中的翻译产物需放入服务的 `translations/`。

## 扩展与开发

接入其他游戏可参考 [双语言 JSON 示例](examples/portable/translation.toml) 和[适配器文档](docs/adapters.md)。通用 JSON 适配器只需 `uv sync --frozen`，无需安装本游戏的可选依赖。

开发检查：`npm test`、`npm run typecheck`、`npm run lint`。

参考：[架构](docs/workflow-design.md) · [领域用语](CONTEXT.md) · [翻译评估样例](examples/evaluation/README.md)。
