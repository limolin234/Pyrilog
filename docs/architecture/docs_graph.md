# Pyrilog 文档图

这里是当前实现的项目级上下文入口。文档图优先记录稳定的设计约定、
模块边界和能力状态，不代替源码、测试或人维护的 `AGENTS.md`。

## 怎样阅读

| 问题 | 入口 |
| --- | --- |
| 为什么这样设计 | [design/design.md](design/design.md) |
| 模型应该怎么写 | [language/language.md](language/language.md) |
| 电、光、热的符号和守恒约定 | [physics/physics.md](physics/physics.md) |
| Python 对象图怎样变成 SPICE/Verilog-A | [compiler/compiler.md](compiler/compiler.md) |
| 什么已实现，什么只是语法或目标 | [capabilities/capabilities.md](capabilities/capabilities.md) |
| 已核验的工具链和数值结果 | [../verification.md](../verification.md) |
| 下一版已确认语义 | [../roadmap.md](../roadmap.md) |

人类用户可先读设计哲学、语法和能力边界。修改编译器的 agent 还应读物理
约定和编译架构，再核对相关源码与测试。

## 状态标签

文档中只使用以下四种状态：

- **已实现**：当前代码和至少一条回归路径支持。
- **仅前端**：Python 可构建对象或 IR，当前后端会明确拒绝。
- **目标设计**：已有方向或契约，但尚未落地，不能当作 API 承诺。
- **未支持**：当前无可执行路径，需要新设计或新后端能力。

凡是没有状态的语法示例，只能证明 Python 表达形式，不自动证明可编译或可仿真。

## 当前基线

- 实现基线：`bb8ab46`（relation lowering 与电热 MNA 候选）。
- 可执行后端：OpenVAF-reloaded + ngspice。
- 已验证范围：原生 SPICE 电器件、关系分类、多端 Verilog-A、`ddt`、
  层次电路和集总电热网络。
- 当前全量回归：108 项。测试数量只是当前快照，能力以
  [capabilities/capabilities.md](capabilities/capabilities.md) 和测试内容为准。

## 事实源

| 内容 | 优先证据 |
| --- | --- |
| 公共语法和对象语义 | `pyrilog/model.py`、`pyrilog/expressions.py`、`tests/test_frontend.py` |
| 单位和量纲 | `pyrilog/units.py`、单位回归 |
| 标准电器件 | `pyrilog/devices.py` |
| lowering 与能力拒绝 | `pyrilog/simulation/compiler.py`、编译回归 |
| 电热映射 | `tests/test_electrothermal.py`、生成网表和工作点 |
| 历史思路、否定路线和待决问题 | `deepseek-context-agent/deepseek_context.md` |

`README.md`、`modeling_language_v1.md` 和 `compiler_lowering.md` 仍可作为历史长文材料，
但其中部分热语义和能力状态已过时。在它们完成单独重写前，当前状态以
本文档图和源码测试为准。

## 维护规则

1. 语法或物理约定变更时，更新 `language` 或 `physics`，并补对应测试。
2. lowering 或后端能力变更时，更新 `compiler` 和 `capabilities`。
3. 跨模块不变量或取舍变更时，才更新 `design`。
4. 可追溯验证结果进入 `docs/verification.md`，不与目标设计混写。
5. 未实现但已确认的接口方向进入 `docs/roadmap.md`。
6. 不为尚未实现的模块预建空文档。
