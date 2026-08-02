# 物理域约定

## 统一框架

电和热使用 potential/flow 守恒拓扑；光使用双向行波参考面。它们在 Python
对象、参数、relation 和层次 `Device` 上统一，但不为了表面一致而折叠物理差异。

| 域 | potential / 状态 | flow / 传播量 | 拓扑 |
| --- | --- | --- | --- |
| 电 | 电压 `v` | 电流 `i/o` | 任意多端 KCL 节点 |
| 热 | 温度 `t` | 热功率 `p.i/p.o` | 任意多端能量守恒节点 |
| 光 | 无节点电势 | 入射/出射复包络 `i/o` | 严格二元参考面 |

## 电域

### 符号

```text
port.v  端口电压
port.i  流入器件为正的电流
port.o  流出器件为正的视图，恒等于 -port.i
```

`.i` 和 `.o` 是同一个 flow 的两个方向视图，不增加未知量。对二端支路
`(p, n)`，DSL 中的 `p.i` 映射到 Verilog-A `+I(p,n)`，`n.i` 映射到
`-I(p,n)`。

器件内部支路守恒由 relation 显式声明：

```python
p.i + n.i == 0
```

连接节点的 KCL 由 SPICE/Verilog-A 拓扑自然提供。两者分别约束器件支路和节点，
不是重复约束。

`Circuit.GND` 是唯一内置电参考，后端映射为 SPICE `0`。

## 热域

### 符号

```text
port.t    端口温度
port.p    热功率符号对象，默认与 .p.o 一致
port.p.o  流出器件的热功率为正
port.p.i  流入器件的热功率为正，等于 -port.p.o
node.t    节点温度
node.p    器件 relation 注入该节点的局部热功率，注入为正
```

纯热阻器件可写为：

```python
class ThermalResistance(Device):
    a = tport()
    b = tport()
    resistance = 20 * K / W
    relation = (
        a.p.i + b.p.i == 0,
        a.p.i == (a.t - b.t) / resistance,
    )
```

### 集总热节点

```python
junction = tnode(C=6 * uJ / K, T=300 * K, P=0 * W)
```

- `C` 是局部热容，量纲 `J/K`，必填。
- `C=0 * J/K` 是无储热的代数热节点，不是缺失值。
- `T` 是可选显式初温；缺省时回退到所属 `Circuit.AMBIENT.t`。
- `P` 是可选常量外部注入；普通电热耦合通常用 `node.p == expression`。
- `fixed=True` 将节点变为固定温度边界。主图优先使用 `Circuit.AMBIENT`。

一个未合并热节点的隐式平衡是：

\[
C\,\frac{dT}{dt}=P_\mathrm{local}+\sum P_{\mathrm{port,out}}.
\]

右侧使用连接器件的流出功率，与 `node.p` 的注入正方向一致。

### 理想节点合并

合并后共享一个 canonical 温度，但每个局部热容和功率注入都保留：

\[
\left(\sum_k C_k\right)\frac{dT}{dt}
=\sum_k P_k+\sum P_{\mathrm{port,out}}.
\]

两个不同显式初温不能理想合并；应在中间放置有限热阻。合并校验覆盖
直接连接、批量连接、层次边界和参数更新；失败更新会回滚。

### SPICE 类比 MNA

当前映射是：

```text
1 V  <-> 1 K
1 A  <-> 1 W
1 ohm-like branch <-> 1 K/W
1 F-like storage  <-> 1 J/K
```

因此：

- 温度是 SPICE 节点电压；
- 热流是支路电流；
- 热阻 relation 生成 `I(a,b) <+ (V(a)-V(b))/Rth`；
- 热容生成对地 `CTH`；
- 热注入生成从地流向热节点的电流贡献；
- `AMBIENT` / fixed 温度生成 `VTH`。

这是方程等价映射，不是数值归一化。大小差距过大的电热参数可能造成
Jacobian 病态，当前需由模型者和后端容差共同控制。

## 光域

### 端口语义

```text
port.i  从参考面入射到器件的复包络
port.o  从器件出射到参考面的复包络
```

`.i` 和 `.o` 是两个独立行波，不是一个 flow 的反号视图。连接只使两个
参考面对齐，不携带损耗、延时或波长响应。这些都属于 Waveguide、Ring、
Splitter 等器件关系。

复场可按幅度和相位构造：

\[
a(\lambda)=\exp\bigl(\alpha(\lambda)+j\phi(\lambda)\bigr),
\]

也可直接使用复数字面量和复表达式。幅值、功率和相位是派生量。

### 当前边界

光对象图和复表达式是**仅前端**。目标 lowering 是将每个复数精确展开为
实部/虚部实数通道，而不是丢弃相位只传功率。波长分布、卷积化简、
参考频率和宽带包络的选择仍是待决问题，不是已实现能力。

当前的最小目标设计是每个光量先表示一个复 travelling-wave 标量，波长作为
显式参数或分析轴；多波长由 Python 层显式展开。连续光谱分布和隐式卷积不在
第一条可执行路径中，除非后续能给出离散化与误差契约。

## 跨物理域耦合

耦合发生在同一 `Device.relation` 或复合图中，不需要特殊“耦合器”基类。例如电热加热器：

```python
relation = (
    p.i + n.i == 0,
    p.i == (p.v - n.v) / resistance,
    junction.p == efficiency * (p.v - n.v) ** 2 / resistance,
)
```

框架保留量纲和拓扑，但不自动猜测能量转换效率或忽略的损耗。器件模型者必须
显式写出跨域关系。

多速率、光包络简化和慢热控制会丢失信息，因此不能由编译器无条件自动决定。
这些简化应由用户选择模型层级，或由未来可证明误差界的优化 pass 完成。
