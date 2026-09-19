# 接入游戏

本仓库默认使用 girlscreation/ 中的 collect 实现。fetch.py、crypto.py、parse.py 保留游戏的 CDN、加解密和脚本格式约定；字段配置和使用方式见 [game/](../game/README.md)。

已有本地提取程序时，导出资源数组并配置 adapter = "json"、adapter.input = "sources/resources.json" 即可。VN 用 dialogue 的有序 lines，其他内容用 text 的 blocks；不需要游戏先有标题、摘要或完整数据表。

需要获取、解密或直接解析资源时，新建 adapters/my_game.py，配置 project.adapter = "adapters.my_game"，只实现 collect(request) 并返回 Resource 实例。格式、参数和失败约定见[契约](../docs/adapters.md)，可参考 json_file.py。

原文获取和成本策略留在适配器，不调用模型或写入译文。游戏专属依赖放入 pyproject.toml，专属参数放入项目的 adapter 配置。
