# 编译链与后端

## 编译边界

编译器接收已经执行完成的 Python 对象图，不编译任意 Python 源码。输入包含
器件实例、参数值、类型化端口、节点、局部 relation 和层次关系；输出包含
SPICE 网表、必要的 Verilog-A 模型和 manifest。

当前唯一可执行目标是 `Spice(simulator="ngspice")`。OpenVAF-reloaded 将生成的
Verilog-A 编译为 OSDI，ngspice 负责 MNA、Newton 迭代、`ddt` 和时间积分。

## 当前流水线

```text
Python object graph
-> choose native hierarchy or flatten
-> validate topology, dimensions and local relation budget
-> select explicit SPICE metadata or exact relation matcher
-> lower remaining supported real relations to Verilog-A contributions
-> emit thermal lumped elements
-> publish SPICE, Verilog-A and manifest
-> OpenVAF-reloaded -> OSDI
-> ngspice operating point or transient
```

每一步只能保留或拒绝语义，不能静默添加近似。

## 层次与展平

编译器有两条互斥路径：

| 条件 | 处理 | 产物 |
| --- | --- | --- |
| 图中存在复合器件，且全图是可精确匹配的原生电 SPICE 器件 | 保留层次 | `main.cir` 和 `subckt/*.cir` |
| 含 Verilog-A relation、热节点或其他非原生元素 | 展平 | 单一主网表和共享 `verilog_a/*.va` |

层次路径为每个复合实例生成私有 `.subckt` 名称，使不同参数实例仍可审计。
当前它不混合 Verilog-A 或热网络；一旦不满足条件，整图转入 flat 路径。
层次 `.include` 的绝对路径当前不能含空白字符，否则编译会明确拒绝。

## 后端选择

### 显式原生元数据

标准库器件用受控元数据声明 SPICE 原语。当前支持：

```text
R C L V I E G D Q
```

元数据固定原语、端口顺序、值参数和可用 model card 参数。它是标准器件的
精确快速路径，不是假装从任意 relation 推导出的结论。

### relation 精确匹配

没有元数据时，编译器按表达式结构匹配当前可证明等价的 `R/C/L/V/I/E/G`。
匹配失败不代表模型错误，只表示不能使用该原生优化；随后尝试 Verilog-A。
manifest 的 `selection` 区分 `explicit_metadata`、`relation_match` 和
`relation_fallback`。

### Verilog-A contribution

当前 fallback 不是一般方程求解器，只接受可定向成显式 contribution 的实数关系：

```text
V(p,n) <+ expression
I(p,n) <+ expression
I(thermal_node) <+ -injected_power
```

表达式支持参数、常数、端口/热节点电势、算术、`exp`、`abs` 和 `ddt`。
`delay`、`piecewise`、复数光场、重复支路驱动和无法定向的隐式残差明确报错。

## 多端守恒 relation

N 端 flow 模型当前要求：

1. 恰好一个包含 N 个不同端口的守恒式。
2. 恰好 N-1 个端口具有显式 flow 定义。
3. 剩余端口作为公共参考，编译器递归消去 flow 间依赖。

例如三端器件由两个显式支路 contribution 表示，第三端流量由 KCL 得出。
缺少定义、重复定义、依赖环或多个 N 端守恒式都会失败。局部 relation budget
只防止明显过约束，不是结构秩或 Jacobian 秩证明。

## `ddt` 与动态状态

编译器优先保留后端原语：原生 C/L 匹配直接生成 SPICE 元件，其他受支持表达式
中的 `ddt` 原样生成 Verilog-A `ddt(...)`。积分算法、初始步和 Newton 耦合由
ngspice/OpenVAF 承担。

`state(...)` 已能进入表达式树并保存初值，但当前不会生成 Verilog-A 内部变量，
所以 relation 一旦真正引用 `state` 就会被后端拒绝。当前可执行动态关系必须用
端口或节点量表达，并最终落入原生 C/L 或含 `ddt` 的受支持 contribution。

## 热网络 lowering

热节点先合并 canonical 拓扑，再按电类比发射：

| Pyrilog | SPICE 产物 |
| --- | --- |
| fixed 温度 | `VTH` |
| `C > 0` | `CTH` 对地电容 |
| 常量 `P` 注入 | `ITH` 从地流向热节点 |
| `node.p == expr` | Verilog-A 电流 contribution，符号取反 |
| 热阻 relation | Verilog-A `I(a,b)` contribution |
| 初始温度 | `.ic V(node)=T` |

合并节点保留每一份局部热容和功率，所以并联到同一后端节点时自然求和。
详细符号和量纲见 [../physics/physics.md](../physics/physics.md)。

## 生成物与审计

manifest 至少记录：

- 源类与稳定实例 ID；
- 前端节点到后端节点名的映射；
- 每个实例选择的 lowering 和参数 SI 值；
- Verilog-A 源文件哈希与 OSDI 目标；
- 热 lump 的 canonical node、热容、初温和 fixed 状态；
- 运行所需能力，如 `hierarchical_subcircuits`、`thermal_analog_mna` 和
  `verilog_a_ddt`。

flat 路径先写临时目录，最后以替换主网表作为发布标记，避免失败编译发布半套
新产物。层次路径当前仍直接写出文件，尚未实现同等原子发布和旧产物清理。

## 运行边界

`CompiledModel.run()` 当前支持 ngspice 的 `OperatingPoint` 和固定输出步长的
`Transient`。它会构建 OSDI、生成临时 `.control` 运行网表并写 ASCII raw。
ngspice control command 引用的模型和输出路径当前不能含空白字符。

以下仍不属于当前运行链：

- `Output` 到 CSV/单位视图的自动重建；
- interactive `Session` 和 accepted-step 参数更新；
- Controller feedback 调度；
- Xyce 可执行目标；
- AC、噪声、扫参与完整 SPICE `.control` 封装。

## 后续 pass

以下是目标设计，不是现有能力：

1. 复数表达式精确标量化为实部/虚部通道。
2. 按量纲和工作尺度归一化方程，并可逆重建输出。
3. 结构秩、Jacobian 秩和欠约束诊断。
4. 一般隐式 residual 到辅助未知量及 contribution 的系统化 lowering。
5. Controller accepted-step 调度和后端 hook 适配。

这些 pass 应分别有等价性条件和回归，不能用“成功生成文本”代替证明。
