# Castore XL 库存监控（Bark + GitHub Actions）

自动每 15 分钟检测 Castore 官网商品 **Oracle Red Bull Racing Unisex Motor Jacket - Egret/Blue** 的 **XL** 码库存，有货时通过 Bark 推送到 iPhone。

## 监控逻辑

1. **优先请求 Shopify 数据接口**：访问 `https://castore.com/products/oracle-red-bull-racing-unisex-motor-jacket-egret-blue.js`（携带常见桌面浏览器 `User-Agent`），精确匹配 `variants` 中 `title` / `option1` / `option2` 等于 `"XL"` 的变体（严格排除 `2XL` / `3XL` / `4XL`），检查 `available == true`。
2. **Playwright 自动降级**：若接口返回 `403`、超时或被 Cloudflare 拦截，自动切换为 Playwright（无头 Chromium）打开商品页并检查尺码选择区中 `XL` 按钮是否可点击/有货。
3. **Bark 推送（GET）**：仅在检测到 XL 码有货时推送；无货时仅打印时间戳和 `XL 码当前无货` 并以 `exit 0` 正常退出。

## 本地运行

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# 正常检测库存（无货时不推送）
python monitor.py

# 发送测试推送（验证 Bark 通道）
$env:BARK_URL = "https://api.day.app/你的BarkKey"
python monitor.py --test
```

## GitHub Actions 部署

1. 将本项目推送到你的 GitHub 仓库。
2. 在仓库页面进入 **Settings → Secrets and variables → Actions → New repository secret**：
   - Name: `BARK_URL`
   - Secret: 你的 Bark 推送前缀（不带末尾斜杠），例如 `https://api.day.app/你的BarkKey`
3. 进入 **Actions → Castore XL Stock Monitor**：
   - 定时任务会按 `cron: '*/15 * * * *'` 每 15 分钟自动执行一次。
   - 也可以点击 **Run workflow** 手动触发；勾选 `test_mode` 即可直接发送一条测试推送验证通道。
