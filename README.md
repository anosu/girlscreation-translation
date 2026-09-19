# 少女艺术绮谭翻译

使用 Agent 翻译名称、剧情和 masterdata，输出供 [GCMod](https://github.com/anosu/GCMod) 使用的原文到译文字典。框架来自 [game-translation-template](https://github.com/anosu/game-translation-template)，游戏获取和解析逻辑独立维护。

## 从这里开始

1. [game/translation.toml](game/translation.toml)：语言、模型后端和获取选项。默认使用 `deepseek-flash`，密钥环境变量为 `DEEPSEEK_API_KEY`。
2. [game/styles/zh-Hans.md](game/styles/zh-Hans.md)：游戏翻译风格；[game/glossary/zh-Hans.json](game/glossary/zh-Hans.json)：补充术语与说明。
3. [game/README.md](game/README.md)：原文选择、交付格式、历史检查与旧工作流迁移。

需要 Python 3.14+、uv 和 Node.js 22.18+ 或 24+：

```sh
uv sync --frozen --extra girlscreation
npm ci
npm run workflow -- config
npm run workflow -- sync
npm run workflow -- plan
```

`sync` 获取游戏资料，默认只下载所选语言中缺少译文文件的剧情；masterdata 按 CDN 版本缓存。`plan` 只读本地不可变快照，不联网、不调用模型。准备好密钥后执行：

```sh
npm run workflow -- translate
npm run workflow -- publish
npm run workflow -- check
```

`update` 连续执行以上五个阶段。中断后重跑 `translate` 可恢复适用答案；发布中断后重跑 `publish`。具体用法见[使用手册](docs/usage.md)。

## 目录

```text
game/                    游戏配置、字段清单、风格与术语
adapters/girlscreation/   CDN 获取、解密、Unity 解析、Resource 提取
workflow/                通用规划、Agent 会话、校验和发布
translations/            既有交付字典及 manifest
docs/                    框架契约与示例
tests/                   框架、游戏适配器和部署回归测试
```

名称以 `names.json` 及 master 中的角色名字段为准，不再复制到 glossary。剧情标题仍写入 `novels/<ID>.json`；master 保留表名、字段名以及 `[]`、`|` 后缀，兼容现有客户端。

## GitHub Actions

在仓库 Secrets 中添加 `DEEPSEEK_API_KEY`，运行 [Update Translations](.github/workflows/update_translation.yml)。默认 `plan_only=true`，同步并规划但不调用模型；关闭后实际翻译和发布。`source_ids` 可指定逗号分隔的剧情 ID，`check_existing` 由本游戏适配器解释。

保留原有每天北京时间 13:30 的自动更新及周日历史检查。完整历史检查会重新下载已有剧情，成本较高；日常更新无需开启。`limit` 只限制仍需翻译的资源数，已完成资源不占额度，也不限制下载；共享一个输出文件的资源必须一起纳入。

## 静态服务与开发

`npm start` 将 `translations/` 映射到 `/translations/`，端口默认 12315，可用 `PORT` 覆盖。现有 Vercel 入口、路径转发和翻译文件打包配置保持适用。独立部署只需 `app.ts`、`package.json`、`package-lock.json` 和 `translations/`，执行 `npm ci --omit=dev --ignore-scripts` 后启动。

开发检查：`npm test`、`npm run typecheck`、`npm run lint`、`npm run check:translations`。测试使用本地资料和模拟 CDN，不抓取全游戏，不调用付费模型。
