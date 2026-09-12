# 术语表

每种目标语言独立维护术语表，路径由 `glossary` 配置指定。names 和 master 中的标准名称会自动加入有效术语；这里只补充固定译法、别名、说明和适用分类。

每项提供 `reference` 或 `translation` 其中之一：

```json
{
    "館長": {"reference": "館長", "note": "主人公，男性"},
    "館長さん": {"reference": "館長", "note": "同一角色的称呼"},
    "イマージュ": {"translation": "形象"},
    "開始": {"translation": "开始", "categories": ["master"]}
}
```

`reference` 引用适配器声明的标准名称；`note` 解释含义；`categories` 限定分类，省略或为空表示全局。无需重复登记没有说明和分类的同名引用。

同一原文有不同用途时可使用数组，例如另一款游戏中的角色名与棋子名：

```json
{
    "Rook": [
        {"reference": "Rook", "note": "角色名"},
        {"translation": "车", "categories": ["ui"], "note": "国际象棋棋子"}
    ]
}
```

分类匹配优先于全局记录。显式分类不能重叠，显式全局译法须与自动来源一致。精确匹配指整条原文相等，不做机械子串替换。

Agent 可提出有出处的新增术语，已有译法通过人工维护修改。
