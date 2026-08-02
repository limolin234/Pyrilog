# 验证范围

## 当前快照

- Python 回归：108 项。
- 本机工具链：OpenVAF-reloaded、ngspice 46；纯原生 SPICE 基准另与 Xyce 7.10
  做工作点比较。
- 自定义 relation 已验证原生分类、非线性 Verilog-A、多端手写 NPN 和 `ddt`
  transient。
- 电热工作点验证：`1 V / 100 ohm = 10 mA`，焦耳热 `10 mW`；环境 `300 K`
  时 case 为 `301.00 K`，junction 为 `301.05 K`。

## 证据入口

| 内容 | 入口 |
| --- | --- |
| 前端和类型拓扑 | `tests/test_frontend.py` |
| relation 分类和 Verilog-A | `tests/test_automatic_lowering.py` |
| 层次 SPICE | `tests/test_hierarchical_spice.py` |
| 电热 MNA | `tests/test_electrothermal.py` |
| ngspice/Xyce 电域对比 | `tests/test_electrical_validation.py` |
| 可审计生成物 | `examples/generated/` |

## 不代表什么

通过回归不代表任意模型、后端或参数范围都正确。跨后端一致也不是独立物理 oracle；
测试额外检查有限值和预期工作范围，但仍可能存在共享建模错误。

简化 `NPNManual` 的 DC smoke 曾触发 singular-matrix 警告并依靠 ngspice dynamic
gmin stepping 收敛，因此它只验证 lowering 可执行，不证明完整 BJT 等价或数值稳健。

示例 `manual_dc.cir` 含本机 OSDI 绝对路径，不是可搬移测试台。层次 `.include`
和 ngspice control command 路径当前也不能含空白字符。
