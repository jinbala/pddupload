# 17zwd Multi-Platform Listing Automation

目标：从 17zwd 的商品和铺货记录出发，将选中的商品分别铺货到拼多多和抖音。

当前推荐路线是使用 Playwright 驱动已经存在于 17zwd 中的
`一键上传 -> 超级店长 -> 平台店铺` 链路。项目负责采集、去重、任务编排、
重试、日志和对账，不在第一版中重写拼多多或抖音的私有上货接口。

## 环境搭建（首次使用 / 换新电脑）

前置：安装 Python 3.11 或更高版本（安装时勾选 “Add Python to PATH”）。

双击运行「安装环境.bat」，它会自动：

1. 创建虚拟环境 `.venv`
2. 安装依赖（playwright、pydantic）
3. 下载 Playwright 的 chromium 浏览器（约 150MB，需联网）

之后双击「一键启动.bat」即可使用。

### 后台运行（不弹浏览器窗口）

项目默认已在 `.env` 里打开后台无头模式（`PDD_AUTOMATION_HEADLESS=true`），
日常上架全程在后台跑，不会弹出浏览器窗口抢前台焦点。

- 想回到可见窗口：把 `.env` 里的 `PDD_AUTOMATION_HEADLESS` 改成 `false`，或删掉该行。
- 需要重新登录 / 扫码（登录态过期、换店铺授权）时：双击「登录账号.bat」，
  它会用可见窗口打开 17zwd 让你登录，登录态保存后回到后台模式即可。

> 登录态不随代码迁移：换新电脑后，首次运行需要双击「登录账号.bat」在浏览器里重新登录 17zwd，
> 并确认超级店长的店铺授权可用。店铺名通过 `.env` 或菜单现场输入配置（见 `.env.example`）。

## Confirmed Facts

- 上传记录页：`https://i.17zwd.com/user/uploadRecord`
- 上传记录页是查询和对账页面，不是新商品发布页面。
- 真正的上传入口在商品卡片或商品详情页的“一键上传”。
- 拼多多当前有 206 条铺货记录，另有 4 条货源下架记录。
- 抖音当前有 0 条铺货记录。
- 超级店长已绑定多只拼多多店铺，其中部分店铺授权已过期。
- 当前没有看到已绑定的抖音店铺。
- 拼多多和抖音的上传应用均为“超级店长”。
- 两个平台的默认上传方式均为“编辑上传”。

## Recommended Scope

第一版只完成一个可审计的闭环：

1. 复用已登录浏览器会话。
2. 从 17zwd 新品商品详情读取商品信息。
3. 自动点击一键上传并选择拼多多或抖音。
4. 自动确认超级店长应用。
5. 停在“选择上货店铺”窗口，由人工确认店铺和模板。
6. 人工点击开始上传，观察编辑上传或直接上传结果。
7. 记录结果、截图和失败原因。

抖音店铺完成绑定后，才进入抖音链路的实机验证。

## Semi-Automatic Run

首次使用：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app init-db
```

准备一次拼多多上传：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app prepare-upload `
  --item-id 167086511 `
  --platform pinduoduo `
  --hold-open-seconds 300
```

脚本会停在“选择上货店铺”。此时人工选择店铺和模板，再点击“开始上传”。
当前命令不会自动点击最终的上传按钮。

自动选择指定店铺，但仍停在最终提交前：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app prepare-upload `
  --item-id 167086511 `
  --platform pinduoduo `
  --shop-name "你的店铺名" `
  --hold-open-seconds 300
```

明确确认后执行最终发布：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app prepare-upload `
  --item-id 167086511 `
  --platform pinduoduo `
  --shop-name "你的店铺名" `
  --confirm-publish PUBLISH `
  --hold-open-seconds 300
```

`--confirm-publish PUBLISH` 会真实点击“开始上传”。使用前必须核对商品、
店铺、模板和授权状态。

准备一次抖音上传：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app prepare-upload `
  --item-id 167086511 `
  --platform douyin `
  --hold-open-seconds 300
```

执行抖音前，需要先在 17zwd 的“我的网店”中绑定至少一只可用抖店。

## 从关注档口上架

从关注档口页 `https://i.17zwd.com/user/favouriteShops` 采集上新款，再逐款一键上传。

先扫描看货（不上架）：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shops --platform pinduoduo
```

按商品 ID 上架（逗号分隔多 ID；默认停在“选择上货店铺”，不点开始上传）：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shops `
  --platform pinduoduo `
  --item-ids 167526371,167531624 `
  --target-shop "你的店铺名" `
  --hold-open-seconds 300
```

按上新日期倒序上最新 N 款：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shops `
  --platform pinduoduo `
  --limit 5 `
  --target-shop "你的店铺名"
```

明确确认后真实发布（会点击“开始上传”）：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shops `
  --platform pinduoduo `
  --item-ids 167526371 `
  --target-shop "你的店铺名" `
  --confirm-publish PUBLISH
```

说明：

- 商品 ID 按市场站点隔离（池尾/潮汕站为 `cs.17zwd.com`，广州站为 `gz.17zwd.com`），
  命令直接从关注档口抓取完整 URL，自动适配站点。
- 上架前会自动对照铺货记录去重：已上过的商品会标记「⚠已上过」并自动跳过，不会重复上架。
  铺货记录库用 `scan-records` 刷新；即使本地库不是最新，超级店长层的“重复铺货确认”也会兜底拦截。
- 不带 `--item-ids` / `--limit` 时只扫描并导出 CSV/JSON，不上架。
- 完整上架是两步：`开始上传 → 确认上货`。不带 `--confirm-publish` 时停在“选择上货店铺”；
  带 `--confirm-publish PUBLISH` 才会点“开始上传”并“确认上货”完成上架。
- `--target-shop` 必须与弹窗里的店铺名完全一致（不同平台的店铺名可能不同，按需配置）。
- `--pages 2` 可扫描关注档口更多页。

## 从档口上架（含老品）

档口「在售商品」含全部老品，用 `page=N` 分页，URL 形如
`https://cs.17zwd.com/shop/527795.htm?spm=...&page=1&search=y`。把档口商品页的这个链接传给脚本即可。

先扫描看货（每页约 140 款，`--pages` 控制扫几页）：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shop `
  --shop-url "https://cs.17zwd.com/shop/527795.htm?spm=0.42.132.0.0.0&page=1&search=y" `
  --pages 2
```

上架指定老品（逗号分隔 ID；真实发布需加 `--confirm-publish PUBLISH`）：

```powershell
.\.venv\Scripts\python -m pdd_listing_automation.app list-from-shop `
  --shop-url "https://cs.17zwd.com/shop/527795.htm?spm=0.42.132.0.0.0&page=1&search=y" `
  --item-ids 167497323,167497291 `
  --target-shop "你的店铺名" `
  --confirm-publish PUBLISH
```

说明：

- `--shop-url` 是档口「在售商品」链接（带 `page=1&search=y`），脚本自动翻页。
- 老品标题/价格/上新日期从商品卡片解析；同样支持去重（已上过自动标记「⚠已上过」并跳过）。
- 其它参数（`--item-ids` / `--limit` / `--target-shop` / `--confirm-publish`）与 `list-from-shops` 一致。
- 单件老品也可用 `prepare-upload --item-id "https://cs.17zwd.com/item/xxxxx"` 直接贴链接上架。

## Documents

- [自动化技术设计](docs/automation-design.md)
