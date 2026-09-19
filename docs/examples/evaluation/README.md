# 翻译评估样例

`zh-Hans.json` 包含两条通用界面译法和四条待审样例；`answers.json` 示范 `{case_id: translation}` 格式。所有内容均为模板示例，请替换为自己项目的评估用例。

```sh
npm run workflow -- evaluate --suite docs/examples/evaluation/zh-Hans.json --answers docs/examples/evaluation/answers.json --output .cache/evaluation.json
```

评估器只读取文件，分别报告格式错误、参考译文匹配和待审项。缺失答案或违反规则会返回非零退出码；措辞差异由人工判断。

扩充样例时在 `note` 中记录场景及判断依据。参考译文经人工审阅后设为 `review_status: confirmed`，其余使用 `pending`。
