# 企业内部多 Agent 业务协同系统

一个使用 FastAPI、SQLite、React 和 DeepSeek Harness 的企业业务演示项目。AI 助手可以把经营问题委派给销售、库存或采购分析子 Agent，通过 HTTP 工具查询模拟数据，保存分析结果并展示执行过程。

## 可以体验什么

- 在助手中发起经营或库存分析，在任务中心查看子 Agent、工具调用和最终报告。
- 根据库存缺口比较内部调拨与外部采购建议，人工修改采购草稿后提交审批。
- 采购需求先由销售主管初审、再由采购主管终审；申请人不得自批，同一人不能完成两级审批。驳回后重新提交会保留旧版本和审批意见。通过审批不会下单、付款或修改库存。

## 在全新的本地目录运行

需要 Python 3.11 或更高版本、支持 Vite 8 的 Node.js，以及与项目锁定的 Harness 运行时兼容的 DeepSeek Harness 源码（含 `python/sdk/src`）。以下为 PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
npm ci
.\.venv\Scripts\python scripts/configure.py
npm run web:build
$env:HARNESS_SOURCE = (Resolve-Path '..\deepseek-harness-master').Path
$env:AGENT_MODE = 'mock'
$env:SALES_API_URL = 'http://127.0.0.1:8180'
.\.venv\Scripts\python -m uvicorn enterprise.app:create_app --factory --host 127.0.0.1 --port 8180 --workers 1
```

按实际位置调整 Harness 源码路径。打开 **http://127.0.0.1:8180/**。普通模式首次启动会建立本项目自己的数据库，首次登录信息由程序在数据目录本地生成。请勿连接已有业务库。公开源码不附带用户、部门成员分配、私有审批记录或任何可直接登录的凭证；完整采购流程需要在自己的环境中配置相应业务身份。

默认示例使用确定性模型替身。若要使用真实模型，须在自己机器上配置模型凭证并显式选择 live 模式；调用可能产生费用。请勿将凭证写入源码或提交到 Git。

## 目录与验证

| 目录 | 用途 |
|---|---|
| enterprise/ | 接口、存储、账号权限、任务执行、采购审批 |
| plugins/、prompts/ | Harness 工具、子 Agent 注册与提示词 |
| frontend/ | React 管理页面与 Vite 配置 |
| scripts/configure.py | 按当前工作目录生成 Harness 配置 |
| tests/ | 可公开的销售汇总、工具和前端交互测试 |

安装依赖后可运行 `python -m pytest tests/test_business.py` 和 `node --test tests/test_plugin.mjs tests/test_frontend_interactions.mjs`。这些示例不代表完整的内部验收套件。

此仓库是独立的源码快照，不含原始 Git 历史、运行数据、备份、日志、内部测试报告或部署产物。
