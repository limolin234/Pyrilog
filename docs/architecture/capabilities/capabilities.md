# 当前能力边界

本页回答“现在能运行什么”。目标架构见 [../design/design.md](../design/design.md)，
具体 lowering 见 [../compiler/compiler.md](../compiler/compiler.md)。

## 能力矩阵

| 能力 | 前端 | 编译 | 运行 | 边界 |
| --- | --- | --- | --- | --- |
| `Device`、参数、`param`、`localparam` | 已实现 | 已实现 | 已验证 | `localparam` 只读，不进实例参数表 |
| `external` 参数 | 已实现 | 作为普通实例值编译 | 未支持交互更新 | 等待 Session/API |
| 电端口、`enode`、`|` / `|=`、GND | 已实现 | SPICE/VA | 已验证 | union-find 句柄不承诺 `is` |
| 热端口、`tnode`、AMBIENT | 已实现 | 电类比 MNA | 已验证 | 尚无自动归一化 |
| 光端口与严格二元连接 | 已实现 | 未支持 | 未支持 | 复包络 lowering 未完成 |
| 复合器件递归 | 已实现 | 原生电图保留 `.subckt`，其余展平 | 已验证 | 层次路径不混合热或 VA |
| 原生 SPICE 器件 | 已实现 | `R/C/L/V/I/E/G/D/Q` | 已验证 | 当前只发射受控参数子集 |
| relation 自动分类 | 已实现 | 精确匹配 `R/C/L/V/I/E/G` | 已验证 | 只匹配有限规范形 |
| 实数二端/多端 relation | 已实现 | 显式 VA contribution | 已验证 | 不是一般隐式 residual |
| `ddt` | 已实现 | 原生 C/L 或 VA `ddt` | 已验证 | `idt` 尚未提供 |
| `state` / `val` / `internal` | 已实现 | 明确拒绝实际内部符号 | 未支持 | 无内部变量声明和状态方程 lowering |
| `delay`、`piecewise` | 已实现 AST | 明确拒绝 | 未支持 | 尚无静态回退 |
| Controller / Feedback | 已实现 IR | 明确拒绝 | 未支持 | 无 accepted-step scheduler |
| `Output` 和单位视图 | 已实现声明 | 观测重建未实现 | 未支持 CSV | `run(output=...)` 明确拒绝 |
| Operating point | 已实现 | ngspice | 已验证 | 仅当前 Spice 目标 |
| Transient | 已实现 | ngspice `.tran` | 已验证 | 固定声明的输出步长 |
| Xyce | 目标接口已调查 | 未实现目标 | 仅手工网表对比 | 不是 Pyrilog runtime |
| 自动归一化、秩分析 | 未支持 | 未支持 | 未支持 | 当前只有量纲检查和 relation budget |
| 自研 MNA 求解器 | 未支持 | 不适用 | 未支持 | 当前复用 SPICE/VA 基础设施 |

“前端已实现”只表示能构建受类型约束的对象或 AST；不等于后端可执行。

## 已验证基准

- 全量回归快照：108 项通过。
- 原生电域：标准器件、受控源、二极管、NPN、层次 `.subckt` 和多组
  ngspice/Xyce 手工跨后端对比。
- relation 路径：自动分类、非线性二端/四端 contribution、多端手写 NPN、
  `ddt` RC 瞬态均经过 OpenVAF-reloaded/ngspice。
- 电热工作点：`1 V / 100 ohm = 10 mA`，焦耳热 `10 mW`；环境 `300 K`
  时 case 为 `301.00 K`，junction 为 `301.05 K`。

测试数量是快照，不是覆盖率声明。数值详情和工具版本见
[../../verification.md](../../verification.md)。

## 失败方式

当前编译器倾向显式失败：

- 无唯一电参考、悬浮电分量或未连接端口；
- 跨物理域误连接、跨图连接或光端口重复连接；
- 不同显式初温的热节点被理想合并；
- 参数或 relation 量纲不一致；
- 多端 flow 少定义、重复定义或循环依赖；
- 同一物理支路被多个 contribution 驱动；
- 光、feedback、`delay`、`piecewise` 或一般隐式 residual 到达不支持后端。

不支持能力不得静默降级为常数、丢弃相位、增加 `gmin` 或自动拼 RLC。

## 已知风险与技术债

- 热映射使用 `1 V = 1 K`、`1 A = 1 W`，保持方程但不保证矩阵条件数良好。
- 层次编译直接写文件，复用旧输出目录时不会清理旧版遗留 `dev/`。
- 示例 `manual_dc.cir` 含本机 OSDI 绝对路径，只是可复验记录，不是可搬移产物。
- 简化 `NPNManual` 的 DC 扫描曾触发 singular-matrix 警告，ngspice 依靠
  dynamic gmin stepping 收敛；它不证明完整 BJT 精度或普遍数值稳健性。
- `CompiledModel.run()` 会写同目录的 `.run.sp` 和 `.raw`；调用者应使用专用
  build 目录。当前实现不会递归删除调用者目录。
- 层次 `.include` 路径和 ngspice control command 路径当前不能含空白字符；
  编译或运行会明确拒绝，而不是尝试不可靠的转义。
- 光的参考频率、波长分布、宽带包络与卷积语义尚未定案，不能先写 lowering
  再倒推物理约定。

## 下一阶段门槛

光域实现前至少要确定：复包络参考面、频率/波长自变量、功率归一化、双向连接
和复数标量化的等价条件。之后再选择 Verilog-A 实数通道、SPICE 辅助网络或
自研求解切片。

Controller 和多速率调度应保持独立模块；它们不能作为“补方程”手段掩盖连续
模型欠约束。
