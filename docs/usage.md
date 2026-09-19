# 使用手册

所有命令从仓库根目录执行。默认配置是 game/translation.toml，路径相对配置文件目录。

## 安装与预览

```sh
uv sync --frozen
npm ci
npm run workflow -- sync
npm run workflow -- plan
```

sync 获取适配器选定的材料。本仓库默认访问游戏 CDN，plan 只读上次成功同步的本地快照，不访问游戏服务器、不调用模型。只想离线体验框架时，为 sync、plan 都传入 --config docs/examples/dictionaries/translation.toml；该 JSON 示例首次产生 5 份资源、27 条待办，重复台词只生成一个字典待办。游戏依赖需额外安装 uv sync --frozen --extra girlscreation。

## 翻译与发布

在配置中填写 backend 的 model，设置 api_key_env 指定的密钥环境变量。本游戏使用 DEEPSEEK_API_KEY，通用示例使用 MODEL_API_KEY；项目不自动加载 .env，后端需支持 Responses API 与工具调用。

```sh
npm run workflow -- translate
npm run workflow -- publish
npm run workflow -- check
```

translate 为每个工作包启动单独的 Agent 会话，保存已校验答案。失败后重跑同一命令继续。publish 在全部目标预检通过后合并字典；中断可重跑，人工修改冲突需先解决。check 不证明语义质量、全游戏覆盖或远端最新。

工作包由框架自动组织：名称优先，剧情正文和标题一起处理；同表字段和短文本适度合包，长表分批。无需配置主 Agent 或并发队列。plan 和 summary 显示工作包数量，translate 输出每包耗时与剩余待办；--limit 限制输入资源范围，不是 Agent 会话数。升级分包逻辑后重新 plan 即可，已有交付不重翻。

update 是同步、规划、翻译、发布和检查的组合命令，会访问资源并在需要时调用模型。只想查看待办时使用 plan，不使用 update。

## 选择与配置

- --config PATH 选择其他项目；--target 可重复或用逗号分隔语言。
- sync/update 的 --source-id 传给适配器；JSON 适配器按资源 ID 选择，未知 ID 报错。
- plan/update 的 --limit 只计算仍有缺失译文的资源，不限制同步成本。已完成资源作为上下文保留，不占额度；共享输出文件的资源仍完整纳入。额度无法容纳任何待翻译组时会报错，并提示最低上限。规划报告和 summary 会分别显示本次选择与因上限暂缓的待办。
- --backend > TRANSLATION_BACKEND > 目标 backend > 项目 backend。
- --model > TRANSLATION_MODEL > 后端 model。
- translate/update 的 --timeout 是每个工作包的模型进程时限，默认 3600 秒。
- project.cache 统一设置缓存根目录，默认 .cache/translation，自动按项目 ID 和语言隔离。目标可指定 translations、glossary、style 和 rules；一般无需手动配置 sources 或 work。
- 资源的 path 定位输出文件中的字典，省略时为根字典。目标的 term_sources 可指定 names.json 等标准译名字典；详见[完整示例](examples/dictionaries/README.md)。

## 创建其他游戏

```sh
npm run workflow -- init projects/my-game --source-language ja --target zh-Hans,en
npm run workflow -- sync --config projects/my-game/translation.toml
npm run workflow -- plan --config projects/my-game/translation.toml
```

init 生成空的 sources/resources.json 及配置、风格、术语和 manifest，不覆盖已有目录。先填写资源，再同步。特殊格式接入见[适配器契约](adapters.md)。

## 维护与 CI

status 显示下一步，summary 显示统计，cache --prune --days 30 清理过期非当前任务答案。迁移工作目录后重新运行 translate 会绑定路径；原文快照须一并保留。

人工改译后运行 npm run build:manifest。提交钩子默认读取暂存的 game/translation.toml；独立项目可传 --config 或调整钩子。

[Update Translations](../.github/workflows/update_translation.yml) 手动运行默认 plan_only=true：同步选定资源并规划，不调用模型或发布，但同步可能联网。实际运行需配置模型与对应的仓库 Secret，关闭 plan_only。CI 先同步一次，按目标语言隔离工作，再统一发布。本游戏保留每日更新和周日历史检查，获取范围与成本见[游戏说明](../game/README.md)。

## 静态服务

npm start 提供根目录 translations/ 下的文件，URL 前缀 /translations/，PORT 默认 12315。独立部署复制 app.ts、package.json、package-lock.json、translations/，运行 npm ci --omit=dev --ignore-scripts 后启动。自定义输出目录需复制到服务的 translations/。

## 从旧工作流迁移

旧逐条 Entry JSON 不再接受，改为 dialogue/text 资源数组。输出文件可以包含多层字典，在每份资源的 path 中指定对应位置即可；最后一级始终是原文到译文。适配器改为 collect(request)，不再实现五个固定方法。

使用 names 作为唯一名称标准时配置 term_sources。旧 glossary 的重复译法可移除并保留 note；其他补充术语继续填写 translation。旧 reference 语法不接受。

fetch/prepare/merge 改为 sync/plan/publish，catalog/setup/finalize 不再是用户入口；不再有 update --dry-run 或通用 --check-existing。CI 的 dry_run 输入改为 plan_only，旧历史检查由游戏适配器自己的配置控制。

已有 dialogue/text 资源、嵌套交付字典和显式 sources/work 配置保持兼容，无需转换。内部计划升级时，在原配置下重新运行 plan、translate；框架校验并复用仍适用的草稿，无需新建工作目录或搬动译文。缓存目录名属于内部实现，不跟随每次计划格式升级而更改。旧逐条 Entry 的迁移仍需按上文修改适配器。

开发检查：npm test、npm run typecheck、npm run lint。
