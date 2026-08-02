# 已确认的下一版语义

本页记录已经确认、但尚未进入 1.1.0 实现的接口方向。示例不能作为当前可运行 API。

## 热端口与节点

目标热端口与电端口使用相同层级：

```text
tport.t    temperature
tport.i    heat power flowing into the device
tport.o    heat power flowing out of the device, equal to -tport.i
tnode.p    local heat injection expression, default 0
```

热节点自动装配：

\[
\left(\sum C_k\right)\dot T=\sum p_k+\sum port.o.
\]

用户只写 `==`。当 N 个同域守恒端口中恰好定义 N-1 个 flow 时，编译器自动用
`sum(port.i) == 0` 补齐最后一个；定义不足、重复驱动或依赖成环时报错。Verilog-A
`<+` 只属于后端 IR，不进入公共建模语法。

## 参数与初态

目标 `tnode` 是“温度状态 + 热容参数 + 功率注入槽”的语法糖：

```python
class Heater(Device):
    junction = tnode(c=6 * uJ / K)

heater.junction.c = 8 * uJ / K
```

`junction.c` 使用普通 parameter schema。若绑定到父级参数表达式，则它是只读视图，
避免同一参数出现两个可写入口。

初始条件属于分析，不属于器件关系：

```python
analysis = Transient(
    stop=100 * us,
    step=100 * ns,
    values={
        heater.resistance: 600 * ohm,
        heater.junction.c: 8 * uJ / K,
        heater.junction: 320 * K,
    },
)
```

`ParameterRef` 在整个仿真中改变方程系数；`StateRef` 只设置 `t=0` 状态。用户共享
一套对象到值的绑定语法，manifest 和后端仍必须区分 parameter 与 initial slot。
未指定的热初态在 transient 启动时读取 `Circuit.AMBIENT.t`。

`initial` 与 Newton `guess` 必须分离。后端不能准确注入状态时应拒绝，不得把
initial 静默降级为 `.nodeset`。参数更新若跨越 DAE 结构边界，例如热容从正值变为
零，也必须触发重新编译。
