# 完成本次翻译更新

先阅读本次工作目录中的 `style.md` 以及 `plan.json` 的项目、源语言、目标语言和任务。你在项目目录工作，可使用终端、文件读取和编辑工具。
所有工作状态保存在文末给出的工作目录；下文用 WORK 表示它。将命令中的 WORK 换成该路径，并按当前 shell 正确引用。

## 操作流程

1. 执行 `uv run --no-sync python -m scripts.agent next --work WORK`，获取下一组任务的 JSON 路径、术语投影和参考文件路径。
2. 阅读这组待办和术语，结合完整场景、标题、说话人、相关数据记录及历史译文理解用途。
   执行 `uv run --no-sync python -m scripts.agent context TASK_ID --work WORK` 获取完整上下文，也可以查询参考 JSON。术语投影中每个原文对应一个记录数组：优先选择 categories 包含当前任务分类的记录，无匹配时使用 categories 为空的全局记录。不要将限定分类的译法用于其他场景。
3. 在 WORK 下创建草稿 JSON：`{"translations":[{"id":"任务原有ID","translation":"译文"}]}`。
   只翻译任务的 source，context 中的相邻文本用于理解语境。每个提交的 ID 必须对应一个非空译文；草稿不含 Markdown 围栏、解释或额外字段。
   你可以自行决定一次处理整组还是几个相互关联的条目。不要更改原文或任务 ID。
4. 执行 `uv run --no-sync python -m scripts.agent submit 草稿路径 --work WORK`。
   如校验失败，阅读报错、查阅上下文并修正草稿，再次提交。不要修改校验器或跳过错误。
   成功条目已持久保存。下一次 next 会跳过它们；需要修订本次已接受的译文时可再次 submit 相同 ID。
5. 回到 next，直到剩余任务为 0。先完成 term=true 的术语任务，再提交其他文本；已确定术语会成为后续参考。category 是文本用途，group 是上下文组，均不等同于提交批次。不要读取其他目标语言的缓存作为本语言的完成结果。
6. 执行 `uv run --no-sync python -m scripts.agent finalize --work WORK`。
   只有命令成功才完成。最终回复简要报告完成情况、术语决策和需要人工关注的歧义。

## 术语决策

发现需要跨文本复用的新术语时，在 WORK 下创建术语提案数组：

```json
[{"source":"原文术语","translation":"目标语言的标准译法","note":"含义、适用场景与选择依据","evidence":"含有此术语的任务ID"}]
```

执行 `uv run --no-sync python -m scripts.agent propose 提案路径 --work WORK`。
已通过的提案会进入后续任务的术语投影，并在独立发布步骤加入术语表。已有标准译名不能通过提案直接覆盖。
提案可附加 `"categories":["ui"]` 限定分类，省略表示全局。出处任务必须属于声明的分类；同一原文可增补其他分类的译法，但不能重定义已有范围。
已通过术语任务确定的普通人物标签不必重复添加为全局术语。
名称或数据记录已提供的标准译法无需重复登记；提案应补充新的术语、别名含义或有助于翻译的具体说明。
可以把跨组判断、歧义和进度记录在 WORK/notes.md，继续任务时先读取；不要把内部判断写进译文。

## 完成边界

- 允许读取原文、历史翻译和项目文档，允许在 WORK 内写草稿、结果和笔记。
- 不修改 `translations/`、`glossary/`、`scripts/`、配置、提示词、测试或工作流；不运行 merge、fetch、prepare、setup、Git commit/push。
- 缓存失效或硬性术语冲突若无法通过修正本次结果解决，应留下具体记录并报告未完成；不要假装成功。
- 不另启动 Codex、其他模型客户端或子 agent。由当前会话完成检索、翻译和修订。
