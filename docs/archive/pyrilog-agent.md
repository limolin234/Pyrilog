# Pyrilog Agent：阶段进展、技术路线与下一步

> 面向导师/项目组的阶段汇报材料
> 更新时间：2026-08-01

## 0. 汇报摘要

Pyrilog 的目标是构建一套基于 Python 的 relation-based 层次建模语言，用统一的方式描述电、光和集总热系统，并将模型编译成可审计的 SPICE、Verilog-A 和后端运行产物。项目不从头实现工业级数值求解器，而是复用 ngspice、Xyce 等成熟 MNA/DAE 后端，把主要工作集中在建模语言、物理语义、编译 lowering、产物映射和验证体系上。

当前已经打通以电学为主的第一个可执行闭环：

```text
Python Device / relation
  -> 类型化拓扑与表达式图
  -> 原生 SPICE primitive 或自动生成 Verilog-A
  -> OpenVAF-reloaded 编译为 OSDI
  -> ngspice 仿真
  -> manifest、raw 数据与可审计生成物
```

本阶段的关键进展如下：

1. 前端已经能够表达端口、参数、局部参数、内部变量、关系、单位、节点、复合器件和层次拓扑。
2. 编译器能够严格识别可等价转换的 `R/C/L/V/I/E/G` relation，并生成原生 SPICE；其他受支持的显式连续关系自动回退为 Verilog-A。
3. 已支持二端和多端电学 contribution、`ddt(...)` 动态表达式、OpenVAF/OSDI 构建及 ngspice 运行。
4. 已能发布原生 SPICE 层次 `.subckt`、顶层网表和 manifest，保留稳定实例 ID 与源层次信息。
5. 已完成 ngspice/Xyce 电学交叉验证；本轮实测最大后端差异为 **0.598 mV**。
6. 已完成一个三晶体管闭环跟随器的 201 点 DC relation-lowering smoke test，验证多端非线性 relation 到 Verilog-A 的端到端链路。

当前能力边界也比较明确：**热、光、离散控制、统一 Output/Session 和通用 SystemIR 尚未形成完整后端闭环；层次 SPICE 当前只接受原生 primitive。** 对不支持的模型，编译器应显式抛出 `BackendCapabilityError`，而不是生成未经说明的近似模型。

---

## 1. 项目目标与问题定义

### 1.1 项目目标

Pyrilog 面向光、电、热耦合行为级建模，核心目标是：

- 使用 Python 完成器件实例化、参数配置、拓扑构造和批量扫描；
- 使用 relation 描述器件局部物理关系，而不是要求用户手写网表；
- 为电、光、热分别建立符合其物理特性的端口和连接语义；
- 将对象图编译为可验证、可映射、可审计的中间表示和后端产物；
- 复用成熟 SPICE/Verilog-A 后端的 MNA、隐式积分与 Newton 求解能力；
- 在后端能力不足时明确报错，不进行静默近似。

### 1.2 为什么采用 Python + SPICE/Verilog-A

SPICE 和 Verilog-A 已经具备成熟的电路装配、非线性工作点和瞬态求解能力，但直接编写大型层次模型时存在以下问题：

- 网表和行为模型语法不利于复杂参数化与批量结构生成；
- 用户需要手工维护实例名、节点名、模型文件和后端路径；
- 光学复数行波、热网络和跨域耦合缺少统一的高层抽象；
- 生成物、源对象和仿真输出之间缺乏稳定映射；
- 不同后端对同一模型的能力和 ABI 并不完全一致。

Pyrilog 因此将 Python 定位为 **elaboration language**：Python 负责构造模型，但不把任意 Python 执行过程当作数值求解语义。正式的实例身份、物理关系和仿真行为由框架的对象图、稳定 ID、IR 和 lowering 规则决定。

### 1.3 当前阶段边界

当前阶段的主要目标不是一次性完成完整光电热平台，而是验证一条可扩展的最小编译链：

1. 前端语法能够构造可信的类型化模型；
2. 电学关系能够自动选择原生 SPICE 或 Verilog-A；
3. 生成物能够被 OpenVAF/ngspice 实际运行；
4. 编译过程和后端结果能够被测试与审计；
5. 未实现能力有明确边界，为后续热、光和控制扩展保留架构位置。

---

## 2. 状态标记与事实边界

本文使用四类状态，避免把设计讨论误写成实现成果：

| 状态 | 含义 |
| --- | --- |
| **已实现并验证** | 已有代码，并有回归测试、生成物检查或后端运行证据 |
| **已实现但待扩大验证** | 主链路已存在，但测试范围、数值鲁棒性或工程化程度仍有限 |
| **前端已有／后端未实现** | 可以构造对象或表达式，但编译器尚不能完成 lowering 和仿真 |
| **设计路线** | 来自架构讨论和 DeepContext 研究记录，尚不能当作当前能力 |

`deepseek-context-agent/deepseek_context.md` 在本文中只作为**设计参考与经验记录**。其中包含候选方案、否定路线、风险和开放问题；当前实现状态仍以源码、测试、示例和可复现产物为准。

---

## 3. 语言基本构成

Pyrilog 的语言结构沿用以下主线：

```text
物理抽象
  -> Device / 复合器件
  -> Circuit / 层次拓扑
  -> Build / lowering 与产物发布
  -> Sim / 分析、执行与结果
```

### 3.1 总体架构

```text
Pint units
  -> symbolic expressions / relations
  -> Device 与 Circuit 对象图
  -> 拓扑、量纲和能力检查
  -> flatten 或 hierarchical lowering
  -> native SPICE / generated Verilog-A
  -> OpenVAF-reloaded / ngspice
  -> manifest / raw / CSV / 可审计文件
```

这一架构中，各层职责如下：

- **单位层**：提供 SI 量纲、参数范围和单位转换检查；
- **表达式层**：构造 relation、`ddt`、内部变量和端口量表达式；
- **模型层**：管理 Device、端口、参数、节点、复合层次和稳定 ID；
- **编译层**：验证对象图，选择原生 primitive 或 Verilog-A lowering；
- **仿真层**：构建模型、运行分析，并将后端结果映射回 Pyrilog 对象。

---

## 4. 物理抽象

### 4.1 电

**状态：已实现并验证。**

当前电学抽象使用 across/flow 形式：

- 节点电势为电压 `V`；
- 端口流量为进入器件的电流 `I`；
- 同一电节点共享电压；
- 节点连接通过 KCL 满足流量守恒；
- 用户可以通过端口 `.i` 和 `.o` 查看流入、流出方向，但物理端口本身不被硬编码为 input/output。

已支持的主要能力包括：

- `eport()` 电端口；
- `enode()` 与 `|`、`|=` 连接语法；
- 惰性创建的 `Circuit.GND`；
- 标准 `R/C/L/V/I/E/G/Q` 器件；
- relation 到原生 `R/C/L/V/I/E/G` 的严格匹配；
- 不满足原生匹配条件的显式连续 relation 转为 Verilog-A；
- 二端和多端电流、电压 contribution；
- 工作点和 `ddt` 瞬态链路。

### 4.2 光

**状态：前端已有／后端未实现。**

光端口的目标不是复制电节点语义，而是表示 incident/outgoing 双向复行波。当前设计原则为：

- 光连接是严格二元、双向的 travelling-wave 连接；
- input、output、through、drop 更适合作为可读端口名，而不是阻止反射的硬类型；
- 复数变量在后端 lowering 时需要转换为实部/虚部标量；
- 第一阶段应先支持单复数窄带行波，波长作为参数；
- 多波长结构优先由 Python 显式展开，不静默引入未经验证的连续光谱积分。

目前可以构造部分光学前端语义，但尚未完成复数标量化、光连接方程装配和 SPICE/Verilog-A 后端闭环。因此本文不把光学列为已仿真能力。

### 4.3 热

**状态：前端已有／后端未实现。**

热端口采用与电学类似的守恒结构：

- potential 为温度 `T`；
- flow 为进入器件或热网络的热功率 `P`；
- 同一热节点共享温度并满足功率守恒；
- `Circuit.AMBIENT` 用于表达环境参考温度；
- 参数 `C: J/K` 已可在前端生成相关温度、功率和热容关系。

后续热学架构不应把温度简化为一个全局参数。更合理的方向是：

- 耗散器件从 ThermalPort 读取局部温度并注入热功率；
- 独立 ThermalNetwork 负责热传输、环境边界和串扰；
- 热网络可依次支持局部 RC、稀疏 RC、静态热阻矩阵、状态空间/ROM 和 FEM adapter；
- 在与电光反馈耦合前，先验证热模型稳定性、无源性、正热阻和物理阶跃响应。

这些内容目前属于后续 lowering 路线，而不是已完成的后端功能。

---

## 5. Device：器件与局部关系

### 5.1 当前建模方式

**状态：已实现并验证。**

Device 类负责声明：

- 电、光、热端口；
- 可配置参数 `param`；
- 只在模型内部使用的 `localparam`；
- 内部状态或中间量；
- 器件局部 relation；
- 可选的复合器件子结构。

例如，一个电导器件只需要表达端口和局部守恒关系：

```python
class Conductance(Device):
    p = eport()
    n = eport()
    conductance = 1e-3 * (u.A / u.V)

    relation = (
        p.i + n.i == 0,
        p.i == conductance * (p.v - n.v),
    )
```

框架负责对象注册、稳定名称、参数量纲、拓扑检查、后端节点名和模型生成。

### 5.2 原生 primitive 与 relation fallback

编译器不会仅凭器件类名猜测后端器件。当前策略是：

1. 标准器件可通过保留元数据明确选择后端 primitive；
2. 用户 relation 只有在编译器能严格证明等价时，才转换为原生 `R/C/L/V/I/E/G`；
3. 其他受支持的显式连续表达式生成 Verilog-A contribution；
4. 隐式残差、重复驱动同一物理支路、关系数量异常等情况显式拒绝。

这种策略在性能与正确性之间保持了明确边界：简单模型优先使用原生 SPICE，复杂模型进入 Verilog-A，但不把任意 relation 猜测成 RLC 或受控源。

### 5.3 参数与局部参数

**状态：已实现并验证。**

- 外部参数进入实例配置和 manifest；
- `localparam` 会生成 Verilog-A 的 `localparam real`；
- 局部参数不会被错误暴露为后端可配置实例参数；
- 参数值携带单位和量纲信息；
- 编译器对无效常量运算和不合法原生参数进行显式检查。

### 5.4 复合器件

**状态：已实现但待扩大验证。**

复合器件能够：

- 包含独立的子器件、内部节点和边界端口；
- 为每个实例创建隔离的内部对象；
- 通过稳定 ID 保留 `u1.child` 形式的源层次；
- 在扁平后端中映射到全局电学拓扑；
- 在原生层次路径中发布独立 `.subckt`。

当前主要限制是：层次发布路径只支持能够原生 lowering 的 SPICE 器件，尚不能在 `.subckt` 内自动混用生成的 Verilog-A 模型。

---

## 6. Circuit：网络与层次拓扑

### 6.1 对象注册与拓扑构造

**状态：已实现并验证。**

`with Circuit()` 提供构造作用域，器件可以自动注册到当前电路。该作用域只负责 elaboration，不决定对象生命周期。

Python 变量名只是对象句柄，不作为正式实例身份。框架负责分配稳定 ID，以避免循环、条件分支、别名或交互环境导致实例命名不稳定。

### 6.2 节点与参考边界

当前已支持：

- 电节点和多端连接；
- 匿名二端连接；
- 惰性 `Circuit.GND`；
- 惰性 `Circuit.AMBIENT` 前端语义；
- 端口域、连接状态、参考节点数量等拓扑检查；
- 编译完成后冻结图状态，避免已发布模型被继续修改。

### 6.3 类型化层次图

Pyrilog 保留类型化、层次化对象图。后端求解时可以展平电学拓扑，但必须保留：

- 源器件和实例层次；
- 稳定 ID；
- Pyrilog 节点到后端节点的映射；
- 参数和 lowering 选择；
- 生成模型的来源。

这也是 manifest 的主要作用：让生成的 SPICE/Verilog-A 不成为无法追踪来源的黑盒文本。

---

## 7. Build：编译、lowering 与产物发布

### 7.1 当前编译分流

**状态：已实现并验证。**

`compile_circuit()` 当前按图能力选择两条路径：

```text
适合原生层次发布
  -> hierarchical SPICE
  -> 顶层 .cir + subckt/*.cir + manifest

其他受支持电学图
  -> flatten
  -> relation classification
     -> native SPICE
     -> generated Verilog-A
  -> .cir + .va + manifest
```

扁平路径的核心步骤为：

1. 展平复合电学图，同时保留层次 ID；
2. 检查端口连接、参考节点、关系数量和能力边界；
3. 对每个 Device 尝试 native relation matching；
4. 对不能原生匹配但受支持的连续表达式生成 Verilog-A；
5. 写入网表、模型源文件和 manifest；
6. 需要时使用 OpenVAF-reloaded 构建 `.osdi`；
7. 由 ngspice 通过 `pre_osdi` 加载模型。

### 7.2 可审计构建产物

当前构建链可产生：

```text
model.sp / compiled.cir
model.manifest.json / compiled.manifest.json
verilog_a/<module>.va
verilog_a/<module>.osdi
subckt/<instance>.cir
raw / CSV / summary（由分析或示例生成）
```

manifest 记录：

- 仿真器和网表路径；
- Pyrilog 稳定节点 ID 到后端节点名的映射；
- 实例稳定 ID、后端实例名和源类；
- 端口连接和参数；
- lowering 类型及选择依据；
- Verilog-A 源文件、OSDI 文件和源哈希；
- 运行所需后端能力。

### 7.3 当前构建限制

| 限制 | 当前行为 | 影响 |
| --- | --- | --- |
| 层次内出现非原生 relation | 抛出 `BackendCapabilityError` | 层次与 Verilog-A 尚不能组合 |
| 热、光或 feedback 进入当前后端 | 显式拒绝 | 尚不能运行完整光电热闭环 |
| ngspice 控制路径包含空白字符 | 当前拒绝 | 影响部分目录下的可搬移性 |
| OSDI ABI | 仅作为 ngspice/OpenVAF 路径 | 不能宣称是所有仿真器通用模型二进制 |
| 复用旧输出目录 | 需进一步规范清理策略 | 可能混入旧版生成物 |

---

## 8. Sim：仿真与结果验证

### 8.1 当前可执行分析

**状态：已实现并验证。**

当前可执行适配器以 ngspice 为主，已验证：

- Operating Point；
- 原生电学 primitive；
- 生成 Verilog-A/OSDI 模型；
- 含 `ddt` 的瞬态链路；
- 层次 `.subckt` 的工作点运行；
- 手工分析 deck 驱动的 DC sweep。

Xyce 当前主要用于独立基准网表的交叉验证，还不是 Pyrilog 完整可执行编译目标。OpenVAF 生成的 OSDI 也不应被描述为 Xyce 或 Spectre 的通用 ABI。

### 8.2 Controller、Session 与 Output

**状态：前端已有／后端未实现。**

当前已有部分 Controller、feedback、`output()` 和 `delay` 的语法或前端契约，但尚未形成完整调度与结果重构。

后续应遵循：

- 连续控制方程可以进入统一 DAE；
- 采样控制、状态机和任意 Python 状态逻辑必须运行在 accepted-step 边界；
- 不允许在 Newton 残差试探中修改控制器状态；
- Session 需要明确 `advance/read/write/checkpoint/restore` 等事务语义；
- Output 应通过稳定 SignalRef 和 manifest 映射结果，而不是要求用户直接填写后端 vector 名称；
- 输出采样间隔与求解器内部步长必须分离。

---

## 9. 阶段成果与验证数据

### 9.1 本轮已实测的回归模块

| 验证模块 | 本轮结果 | 主要覆盖 |
| --- | ---: | --- |
| `tests/test_automatic_lowering.py` | **12/12 通过** | native 分类、Verilog-A fallback、localparam、错误拒绝、实际 OP |
| `tests/test_hierarchical_spice.py` | **1/1 通过** | `.subckt` 发布、manifest、ngspice OP |
| `tests/test_electrical_validation.py` | **2/2 通过** | 非有限值保护、ngspice/Xyce 基准对照 |

本轮针对上述三个核心模块共执行 **15 项测试，15 项通过**。本轮调研没有重新运行完整测试集，因此本文不将局部结果表述为“全量测试全部通过”。

### 9.2 自动 relation lowering

已验证：

- 二端 relation 自动识别 `R/C/L/V/I`；
- 四端 relation 自动识别 `E/G`；
- 非线性四端关系生成 Verilog-A；
- 参数函数表达式可以回退到 Verilog-A；
- `localparam` 正确发射并从实例参数中隐藏；
- 关系数量超额、常量计算失败、重复驱动物理支路会在发布产物前报错；
- manifest 能区分 `relation_match` 与 `relation_fallback`。

### 9.3 ngspice/Xyce 电学交叉验证

交叉验证覆盖：

- BJT 偏置；
- 差分对；
- 晶体管闭环放大器；
- VCVS 反相电路；
- VCVS 双输入求和电路。

验证不是只比较两个后端，而是按以下顺序执行：

1. 检查结果为有限值；
2. 检查独立的预期物理范围；
3. 再比较 ngspice 与 Xyce 的差异和容差。

本轮实测的最大后端差异为 **0.000598 V，即 0.598 mV**，出现在 BJT 偏置基准；其余案例差异约为 `2.3e-5 V` 到 `5e-10 V`。

该结果说明当前基准范围内的网表、方向约定和数值结果具有较好一致性，但不能据此宣称所有模型、工作区间或仿真器都绝对等价。

### 9.4 三晶体管闭环 DC smoke test

`examples/opamp_spice_test.py` 使用三个完全由局部 relation 描述的 `NPNManual`，构建两级闭环电压跟随器。其作用是验证：

```text
多端非线性 relation
  -> Verilog-A contribution
  -> OpenVAF/OSDI
  -> ngspice DC sweep
  -> raw / CSV / summary / 图形产物
```

已生成结果：

| 指标 | 结果 |
| --- | ---: |
| 扫描范围 | `-0.2 V` 至 `0.2 V` |
| 扫描点数 | **201** |
| `0.09–0.11 V` 局部闭环增益 | **0.94333** |
| `0.1 V` 输入对应输出 | **0.13215 V** |
| 工作点跟踪误差 | **32.15 mV** |
| 局部最大跟踪误差 | **32.72 mV** |
| 局部传输单调性 | **通过** |
| smoke 误差阈值 | **40 mV** |

需要强调：

- 这是 **relation-lowering DC smoke test**，不是精密电压跟随器指标验证；
- `NPNManual` 是最小 Ebers-Moll 风格关系，不等价于完整工业 BJT 模型；
- 最大误差距离 40 mV 阈值仅约 **7.3 mV**，回归裕量有限；
- 该示例目前尚缺独立的专门测试入口；
- 数值收敛和模型精度仍需进一步增强，不能把一次收敛等同于完整数值鲁棒性。

---

## 10. 当前限制、风险与应对

| 限制或风险 | 影响 | 当前证据 | 建议应对 |
| --- | --- | --- | --- |
| 层次 lowering 只接受 native SPICE | 复杂 relation 不能直接放入层次 `.subckt` 链 | 编译器对此显式报错 | 增加层次模型收集、Verilog-A 构建和 OSDI 加载 |
| 热、光、Controller 尚未 lowering | 尚不能完成完整光电热闭环 | README 能力矩阵和 compiler capability gate | 按热网络、光实虚标量化、accepted-step 控制分阶段实现 |
| 运放 smoke 缺专门回归 | 生成物和数值指标可能发生未检测漂移 | 当前只有示例自校验 | 新增测试并分别记录编译正确性与模型性能阈值 |
| 运放误差阈值裕量较小 | 环境或模型变化可能触发临界失败 | 最大误差 32.72 mV，对比阈值 40 mV | 优化测试电路，并避免用宽阈值掩盖模型退化 |
| 完整测试集本轮未重跑 | 不能确认其他模块与当前改动完全兼容 | 本轮只运行三个核心模块 | 整理后运行完整 `unittest discover` |
| 生成物与源码混杂 | 评审、提交和复现边界不清晰 | 工作区含 raw、CSV、PNG、OSDI 等 | 制定文本 golden、二进制 artifact 和临时构建物策略 |
| 输出目录清理契约不完整 | 旧 `dev/`、`subckt/` 或模型文件可能残留 | 已有旧目录遗留风险记录 | 使用受控 staging 目录和原子发布清单 |
| 路径空白与绝对 OSDI 路径 | 影响目录迁移和跨环境复现 | ngspice 控制命令存在路径限制 | 统一路径转义、相对路径与运行目录契约 |
| 跨后端结果一致不等于物理正确 | 两个后端可能共享同一模型错误 | 当前已有范围检查但覆盖仍有限 | 增加解析解、守恒、corner、scale 和独立参考模型 |

---

## 11. Pyrilog Agent 工作方式

这里的“Pyrilog Agent”首先指一套面向项目研发和汇报的上下文工作方法，而不是宣称当前已经完成一个全自治建模 Agent。

该方法参考 DeepContext 的核心经验，但将事实源固定在 Pyrilog 代码、测试和生成物上。

### 11.1 上下文分层

Agent 在处理项目任务时，应区分：

- **事实**：源码、测试结果、生成物和环境实测；
- **决策**：已经确认的接口和架构选择；
- **推断**：根据现有实现提出的解释或建议；
- **风险**：负例、失败路线和已知技术债；
- **开放问题**：尚未决定或尚未验证的方案。

汇报时不应把推断、候选方案或 DeepContext 历史记录直接改写为“项目已经支持”。

### 11.2 建议工作流

```text
读取事实与当前能力矩阵
  -> 定位对象图、编译路径和相关测试
  -> 明确本任务的后端能力边界
  -> 提出设计或修改
  -> 生成可审计产物
  -> 运行分层验证
  -> 更新状态、风险和下一步
```

### 11.3 产物审计原则

每次编译或 Agent 修改都应尽可能保留：

- 源 Device、Circuit 和 relation；
- 稳定 ID 与层次结构；
- lowering 选择原因；
- SPICE 网表；
- Verilog-A 源文件；
- manifest 和 source mapping；
- 后端日志、结果文件和容差；
- 未支持能力的明确错误。

不能只以“后端运行成功”作为完成标准，因为运行成功并不能证明 relation、单位、方向、参数映射和物理范围正确。

### 11.4 分层验证顺序

建议统一采用以下验证阶梯：

1. **语义检查**：所有权、端口域、连接基数、参考节点；
2. **单位检查**：参数、relation 和输出量纲；
3. **结构检查**：关系数量、重复支路贡献、稳定 ID 和 source map；
4. **解析小模型**：用可手算结果验证符号、方向和数值；
5. **物理不变量**：KCL、功率/热流守恒、有限值、单调性；
6. **参数 corner 与尺度**：检查极端值、零值和数值归一化；
7. **单后端 smoke**：确认生成物可编译、可加载、可运行；
8. **跨后端差分**：在各后端独立通过功能范围后再比较结果；
9. **明确范围与容差**：不把局部通过扩大成全模型等价结论。

### 11.5 不做静默近似

Pyrilog Agent 和编译器都应坚持：

- 只对严格识别的 relation 使用原生 primitive；
- 不把未知动态关系猜成 R、C 或 L；
- 不把采样控制塞进 Newton 回调；
- 不把光谱分布静默积分为单个标量；
- 不把 OpenVAF OSDI 当作通用仿真器 ABI；
- 不在缺少后端能力时生成看似可运行但物理语义不明确的模型。

---

## 12. 下一阶段计划与验收指标

### 12.1 近期：稳定当前电学编译切片

**目标：使当前成果可重复、可评审、可作为后续架构基线。**

任务：

1. 整理源码、测试、文档和生成物的提交边界；
2. 为运放 DC smoke 增加专门回归；
3. 运行完整测试集并记录环境和结果；
4. 规范 staging、旧产物清理、相对路径和原子发布；
5. 在公开文档中明确层次路径只支持 native SPICE；
6. 将编译正确性阈值与示例模型性能指标分开。

验收指标：

- 完整测试集通过，失败和跳过项有清单；
- 运放示例的 201 点、有限性、单调性、lowering 类型和误差门槛进入回归；
- 从干净目录可以重复生成相同文本产物；
- manifest 中不存在意外的环境绝对路径；
- 编译失败不留下可被误认为成功结果的半成品。

### 12.2 中期：稳定 IR 与通用 lowering pass

**目标：将当前集中在 compiler 中的职责拆分为可复用、可测试的阶段。**

建议 pass：

```text
对象图冻结
  -> 层次与边界展开
  -> 拓扑/单位检查
  -> relation 分类
  -> 复数标量化
  -> 参数与残差归一化
  -> 结构诊断
  -> native / Verilog-A 选择
  -> manifest 与 source map 发布
```

验收指标：

- 后端不直接依赖可变 Python Device 对象；
- IR 固化变量、参数、关系、层次、观测量和来源；
- 每个 pass 有独立输入、输出和单元测试；
- 扁平与层次路径复用同一关系分类和模型收集逻辑；
- 层次 `.subckt` 可以引用生成的 Verilog-A 模型。

### 12.3 中期：最小热网络闭环

**目标：完成第一条可验证的电热耦合链。**

首批能力：

- 热端口和热节点；
- 环境边界；
- 热阻、热容和一阶 RC transient；
- 器件耗散功率注入；
- 局部温度反馈到电学参数。

验收指标：

- 热节点满足功率守恒；
- 正热阻和正热容检查；
- 一阶阶跃响应与解析解一致；
- 电热闭环结果有限、稳定且可复现；
- manifest 能映射热变量和观测量。

### 12.4 后期：光学复包络与多物理耦合

**目标：建立最小、明确、可验证的光学语义。**

首批能力：

- 严格二元双向光连接；
- 单复数 travelling-wave；
- 实部/虚部标量化；
- 波长作为参数；
- Python 显式展开多波长；
- 简单传播、耦合和电光转换模型。

验收指标：

- 复数标量化前后结果一致；
- 端口方向与反射关系有解析测试；
- 无源模型满足功率守恒或声明的损耗范围；
- 不支持的光谱算子明确报错。

### 12.5 后期：Controller、Session 与 Output

**目标：支持可交互、可采样、后端无关的闭环仿真。**

验收指标：

- accepted-step、sample、hold 的时序契约明确；
- Newton 试探过程不修改离散状态；
- Session 支持推进、读取、写入、检查点和恢复；
- SignalRef 可以稳定映射到不同后端 probe；
- Output 的显示单位和采样网格不改变求解语义；
- 同一控制器在支持的后端上具有声明容差内的一致结果。

---

## 13. 当前需讨论或决策的问题

建议导师/项目组重点确认以下问题：

1. **近期优先级**：先完成电学编译器工程化，还是立即进入最小热网络？
2. **IR 边界**：下一阶段是否正式冻结 backend-neutral SystemIR，避免后端继续直接读取 Device 对象？
3. **层次目标**：是否将“层次 `.subckt` + generated Verilog-A”作为下一里程碑的硬要求？
4. **验证目标**：当前主要面向语言和编译正确性，还是同步建立器件模型精度指标？
5. **生成物策略**：哪些文本产物作为 golden 纳入版本控制，哪些 raw/OSDI/图片只作为 CI artifact？
6. **后端顺序**：在 ngspice 之后，优先建设 Xyce 可执行适配、Spectre package，还是先完善独立 SystemIR？
7. **光学首版范围**：是否确认“单复数窄带、波长参数化、Python 显式展开多波长”，暂不实现连续光谱积分？

---

## 14. 证据与复现入口

### 14.1 关键文档

本节链接已按当前 `pyrilog/` 子仓库布局保留；本文其余内容是历史工作记录，
不代表当前 API。

- [`pyrilog.md`](pyrilog.md)：原始项目目标和章节骨架；
- [`../../README.md`](../../README.md)：当前能力矩阵、运行入口和项目边界；
- [`modeling_language_v1.md`](modeling_language_v1.md)：语言和编译契约；
- [`compiler_lowering.md`](compiler_lowering.md)：lowering 目标与阶段边界；
- [`../../../deepseek-context-agent/deepseek_context.md`](../../../deepseek-context-agent/deepseek_context.md)：设计参考、负例和开放问题。

### 14.2 关键源码

- [`../../pyrilog/model.py`](../../pyrilog/model.py)：Device、Circuit、端口、节点、参数和层次对象图；
- [`../../pyrilog/expressions.py`](../../pyrilog/expressions.py)：relation、内部表达式、`ddt` 和 `delay`；
- [`../../pyrilog/units.py`](../../pyrilog/units.py)：Pint 单位和量纲适配；
- [`../../pyrilog/devices.py`](../../pyrilog/devices.py)：标准电学器件；
- [`../../pyrilog/simulation/compiler.py`](../../pyrilog/simulation/compiler.py)：编译、原生匹配、Verilog-A 和层次发布。

### 14.3 关键测试与示例

- [`../../tests/test_automatic_lowering.py`](../../tests/test_automatic_lowering.py)；
- [`../../tests/test_hierarchical_spice.py`](../../tests/test_hierarchical_spice.py)；
- [`../../tests/test_electrical_validation.py`](../../tests/test_electrical_validation.py)；
- [`../../examples/automatic_lowering.py`](../../examples/automatic_lowering.py)；
- [`../../examples/electrical_validation.py`](../../examples/electrical_validation.py)；
- [`../../examples/hierarchical_spice.py`](../../examples/hierarchical_spice.py)；
- [`../../examples/opamp_spice_test.py`](../../examples/opamp_spice_test.py)；
- [`../../examples/generated/opamp_spice_test/summary.json`](../../examples/generated/opamp_spice_test/summary.json)。

### 14.4 本轮已执行的核心验证

```bash
python -m unittest tests.test_automatic_lowering -v
python -m unittest tests.test_hierarchical_spice -v
python -m unittest tests.test_electrical_validation -v
```

完整回归命令：

```bash
python -m unittest discover -s tests -v
```

---

## 附录 A：能力状态表

| 能力 | 当前状态 |
| --- | --- |
| Device 端口、参数、localparam、relation 反射 | 已实现并验证 |
| Circuit 上下文自动注册与稳定 ID | 已实现并验证 |
| Pint 单位、参数范围和量纲检查 | 已实现并验证 |
| 电节点、GND、端口流入/流出视图 | 已实现并验证 |
| 复合实例、内部节点、层次成员访问 | 已实现并验证 |
| 标准 `R/C/L/V/I/E/G/Q` 原生 SPICE | 已实现并验证 |
| 自定义 relation 自动识别 `R/C/L/V/I/E/G` | 已实现并验证 |
| 二端/多端显式 relation 到 Verilog-A | 已实现并验证 |
| `ddt` Verilog-A 与 transient 链 | 已实现并验证 |
| `.subckt` 原生层次发布 | 已实现并验证 |
| manifest 节点、实例、参数和模型映射 | 已实现并验证 |
| OpenVAF/OSDI/ngspice OP | 已实现并验证 |
| ngspice/Xyce 电学差分验证 | 已实现并验证 |
| 热节点、AMBIENT、自动热容前端语义 | 前端已有／后端未实现 |
| 光学双向复行波前端语义 | 前端已有／后端未实现 |
| 光学实部/虚部 lowering | 设计路线 |
| 热网络 RC/ROM lowering | 设计路线 |
| 层次内 generated Verilog-A | 尚未实现，显式报错 |
| `delay` 历史 | 前端契约已有／后端未实现 |
| accepted-step Controller/Session | 前端部分已有／后端未实现 |
| 统一 SignalRef/Output reconstruction | 设计路线 |
| backend-neutral SystemIR | 设计路线 |
| normalization、结构秩和 Jacobian 诊断 | 设计路线 |

## 附录 B：术语

- **MNA**：Modified Nodal Analysis，改进节点分析法。
- **DAE**：Differential-Algebraic Equation，微分代数方程。
- **Elaboration**：用 Python 实例化器件、展开结构并构造对象图的过程。
- **Relation**：器件局部物理关系表达式。
- **Lowering**：把高层对象图和 relation 转换为后端可执行形式的过程。
- **Native lowering**：转换为 SPICE 原生 primitive。
- **Verilog-A contribution**：将关系发射为 Verilog-A 电压或电流贡献。
- **Stable ID**：不依赖 Python 局部变量名的稳定对象身份。
- **Manifest**：记录源对象、后端节点、实例、参数、模型和能力的构建清单。
- **Source map**：从后端产物或结果回溯到 Pyrilog 源对象的映射。
- **OSDI**：ngspice 可加载的开放模型接口；本文中特指 OpenVAF-reloaded/ngspice 路径。
- **SystemIR**：计划中的后端无关系统中间表示。
- **Accepted-step**：求解器已经接受的时间步边界，适合提交离散控制状态。
- **SignalRef**：计划中的稳定、后端无关观测引用。
- **Complex scalarization**：将复数变量转换为实部、虚部实数变量的 lowering。
