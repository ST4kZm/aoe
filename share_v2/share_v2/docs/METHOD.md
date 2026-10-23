# v2 方法说明

## 全流程

```text
17项工序 + 工期 + 原表资源 + 机型条件/规程/已知依赖
  → 各工序执行条件、完成状态和未知事项 + timeline（同一次调用）
  → 一次性输出全部136对的工艺关系与资源顺序建议
  → 重复5次，分别统计工艺关系和资源方向的经验分布
  → 物理偏序MILP：最小化明确判断的违背，UNKNOWN弃权
  → 固定物理偏序，独立的资源定向MILP优化工期和资源方向
  → 传递约简，输出DAG及未解决关系；有UNKNOWN时仅为候选方案
  → 普通CPM前推/后推：ES、EF、LS、LF、总时差、关键路径
```

资源约束不是在CPM之后补救。CPM不进行资源排序；资源顺序必须已经被DAG编码。
程序会检查每对共享独占资源的工序是否在图中存在某个方向的可达路径，缺失时直接报错。

当前仅支持容量为1、每项需求为0或1、全工期占用的可再生独占资源。
这覆盖本例的一台吊车。多台同类资源需要资源分配/流量等额外建模，
不能简单对所有共享资源工序串行化。当前明确拒绝这类输入，不作隐式退化。
输入中的unit_pit也明确拒绝。工期仍按正整数时间单位、固定不可中断处理。

## 两层标签而非一个混合标签

每个工序对同时含有：

- physical_relation：BEFORE / AFTER / INDEPENDENT / UNKNOWN。
- resource_conflicts：根据输入资源表确定的冲突资源列表。
- resource_order：有冲突时为BEFORE / AFTER / UNDECIDED，无冲突时为NONE。
- basis、evidence：工艺判断的来源和说明，继续保留；不直接作为目标权重。

BEFORE的工艺含义仍是完成—开始。INDEPENDENT表示物理偏序互不可达的判断，
不是资源并行许可。UNKNOWN表示弃权，不能作为无依赖的支持票。
basis=unknown与physical_relation=UNKNOWN必须同时出现。
工艺边和资源边不是互斥类别，同一对可以同时有两种约束。
真实资源冲突由程序根据需求和容量计算，再放入全局提示词。
模型不能通过投票把一台吊车的互斥要求删除，只对顺序提出建议。

timeline是显式JSON对象，relation_schema=2；task_conditions必须为每项工序提供
task、preconditions（非空字符串数组）、postconditions（非空字符串数组）、
uncertainties（可空字符串数组）、basis。执行条件先于关系输出，用于解释判断，
不自动成为硬约束。stages必须覆盖全部节点一次；阶段描述应表达分支和汇合，
不是同步屏障。程序校验结构和覆盖，不保证自然语言timeline与关系在语义上完全一致；
最终结构一致性由MILP保证，实际物理正确性仍依赖输入条件和模型判断。

响应结构示例（完整响应需覆盖全部工序和136对）：

```json
{
  "timeline": {
    "relation_schema": 2,
    "task_conditions": [
      {"task": "P01", "preconditions": ["停机准备已完成（假设）"],
       "postconditions": ["已停机排水"], "uncertainties": ["具体规程缺失"], "basis": "hypothesis"}
    ],
    "summary": "整体工艺与资源使用概述",
    "stages": [
      {"id": "S1", "tasks": ["P01"], "description": "阶段与分支关系"}
    ]
  },
  "pairs": [
    {
      "source": "P03",
      "target": "P07",
      "physical_relation": "AFTER",
      "basis": "hypothesis",
      "evidence": "按所假设机型从上方吊出时的通道关系",
      "resource_conflicts": ["crane"],
      "resource_order": "AFTER"
    }
  ]
}
```

五次生成分别得到physical_probabilities、resource_probabilities。
这些是票数/次数，不是校准后的真实正确概率。全部原始响应与timeline保存在输出中。
无LLM的complete模式不伪造票数，概率字段为null。

## 分离的物理偏序与资源定向模型

物理模型不包含工期、开始时间或资源方向。每对工序有两个二元变量x_AB、x_BA：

- x_AB+x_BA≤1；都为0时表示物理偏序未选定先后，不能自动理解为独立。
- 先后传递：x_AB+x_BC−x_AC≤1。
- known_precedence为硬约束；complete模式的候选边也固定。其传递闭包必须保留。

设每对票数为b（BEFORE）、a（AFTER）、i（INDEPENDENT）、u（UNKNOWN）：

| 选择 | 违背代价 |
|---|---:|
| A→B | a+i |
| B→A | b+i |
| 不选先后 | b+a |

先最小化总代价；同分时最小化物理先后关系数量，避免凭空补边。
该次平局处理不使用工期或资源信息，也不增加高票锁边阈值。
UNKNOWN不贡献任何得分或惩罚。最大化明确标签支持与上述目标等价；
仍可能因全局一致性牺牲局部高票边，不保证高票永不被修改。

未选择方向时，只有INDEPENDENT票数严格高于BEFORE和AFTER票数才输出INDEPENDENT。
这里UNKNOWN作为弃权，不参与领先比较；明确标签票数并列或无独立性支持时输出UNKNOWN。
这是“模型支持的独立判断”，不是已由工程人员确认的事实。

随后冻结全部物理偏序，建立第二个MILP。共享资源工序每对选一个方向，
引入开始时间s和总工期C；固定物理边和资源边都必须满足完成—开始约束。
工期为正保证混合无环；M使用串行工期之和。先最小化C，再在相同工期下
最小化违背资源方向明确票数，UNDECIDED弃权。资源阶段不能修改物理图。
辅助时间最终丢弃，由普通CPM重算，并核对总工期一致。

若物理关系为UNKNOWN，资源方向仍可用于生成**候选排程**，但必须记录到
resource_order_assumptions，不能解释为物理可行的方向已经确定。所有未解决关系
写入unresolved_pairs；CPM实际重叠的未知对另列unresolved_overlaps。
即使资源排序碰巧消除了重叠，也不会自动消除物理未知状态。
无未知关系也不等于现场物理正确性已经验证，physical_validity_verified始终为false。
complete模式只确认输入的边，未指定先后的工序对仍未知，不伪造INDEPENDENT票数。

四个阶段为physical_disagreement、physical_sparsity、makespan_fixed_physics、
resource_disagreement。无工序对/无资源冲突时跳过无意义阶段。
时间上限按四份分配（跳过阶段不重新分配）；--time-limit 0取消上限。
每阶段要求证明最优，否则停止报错；这不改变API自身超时。

## 图与CPM输出

- edges：最终传递约简后的DAG。
- edge_records：每条保留边的physical / resource类型，可同时拥有两种类型。
- physical_edges：选中的完整工艺先后关系。
- resource_edges：全部资源冲突的确定方向，即使其对应直接边在约简中被移除。
- pairs：两层原始票数、概率和最终选择。
- construction_solver：各阶段状态、含常数项的目标值与界，及物理图已冻结标记。
- unresolved_pairs：物理先后与独立性尚未解决的工序对。
- resource_order_assumptions：对未知物理关系采用的候选资源方向。
- CPM的unresolved_overlaps：候选排程中实际重叠的未知对。
- 有未知时status为conditional_candidate_dag / conditional_candidate_schedule。
- CPM输出：最早/最晚开始完成时间、总时差、关键工序、其中一条关键路径。

CPM前推ES(j)=max EF(i)，后推LF(i)=min LS(j)，均沿DAG进行。
CPM核心复杂度为O(V+E)，外围还有资源可达性和容量验证。
它得到的是固定DAG的最早排程；构图阶段的最优性范围见上面的分级目标。

