# 接入游戏适配器

已有提取程序可输出规范化 JSON，交给 `scripts.games.json_file`。完整示例见 [examples/portable](../examples/portable/translation.toml)；需要直接读取游戏资源时，实现下面的模块接口，并将模块名写入 `project.adapter`。

## JSON 输入

`[adapter].input` 指向条目数组文件：

```json
[
    {
        "id": "chapter-1/line-1",
        "category": "dialogue",
        "group": "chapter 1 / arrival",
        "source": "Welcome, {player}!",
        "targets": [{"file": "scenes/arrival.json", "path": ["line-1"]}],
        "context": {"speaker": "Rook", "line": 1},
        "rules": {"protected_patterns": ["\\{[^{}]+\\}"]}
    }
]
```

| 字段 | 约定 |
| --- | --- |
| `id` | 项目内唯一且稳定；固定 ID 的原文修订不改变它，同文异境可使用不同 ID。 |
| `category` / `group` | 文本用途 / 上下文组。 |
| `source` / `context` | 非空白原文 / 上下文对象或数组。 |
| `targets` | 发布位置列表。file 是交付目录内的相对 JSON 路径，path 是对象键序列；禁止目录逃逸、隐藏文件和 manifest。 |
| `term` | 默认为 false；为 true 时先翻译该条目，并将其声明为标准术语来源。 |
| `use_terms` | 默认为 false；为 true 时允许精确命中术语复用，并要求结果遵守该译法。 |
| `reconcile` | 默认为 false；为 true 时按 `targets[].priority` 复用已有译文填补缺失位置，数值越大越优先。保留已有译法；最高优先级存在不同译法时，报告歧义并暂缓填补。 |
| `rules` | 格式约束，例如 `protected_patterns`、`preserve_tags`、`preserve_newlines`。 |

JSON 适配器自动生成上下文版本和引用。完整字段与校验规则以 [models.py](../scripts/models.py) 和 [Rules](../scripts/config.py) 为准。

增量处理按交付键是否已有有效译文判断。固定 ID 背后的原文修订不自动追踪；需要重译时清除对应译文后重新规划。

## 自定义模块

| 接口 | 返回值与职责 |
| --- | --- |
| `settings(options)` | 返回校验、规范化后的适配器设置。 |
| `fetch(cache, selection, options, *, translations=None, check_existing=False)` | 根据所选语言的译文目录和检查范围获取快照，返回概要；获取期间保留 `.fetching`，全部成功后移除。 |
| `extract(cache, options, *, translations=None, check_existing=False)` | 提取当前检查范围，返回共享 Catalog；未覆盖全部原文时 `complete=false`，source_files 列出所需原文文件。 |
| `publication(catalog, translations, options)` | 返回当前语言的独立 Catalog，补充发布映射和术语声明。 |
| `context(task, cache, options)` | 返回指定条目的完整场景或相关记录。 |

`fetch`、`extract`、`publication` 的 options 包含配置目录 `root`。前两者的 translations 是译文目录列表，publication 接收单个目录。适配器决定如何筛选原文；本游戏日常跳过所有所选语言都已有译文文件的剧情，masterdata 始终提取。publication 不修改传入的 Catalog 或发布文件。

自定义条目需提供 `context_version` 和 `references`：版本覆盖翻译所需资料，引用使用快照内的相对路径。上下文变化必须更新版本，发布地址不应影响该版本。

以交付文件是否存在判断完成时，将对应路径列入 `Catalog.atomic_files`。核心保证新文件的待办整份进入计划，避免限量运行留下半份文件；相互关联的新文件一并处理。

## 术语来源

通过 `Catalog.term_bindings` 声明标准译名的位置，供术语表引用：

```json
{
    "source": "Rook",
    "reference": "character-rook",
    "target": {"file": "characters.json", "path": ["rook"]}
}
```

`reference` 是可选的标准名称标识。`publication` 须接受空 Catalog，并从现有译文结构或配置的原文导出文件中生成术语声明，供交付检查使用。

适配器测试应覆盖原文修订、同文异境、多位置冲突、完整上下文、部分获取和格式约束。可参考 [test_framework.py](../tests/test_framework.py)。
