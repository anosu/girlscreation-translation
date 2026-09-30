# 当前工作流架构

适配器只收集真实原文并输出公共资源格式；框架负责快照、待译项、校验和发布；Codex Agent 自行阅读、翻译和审校。接入新游戏先看[资源契约](adapters.md)与 collect 接口。

## 数据流

`collect → sync 保存不可变资源 → plan 找出缺失字典键 → 每个目标语言的 Agent 按需阅读并提交 → 校验 → publish 原文到译文字典`

sync 由适配器决定获取范围，不替游戏检查远端是否更新。运行中的 plan 绑定不可变快照；新的 sync 不修改已有计划依据。plan 不调用模型。

## 核心文件

| 模块 | 职责 |
| --- | --- |
| resources.py、adapters.py | 公共资源格式和游戏提取接口 |
| snapshot.py、prepare.py | 不可变原文与缺失字典键计划 |
| session.py、agent.py | 按资源提交译文、答案持久化与校验 |
| translate.py、codex.py | 为每个目标语言启动 Agent 并保护输入 |
| glossary.py、validate.py | 现有标准译名与格式规则 |
| merge.py、build.py | 输出路径合并、预检和 manifest |
| cli.py、ci.py | 本地和 CI 入口 |

## Agent 接口

工作目录中的 plan.json 列出待译 tasks；每个 task 的 references 是资源 ID，resources 将资源 ID 映射到原文快照路径；runtime.json 给出快照根目录。Agent 用自己的文件和搜索工具阅读完整剧情、已有译文、names 与需要的其他上下文。框架不截断资源正文，不管理 Agent 上下文，也不预加载原文。

Agent 通过 workflow.agent submit 提交 JSON：顶层为资源 ID，内层为该资源的精确原文到译文映射。一次可以提交多个资源或部分键；相同输出字典位置的重复原文归为一个待译项，跨表或跨输出文件的同文仍可有不同译法。再次提交同一原文可修订。每次提交先校验，再原子更新工作目录的单个 answers.json。status 显示剩余数量，所有待译项完成后由 translate 做快照和全局校验并写 results.json。Agent 不直接改发布文件。

已有 names 等标准译名来自输出字典；明确配置的 required_terms 必须遵守。普通剧情台词与标题按语境翻译，不因为与角色名同文就强制套译。标签、占位符与其他格式规则由确定性校验守住；语义质量仍需要 Agent 审校。

## 发布与限制

publish 独立于 Agent，对所有目标先预检，再合并原文:译文字典。它保留人工修改冲突检查、原子文件替换和 manifest 更新；不承诺多文件事务或并发写同一目标。--limit 仅选择本次待译资源，已完成资源可保留为上下文；共享输出文件不会被拆开。框架不保证快照覆盖全游戏或远端最新。
