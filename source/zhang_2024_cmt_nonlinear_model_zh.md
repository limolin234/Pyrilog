# 基于耦合模理论的电子-光子协同仿真非线性相互作用紧凑光子模型

**作者**：Siyuan Zhang、Xiaolong Fan、Nuo Chen、Ciyuan Qiu、Xingsheng Wang、Ken Xingze Wang、Jing Xu、Min Tan
**来源**：Optica Open 预印本；正式 DOI：[10.1364/OE.529044](https://doi.org/10.1364/OE.529044)

## 摘要

本文用耦合模理论建立支持四波混频（FWM）的 Verilog-A 紧凑光子模型，给出可迁移到其他非线性相互作用的建模框架。模型兼容现有 EDA 平台，可在电子-光子协同仿真中快速追踪泵浦、信号和闲频光的关键参数，不需要系统设计者直接求解复杂的 FWM 物理过程。模型不是针对某一种器件定制，而是可作为单环、双环及更复杂光子器件的基础模块。仿真结果与数值解和实验结果一致，并成功完成闭环波长锁定协同仿真。

## 1 引言与已有模型

FWM 可用于波长转换、相位共轭和光子对产生，但传统 EDA 光子模型通常只支持线性传播或单一器件。散射参数模型难以表达温度等连续状态变化；器件专用 Verilog-A 模型在器件结构改变时需要重写。本文保留 EPHIC 基础模型的基带等效和模块化思想，补充不同波长之间的交叉相位调制（XPM）与 FWM。

## 2 基带等效

光载波约为 200 THz，调制信号约 100 GHz，热扰动约 100 kHz。若直接求解，时间步长会被光载波限制。令参考角频率为 $\omega_R$，实际频率为 $\omega=\omega_R+\Delta\omega$，解析光场可写为

$$\tilde E(z,t)=e^{j\omega_Rt}\tilde E_{bb}(z,t).$$

一阶色散下 $\beta(\omega)=\beta(\omega_R)+\Delta\omega\,\partial\beta/\partial\omega|_{\omega_R}$，波导长度 $L$ 的基带输出为

$$\tilde E_{bb}(L,t)=e^{\alpha L+j\beta(\omega_R)L}\tilde E_{bb}\!\left(0,t-\frac{n_gL}{c}\right).$$

因此光载波被移到基带，参考频率只负责定义相位和延迟；不同光学通道可使用不同参考频率。

## 3 提出的 FWM 模型框架

旧框架把同向传播的所有相对频率叠加到同一复数通道，衰减、相移和延迟对所有波长相同，不能表示不同波长之间的非线性相互作用；当泵浦、信号和闲频相差数百 GHz 或数 THz 时，统一参考频率又会降低仿真速度。

本文把通道拆成 signal、pump、idler 三类。每个通道包含“理想传播部分”和“非线性相互作用部分”，分别按理论计算后叠加；泵浦通道加入自相位调制（SPM），信号和闲频通道加入 XPM；三类波分别使用有效折射率、群折射率和参考频率。简化模型主要考虑泵浦与信号的相互作用，因为信号功率通常远低于泵浦，信号之间的相互作用可忽略。需要更细模型时，可先用 FFT 分离各频率，再为每个频率增加相互作用通道。

### 3.1 简并 FWM 耦合方程

简并 FWM 满足

$$\omega_s+\omega_i=2\omega_p,$$

并选择 $\omega_{R,i}=2\omega_{R,p}-\omega_{R,s}$，使基带相对频率仍满足 $\Delta\omega_i=2\Delta\omega_p-\Delta\omega_s$。在小信号条件 $|E_p|\gg|E_s|,|E_i|$ 下，考虑线性损耗、TPA、FCA、FCD、SPM 和 XPM：

$$\frac{\partial \tilde E_p}{\partial z}=\left(-\frac{\alpha_0}{2}+j\beta(\omega_p)+j\gamma P_p\right)\tilde E_p,$$

$$\frac{\partial \tilde E_s}{\partial z}=\left(-\frac{\alpha_0}{2}+j\beta(\omega_s)+j2\gamma P_p\right)\tilde E_s+j\gamma\tilde E_p^2\tilde E_i^*,$$

$$\frac{\partial \tilde E_i}{\partial z}=\left(-\frac{\alpha_0}{2}+j\beta(\omega_i)+j2\gamma P_p\right)\tilde E_i+j\gamma\tilde E_p^2\tilde E_s^*,$$

其中 $\alpha_0=\alpha+\alpha_{TPA}+\alpha_{FCA}$，$\gamma$ 为非线性系数，$P_p$ 为泵浦功率。低功率下忽略参量增益，解析输出为

$$\tilde E_{bb,p}(L)=\tilde E_{bb,p}(0)e^{-\alpha_0L/2+j[\beta(\omega_p)+\gamma P_p]L},$$

$$\tilde E_{bb,s}(L)=\tilde E_{bb,s}(0)e^{-\alpha_0L/2+j[\beta(\omega_s)+2\gamma P_p]L},$$

$$\tilde E_{bb,i}(L)=\left[\tilde E_{bb,i}(0)+j\eta\gamma\tilde E_{bb,p}^2(0)\tilde E_{bb,s}^*(0)\right]e^{-\alpha_0L/2+j[\beta(\omega_i)+2\gamma P_p]L}.$$

效率因子 $\eta$ 由总相位失配 $\Delta k=\Delta\beta+2\gamma P_p$ 决定，$\Delta\beta=\beta_s+\beta_i-2\beta_p$；忽略高阶色散时 $\Delta\beta=\beta_2(\omega_s-\omega_p)^2$。

### 3.2 高功率模型

高功率时必须保留参量增益。泵浦仍按上式传播，信号与闲频输出分别写成

$$\tilde E_{bb,s}(L)=u(L)\tilde E_{bb,s}(0)+v(L)\tilde E_{bb,i}^*(0)e^{-\alpha_0L/2+j[\beta(\omega_s)L+\phi_{NL}L]},$$

$$\tilde E_{bb,i}(L)=u(L)\tilde E_{bb,i}(0)+v(L)\tilde E_{bb,s}^*(0)e^{-\alpha_0L/2+j[\beta(\omega_i)L+\phi_{NL}L]}.$$

其中 $u=[\cosh(g)+jq\sinh(g)/g]e^{-\alpha_0L/2}$，$v=[j\gamma \tilde E_{bb,p}^2(0)L_{eff}\sinh(g)/g]e^{-\alpha_0L/2}$，$g=\sqrt{(\gamma P_pL_{eff})^2-q^2}$，$q=\Delta\beta L/2+\gamma P_pL_{eff}$，$L_{eff}=(1-e^{-\alpha_0L})/\alpha_0$，$\phi_{NL}=\gamma P_pL_{eff}-\Delta\beta L/2$。低功率模型速度更快，高功率模型适用范围更广但计算量更大。

## 4 微环模型与验证

单环由直波导和环波导组成，用一个方向耦合器和一段直波导连接；双环由直波导、主腔、辅助腔、两个耦合器和三段直波导组成。示例参数为 Si$_3$N$_4$，$n_{eff}=1.808$、$n_g=2.17$、损耗 $0.2$ dB/cm、$\beta_2=-5000$ ps$^2$/km、$\gamma=1$ W$^{-1}$m$^{-1}$。单环周长约 1200.22 $\mu$m，双环周长分别为 1204.51 和 602.25 $\mu$m。

模型用 Verilog-A 连接耦合器和波导，分别扫描信号失谐下的转换效率与参量增益。结果与逐圈求解的全图耦合非线性薛定谔方程一致；双环在耦合率大于本征损耗时，谐振峰中心出现窄凹口，这不是模型误差而是主辅腔耦合的物理结果。实验中单环泵浦 21.5 dBm、信号 0.5 dBm 时，实测闲频转换效率 3.36 dB、信号增益 4.72 dB，模型分别为 5.24 dB 和 5.36 dB；差异归因于工艺偏差和级联 FWM。双环实验的模型转换效率为 -59.09 dB，实测为 -57.60 dB。

## 5 闭环波长锁定协同仿真

为了保持高转换效率，泵浦和信号波长都必须与高 Q 微环谐振对准。系统包含两个不同时开启的反馈环：谐振波长调谐环和激光波长调谐环。初始化时先启用谐振调谐环，TIA 采样经状态诊断和锁定到最小值（LTM）算法决定加热器方向/步长；泵浦与谐振对准后，切换到激光调谐环使信号光对准。完成初始化后，再切回谐振调谐环进行实时温漂补偿。

Cadence Virtuoso IC618 闭环瞬态仿真中，初始化阶段两环光电流均锁定到最小值，转换效率由约 -78 dB 提升到约 3.7 dB；施加 10 Hz 环境温度扰动时，闭环保持光电流最小，断开反馈后光电流显著漂移。该示例说明非线性光学模型可以直接与 TIA、LTM、DAC、驱动器和加热器组成完整 EDA 闭环。

## 6 结论与边界

本文提出可迁移的 FWM/XPM Verilog-A 紧凑模型，适用于不同光子器件和 EDA 平台。模型与数值全图方程和实验结果相符，并支持闭环波长锁定；同一框架还可扩展到其他非线性相互作用或改写为 SPICE。论文没有公开可直接复现的完整代码和实验数据，文中结果依赖给定参数与作者模型实现。

**本项目建模结论**：该论文是“非线性多波长传播层”，不是单腔微环热状态模型。微环本体仍需由耦合器、波导损耗/相位/延迟、环路边界条件和热光状态分层组合。
