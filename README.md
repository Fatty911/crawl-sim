# crawl-sim — 四大运营商手机套餐资费爬虫

抓取中国联通 / 中国电信 / 中国广电 / 中国移动的北京及全国资费，经 Mihomo 轮换机场节点代理运行，
自动合并、过滤并发布到 GitHub Pages。

**在线站点：https://sim.jiucai.eu.org**

## 数据源

| 运营商 | 入口 | 抓取方式 |
|---|---|---|
| 中国联通 | `imgxx.client.10010.com/zifeizhuanquwt/` | 直接调用 `/queryTariffNew/*` API（POST form，无需登录） |
| 中国移动 | `h.app.coc.10086.cn/.../tariffZonePers.html` | 页面为 AES 加密 API，改用 Playwright 渲染 DOM |
| 中国电信 | `www.189.cn/tariffZone/` | 瑞数(Riversafe)反爬，需 `playwright==1.61.0` + 对应 chromium |
| 中国广电 | `m.10099.com.cn/costNotice/` | 直接调用 `/contact-web/api/goods/*` API（POST JSON） |

## 发布规则

- **排除**：充话费送手机 / 买手机必须用多少钱以上档位套餐的合约机套餐（`excluded_phone_contract`）；校园专属宽带（校园/高校/学生 限定，如"校园宽带""沃派校园专属"）仅限高校区域办理，不进入 Pages（`excluded_campus`）。
- **广电年付档位价**：产品名"一年…XX元档"（如 靓号宽带双享包48元档）以档位价/12 作为月租，源站 API 的 productPrice 对档位产品返回统一基础价不可靠。
- **默认展示**：
  - 套餐：通用非定向流量 `>= 20G` 且月租 `<= 69 元`；
  - 流量包：通用流量 `>= 10G`、费用 `<= 30 元`、每 GB `<= 1 元`（高性价比选装包）。
- **宽带套餐**（`is_broadband`，独立「宽带套餐」视图）：带 **带宽大小**（`broadband_mbps`，从 `broadband` 字段/套餐名/资费内容提取，移网峰值速率不计入）与 **接入方式**（`access_method`：光纤 / FTTR / 同轴(HFC) / 无线(FWA) / ADSL），前端独立筛选（带宽下限/接入方式）与排序。
- 详情见 `config/filter_conditions.json` 与 `scripts/merge_data.py`。

## 工作流

| 工作流 | 触发 | 作用 |
|---|---|---|
| Crawl Unicom / Broadnet / Mobile / Telecom | 定时(每日 2 次) + 手动 | 经 Mihomo 代理抓取，产出 artifact |
| Merge and Filter | 爬虫完成后 | 合并 4 源、去重、应用发布规则、打 Release |
| Deploy Pages | Release 后 | 生成静态站点并部署 GitHub Pages |
| CI | push/PR | py_compile + workflow 语法校验 + 冒烟测试 |
| AI Repair Sim | 手动 + Watchdog | 自发现自修复（OpenCode 生成 patch → 校验 → 评审 → 合入） |
| Self-Repair Watchdog | 每 30 分钟 | 检测爬虫失败并触发有界 AI 修复 |

## 本地运行

```bash
pip install -r requirements.txt
python scripts/crawl_unicom.py --output data/unicom.json
python scripts/crawl_broadnet.py --output data/broadnet.json
# 电信/移动需 playwright==1.61.0 及对应 chromium
python scripts/crawl_mobile.py --output data/mobile.json
python scripts/crawl_telecom.py --output data/telecom.json
python scripts/merge_data.py --data-dir data --output data/merged.json
```

## 仓库 Secrets

- `PROXY_SUBSCRIPTIONS`：机场订阅（Mihomo 轮换节点）——必须
- `ACTION_PAT`：推送/触发工作流用 PAT（默认分支直接推送）
- AI 修复：`ZENMUX_API_KEY`（生成）、`NVIDIA_NIM_API_KEY`（评审）、`DMIT_PROXY_URL`（可选代理）
