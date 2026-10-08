# 前端 Vendor 资源

浏览器依赖由 QuicData 自身提供。包版本、资源路径和 SHA-256 固定在 `manifest.json`，避免运行时依赖公共 CDN。

仅从显式配置的国内 npm registry 更新：

```bash
cp deploy/mirrors.cn.env.example deploy/mirrors.cn.env
make vendor-frontend
```

正常命令会验证已固定的哈希。维护者升级依赖时必须显式运行：

```bash
NPM_REGISTRY_URL=... bash scripts/vendor-frontend.sh --update-hashes
```

更新后同时审查并提交 manifest 与生成资源，不接受未固定版本或哈希的浏览器依赖。
