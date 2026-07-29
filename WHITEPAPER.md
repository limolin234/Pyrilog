# Pyrilog 1.0.0 技术白皮书

## 摘要

光、电、热联合系统的建模困难，不只在于数值求解，还在于如何用一种简洁语言同时
表达层次拓扑、带单位参数、守恒量、复数信号、状态和离散控制。传统 SPICE
擅长基于节点的电路求解，Verilog-A 擅长局部模拟行为，但将光学复包络和热网络
硬塞入这些接口会使模型变得难写、难复用、难验证。

Pyrilog 的选择是把 Python 作为宿主语言，把“节点、端口、参数、关系”作为最小建模核心。
编译器先构造和校验对象图，再为已支持的子集选择原生 SPICE 或 Verilog-A 后端。
这一路径保留了 SPICE 生态，同时为未来原生光电热求解器保留了明确的语义边界。

## 1. 问题定义

连续多物理模型最终可写成一组隐式关系：

$$
F\!\left(x, \dot{x}, t; p\right)=0,
$$

其中 $x$ 是未知量和内部状态，$\dot{x}$ 是其时间导数，$p$ 是模型参数。得到这个形式之后，
牛顿迭代、时间积分和 MNA 装配都已有成熟方法。因此 Pyrilog 1.0.0 首先解决的是表述和编译问题：

- 用户如何只描述器件的接口、参数和局部关系；
- 框架如何从 Python 对象中提取类型拓扑与约束；
- 编译器如何在不改变方程的前提下选择现有后端；
- 未支持的物理量或算子如何显式失败，而不是生成看似可运行的错误近似。

## 2. 最小语言核心

一个 Pyrilog 器件只有三类公开要素。

| 要素 | 作用 | 例子 |
| --- | --- | --- |
| 端口 | 声明物理域与可观测量 | `p = eport()` |
| 参数 | 提供默认值、单位与可选约束 | `resistance = 1 * kohm` |
| 关系 | 声明必须为真的局部方程 | `p.v - n.v == resistance * p.i.i` |

```python
class Resistor(Device):
    p = eport()
    n = eport()
    resistance = param(1 * kohm, min=0 * ohm)
    relation = (
        p.i + n.i == 0,
        p.v - n.v == resistance * p.i.i,
    )
```

类定义完成时，框架收集端口、参数 schema 和关系表达式。实例化时，构造关键字覆盖类默认值。
这不是扫描任意 `__init__` 参数的猜测机制；只有框架支持的声明会进入模型 schema。

### 2.1 为什么关系放在类里

端口、参数和本构件方程共同定义了一个器件。将关系拆成类外的函数库会让局部端口语义和
参数 schema 脱节。Pyrilog 因此把 `relation` 保留在 `Device` 类体中，而将关系的包装、注册和编译
交给框架。用户不需要为每个方程再写一层装饰器或 lambda。

### 2.2 参数反射

常用参数直接赋值：

```python
class Ring(Device):
    radius = 10 * um
    coupling = 0.12
```

只有需要范围或其他高级元数据时才使用 `param(...)`：

```python
coupling = param(0.12, min=0.0, max=1.0)
```

反射在类创建时发生一次，并形成稳定 schema。`ClassVar` 不会被收集为模型参数。

## 3. 节点、端口与拓扑

电和集总热学网络使用保守节点。节点上的端口共享 potential，后端 MNA 对 flow 求和。
光端口的入射与出射波则是独立量，所以前端将光连接保留为严格二元、双向的端口连接。
光学连接的后端标量化尚未实现。

```python
with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    load = Resistor(resistance=1 * kohm)

    source.p | load.p
    circuit.GND |= (source.n, load.n)
```

`|` 创建连接，`|=` 扩展已有节点。操作过程先检查端口类型、图归属和占用状态，
再一次性提交拓扑；失败的连接不应留下半注册节点。

Python 局部变量名不是网表 ABI。框架按器件类型和层次结构生成稳定 ID，例如
`hierarchical_load_1.section.load`。这样重构用户函数内的局部标识符不会改变编译结果中的层次名称。

### 3.1 内置参考节点

```python
with Circuit() as system:
    system.AMBIENT.t = 298.15 * K
    system.GND |= electrical_ports
    system.AMBIENT |= thermal_ports
```

`GND` 映射到 SPICE `0`。`AMBIENT.t` 读取时仍是可用于关系和观测的符号温度，
赋值则设定固定环境温度。两者都惰性创建，未使用的 `AMBIENT` 不会污染纯电拓扑。

## 4. 从 relation 到 MNA/DAE

Pyrilog 将等式 `lhs == rhs` 存储为残差 $r=lhs-rhs$。拓扑决定哪些端口共享节点未知量，
器件 relation 提供构件方程与内部状态关系。

在理想的通用装配中，节点 potential 是 MNA 未知量，连接到该节点的 flow 贡献参与 KCL。
当前 1.0.0 并未实现通用隐式残差求解器，而是将已识别的电学子集降低给 SPICE/Verilog-A。

对当前自定义二端电学 lowerer，器件必须显式提供可证明的端口电流守恒关系，
例如 `p.i + n.i == 0`。这是为了让编译器能确定 Verilog-A branch contribution 的方向，
不是要用户在每个拓扑节点手写一遍全局 KCL。

### 4.1 微分算子

```python
relation = p.i.i == capacitance * ddt(p.v - n.v)
```

在已支持的 contribution 形式中，`ddt(expr)` 原样降低为 Verilog-A `ddt(expr)`。
时间积分、步长控制和 Newton 迭代仍由模拟后端负责；Pyrilog 不在 Python 中用前一采样点做差分近似。

## 5. 编译链

```text
object graph
  -> non-mutating hierarchy flattening
  -> electrical topology validation
  -> device and relation classification
  -> native primitive or Verilog-A lowering
  -> SPICE topology and instance emission
  -> manifest
```

当前展平结果是编译器内部视图，仍引用 Python 器件和表达式对象；它不是已冻结、可序列化的
通用 DeviceIR。将这一实现细节与未来的稳定 IR 契约分开，可以避免对当前能力过度声明。

### 5.1 原生 SPICE 路径

`pyrilog.devices` 中的 `Resistor`、`Capacitor`、`Inductor`、`VoltageSource` 和
`CurrentSource` 显式携带原语元数据。编译正确性不依赖从任意用户方程中猜测 RLC；
关系模式匹配只能作为严格、可证明的可选优化。

原生路径检查端口形状、参数量纲和有限实数值。`R/C/L` 还必须严格为正。
编译器不会把零电阻静默替换为小电阻或在方程中插入寄生元件。

### 5.2 Verilog-A 路径

无法或不应用原生元件表示的已支持局部关系会生成 Verilog-A contribution。例如：

```verilog
I(p, n) <+ ddt(q(V(p, n), parameters));
```

生成模型经 OpenVAF-reloaded 编译为 OSDI，再由 ngspice 加载。目前只支持可识别的二端实数
电学 contribution，不能将“能生成 Verilog-A”等同于“能编译任意隐式多物理方程”。

## 6. 单位与数值语义

Pyrilog 使用 Pint 作为单位后端，并通过适配层让带单位常量与符号表达式参与同一套运算。
常用单位可直接导入，罕见单位通过 `u.xxx` 访问。量纲检查在编译前拒绝例如
“用电压作为电流残差”的模型。

单位检查不等于数值调理。未来对物理未知量和残差做可逆归一化时，目标形式是

$$
x = D_x \hat{x}, \qquad \hat{F}=D_f^{-1}F,
$$

$$
\hat{J}=D_f^{-1}JD_x.
$$

正的有限对角缩放不改变方程零点，但自动平衡也不能保证改善每个 Jacobian 的条件数。
这些缩放和诊断仍是目标架构，尚未进入 1.0.0 执行链。

## 7. 热、光与控制的边界

### 7.1 热

当 `Device` 声明量纲为 $\mathrm{J/K}$ 的热容参数 `C` 时，前端自动生成内部 `T`、`P`
和热端口 `TP`。默认初始温度回退到 `Circuit.AMBIENT.t` 的设定。这套前端语义已实现，
但热节点、热容和自由热网络还不能降低到当前 SPICE 后端。

### 7.2 光

`oport()` 的 `.i` 和 `.o` 是独立的入射/出射复包络量。目标编译器需要将复数表达式精确拆成
实部和虚部标量通道，而不是只保留幅值。该标量化 pass 尚未实现，因此 1.0.0 拒绝
将光学拓扑编译为可执行网表。

### 7.3 离散控制

Controller 前端可声明采样周期、延迟、保持方式、内部状态和输出，并使用赋值语法
将输出绑定到器件参数。但采样控制不能在 Newton 残差试探中修改状态，必须依赖后端
accepted-step 边界。当前只保存 Feedback IR，尚无调度器和 Session hook。

## 8. 验证边界

Pyrilog 1.0.0 的开发验证包含 80 项回归，其中覆盖：

- 参数、端口、单位、方向视图与类型拓扑；
- 失败连接和 Feedback 绑定的原子回滚；
- 复合器件展平不修改源层次图；
- 原生 SPICE `R/C/L/V/I` 生成与值校验；
- 二端 Verilog-A 生成、OpenVAF-reloaded 编译和 ngspice 工作点仿真；
- 非线性电荷 `ddt` 的 transient，以及 RC 衰减与 $e^{-t/RC}$ 的数值对照；
- 对光、热、Feedback、Output 和 Session 缺失能力的显式报错。

“通过回归”只证明当前用例和工具版本下的行为，不是对任意模型、任意仿真器或任意
数值条件的通用保证。当前已验证工具链是 OpenVAF-reloaded 与 ngspice 46。

## 9. 设计原则与路线图

Pyrilog 保持以下原则：

1. **KISS**：用户只描述接口、参数和关系，框架负责注册和装配。
2. **精确优先**：只做可证明等价的 lowering，不静默插入寄生、延迟或量化近似。
3. **语义先于优化**：标准器件用显式元数据选择原生后端，不依赖脆弱的方程猜测。
4. **能力可见**：仅前端和可执行后端必须在文档和异常中区分。
5. **稳定边界**：单位、符号、层次 ID、参数 ABI 和后端 manifest 是可验证契约。

下一阶段按编译 pass 而不是按单个示例补洞：

1. 复数表达式实部/虚部标量化与光学参考面契约；
2. 热节点、热容与自由热支路 lowering；
3. accepted-step Controller 调度与可交互 Session 后端；
4. `delay` 历史、Output 单位/采样与 CSV 重构；
5. 可逆未知量/残差归一化、结构秩和名义 Jacobian 诊断；
6. 跨后端差分测试与明确的数值容差档案。

## 结论

Pyrilog 1.0.0 证明了一个最小闭环：Python 类体可以同时承载器件 schema 与局部物理关系，
对象和运算符可以构造类型安全的层次拓扑，而编译器可以把已支持的子集精确地交给
SPICE 和 Verilog-A 生态。它的价值不在于把所有物理问题伪装成电路，而在于先给每个物理域
一个明确的高层语义，再对可证明等价的部分做后端降低。
