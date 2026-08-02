# 支持电子-光子协同仿真的四波混频 Verilog-A 紧凑模型

**原文题目**：A Verilog-A Compact Model for Four-Wave Mixing Supporting Electronic-Photonic Co-Simulation
**作者**：Siyuan Zhang，Xiaolong Fan，Nuo Chen，Ken Xingze Wang，Jing Xu，Min Tan
**会议**：2024 2nd International Symposium of Electronics Design Automation（ISEDA）
**DOI**：[10.1109/ISEDA62518.2024.10617775](https://doi.org/10.1109/ISEDA62518.2024.10617775)
**页码**：16-19（4 页）
**译文证据边界**：本文根据项目中保存的 IEEE 全文 PDF 逐页翻译，包含正文、公式、图 1-5、表 I 和 19 条参考文献。原 PDF 为会议短文，未附作者简介页；图中坐标、图例和英文标注保留原样。

**作者单位与邮箱（原文首页）**：Siyuan Zhang 就职于华中科技大学物理学院，邮箱 `sy_zhang@hust.edu.cn`；Xiaolong Fan、Nuo Chen、Jing Xu 就职于华中科技大学光学与电子信息学院，邮箱分别为 `xiaolongfan@hust.edu.cn`、`nuochen@hust.edu.cn`、`jing_xu@hust.edu.cn`；Ken Xingze Wang 就职于华中科技大学物理学院，邮箱 `wxz@hust.edu.cn`；Min Tan 就职于华中科技大学集成电路学院，邮箱 `mtan@hust.edu.cn`。Siyuan Zhang 与 Xiaolong Fan 贡献相同；Jing Xu 与 Min Tan 为通信作者。本文得到中国国家重点研发计划项目 2018YFA0704400 资助。

## 摘要

本文提出一种用于四波混频（four-wave mixing，FWM）的 Verilog-A 紧凑模型。该模型完全兼容现有电子设计自动化（EDA）平台，可支持快速电子-光子协同仿真。模型避免描述 FWM 的复杂物理过程，为系统设计者监测关键光学参数变化提供了一种简便方法，从而加速包含 FWM 过程的混合系统协同设计与协同优化。文中给出了建模框架和关键推导；模型仿真结果与数值全图（full-map）结果吻合良好。

**关键词**：Verilog-A；紧凑模型；电子-光子协同仿真；四波混频。

## I. 引言

电子-光子异质融合集成电路（electronic-photonic heterogeneous-converging integrated circuits，EPHIC）被广泛认为是后摩尔时代具有前景的技术 [1]。电子集成电路（EIC）经过数十年发展，在制造和设计方法方面已经成熟；光子集成电路（PIC）则天然具有低功耗、低延迟和高带宽优势。充分结合 EIC 与 PIC 的优点，有望解决数据传输和处理等不同领域中的诸多挑战。

随着 EPHIC 的规模和复杂度不断提高，对电子-光子协同仿真和协同设计的需求日益迫切。Lumerical Device、FDTD Solutions、Mode Solutions 等专用软件常用于器件物理层仿真。它们能够提供高精度结果，但计算量太大，难以支持数百个器件的协同仿真，尤其是在长时间尺度下 [2]。为此，人们开发了基于 MATLAB [3]、编程语言 [5]、[6] 的系统级仿真工具，以及 OptiSpice [7]、[8] 和 Lumerical INTERCONNECT 等仿真器。这些程序可从系统角度提供仿真和优化平台，但大多数是独立软件，难以适配既有电路设计和版图基础设施，尤其难以与标准工艺设计套件（PDK）提供的晶体管 SPICE 或 Spectre 模型配合 [2]。其中一些工具提供了与 Cadence Virtuoso、Mentor Graphics Pyxis 等 EDA 工具的仿真接口，但它们通常将电子部分和光子部分分开仿真，并将二者之间的交互理想化 [2]、[9]、[10]，因此精度有限，不适合电子和光子器件紧密耦合的系统。

为了在 EDA 环境中完成光子器件与电子电路的全流程流片前验证，人们提出了许多基于 Verilog-A [9]-[11] 或 SPICE [12]、[13] 的光子器件行为宏模型，包括器件专用模型 [11]、[12] 和通用模型 [9]、[10]、[13]。这些模型已证明有效且高效，可供 IC 设计者在 Cadence Virtuoso 等成熟 EDA 平台上设计 EPHIC [9]、[11]。不过，EDA 环境中的光子器件建模仍处于发展阶段；不同波长光之间的非线性交互（例如四波混频和交叉相位调制，XPM）等问题尚未得到充分考虑。非线性交互已用于波长转换、相位共轭和光子对产生，应用范围从非线性光信号处理 [14] 延伸到量子光学 [15]。因此，包含非线性交互的 EPHIC 还无法完全在 EDA 平台上协同仿真和协同设计。

为解决这一问题，本文提出一种支持电子-光子协同仿真的 FWM Verilog-A 紧凑模型。该模型是通用模型，还可进一步用于构建复杂但实用的光子器件。全文结构如下：第 II 节介绍既有框架的局限性，以及所提出模型的框架和原理；第 III 节利用微环验证所提出模型；第 IV 节给出结论。

## II. 用于 FWM 的 Verilog-A 紧凑模型

### A. 基带等效

为更清楚地说明既有框架的局限性，先简要介绍电子-光子协同仿真的基本建模思想。对于 EPHIC，光信号和电信号之间存在很大的频率失配：光载波频率通常很高（约 200 THz），加载在光上的调制信号频率约为 100 GHz，而热波动频率约为 100 kHz。频率失配迫使仿真器使用最小时间步长，导致极长的仿真时间。文献 [4]、[9] 提出了基带等效：将光载波频率移到基带后，光信号可写为

$$\tilde E(z,t)=e^{j\omega_Rt}\int_{-\infty}^{+\infty}\tilde E_{bb}(z,\Delta\omega)e^{j\Delta\omega t}\,d\Delta\omega=e^{j\omega_Rt}\tilde E_{bb}(z,t)$$
$$=e^{\alpha z+j[\beta(\omega_R)z+\omega_Rt]}\tilde E_{bb}\left(0,t-\frac{zn_g}{c}\right). \tag{1}$$

其中，$\tilde E(z,t)$ 是解析电场，$\tilde E_{bb}(z,t)$ 是其基带等效电场，二者均用复数表示。$\omega_R$ 为参考频率，实际频率可写成 $\omega=\omega_R+\Delta\omega$。$\beta$ 为波导中的波矢，$n_g$ 为群折射率，$c$ 为光传播速度。对于长度为 $L$ 的波导，输出场与输入场的关系为

$$\tilde E_{bb}(L,t)=e^{\alpha L+j\beta(\omega_R)L}\tilde E_{bb}\left(0,t-\frac{n_gL}{c}\right). \tag{2}$$

其中 $e^{\alpha L}$ 表示传播损耗，$e^{j\beta(\omega_R)L}$ 表示相对于 $\omega_R$ 的相移，$n_gL/c$ 表示相对于 $\Delta\omega$ 的时延。

### B. 既有框架的局限性

图 1 给出了既有波导模型框架。该框架适用于许多应用，但不能充分满足 FWM 等非线性光学交互应用的需求。

> 图 1 图片文件未随当前证据包归档；请以配套 PDF 核对。

**图 1.** 既有波导模型框架。

首先，在波长转换等应用中，信号光、泵浦光和闲频光之间的最大波长差通常超过数纳米（对应数百 GHz，甚至数 THz）。但是，为保证仿真速度，模型要求所有信号频率都位于参考频率附近，通常在 100 GHz 以内。因此，尽管既有框架原则上可以支持频率差很大的系统仿真，仿真速度会大幅下降，无法充分满足快速仿真的需求。

其次，如图 1 所示，既有框架仅为同向传播的光提供单一通道，其中两条总线分别表示实部和虚部。同时，不同相对频率的信号以数值形式线性叠加。因此，所有处理（衰减、相移和时延）对不同光信号都相同，无法分别处理不同相对频率的信号，不同波长之间的非线性交互也无法表示。

### C. 所提出的框架

图 2 给出了用于 FWM 的新波导模型框架。

> 图 2 图片文件未随当前证据包归档；请以配套 PDF 核对。

**图 2.** 所提出的波导模型框架。

采用三个不同通道分别表示信号光、泵浦光和闲频光。每个通道的输入包含理想部分和非线性交互部分。理想部分是激光器正常入射、未考虑非理想效应的光；非线性交互部分的输出是 FWM 产生的光。本文不采用统一处理，而是针对不同通道进行定制处理：例如，在泵浦光通道中表征自相位调制（SPM），在信号光和闲频光通道中表征交叉相位调制（XPM）。为了避免大波长差导致的长仿真时间，分别为信号光、泵浦光和闲频光采用三个不同的参考波长。三类光的实际波长可以在各自参考波长附近任意选择；如有必要，也可以在不同参考波长之间自由切换，以分析特定应用。

需要注意的是，非线性交互主要取决于介质的高阶非线性系数以及激光器的光功率。由于 FWM 中信号光的功率通常远低于泵浦光，因此在大多数条件下，不同信号之间的交互影响可以忽略。基于此，本文模型主要考虑泵浦光与信号光之间的交互。

### D. 所提出模型的原理与实现

本节以简并四波混频（degenerate four-wave mixing，DFWM）为例说明模型原理和实现。非简并四波混频（NDFWM）版本可通过对比容易得到。对于 DFWM，泵浦光、信号光和闲频光的实际频率满足

$$\omega_s+\omega_i=2\omega_p,$$

其中 $\omega_p$、$\omega_s$、$\omega_i$ 分别为泵浦光、信号光和闲频光的实际频率。由 $\omega=\omega_R+\Delta\omega$ 可知，移到基带后，三者的相对频率满足

$$\Delta\omega_i=2\Delta\omega_p-\Delta\omega_s.$$

考虑线性传输损耗、双光子吸收（TPA）、自由载流子吸收（FCA）、色散、波导中的 XPM 和 SPM，在小信号分析条件 $|\tilde E_p|\gg|\tilde E_s|,|\tilde E_i|$ 下，DFWM 耦合方程为 [16]

$$\frac{\partial\tilde E_p}{\partial z}=-\frac{1}{2}\alpha'\tilde E_p+j[\beta(\omega_p)+\gamma P_p]\tilde E_p, \tag{3}$$
$$\frac{\partial\tilde E_s}{\partial z}=-\frac{1}{2}\alpha'\tilde E_s+j[\beta(\omega_s)+2\gamma P_p]\tilde E_s+j\gamma\tilde E_p\tilde E_p\tilde E_i^*, \tag{4}$$
$$\frac{\partial\tilde E_i}{\partial z}=-\frac{1}{2}\alpha'\tilde E_i+j[\beta(\omega_i)+2\gamma P_p]\tilde E_i+j\gamma\tilde E_p\tilde E_p\tilde E_s^*. \tag{5}$$

其中 $\tilde E_p$、$\tilde E_s$、$\tilde E_i$ 分别为泵浦光、信号光和闲频光的电场；$\alpha'=\alpha+\alpha_{TPA}+\alpha_{FCA}$，其中 $\alpha$、$\alpha_{TPA}$ 和 $\alpha_{FCA}$ 分别为线性传输损耗系数，以及由 TPA 和 FCA 引起的非线性传输损耗系数；$\beta(\omega_{p,s,i})$ 为波矢；$\gamma$ 为非线性参数；$P_p$ 为输入泵浦光功率。

在低功率激励下，参量增益可以忽略，所提出框架中的信号交互部分为零。方程（3）-（5）可进一步化简，其解析解为 [17]

$$\tilde E_p(L,t)=\tilde E_p(0,t)e^{-\alpha'L/2+j[\beta(\omega_p)+\gamma P_p]L}, \tag{6}$$
$$\tilde E_s(L,t)=\tilde E_s(0,t)e^{-\alpha'L/2+j[\beta(\omega_s)+2\gamma P_p]L}, \tag{7}$$
$$\tilde E_i(L,t)=\left[\tilde E_i(0,t)+j\eta\gamma\tilde E_p(0,t)\tilde E_p(0,t)\tilde E_s^*(0,t)\right]e^{-\alpha'L/2+j[\beta(\omega_i)+2\gamma P_p]L}. \tag{8}$$

其中 $\eta$ 为受相位失配影响的效率因子：

$$\eta=\frac{1-e^{-\alpha'L+j\kappa L}}{\alpha'L-j\kappa L}.$$

在高功率激励下，不能忽略参量增益，必须考虑信号的交互部分。方程（3）-（5）的解析解为 [18]

$$\tilde E_p(L,t)=\tilde E_p(0,t)e^{-\alpha'_0L/2+j[\beta(\omega_p)+\gamma P_p]L}, \tag{9}$$
$$\tilde E_s(L,t)=\left[u(L)\tilde E_s(0,t)+v(L)\tilde E_i^*(0,t)\right]e^{-\alpha'_0L/2+j[\beta(\omega_s)L+\phi_{NL}L]}, \tag{10}$$
$$\tilde E_i(L,t)=\left[u(L)\tilde E_i(0,t)+v(L)\tilde E_s^*(0,t)\right]e^{-\alpha'_0L/2+j[\beta(\omega_i)L+\phi_{NL}L]}. \tag{11}$$

其中

$$u(L)=\left[\cosh(g)+j\frac{q\sinh(g)}{g}\right]e^{-\alpha'_0L/2},$$
$$v(L)=\left[j\gamma P_pL_{eff}\frac{\sinh(g)}{g}\right]e^{-\alpha'_0L/2},$$
$$g=\sqrt{(\gamma P_pL_{eff})^2-q^2},\qquad L_{eff}=\frac{1-e^{-\alpha'_0L}}{\alpha'_0}.$$

$L_{eff}$ 是考虑损耗后的波导有效长度；$\phi_{NL}$ 为非线性相位，满足 $\phi_{NL}=\gamma P_pL_{eff}-\Delta\beta L/2$；$\Delta\beta=\beta_s+\beta_i-\beta_p$ 为三个相关波的线性相位失配，并与群速度色散参数 $\beta_2$ 有关。基于方程（6）-（8）和（9）-（11），分别对三种光作基带等效，并在图 2 所示框架下用 Verilog-A 实现代码。

## III. 仿真结果

光参量放大器（OPA）是一类由 FWM 驱动的光放大器，被认为是高速光通信系统的有前景候选器件。高品质因数（Q）的微环谐振器（MRR）凭借增强的光-物质交互，在片上 OPA 中显示出巨大潜力。因此，本文给出一个 MRR 示例，以展示所提出模型的实用性和精度。

> 图 3 图片文件未随当前证据包归档；请以配套 PDF 核对。

**图 3.** MRR 的结构（a）和示意图（b）。

如图 3 所示，MRR 由直波导和环形波导组成。本文通过连接定向耦合器模型与直波导模型，实现 MRR 的 Verilog-A 紧凑模型。表 I 给出了 MRR 的关键仿真参数。

> 表 I 图片文件未随当前证据包归档；下方文字表格保留可检索内容。

**表 I. MRR 的关键仿真参数。**

| 参数 | 数值 | 参数 | 数值 |
| --- | --- | --- | --- |
| 材料 | Si₃N₄ | 有效折射率 | 1.808 |
| 环周长（μm） | 1200.221 | 群折射率 | 2.17 |
| 损耗（dB/cm） | 0.2 | 泵浦光功率 $P_{pump}$（dBm） | 33 和 20 |
| 非线性参数 $\gamma$（W⁻¹m⁻¹） | 1 | 信号光功率 $P_{signal}$（dBm） | 0 |
| 二阶色散 $\beta_2$（ps²/km） | -5000 | 耦合系数 | 0.3761 |

图 4 给出输入泵浦光功率为 20 dBm 时，转换效率随信号光频率失谐的仿真结果。图 5(a)、(b) 和图 5(c)、(d) 分别给出输入泵浦光功率为 33 dBm 时，转换效率和参量增益随信号光频率失谐的仿真结果。图 4 和图 5 中的深红色圆点由数值求解全图耦合非线性薛定谔方程 [19] 得到，该方程跟踪光在每一圈传播中的非线性过程：

> 图 4 图片文件未随当前证据包归档；请以配套 PDF 核对。

**图 4.** 输入泵浦光功率为 20 dBm 时，所提出模型与数值全图解的转换效率。

$$\frac{\partial A}{\partial z}=\left[-\alpha-i\delta-i\frac{\beta_2}{2}\frac{\partial^2}{\partial\tau^2}+i\gamma|A|^2\right]A+\sum_{n=-\infty}^{+\infty}\delta(z-nL)[i\kappa A_{in}-(1-r)A_{in}]. \tag{12}$$

其中 $A$ 为环内复场振幅，包含泵浦光、信号光和闲频光。$\delta$ 为泵浦光与最近谐振峰之间的失谐。在耦合区域，输入-输出关系为 $A_{out}=i\kappa A|_{z=nL}+rA$，其中 $n$ 表示第 $n$ 次往返。采用标准分步傅里叶法数值求解方程（12），得到的转换效率和参量增益与结果吻合良好，证明了所提出 Verilog-A 紧凑模型的有效性。

> 图 5 图片文件未随当前证据包归档；请以配套 PDF 核对。

**图 5.** 输入泵浦光功率为 33 dBm 时，所提出模型与数值全图解的转换效率（a）、（b）和参量增益（c）、（d）。

需要指出的是，所提出模型是通用模型，并不局限于描述 MRR 中的 FWM 现象，也可用于模拟更复杂光子结构甚至大型系统中的 FWM 现象。

## IV. 结论

本文提出一种支持电子-光子协同仿真的 FWM Verilog-A 紧凑模型。该模型完全兼容现有 EDA 平台，为系统设计者在 EDA 平台中监测关键光学参数变化提供了简便方法。作为通用模型，它可作为不同系统中的基础器件，促进包含光学非线性交互过程的混合系统协同设计与协同优化。模型仿真结果与数值求解全图薛定谔方程的结果一致；基于所提出框架，还可以进一步建立其他光学非线性交互模型。

## 参考文献

[1] M. Tan 等，“Circuit-level convergence of electronics and photonics: basic concepts and recent advances,” *Frontiers of Optoelectronics*, vol. 15, no. 1, pp. 1-17, Apr. 2022。
[2] W. Bogaerts 等，“Silicon photonics circuit design: Methods, tools and challenges,” *Laser & Photonics Reviews*, vol. 12, no. 4, Art. no. 1700237, 2018。
[3] S. Lin 等，“Electronic-photonic co-optimization of high-speed silicon photonic transmitters,” *Journal of Lightwave Technology*, vol. 35, no. 21, pp. 4766-4780, 2017。
[4] Y. Ye 等，“Numerical modeling of a linear photonic system for accurate and efficient time-domain simulations,” *Photonics Research*, vol. 6, no. 6, pp. 560-573, Jun. 2018。
[5] X. Chen 等，“Modeling and analysis of optical modulators based on free-carrier plasma dispersion effect,” *IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems*, vol. 39, no. 5, pp. 977-990, 2020。
[6] M. Fiers 等，“Time-domain and frequency-domain modeling of nonlinear optical components at the circuit-level using a node-based approach,” *Journal of the Optical Society of America B*, vol. 29, no. 5, pp. 896-900, May 2012。
[7] P. Gunupudi 等，“Self-consistent simulation of opto-electronic circuits using a modified nodal analysis formulation,” *IEEE Transactions on Advanced Packaging*, vol. 33, no. 4, pp. 979-993, 2010。
[8] T. Smy，P. Gunupudi，“Robust simulation of opto-electronic systems by alternating complex envelope representations,” *IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems*, vol. 31, no. 7, pp. 1139-1143, 2012。
[9] C. Sorace-Agaskar 等，“Electro-optical co-simulation for integrated CMOS photonic circuits with Verilog-A,” *Optics Express*, vol. 23, no. 21, pp. 27180-27203, Oct. 2015。
[10] M. J. Shawon，V. Saxena，“Rapid simulation of photonic integrated circuits using Verilog-A compact models,” *IEEE Transactions on Circuits and Systems I: Regular Papers*, vol. 67, no. 10, pp. 3331-3341, 2020。
[11] B. Wang 等，“A compact Verilog-A model of silicon carrier-injection ring modulators for optical interconnect transceiver circuit design,” *Journal of Lightwave Technology*, vol. 34, no. 12, pp. 2996-3005, 2016。
[12] M. Kim 等，“Large-signal SPICE model for depletion-type silicon ring modulators,” *Photonics Research*, vol. 7, no. 9, pp. 948-954, Sep. 2019。
[13] Y. Ye 等，“SPICE-compatible equivalent circuit models for accurate time-domain simulations of passive photonic integrated circuits,” *Journal of Lightwave Technology*, vol. 40, no. 24, pp. 7856-7868, 2022。
[14] N. Takanashi 等，“All-optical phase-sensitive detection for ultra-fast quantum computation,” *Optics Express*, vol. 28, no. 23, pp. 34916-34926, Nov. 2020。
[15] W. Yang 等，“Phase regeneration for polarization-division multiplexed signals based on vector dual-pump nondegenerate phase sensitive amplification,” *Optics Express*, vol. 23, no. 3, pp. 2010-2020, Feb. 2015。
[16] G. P. Agrawal, *Nonlinear Fiber Optics*, 5th ed., Amsterdam, The Netherlands: Elsevier, 2013, pp. 616-622。
[17] P. P. Absil 等，“Wavelength conversion in GaAs micro-ring resonators,” *Optics Letters*, vol. 25, no. 8, pp. 554-556, Apr. 2000。
[18] M. Karlsson 等，“Analytic theory for parametric gain in lossy integrated waveguides,” *Conference on Lasers and Electro-Optics*, 2021, p. JTh3A.5。
[19] X. Xue 等，“Super-efficient temporal solitons in mutually coupled optical cavities,” *Nature Photonics*, vol. 13, no. 9, pp. 616-622, Sep. 2019。
