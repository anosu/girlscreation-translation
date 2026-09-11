# GirlsCreaionR Translation

少女艺术绮谭的简中翻译，使用GalTransl + Deepseek-V4翻译，Github Actions自动化更新

插件请看[GCMod](https://github.com/anosu/GCMod)

### 本地运行

使用 Node.js 22.18+（22 LTS）或 24+，直接运行 TypeScript，无需构建：

```sh
npm ci
npm start
```

默认端口为 `12315`，可通过环境变量 `PORT` 修改。翻译文件通过
`/translations/` 访问，例如 `/translations/zh-CN/manifest.json`。
服务使用 Fastify 及官方 static、compress、cors 插件，支持压缩、跨域请求和条件缓存，文件更新后会重新验证缓存。

运行 `npm run typecheck` 检查类型，运行 `npm test` 验证 HTTP 接口。
