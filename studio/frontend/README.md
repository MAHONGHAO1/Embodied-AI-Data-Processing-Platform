# 前端控制台

前端是 Vue 3 + Element Plus 的静态控制台，由 FastAPI 同源托管，不需要 Node 构建流程。所有 API 调用统一通过 `frontend/js/api.js`；页面不接收 OSS 凭据，也不直接提交不受信任的存储 URI。

Access Token 只保存在页面内存并通过 Bearer Header 发送。跨标签页恢复依赖后端签发的 HttpOnly 浏览器会话 Cookie，前端不在 `localStorage` 或 `sessionStorage` 保存认证令牌和用户信息。`localStorage` 只保存语言等非敏感界面偏好。

前端权限判断只用于隐藏不可用操作，真实认证、工作空间范围和对象级授权始终由后端执行。

运行测试：

```bash
node --test frontend/tests/*.test.mjs
```
