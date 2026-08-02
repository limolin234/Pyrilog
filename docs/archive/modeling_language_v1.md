# Pyrilog 1.0.0 建模语言

## 1. 目标与边界

该语言用 Python 描述光、电和集总热行为模型，并将用户写下的局部约束编译为
后端无关的隐式系统：

\[
F(x, \dot{x}, s, t, p, h)=0.
\]

其中 `x` 是当前未知量，`ddt(x)` 引入 \(\dot{x}\)，`s` 用于小信号频域，`p`
是参数，`h` 是 `delay(...)` 访问的已接受历史。

v1 的核心产物是：

1. 类型安全的 Python 对象图；
2. 局部关系表达式和编译后 IR；
3. SPICE 网表、Verilog-A 模型和源映射 manifest；
4. 后端能力检查和可重复验证。

v1 不自己实现完整 DAE 求解器。它先使用 OpenVAF-reloaded 和 ngspice 验证
编译链，以后可接入 Xyce、Spectre 或自研 MNA 求解器。

## 2. KISS 原则

- 用户只描述器件的接口、参数和局部关系。
- 所有可实例化模块都是 `Device`；原始器件和复合器件递归同构。
- 先实例化全部器件，再集中连接，最后声明输出与分析。
- 电和热使用守恒节点；光使用严格二元参考面连接。
- 用户不写全局 stamp、残差装配、稳定 ID 或网表模板。
- 不支持的后端能力必须报错，不静默近似。

根包是基础建模 facade，控制和仿真功能按需导入：

```python
from pyrilog import *
from pyrilog.control import Controller, output
from pyrilog.devices import Capacitor, CurrentSource, Inductor, NPN, Resistor
from pyrilog.devices import VoltageControlledCurrentSource, VoltageControlledVoltageSource
from pyrilog.devices import VoltageSource
from pyrilog.simulation import Output, Spice, Transient
```

内部依赖按 `units -> expressions -> model -> simulation` 分层：单位适配不依赖
表达式，表达式不依赖拓扑，模型层只负责结构和局部关系，仿真层负责分析声明、
编译和后端。草稿阶段的 `multiphysics` 包和 `core`、`expr`、`analysis`、
`backends`、`compiler`、`nodes`、`ports` 转发模块不进入 1.0.0 公共 API。

## 3. 器件定义

### 3.1 接口、参数、关系

自定义器件直接声明三要素：

```python
class TemperatureDependentResistor(Device):
    p = eport()
    n = eport()
    resistance = param(1 * u.kohm, min=1e-15 * u.ohm)
    temperature_coefficient = 0 / u.K
    temperature = external(300 * u.K)
    relation = (
        p.i + n.i == 0,
        p.v - n.v
        == resistance
        * (1 + temperature_coefficient * (temperature - 300 * u.K))
        * p.i,
    )
```

`Device` 类体只有三类仿真元素：

- 接口：`eport()`、`oport()`、`tport()`；
- 参数：普通数值或带单位数值，高级元数据用 `param(...)`；
- 关系：一个或多个必须同时满足的局部等式。

普通参数直接赋值：

```python
class Ring(Device):
    radius = 10 * u.um
    coupling = 0.12
    loss = 2 * u.dB / u.cm
```

只有需要范围、别名或扫参元数据时才使用包装器：

```python
coupling = param(0.12, min=0.0, max=1.0)
command = external(0 * u.V)
```

`external(...)` 仅保留给没有本地控制器、需要由外部 Session/API 写入的参数。
控制器输出会自动把目标参数登记为运行时可写。

常用标准元件不需要重复写关系：

```python
from pyrilog.devices import Capacitor, CurrentSource, Inductor, Resistor, VoltageSource

r1 = Resistor(resistance=1 * u.kohm)
c1 = Capacitor(capacitance=1 * u.pF)
```

这些库类都包含端口和参数。线性器件还包含局部关系；`NPN` 则将非线性
本构委托给后端标准 `Q ... NPN` model。它们额外携带框架保留的原生 SPICE
primitive 元数据。编译器据此直接生成 `R/C/L/V/I/E/G/Q`；不会根据类名猜测，也不把
任意微分关系近似拼成 RLC。严格的关系识别可以作为后续优化，但不是正确性基础。

内部未知量用 `val(initial_value)` 声明；`internal(...)` 保留为兼容别名：

```python
state = val(0 * u.V)
relation = time_constant * ddt(state) + state == gain * sense.v
```

框架根据一个内部量是否出现在 `ddt(...)` 中将它分类为微分状态或代数
未知量。不另设 `state` 或 `integral` 核心对象；积分关系通过新内部量
`z = val(...)` 与 `ddt(z) == input` 表示。

`Controller` 是与 `Device` 平行的离散调度对象，不参与 MNA 拓扑。输出数量必须
预先声明，赋值语法用于登记 feedback：

```python
class DualController(Controller):
    sample = 10 * ns
    delay = 2 * ns
    hold = "zoh"
    out1 = output(V)
    out2 = output(V)
    state = val(0 * V)

    def step(self, first, second):
        return first, second


command = DualController()(sensor1.v, sensor2.v)
source1.dc = command.out1
source2.dc = command.out2
```

`sample` 是必填的正时间常量；`delay` 默认为 `0 * s`，`hold` 默认为 `"zoh"`，
也可选 `"foh"`。`step()` 必须能接收调用时给定的输入数；`val()` 在 `Controller`
中表示 accepted-step 之间保存的离散状态。当前前端只建立 Feedback IR，
后端调度仍未实现。

### 3.2 Python 反射契约

`Device` 的 metaclass 使用自定义类命名空间。类体执行到受支持的数值赋值时，
命名空间立即将它提升为参数符号；因此后续 `relation` 引用的是参数引用，
而不是被烘死的 Python 常量。

类创建结束时，框架只扫描明确支持的元素：

- `Port` 声明；
- 数值、`Quantity`、`param(...)`、`external(...)` 和 `internal(...)`；
- `relation`；
- 类体中实例化的子 `Device` 和内部连接。

方法、`ClassVar`、字符串和任意 Python 对象不自动成为仿真参数。参数 schema
在类建立时收集并冻结，不在每次实例化时重新猜测。

### 3.3 实例参数

```python
ring = Ring()
ring = Ring(radius=12 * u.um, coupling=0.15)
ring.radius = 13 * u.um
```

绑定优先级为：

```text
显式构造参数 > 外层复合器件同名符号 > 器件默认值 > 报错
```

编译前修改数值参数会立即传播到绑定的子器件，并重用子参数的量纲与
范围校验；任一子参数拒绝时，本次层次更新整体回滚。编译后再修改普通参数必须
重新编译；只有 `external` 能通过 Session 更新。

## 4. 端口量

### 4.1 电学端口

```text
port.v  端口电势
port.i  流入器件的电流，是唯一底层电流未知量
port.o  流出器件的电流视图，恒等于 `-port.i`
```

电端口不再提供 `port.i.i` 或 `port.i.o` 嵌套形式。`.i` 和 `.o`
只是同一电流的两个方向视图，不会增加 MNA 未知量或局部约束预算。

当前二端电学 lowering 要求模型显式给出且只给出一条
`p.i + n.i == 0` 守恒关系，再给出一条本构关系。SPICE/Verilog-A 的支路语义
虽然也会产生等量反向端口电流，但编译器不得替用户补写 DSL 中缺失的约束。

对二端器件，`p.i + n.i == 0` 是器件内部支路守恒；连接节点的 KCL
由拓扑编译器另行生成，两者不重复。

### 4.2 光学端口

```text
port.i  从连接参考面入射到器件的复包络
port.o  从器件出射到连接参考面的复包络
```

派生量由表达式对象提供：

```text
wave.abs       幅值
wave.power     功率
wave.phase     相位
```

光场约定为 \(\operatorname{Re}\{a(t)e^{+j\omega_\mathrm{ref}t}\}\)。`dB / length`
表示供紧凑模型公式使用的代数功率损耗计数，因此不会在参数构造时被 Pint
提前线性化；场幅因子为 \(10^{-L_\mathrm{dB}/20}\)。`dBm` 则仍表示可转换为瓦特的
绝对功率级。

### 4.3 热学端口

```text
port.t    温度
port.p    热功率符号对象，默认/`.o` 为流出器件为正
port.p.i  同一热功率的流入器件为正视图，即 `-port.p.o`
port.p.o  同一热功率的流出器件为正视图，即 `port.p`
```

普通器件只有在类体声明量纲为 `J/K` 的热容参数 `C` 时，框架才自动生成
内部热元素：

- `T` 是内部温度状态，初值默认回退到 `Circuit.AMBIENT.t` 的设定，构造时可用
  `T=...` 覆盖；
- `P` 是用户关系定义的内部净发热功率；
- `TP` 是唯一自动热端口，具有 `TP.t` 和 `TP.p`；
- 框架自动加入 `TP.t == T` 和 `C * ddt(T) == P - TP.p.o`；
- 没有热容 `C` 的普通器件不自动生成 `T/P/TP`；同名电容 `C: F` 不会误触发。

显式 `tport()` 用于无自身热容的纯热支路器件，例如热阻和热耦合矩阵，因而
不要求同时声明 `C`。光端口的 `port.i` 与 `port.o` 始终是两个独立复波，不使用
上述流量方向视图。

## 5. 局部关系语言

`relation` 是 `Relation` 对象或它们的 tuple：

```python
relation = (
    p.i + n.i == 0,
    p.v - n.v == resistance * p.i,
)
```

类体执行时，端口量、参数和算子已构建表达式树。用户不需要再写
`equations()`、返回列表或调用 `model_add()`。

v1 表达式至少支持：

```text
+ - * / **
exp, abs, phase, power
ddt(expr)
delay(expr, tau, initial=...)
piecewise(...)
```

`ddt(expr)` 对 DC 为零，对 AC 变为 \(s\,expr\)，对 transient 由后端时间积分
离散化。

`delay(expr, tau, initial=...)` 是纯历史算子：

- `tau` 必须是非负、参数值延迟；
- DC 退化为 `expr`；
- AC 乘以 \(e^{-s\tau}\)；
- transient 读取已接受历史，`initial` 定义仿真前历史；
- 裸零 `initial=0` 按 `expr` 的类型和单位提升。

物理传输线仍是有阻抗、波关系和局部状态的 `Device`，不是通用 `delay`
的别名。后端无法精确表示时必须报错。

## 6. 复合器件

复合器件仍然继承 `Device`，类体先声明边界端口和参数，再实例化子器件，
最后连接内部图：

```python
class RingChannel(Device):
    opt_in = oport()
    opt_out = oport()
    heater_p = eport()
    heater_n = eport()

    radius = 10 * um
    coupling = 0.12

    ring = Ring(radius=radius, coupling=coupling)
    heater = Heater()

    opt_in | ring.input
    ring.through | opt_out
    heater_p | heater.p
    heater_n | heater.n
```

类命名空间内的子器件和连接构成冻结定义图。每次实例化复合器件时，
框架生成独立的子实例、边界绑定和参数绑定。编译器可展平，也可在支持的
后端保留层次。

实例层次可直接通过 Python 属性访问：

```python
u1.ring.radius
u1.heater.p.v
u1.internal_node.v
```

这些是对实例内独立对象的引用，不是字符串路径。编译器再将对象所有权
转换为 `u1.ring` 形式的稳定层次 ID。

主图和子图不是两种用户类型；区别只是一个 `Device` 类是否拥有内部图。
复合 `Device` 也可同时声明自身局部 `relation`；编译时这些关系与子器件都会
贡献到展平后的电路。

## 7. 构建电路

```python
with Circuit() as system:
    system.AMBIENT.t = 298.15 * u.K

    source = VoltageSource(dc=1.2 * u.V)
    load = Resistor(resistance=1 * u.kohm)

    drive = source.p | load.p
    system.GND |= (source.n, load.n)
```

`Circuit` 使用 `ContextVar` 保存当前活动构建上下文。器件完成参数和端口初始化
后自动注册，不要求用户传入实例名。框架按类型和注册顺序生成稳定内部 ID。

Python 局部变量名不能由普通运行时反射稳定获取，因此实例、节点和连接的内部
ID 不依赖 `source`、`drive` 等局部标识符。层次子器件的类成员名可在类建立时记录。

### 7.1 连接算子

| 左操作数 | 运算符 | 右操作数 | 结果 |
| --- | --- | --- | --- |
| 电端口 | `\|` | 电端口 | 新建并返回匿名电节点 |
| 热端口 | `\|` | 热端口 | 新建并返回匿名热节点 |
| 光端口 | `\|` | 光端口 | 新建并返回严格二元光连接 |
| 电节点 | `\|=` | 电端口或序列 | 扩展原节点 |
| 热节点 | `\|=` | 热端口或序列 | 扩展原节点 |

`system.GND` 是内置电参考节点，后端映射为 SPICE `0`；`system.AMBIENT` 是
内置固定温度参考节点。写入 `system.AMBIENT.t = 298.15 * u.K` 设置环境温度；
读取 `system.AMBIENT.t` 返回可用于关系和观测的温度表达式。两者均在首次访问时
惰性创建引用对象；`AMBIENT` 只在首次读取 `.t`、成功设温或连接时
加入当前 `Circuit`，因此纯电路不会因为未使用的 `AMBIENT` 引入热拓扑。
`system.ambient_temperature` 仅保留为兼容入口，不再是推荐语法。`|=` 增强赋值
写回同一内置节点，不会替换该节点对象。

`enode()` 和 `tnode()` 只用于普通内部节点或需要独立状态的热节点，不再用于
构造全局地与环境参考。名称、参考属性和观测标签由节点对象保留。连接操作必须
先完整校验所有端口的 domain、所有权和已连接状态，然后原子提交。

电节点使电势相等并生成 KCL；热节点使温度相等并生成热功率守恒。
理想连接不包含损耗，互连损耗必须由显式器件表示。

光连接不是多端节点，它只生成：

\[
a_2=b_1, \qquad a_1=b_2.
\]

分光、合光、反射、模式转换、损耗和传播延迟都必须是器件。

## 8. 环境、输出和分析

```python
output = Output(
    drive.v.mV,
    ring.T.degC,
    detector.optical.i.power.mW,
    file="result.csv",
)

analysis = Transient(stop=100 * u.ns, step=1 * u.ns)
```

`Output` 只声明要记录的量、单位和输出文件。时间步长、终止时间、容差和
扫参属于 Analysis，不属于 Output。同一个观测量的大小、相位、功率和单位转换
由表达式属性统一描述。

复合器件内部量通过层次成员访问，例如 `channel.ring.T`。内部实例名来自
复合类成员，因此可以进入 manifest 和输出标签。

## 9. 编译链

```python
target = Spice(
    simulator="ngspice",
    netlist="build/model.sp",
    verilog_a_dir="build/verilog_a",
)

compiled = system.compile(target)
result = compiled.run(analysis, output=output)
```

### 9.1 前端展开

当前可执行编译器已实现电学复合图的递归展平：

1. 冻结顶层 `Circuit` 的实例、节点和光连接；
2. 递归实例化复合 `Device` 的定义图；
3. 将复合边界端口绑定到内部端口；
4. 生成不依赖 Python 对象地址的层次稳定 ID；
5. 校验端口基数、domain、电学参考、热容和必填参数。

### 9.2 表达式 IR

当前 1.0.0 编译器构建一个不修改源层次图的 flat topology view。该视图
记录展平后的 primitive contributors、节点等价类、端口映射和后端能力，
同时复用已反射生成的关系表达式对象。后端仍从这些器件对象读取端口、
参数和关系 schema；当前尚未生成完全脱离 Python 对象的冻结 DeviceIR。

后续原生求解器和多后端共享的目标 IR 形状为：

```text
DeviceIR
  stable_id
  typed_ports
  parameter_bindings
  relations
  source_location
```

表达式节点已保留算子、量纲、值类型（实数/复数）以及对端口和参数的引用；
完整源位置和独立冻结 IR 留作后续工作。

### 9.3 后端能力检查

完整分类体系需要收集：

```text
real_relations
complex_envelopes
ddt
delay
piecewise
interactive_advance
live_read
held_write
```

后端不满足时在生成不完整网表前报 `BackendCapabilityError`。
当前 manifest 已实际写入 `real_relations`、按需写入 `verilog_a_osdi` 和
`verilog_a_ddt`；其余能力仍在实现边界之外。

## 10. SPICE 与 Verilog-A lowering

编译器先读取标准器件的显式 primitive 元数据：

- `pyrilog.devices` 的标准器件直接生成 SPICE `R/C/L/V/I/E/G/Q`；
- 编译器复核有序端口、参数量纲、实数有限性、`R/C/L` 正值与 BJT model card；
- 自定义关系先严格识别 `R/C/L/V/I/E/G`，无需用户选择 backend；
- 可由受支持 Verilog-A 子集表示的其他二端或多端关系生成共享 `.va` 模型；
- 同类器件实例复用同一个模型，网表只写拓扑和参数覆盖。

对自定义局部关系的严格结构识别仅是可选优化，不是标准器件编译的正确性基础。
原生 `R/C/L` lowering 要求实例值为正且有限；例如 ngspice 会把 `R=0` 静默
替换为一个极小电阻，这不等价于 DSL 的理想短路约束，因此当前编译器直接报错。
理想短路需要日后定义专门的拓扑合并或零伏电压源 lowering。

二端显式贡献中的 `ddt(expr)` 原样生成 Verilog-A `ddt(expr)`，由模拟器负责
DC、AC 和 transient 的积分语义。当前未把任意内部状态隐式残差转换成后端模型。
优先保留这些成熟后端原语；只有在受限模型类上证明严格等价时，才可将其
优化为原生 SPICE 网络，不把任意微分关系猜成 RLC。

当前纯电器件的局部 relation 数不能超过电端口数；少于上限可为未来
Controller 或外部约束保留空间，超过则在 relation analysis 阶段、
backend 选择前直接报错。即使未超过总预算，同一物理支路也不能由
多条等式同时驱动；解析出重复驱动目标后立即报错，因为 Verilog-A
contribution 会相加，不等价于原来多条等式同时成立。这只是保守的
结构检查，不声称已完成一般符号秩分析。

ngspice/OpenVAF-reloaded 路径为：

```text
Python object graph
  -> validated IR
  -> native SPICE instances + generated Verilog-A
  -> OpenVAF-reloaded .osdi
  -> ngspice netlist using pre_osdi
  -> raw/CSV results mapped through manifest
```

目标 ABI 规定：模拟后端不原生支持光学复包络时，编译器将每个复数量系统性降低为
实部和虚部两个实数量，并在 manifest 中保留复数重组方式。这是后端 ABI，
不改变 Python 层用户看到的复数表达式。当前可执行切片尚未实现该 lowering，
遇到光连接会明确拒绝。

完整 v1 manifest 的目标内容至少包括：

```text
stable device/port/node/signal IDs
backend instance and node names
parameter values and units
observable reconstruction and units
generated model files and hashes
required backend capabilities
```

当前 manifest 已包含稳定器件/端口/节点 ID、后端实例与节点名、参数 SI 值和
量纲、生成模型文件与哈希、所需后端能力；observable 重构和源位置映射尚未实现。

## 11. 交互 Session 边界

交互仿真是编译后后端能力，不是器件关系语法：

```python
with compiled.session(analysis, output=output) as session:
    session.initialize()
    session.advance(sample_time)
    measured = session.read(detector_output.v)
    session.write(source.dc, command, at=sample_time, hold=True)
    session.advance(analysis.stop)
    result = session.result()
```

`advance(target)` 必须精确到达已接受 target breakpoint，否则报错。`read()`
读取该边界的 \(t^-\) 状态；`write(..., hold=True)` 从 \(t^+\) 起保持，不回溯改变
刚接受的状态。`result()` 合并所有分段并去除公共边界重复点。

控制器状态只在 `advance()` 成功后更新，不得在 Newton 试探残差回调中改变。
连续控制器仍是普通 `Device`，由模拟求解器和被控对象一起求解。

Xyce 可将 Session 映射到 `simulateUntil`、电路量读取、参数更新和 DAC/ADC
接口，但仍需根据实际安装能力启用。在没有已核验逐步 API 的 Spectre 版本上，
`compiled.session()` 必须报不支持；OCEAN/SKILL 作业编排不自动等价于逐时刻 hook。

## 12. 错误与事务性

以下操作必须先全量检查，然后一次性提交：

- 器件构造和自动注册；
- 一个或一批端口连接；
- 复合器件定义冻结；
- `Circuit.compile()`；
- 后端源码、网表和 manifest 发布。

错误不得留下半连接端口、部分冻结图、混合新旧编译产物或无源映射网表。

## 13. 实现与验证顺序

1. 单位、表达式、端口 descriptor、参数 descriptor；
2. `Device` 类体反射、实例绑定和 `Circuit` 自动注册；
3. 电/热节点、光连接和复合图展开；
4. 稳定 ID、量纲检查和局部关系 IR；
5. 电压源、电阻等原生 SPICE lowering；
6. 二端电学局部关系的 Verilog-A lowering；
7. OpenVAF-reloaded 编译与 ngspice DC/transient smoke test；
8. 热网络和光学复数 lowering；
9. 延迟、分段表达式、观测重构和交互 Session。

每一阶段都必须同时拥有 Python 单元测试、生成物检查和可执行后端 smoke test。
只有 `py_compile` 通过不能证明仿真 API 已实现。

## 14. v1 之后

- 自研 MNA/DAE 求解器与自动 Jacobian；
- 噪声、参数分布、相关性和不确定性传播；
- 离散事件、多速率调度和数字信号 domain；
- S 参数、频率数据、因果性修复和降阶模型；
- PDE 热场、热卷积核和自动降阶；
- Spectre/Xyce 版本化适配器与 checkpoint/restore；
- 大规模光电热混合模型的稀疏结构和并行调度。
