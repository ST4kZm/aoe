# JSON → AOE 风格活动网络图与资源约束调度

## 1. 功能

- 从 JSON 读取工序、工期、成本、前置依赖和资源需求。
- 按依赖关系进行拓扑排序，**不把工序 ID 当作先后顺序**。
- 满足依赖与资源容量，允许可以并行的工序同时开始。
- 成本 `cost` 不参与排序或资源调度，只显示在 AOE 活动边标签中。
- 输出 AOE 风格活动网络图、SVG 矢量图、甘特图、调度 JSON 和拓扑序文本。
- 对依赖环、未知工序 ID、重复 ID、资源容量不足等问题进行检查。

## 2. 输入 JSON 约定

```json
{
  "name": "demo",
  "time_unit": "day",
  "cost_unit": "unit",
  "resource_capacity": {"crane": 1},
  "precon": {
    "task-B": ["task-A"],
    "task-C": ["task-A", "task-B"]
  },
  "tasks": [
    {"id": "task-A", "name": "A", "duration": 2, "cost": 1, "resources": {}},
    {"id": "task-B", "name": "B", "duration": 3, "cost": 2, "resources": {"crane": 1}},
    {"id": "task-C", "name": "C", "duration": 1, "cost": 0.5, "resources": {}}
  ]
}
```

`precon` 的含义：`"task-B": ["task-A"]` 表示 **task-B 依赖 task-A**，即 task-A 完成后 task-B 才能开始。前置关系来自 `precon`，不是 ID 大小、名称或 `tasks` 数组中的排列顺序。也支持每个 task 对象中单独设置 `precon` 列表，二者会合并。

`resource_capacity` 是资源总容量；`tasks[].resources` 是该工序的资源需求。比如 `crane` 容量为 1 且吊车工序需求为 1 时，这些工序不能重叠执行。若工序使用的资源没有在 `resource_capacity` 声明，程序会发出警告，并将该资源容量默认为所有工序需求之和（即不人为限制其并行度）；建议正式输入中显式声明资源容量。

## 3. 安装

```bash
python -m pip install networkx matplotlib numpy pydot
```

为了得到排版较好的 AOE PNG/SVG，还建议安装 Graphviz，并确保 `dot` 命令可执行。没有 Graphviz 时程序会退回 matplotlib 布局，但复杂图可能不如 Graphviz 排版清晰。

## 4. 运行

```bash
python aoe_scheduler.py tasktest.json --outdir aoe_output
```

输出文件：

- `aoe_graph.png`：AOE 风格活动网络 PNG。
- `aoe_graph.svg`：可放大查看的矢量图。
- `aoe_graph.dot`：Graphviz DOT 源文件，可继续调整布局。
- `schedule_gantt.png`：工序并行/资源占用甘特图。
- `schedule.json`：项目工期和每道工序的开始、结束、资源、前置工序等。
- `topological_order.txt`：按图依赖计算的一个合法拓扑序。

## 5. 图的含义与算法边界

- 图的初始事件节点标记 `E0, t=0`。
- 每个工序使用独立的“开始事件”和“完成事件”节点；节点标签显示计划时间。即使两个事件时间相同，也不自动合并，以免把无关事件误认为同一事件。
- 蓝色实线表示真实工序活动边，标签中包含工序名、ID、`duration`、`cost` 和资源需求。
- 灰色虚线表示前置依赖、容量为 1 的资源串行顺序或等待间隔。
- 调度是**非抢占式资源约束列表调度启发式算法**：在当前时刻启动所有符合前置和资源条件的工序，优先启动剩余依赖路径工期较长的工序，工期优先级相同则按工序名称打破并列。成本不会影响排程。
- 该方法保证输出排程满足代码所建模的前置关系和资源容量，但不保证全局最短工期。一般的资源约束项目调度问题（RCPSP）是组合优化问题；需要严格最优结果时，应改用 CP-SAT / MILP 等优化求解器。
- 资源容量大于 1 时，调度器会按实际容量检查并发使用；图中会显式画出容量为 1 且需求为 1 的资源串行边。其他容量的具体使用量可查看任务边标签和调度 JSON。
- 输入里没有定义的人员、空间、班次、作业面、准备时间等约束不会被程序自行猜测。
