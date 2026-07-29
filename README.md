# Pyrilog 1.0.0

Pyrilog 是一个以 Python 为宿主语言的关系式电路建模前端。用户使用类定义
器件的端口、参数和局部约束，再用 Python 对象和运算符构造层次拓扑。编译器
将已支持的电学子集降低为原生 SPICE 元件或生成的 Verilog-A。

```text
Python Device + relation + typed topology
                  |
                  v
       flatten, validate, classify
             /             \
            v               v
  native SPICE R/C/L/V/I   generated Verilog-A
             \             /
              v           v
                ngspice
```

Pyrilog 1.0.0 是第一个可执行编译切片，不是已完成的通用光电热求解器。
光学复包络、热网络、离散控制调度和输出重构仍在路线图中；对这些能力，
当前编译器会明确报错，不生成静默近似。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python examples/quickstart.py
```

`quickstart.py` 只使用原生 SPICE 元件，不需要外部仿真器就能生成网表和 manifest。

```python
from pyrilog import Circuit, V, kohm
from pyrilog.devices import Resistor, VoltageSource


with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    upper = Resistor(resistance=1 * kohm)
    lower = Resistor(resistance=1 * kohm)

    source.p | upper.p
    upper.n | lower.p
    circuit.GND |= (source.n, lower.n)
```

`port_a | port_b` 创建匿名节点，`node |= ports` 将端口并入已有节点。
`Circuit.GND` 是内置电参考节点，后端稳定映射到 SPICE `0`。

## 自定义器件

器件只声明接口、参数和必须满足的关系：

```python
from pyrilog import A, V, Device, eport


class Conductance(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (A / V)
    relation = (
        p.i + n.i == 0,
        p.i.i == conductance * (p.v - n.v),
    )
```

Pyrilog 对类体做一次反射：数值和带单位的类属性成为参数，`eport()` 成为类型端口，
`relation` 成为局部约束。构造参数可覆盖默认值，而自动注册、稳定层次 ID、量纲检查
和后端生成由框架处理。

端口的默认 flow 视图以“流出器件”为正；`.o` 是同向视图，`.i` 是反号的
“流入器件”视图。因此 `p.i.i` 表示从 `p` 端流入器件的电流。

## 当前能力

| 能力 | 1.0.0 状态 |
| --- | --- |
| `Device`、类体参数反射、`eport()`、局部 `relation` | 已实现 |
| `Circuit` 自动注册、类型拓扑、失败连接原子回滚 | 已实现 |
| Pint 单位、参数范围和关系量纲检查 | 已实现 |
| 层次复合器件、内部节点、稳定层次 ID 和电学展平 | 已实现 |
| 标准 `R/C/L/V/I` 原生 SPICE lowering | 已实现 |
| 二端实数电学关系的 Verilog-A lowering | 已实现 |
| `ddt(...)` 在已支持的二端关系中生成 Verilog-A | 已实现 |
| OpenVAF-reloaded + ngspice 工作点/瞬态验证 | 已验证的开发工具链 |
| `oport()`、热 `T/P/TP`、Controller/Feedback | 仅前端建图 |
| 光学复数标量化、热网络 lowering、离散调度 | 未实现 |
| `delay`、Output CSV 重构、interactive Session | 未实现 |
| 自动归一化、结构秩与 Jacobian 病态诊断 | 目标架构，未实现 |

## 验证

运行回归：

```bash
python -m unittest discover -s tests -v
```

纯 Python 前端和原生网表测试可直接运行。端到端 Verilog-A 测试还需要：

- `openvaf-r` 可在 `PATH` 中找到；
- 支持 OSDI `pre_osdi` 的 ngspice 可在 `PATH` 中找到。

```bash
PYTHONPATH=. python examples/compiler_smoke.py
```

该示例会生成 SPICE 网表、manifest、Verilog-A、OSDI 和 ngspice raw 文件。

## 阅读路线

1. 运行 [`examples/quickstart.py`](examples/quickstart.py)，先看清器件实例与节点拓扑。
2. 阅读 [`WHITEPAPER.md`](WHITEPAPER.md)，理解 relation 如何进入 MNA/DAE 与后端分类。
3. 运行 [`examples/compiler_smoke.py`](examples/compiler_smoke.py)，检查生成的网表和 Verilog-A。
4. 将 [`examples/modeling_language_v1.py`](examples/modeling_language_v1.py) 作为完整语法参考；
   其中光、热和 Controller 部分展示前端语义，不代表当前后端可执行。

## License

MIT。详见 [`LICENSE`](LICENSE)。
