# 资源输入与适配器契约

当前使用资源式输入。旧 Entry 数组、逐条 targets.path 和五方法适配器不再支持。每份资源通过 output 和可选 path 定位一个字典，字典内部始终为精确原文到译文；文件可以包含多个这样的字典。

## 本地 JSON 接入

默认使用 JSON 适配器，读取 sources/resources.json，无需配置 adapter。需要其他位置时设置相对配置目录的路径：

```toml
[adapter]
input = "sources/resources.json"
```

输入可以是单份资源对象、资源数组，或目录（按文件名读取当前层的 *.json，不递归）。数组和目录均可混合以下两种资源。只需提供 output、kind 和对应材料；path 默认为 []，即文件根字典。其他信息按真实资料提供，不要求标题、摘要或逐行 ID。

id 可省略：根字典使用 output，嵌套字典使用 output 加 # 和紧凑 JSON path，例如 master.json#["table","name"]。多个资源共享同一 output/path 时，须指定各自稳定且唯一的 id。已有显式 ID 可以继续使用；不要随意更换，否则会影响选择参数和草稿复用。

### VN 对话

```json
{
  "id": "scenario/001",
  "output": "novels/001.json",
  "kind": "dialogue",
  "lines": [
    [null, "ここはどこ？"],
    ["アリス", "目が覚めたのね。"],
    ["アリス", "もう大丈夫よ。"]
  ]
}
```

每行必须是 [原始说话人或 null, 非空白原文]。保留顺序和重复出现；说话人只是上下文，不自动翻译。名称确需交付时，另放入 text 资源。

### 其他分组文本

```json
{
  "id": "ui",
  "output": "ui.json",
  "kind": "text",
  "blocks": [
    {"texts": ["はじめる", "つづきから"]},
    {"context": {"path": ["Settings", "Help"]}, "texts": ["音量を調整します。"]}
  ]
}
```

blocks 中的 texts 是待翻译字符串数组，context 可省略。原始上下文可以是对象或数组，框架只展示，不把其中的字段名、ID 或文本自动加入翻译队列。它可以记录任何游戏实际具有的分组信息，不限定数据表结构。

两种资源都可使用资源级 context、rules 和 term。rules 支持 [Rules](../workflow/config.py) 中的占位符、标签等约束；term=true 表示本轮优先确定的术语资源，译法保存在它的输出字典中，不自动复制到 glossary。希望以后没有重新提取该资源时仍使用全部已有译名，应配置 term_sources。没有术语资源也可以正常翻译。

output 是目标语言交付目录下的相对 .json 路径，禁止绝对路径、目录逃逸、隐藏路径和 manifest.json。path 是 JSON 对象键的字符串数组，不是文件系统路径或数组下标；ml_name[] 等键按字面值使用。每条待办的最后一级键恒为精确原文。

同一 output/path 中相同原文只生成一个待办，但保留全部出现位置供阅读；不同字典位置的同文互相独立，可以有不同译法。不能同时把某个位置声明为字典和其子字典，例如同一文件的 [] 与 ["table"]，或 ["table"] 与 ["table","name"]；框架会提前拒绝这种重叠。

### 同一文件中的多个表或字段

```json
[
  {
    "id": "actions",
    "output": "master.json",
    "path": ["mActionPatterns", "name"],
    "kind": "text",
    "blocks": [{"texts": ["AI：攻撃的", "AI：防御的"]}]
  },
  {
    "id": "filters",
    "output": "master.json",
    "path": ["mActiveSkillSideEffectFilters", "ml_name[]"],
    "kind": "text",
    "blocks": [{"texts": ["即死", "再行動"]}]
  }
]
```

规划、Agent 历史译文读取及发布都使用同一字典位置。修改其中一个位置会保留文件里的其他表、字段和旧译文。数组、数字等原始游戏字段不属于交付格式，应由适配器提取为待翻译字符串。

### 名称字典作为标准来源

```toml
[targets.zh-Hans]
term_sources = [
    { file = "names.json" }
]
```

文件路径相对该目标语言的 translations 目录；也可以配置 { file = "master.json", path = ["mCharacters", "name"] }。来源不存在时按空字典处理，可从首次名称翻译开始使用。配置来源的资源自动优先处理，无需再写 term=true。

框架读取这些字典的全部已有译法，包括本次未重新提取的历史名称，并将本次已接受的新译名投影给后续剧情。glossary 只补充术语或说明；同一全局标准名称出现相互矛盾的译法时会报错。说明和旧数据迁移见[术语文档](glossary.md)。

可运行的 names、VN、多个表及独立标题组合见[完整示例](examples/dictionaries/README.md)。

多个资源可写入同一 output，同一文件的资源作为完整规划单位。--limit 只计算其中仍有缺失译文的资源；已完成资源仍可阅读，不占额度，不能将待翻译组拆半。上限容纳不了任何待翻译组时会报错；部分选择时报告会列出暂缓的待办数量。矛盾的同文规则和译法必须解决后才能发布。

## 自定义适配器

在 adapters/my_game.py 实现唯一必要入口，并设置 project.adapter = "adapters.my_game"：

```python
from collections.abc import Iterable
from workflow.adapters import CollectRequest
from workflow.resources import Resource

def collect(request: CollectRequest) -> Iterable[Resource]:
    # 自行校验 request.options，并从本地或远端提取真实材料。
    # 每次 yield 一份完整 Resource；异常终止本次同步。
    ...
```

| request 字段 | 含义 |
| --- | --- |
| root | 配置目录，Path |
| options | adapter 配置字典，由适配器校验 |
| cache | 适配器专用缓存目录，可能尚不存在 |
| selection | --source-id 传入的字符串列表或 None，语义由适配器定义 |
| translations | 目标语言到交付目录 Path 的映射，只读 |

适配器负责选择、下载、解密、解析、限速和获取重试。不调用模型，不改译文、术语、计划或框架代码。任一所选语言仍需资源时，不应只因另一语言已有交付就跳过。

collect 只在 sync/update 的同步阶段调用。逐份返回 Resource 实例，ID 不重复且应来自稳定资源标识。主动未选择的历史资源不属于失败；选中资源获取失败则抛异常，不能静默遗漏。框架只在全部返回成功后替换当前快照指针，失败保留上一次有效快照。

框架不要求完整游戏范围或远端修订检测，不根据缺席资源删除译文。适配器应说明日常范围、选择参数、上下文来源及成本。高级历史检查使用游戏自己的配置选项，不再有全局 check-existing 开关。

同步完成后，plan、Agent 阅读、校验和发布只使用本地材料，不再回调适配器。原始资源内部可自行拆分获取与解析代码，框架不要求 settings/fetch/extract/publication/context 五个方法。

## 验收

[test_resources.py](../tests/test_resources.py) 覆盖资源与 Agent 流程，[test_dictionary_paths.py](../tests/test_dictionary_paths.py) 覆盖多表字典、名称来源和发布恢复。游戏自己的测试还应验证真实解析、选择范围和网络失败行为。不要通过联网抓取全游戏来完成普通契约测试。
