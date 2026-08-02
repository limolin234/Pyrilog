# 建模语法

## 最小模型

```python
from pyrilog import *


class Conductance(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * A / V

    relation = (
        p.i + n.i == 0,
        p.i == conductance * (p.v - n.v),
    )
```

`Device` 的用户契约只有三部分：端口、参数、关系。类体中的子 `Device`、
节点和连接自动构成复合器件的内部图。

## import 分层

```python
from pyrilog import *
from pyrilog.devices import Capacitor, NPN, Resistor, VoltageSource
from pyrilog.control import Controller, output
from pyrilog.simulation import OperatingPoint, Output, Spice, Transient
```

- 根包：基础建模对象、表达式和常用单位。
- `devices`：可直接 lowering 的标准器件库。
- `control`：可选的离散控制前端。
- `simulation`：编译目标、分析和输出契约。

常用单位可直接导入，罕见单位使用 `u.xxx`，避免根命名空间无限增长。

## 参数和内部量

### 普通参数

类体中受支持的数值或带单位数值自动成为参数：

```python
radius = 10 * um
coupling = 0.12
loss = 2 * dB / cm
```

需要范围元数据时使用：

```python
coupling = param(0.12, min=0.0, max=1.0)
```

实例参数可在编译前覆盖或修改，量纲、范围和层次绑定失败时整体回滚：

```python
ring = Ring(radius=12 * um)
ring.radius = 13 * um
```

### 特殊参数

| 语法 | 语义 | 当前状态 |
| --- | --- | --- |
| `localparam(value)` | 类级只读中间量，不进构造参数或 manifest 实例参数 | 已实现 |
| `external(value)` | 声明为未来 Session/API 外部更新的参数 | 声明已实现，交互更新未支持 |
| `state(initial)` | 内部代数或微分标量 | 前端已实现，通用状态 lowering 未完成 |

`state`、`val` 和 `internal` 当前是同一构造器的别名。新建模推荐 `state`，
因为它能与拓扑节点区分；现有代码使用 `val` / `internal` 也等价。它目前是
**仅前端**元数据，relation 引用内部符号尚不能 lowering。

`ClassVar` 不会被反射为模型参数。方法、字符串和任意 Python 对象也不会被猜测。

## 端口和可读物理量

| 构造器 | 量 |
| --- | --- |
| `eport()` | `.v`、`.i`、`.o` |
| `tport()` | `.t`、`.p`、`.p.i`、`.p.o` |
| `oport()` | `.i`、`.o`，以及 `.abs/.power/.phase` 派生表达式 |

具体正方向见 [../physics/physics.md](../physics/physics.md)。

## 节点和连接

```python
electrical = enode()
thermal = tnode(C=10 * uJ / K, T=300 * K, P=0 * W)
optical = onode()
```

- `enode(reference=False)` 创建电守恒节点。主图通常直接使用 `system.GND`。
- `tnode(C=..., T=None, P=None, fixed=False)` 创建集总热节点；`C` 必填。
- `onode()` 是严格二元光连接的可选有名句柄。

### 保守节点

电和热支持以下等价形式：

```python
node = port1 | port2 | port3
node |= port4
node |= (port5, port6)
node |= port7 | port8
merged = node1 | node2
```

`node |= portA | portB` 的 Python 执行顺序是：先计算 `portA | portB` 得到一个
节点，再将它与 `node` 合并，最后把返回的 canonical node 写回 `node`。

合并使用 union-find。旧节点句柄会转发到 canonical root，但不承诺
`node1 is node2`。应使用 `node.canonical()` 或物理量访问，不依赖 Python 对象同一性。

### 光连接

```python
reference = device_a.out | device_b.input
```

光只允许两个光端口构成一条连接。已连接端口不能继续 `|`；多端分光、
合光或反射必须建模为显式 `Device`。

## 关系表达式

```python
relation = (
    p.i + n.i == 0,
    p.v - n.v == resistance * p.i,
)
```

`==` 构建等式对象，不立即求值。表达式前端支持：

```text
+ - * / **
exp(expr), ddt(expr)
delay(expr, tau, initial=...)
piecewise(..., otherwise=...)
expr.abs, expr.power, expr.phase
expr.<unit>
```

能够构建 AST 不代表当前后端能够 lowering。当前 Verilog-A 子集只接受实数
参数、常数、受支持的端口/热节点电势、算术、`exp`、`abs` 和 `ddt`。
`delay`、`piecewise`、光复数和任意隐式残差会明确报能力错误。

单位视图如 `node.v.mV` 是输出元数据，不能写入器件 relation。

## 复合器件

```python
class Divider(Device):
    p = eport()
    n = eport()
    resistance = 2 * kohm

    upper = Resistor(resistance=resistance / 2)
    lower = Resistor(resistance=resistance / 2)

    p | upper.p
    midpoint = upper.n | lower.p
    lower.n | n
```

每次实例化都递归克隆子器件、内部节点和参数绑定。可用
`u1.upper.resistance`、`u1.midpoint.v` 或命名内部热节点访问子图。

有公开边界端口的子器件仍必须连接该端口。不能一边留下未连接边界端口，
一边从父层绕过端口访问内部节点。无公开端口的命名内部节点可被父层显式复用。

## 主图

```python
with Circuit() as system:
    system.AMBIENT.t = 298.15 * K

    source = VoltageSource(dc=1 * V)
    load = Resistor(resistance=1 * kohm)

    output = source.p | load.p
    system.GND |= source.n | load.n
```

`Circuit` 用 `ContextVar` 保存当前构建上下文。器件先实例化，连接开始后不能再
添加普通器件。`GND` 和 `AMBIENT` 是惰性、每图独立、不可替换的内置节点。

## 离散控制和输出

```python
class BiasController(Controller):
    sample = 10 * ns
    delay = 0 * ns
    hold = "zoh"
    drive = output(V)

    def step(self, measured):
        return {"drive": measured}


source.dc = BiasController()(sense.v).drive
```

Controller 输出赋给器件参数时注册 Feedback IR；多输出可使用 tuple 拆包。
当前是**仅前端**，编译时对 feedback scheduling 报错。

```python
output = Output(node.v.mV, heater.junction.t.degC, file="result.csv")
analysis = Transient(stop=10 * ns, step=1 * ns)
```

`Output` 可保存观测对象和单位视图，但 CSV 重建尚未实现。`OperatingPoint`
和 `Transient` 已可由 ngspice 执行。

## 常见错误

- 连接不同物理域的端点。
- 连接不同 `Circuit` 的对象。
- 将已连接光端口继续并接。
- 将不同显式初温的热节点理想合并。
- 在连接阶段开始后继续实例化器件。
- 在 relation 中使用任意 Python 函数、布尔分支或输出单位视图。
- 把“能 `py_compile`”当作“后端已支持”。
