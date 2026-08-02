# Pyrilog 1.1.0 技术白皮书

## 定位

Pyrilog 用 Python 描述器件接口、参数、关系和层次拓扑，并把受支持的连续模型
编译到 SPICE/Verilog-A。它解决“怎样一致表达、怎样审计后端映射”，不单独保证
模型对真实芯片的预测可信度。

在上层可信降阶研究中，Pyrilog 应承担三项职责：

1. 为快速模型和 reference 模型保留共同的端口、单位、参数和观测语义。
2. 记录模型到后端实例、节点和参数槽的映射。
3. 对不能精确 lowering 的能力显式拒绝，避免无记录的近似进入验证链。

自动选择降阶模型、估计闭环决策风险和升级 reference 尚不属于 1.1.0。

## 最小语言

一个 `Device` 公开三类模型元素：

| 要素 | 作用 | 示例 |
| --- | --- | --- |
| 接口 | 声明物理域和可访问量 | `p = eport()` |
| 参数 | 给出默认值、单位和约束 | `resistance = 1 * kohm` |
| 关系 | 声明局部本构等式 | `p.i == (p.v - n.v) / resistance` |

Python 只承担 elaboration。编译器不扫描任意 `__init__`，也不把任意 Python
控制流当作数值模型。类体中受支持的数值自动成为参数；需要范围或外部属性时再
使用 `param(...)`。

原子器件和复合器件都继承 `Device`。复合器件通过子实例和内部连接递归构造，
不再公开另一套 subcircuit 类型。

## 物理拓扑

电和热使用 potential/flow 守恒节点；光使用严格二元、双向的行波参考面。

```text
electrical: voltage + signed current, arbitrary KCL node
thermal:    temperature + signed heat flow, lumped energy node
optical:    independent incident/outgoing complex envelopes, binary link
```

连接只表示理想拓扑等价。损耗、延时、热阻、反射和模式转换属于器件关系，不能
藏在 `|` 运算符中。

热节点显式写为 `tnode(C=...)`。当前实现的节点平衡为：

\[
\left(\sum_k C_k\right)\frac{dT}{dt}
=\sum_k P_k+\sum_j P_{j,\mathrm{out}}.
\]

合并节点共享温度，但保留每个局部热容和功率注入。固定温度、热容、常量功率
和热 relation 分别降低为 SPICE/Verilog-A 中的 `VTH`、`CTH`、`ITH` 和
电流 contribution。

## 编译链

```text
object graph
-> native hierarchy eligibility or non-mutating flatten
-> topology, dimension and relation validation
-> explicit primitive metadata or exact relation matching
-> supported real relation to Verilog-A contribution
-> thermal lump emission
-> SPICE + Verilog-A + manifest
-> OpenVAF OSDI + ngspice
```

标准库通过显式元数据选择 `R/C/L/V/I/E/G/D/Q`。自定义 relation 只有在表达式
结构能证明等价时才匹配 `R/C/L/V/I/E/G`；匹配失败后，只对可定向为显式实数
contribution 的关系生成 Verilog-A。

当前 fallback 不是一般隐式方程求解器。复数光场、内部 `state`、`delay`、
`piecewise`、重复支路驱动和无法定向的 residual 会明确报错。

## 层次和生成物

纯原生电复合图保留为实例私有的 SPICE `.subckt`。包含热节点或 Verilog-A
relation 的图转入 flat 路径。原生 SPICE 实例直接写入主网表或所属 `.subckt`，
不会为每个实例创建独立器件文件。

manifest 记录：

- 稳定实例 ID 和源 Python 类；
- 前端节点到后端节点的映射；
- 参数 SI 值、量纲和 external 属性；
- 每个实例选择的 lowering；
- Verilog-A 文件、哈希和 OSDI 目标；
- thermal lump 的 canonical node、热容、初温和 fixed 状态；
- 运行所需后端能力。

这些映射是未来多保真模型对齐、参数扫描和输出比较的基础，但 1.1.0 还没有
形成统一的 reduced/reference provenance schema。

## 数值与验证边界

Pyrilog 复用 SPICE 的 MNA、Newton 迭代和时间积分，并优先保留 `ddt` 等后端
原语。量纲检查可以拒绝物理类型错误，但不能自动解决 Jacobian 病态。

热类比采用 `1 V = 1 K`、`1 A = 1 W`。它保持方程映射，不是自动归一化；尺度
相差过大时仍可能导致收敛问题。编译器也不静默增加 `gmin` 或寄生元件。

108 项回归覆盖前端、原生 SPICE、层次、relation 分类、多端 contribution、
`ddt` 和电热工作点。测试与跨后端一致只证明声明范围内的实现行为，不能替代
硅后测量或高保真物理 reference。

## 与可信降阶的接口

后续上层研究需要在 Pyrilog 之上增加，而不是混入 relation 语法：

```text
model family
-> declared discarded freedoms and applicability domain
-> reduced/reference paired execution
-> parameter and uncertainty propagation
-> closed-loop decision metrics
-> confidence gate
-> selective reference escalation
```

核心评价应面向捕获、失锁、恢复时间、功耗和共享控制器服务能力，尤其关注
快速模型给出成功而 reference 失败的 false-safe。Pyrilog 只负责让模型结构、
参数和后端产物可追溯；风险判定必须由独立的验证层完成。

## 路线边界

下一步语言收敛和编译工作包括：

1. 热端口直接统一为 `.i/.o`，节点 `.p` 作为默认零的局部注入槽。
2. 在结构唯一时自动补全缺失的守恒 flow，公共语法只保留 `==`。
3. `tnode.c` 纳入统一参数槽；状态初值由 transient 分析配置，而不是器件模型。
4. 用类型化 `analysis.values` 统一参数 override 和初始状态绑定，后端分别注入。
5. 光复包络参考面、波长参数和实部/虚部标量化。
6. 参数 ABI、输出重建、归一化和结构秩诊断。

这些是已确认方向，不是 1.1.0 可执行 API。当前能力以 README、
`docs/architecture/capabilities/`、源码和测试为准。
