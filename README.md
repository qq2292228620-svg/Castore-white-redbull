# Castore XL 库存监控（Bark + GitHub Actions）

自动每 15 分钟检测 Castore 官网商品 **Oracle Red Bull Racing Unisex Motor Jacket - Egret/Blue** 的 **XL** 码库存，有货时通过 Bark 推送到 iPhone。

## 为什么需要转发通道

`castore.com` 整站受 Cloudflare **Managed Challenge** 保护——不只是商品页，连 `robots.txt`、`/products.json` 都返回 `403 cf-mitigated: challenge`。

关键实测结论：

- 数据中心 IP（GitHub Actions / Azure 段）**100% 命中挑战**，这是 IP 信誉判定，不是脚本写得像不像人。
- 用 `curl_cffi` 伪装完整 Chrome 124 / 131 / Safari 17 的 TLS 指纹，**依然 403**。所以「加 User-Agent」「上 stealth 插件」这类做法无效，Playwright 无头浏览器在 Actions 里同样被拦。
- 结论：没有「破解」这条路，只能换出口 IP，或换取数通道。

## 监控逻辑（三级链路，任一成功即返回）

1. **直连 Shopify 数据接口**：`https://castore.com/products/oracle-red-bull-racing-unisex-motor-jacket-egret-blue.js`。本地运行或配置了 `SHOPIFY_PROXY` 住宅代理时走这条，最快（~0.3s）且零成本。
2. **r.jina.ai 转发通道**：由转发服务从它自己的出口取数，绕开本机 IP 信誉问题。实测约 **2.5 秒**返回原始商品 JSON，是 Actions 环境下的主力通道。
   - 带毫秒时间戳 cache-buster + `X-Cache-Tolerance: 0`，确保读到的是实时库存而不是缓存（库存监控漏掉一次补货就白做了）。
   - 转发出口**偶尔也会被临时拦截**（实测约 1/8 概率，返回的是人机验证页但 `httpStatus` 仍报 200），因此内置 **3 次重试 + 退避**，并对验证页特征词做识别。
3. **Playwright（无头 Chromium）**：最后兜底，检查尺码选择区中 `XL` 按钮是否可点击。Actions 环境下通常也会被拦，默认不安装以节省 ~90 秒/次运行时间。

尺码匹配逻辑：精确匹配 `variants` 中 `title` / `option1` / `option2` 等于 `"XL"` 的变体，**严格排除 `2XL` / `3XL` / `4XL`**（该商品 4XL 长期有货，绝不能误报）。

4. **Bark 推送（GET）**：仅在检测到 XL 码有货时推送；无货时仅打印时间戳和 `XL 码当前无货` 并以 `exit 0` 正常退出。

## 本地运行

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 正常检测库存（无货时不推送）
python monitor.py

# 发送测试推送（验证 Bark 通道）
$env:BARK_URL = "https://api.day.app/你的BarkKey"
python monitor.py --test

# 跑单元测试
python -m pytest -q
```

本地运行会优先走直连通道，通常 0.3 秒出结果；若本地也被拦，会自动降级到转发通道。

## GitHub Actions 部署

1. 在仓库页面进入 **Settings → Secrets and variables → Actions → New repository secret**：

   | 名称 | 必填 | 说明 |
   |---|---|---|
   | `BARK_URL` | 是 | Bark 推送前缀（不带末尾斜杠），例如 `https://api.day.app/你的BarkKey` |
   | `JINA_API_KEY` | 否 | 转发通道的 API Key，免费额度约 20 次/分钟；不配也能跑（15 分钟一次远低于限额） |
   | `SHOPIFY_PROXY` | 否 | 住宅/静态代理，如 `http://user:pass@host:port`；配了之后可恢复直连通道，把耗时从 2.5s 降到 0.3s |

2. 进入 **Actions → Castore XL Stock Monitor**：
   - 定时任务按 `cron: '*/15 * * * *'` 每 15 分钟执行一次。
   - 也可以点击 **Run workflow** 手动触发；勾选 `test_mode` 即可发送一条测试推送验证通道。

3. 如需启用 Playwright 兜底，在 **Settings → Secrets and variables → Actions → Variables** 新增仓库变量 `ENABLE_PLAYWRIGHT` = `true`，工作流会自动安装 Chromium。

## 排查

| 日志 | 原因 | 处理 |
|---|---|---|
| `转发通道被限流（HTTP 429）` | 免费额度 20 次/分钟被超 | 配 `JINA_API_KEY`，或降低运行频率 |
| `转发出口被 Cloudflare 临时拦截` | 转发服务出口被临时判定为风险 | 已自动重试 3 次；持续出现可换 `SHOPIFY_PROXY` 走直连 |
| `全部通道均检测失败` | 三条链路同时失效 | 先手动 `python monitor.py` 复现，看哪一层报错 |
| `转发通道商品 JSON 解析失败` | 上游返回结构变化 | 打 `print` 看 `content` 原文 |
