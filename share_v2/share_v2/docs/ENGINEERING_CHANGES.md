# 相较原论文代码的工程适配

## 对照依据

原仓库：https://github.com/AlonEirew/GlobalZeroShotTRE

2026-10-19 读取公开 main 分支并逐文件核对：本地 `scripts/run/run_zsl_tre.py`、`scripts/eval/shared/gurobi_optimizer_entrop.py` 与远端相同；`scripts/utils/llms_definitions.py` 存在改动，逐行差异见 `llms_client.diff`。本地没有 .git 历史，远端提交查询受限，因此不能据此声称与最初下载时的某个 commit 完全一致。本报告将已核对的共享代码变化与新增工程模块分开说明。

## 方法与任务的变化

| 项目 | 原论文代码路线 | v2 工程实现 |
|---|---|---|
| 输入和目的 | 文本中的事件与时间关系，使用 TRE 数据集和评价标签 | 17 项检修工序、整数工期、固定成本、资源表及机型假设，输出候选执行 DAG 与排程 |
| 提示词 | 事件 timeline / 全局关系预测 | 同次调用先输出每项工序的执行条件、完成状态、未知事项和 timeline，再输出全部 136 对关系；新增 basis/evidence |
| 标签语义 | 数据集对应的时间关系标签及其组合约束 | 工艺层 BEFORE/AFTER/INDEPENDENT/UNKNOWN；BEFORE 明确定义为完成—开始。资源顺序为独立字段，不把潜在并行解释为 EQUAL |
| 多次生成 | 重复生成与全局一致性优化路线 | 本实验固定保存五轮；分别统计工艺和资源方向经验分布，UNKNOWN/UNDECIDED 弃权，不把频率称为校准概率 |
| 全局优化 | 类别选择、对称与传递约束，最大化预测概率支持 | 首先最小化工艺明确判断的违背，再最小化物理先后关系数量；冻结物理图后最小化工期，再最小化资源方向违背，共四个词典序阶段 |
| 资源 | TRE 实验不使用检修资源容量 | 冲突由输入资源表确定，模型仅建议顺序；容量 1、需求 0/1、全工期占用，明确拒绝机坑和多容量输入 |
| 排程 | 时间关系抽取评价 | 新增传递约简、普通 CPM 前推/后推、时差、关键路径、甘特图；CPM 前检查资源冲突是否已由 DAG 排序 |
| 不确定性 | 使用原数据集的标签与评价流程 | unresolved_pairs、unresolved_overlaps、resource_order_assumptions；未知关系不自动转成独立，物理有效性始终未现场验证 |

这包含方法建模上的扩展，不能全部称为“运行环境适配”，也不能将 23 天结果作为原论文性能复现。原论文完整的多类别时间关系组合规则没有直接移植到检修偏序模型。

## 运行可靠性与依赖适配

- `scripts/utils/llms_definitions.py`：Gemini/Together 改为选择提供商时才导入；GPT 增加 stream、max_retries、on_chunk 参数；流式响应必须以 stop 完整结束，否则报错；关闭响应连接。
- `scripts/maintenance/pipeline.py`：真实推理启用流式接收、禁用 SDK 自动重试，落盘原始完整响应和中断片段；manifest 绑定提示词哈希、模型、接口；支持显式断点续跑，避免把残缺输出当成成功轮次。
- `scripts/maintenance/resource_dag.py`：严格校验 schema、工序覆盖、136 对覆盖与输入资源冲突；仅在存在唯一完整末尾 JSON 时恢复响应，并保留审计记录，不拼凑缺失预测。
- `BinaryMILP`：新增 SciPy/HiGHS 后端及 Gurobi 适配；各阶段必须证明最优，否则停止，time-limit=0 可取消求解时间限制。
- `visualize.py` 与测试：增加 DAG/甘特图输出及工艺冻结、资源互斥、未知语义、目标函数、流式中断和续跑的验证。

这些工程模块是新增实现；v2 构图并没有直接调用原论文的 `GurobiOptimizer` 类。

## 本次分享包的裁剪（与上述 v2 改动区分）

1. 移除包内原论文数据集入口、评价链路、tokenizer、GPU 扫描脚本和论文 PDF，以来源链接代替。
2. 不收录旧 live 实验、旧保守资源方案、旧图和中断 partial 日志。保留五份完整文本、五份解析 JSON、manifest、参考图与排程。
3. 删除分享包 `pipeline.py` 中未被 v2 调用的 legacy_* 函数及旧版测试；将当前测试需要的 dataset 工厂移到 test_fixtures.py。保留两份离线回归夹具，因此测试仍可独立运行。
4. 分享包 CLI 默认输入改为 tasks_live.json、默认求解器改为 scipy；未改动 v2 核心 resource_dag.py、输入和原始响应。
5. 新增锁定依赖、复现说明、方法说明、校验清单和验证报告；不包含密钥、缓存或本次重算的重复结果。
