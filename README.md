# Pyrilog 1.1.0

Pyrilog 是一个以 Python 为宿主语言的类型化 relation 建模与编译前端。模型作者
声明器件端口、参数、局部关系和层次拓扑；编译器将当前支持的电热子集精确降低为
原生 SPICE 或 Verilog-A，并保留可审计的 manifest。

```text
Python Device + relation + typed topology
                  |
                  v
       validate, flatten or keep hierarchy
             /                    \
            v                      v
    native SPICE              generated Verilog-A
             \                    /
              v                  v
                OpenVAF + ngspice
```

Pyrilog 是多保真仿真研究的基础设施，不是研究贡献本身。长期目标是让同一个带
物理语义的模型产生快速降阶版本和高保真 reference 版本，并追踪适用域、误差与
升级原因。1.1.0 尚未实现自动降阶、误差传播或 reference 调度，不应据此宣称
仿真结果已经具有工程置信度。

## 1.1.0 更新

- 电、热保守节点支持任意 `|` 链、后续 `|=` 和 node-node union。
- 显式 `tnode(C=...)`、热容、功率注入、固定温度和集总电热 MNA 已可执行。
- 自定义 relation 可严格匹配原生 `R/C/L/V/I/E/G`。
- 标准库支持原生 `R/C/L/V/I/E/G/D/Q`。
- 多端电流 relation 可生成 Verilog-A contribution。
- 原生电复合图可保留为 SPICE `.subckt`；其他受支持模型走 flat lowering。
- `localparam`、稳定实例 ID、节点映射和热 lump 信息进入 manifest。
- 当前全量回归快照为 108 项。

## 快速开始

需要 Python 3.11 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python examples/quickstart.py
```

`quickstart.py` 只生成原生 SPICE 网表和 manifest，不要求外部仿真器。

```python
from pyrilog import Circuit, V, kohm
from pyrilog.devices import Resistor, VoltageSource


with Circuit() as circuit:
    source = VoltageSource(dc=1 * V)
    upper = Resistor(resistance=1 * kohm)
    lower = Resistor(resistance=1 * kohm)

    source.p | upper.p
    midpoint = upper.n | lower.p
    circuit.GND |= source.n | lower.n
```

`|` 返回节点对象，`|=` 扩展或合并保守节点。`Circuit.GND` 和
`Circuit.AMBIENT` 是惰性、每图独立的内置边界。

## 自定义器件

```python
from pyrilog import A, V, Device, eport


class Conductance(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * A / V

    relation = (
        p.i + n.i == 0,
        p.i == conductance * (p.v - n.v),
    )
```

器件类只声明接口、参数和关系。自动注册、稳定命名、拓扑检查、量纲检查和后端
选择由框架完成。当前版本仍要求模型显式写出器件端口 flow 守恒；下一版的自动
闭合规则见 [docs/roadmap.md](docs/roadmap.md)。

## 当前能力

| 能力 | 1.1.0 状态 |
| --- | --- |
| `Device`、参数反射、`param`、`localparam`、局部 relation | 已实现 |
| 电/热类型拓扑、union-find 连接、GND/AMBIENT | 已实现 |
| Pint 单位、范围和 relation 量纲检查 | 已实现 |
| 原生 SPICE `R/C/L/V/I/E/G/D/Q` | 已实现 |
| relation 自动分类 `R/C/L/V/I/E/G` | 已实现 |
| 二端和多端显式实数 relation 到 Verilog-A | 已实现 |
| `ddt(...)`、OpenVAF OSDI、ngspice OP/transient | 已验证 |
| 纯原生电复合图到 `.subckt` | 已实现 |
| 集总热节点与电热 MNA | 已实现并验证 |
| 光端口和复表达式 | 仅前端；lowering 未实现 |
| `state`、`delay`、`piecewise` | 仅前端；后端明确拒绝 |
| Controller/Feedback、Output 重建、interactive Session | 仅前端或未实现 |
| 自动归一化、一般秩分析、通用隐式 residual | 未实现 |
| 自动降阶、误差传播、reference 升级 | 研究路线，未实现 |

编译器对不支持能力抛出 `BackendCapabilityError`，不静默插入寄生、丢弃相位
或生成未经说明的近似。

## 验证

```bash
python -m unittest discover -s tests -q
PYTHONPATH=. python examples/compiler_smoke.py
```

完整端到端测试需要：

- `openvaf-r` 可在 `PATH` 中找到；
- 支持 OSDI `pre_osdi` 的 ngspice；
- 电域跨后端测试还会在本机存在 Xyce 时运行。

工具链、数值基准和验证边界见 [docs/verification.md](docs/verification.md)。

## 阅读路线

1. [WHITEPAPER.md](WHITEPAPER.md)：项目边界和编译方法。
2. [docs/architecture/docs_graph.md](docs/architecture/docs_graph.md)：按设计、语法、
   物理、编译和能力状态阅读。
3. [examples/modeling_language_v1.py](examples/modeling_language_v1.py)：下一版候选语法，
   明确不是当前可运行示例。
4. [docs/roadmap.md](docs/roadmap.md)：已确认但尚未实现的接口收敛方向。
5. [source/](source/)：EPHIC、CMT 和 Verilog-A 紧凑模型研究材料。

历史文档保存在 `docs/archive/`，其中部分语义已经过时，不是当前能力真相源。

## License

MIT。详见 [LICENSE](LICENSE)。
