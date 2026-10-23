# 检修工序全局 DAG + CPM

这是基于《Beyond Pairwise: Global Zero-shot Temporal Graph Generation》思路的工程扩展

## 目录

- `scripts/maintenance/`：v2 构图、CPM、可视化及 25 项离线测试。
- `scripts/utils/llms_definitions.py`：真实生成使用的 API 客户端。
- `examples/maintenance/tasks_live.json`：v2 原始输入，17 项工序、1 台吊车。
- `examples/maintenance/tasks.json`、`candidate_graph.json`：构图回归测试夹具，不是 v2 的五轮生成输入。
- `reference/v2/`：2026-10-12 原始 v2 图、排程、图片及五份完整响应；manifest 保留当时模型和接口地址作为来源记录，不含密钥。
- `docs/ENGINEERING_CHANGES.md`：相较原论文代码的工程改动与本次裁剪说明。
- `docs/METHOD.md`：标签、优化目标、约束和结果字段。
- `docs/VALIDATION.md`：本次验证结果。
- `SHA256SUMS`：分享文件完整性校验。

## 安装

建议 Python 3.12（本次验证为 3.12.3）；原项目要求 Python 3.10。锁定版本来自本次验证环境，并非已追溯的历史运行环境。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

默认使用 SciPy/HiGHS，不需要 Gurobi 许可。Gurobi、Gemini、Together 是可选路径，不在本包验证范围内，使用时另装对应依赖。中文图形建议安装 Noto Sans CJK SC 字体；无中文字体可能出现方框。

## 离线复现（推荐，不调用 API）

在本包根目录执行：

```bash
python -m scripts.maintenance.pipeline aggregate \
  --data examples/maintenance/tasks_live.json \
  --responses reference/v2/graph.runs/run_1.json reference/v2/graph.runs/run_2.json reference/v2/graph.runs/run_3.json reference/v2/graph.runs/run_4.json reference/v2/graph.runs/run_5.json \
  --backend scipy --time-limit 0 --output output/reproduced/graph.json
python -m scripts.maintenance.pipeline schedule \
  --data examples/maintenance/tasks_live.json \
  --graph output/reproduced/graph.json --output output/reproduced/schedule.json
python -m scripts.maintenance.visualize \
  --graph output/reproduced/graph.json --schedule output/reproduced/schedule.json \
  --output-dir output/reproduced
python -m unittest scripts.maintenance.test_resource_dag scripts.maintenance.test_inference -v
```

参考结果：串行 40 天 → 候选排程 23 天，减少 17 天（42.5%）；固定成本 28.5（原表未注明单位）。仍有 2 对物理关系未解决，其中 P14/P15 在排程中重叠；结果为 conditional_candidate_schedule，未经现场工艺验证。不同求解器版本可能在同分解中选取不同边，不应只按图形外观判断复现成功。

## 重新调用模型生成

密钥通过环境变量提供，程序不会自动读取 `.env`。下列为本地终端配置示例；填入自己有权使用的模型和接口。

```bash
export OPENAI_API_KEY='填写自己的密钥'
export MODEL_NAME='填写可用模型名'
# 使用兼容服务时另设 OPENAI_BASE_URL 为其接口地址。
python -m scripts.maintenance.pipeline infer \
  --data examples/maintenance/tasks_live.json --api gpt --model "$MODEL_NAME" \
  --repeat 5 --backend scipy --time-limit 0 --output output/new_run/graph.json
python -m scripts.maintenance.pipeline schedule \
  --data examples/maintenance/tasks_live.json \
  --graph output/new_run/graph.json --output output/new_run/schedule.json
```

中断后对同一路径的 infer 命令追加 `--resume`；已完成轮次会跳过，失败轮次重新请求。新实验使用新目录。`reference/v2` 只用于参照，不要覆盖。在线重新生成有随机性，模型服务也可能变化，不保证得到原来 23 天的结果。manifest 中的历史模型名仅是服务端报告的标识，不代表其底层模型身份已验证。
