# Pyrilog compiler lowering target contract

This document separates the target compiler architecture from the executable
slice. Sections 1, 2, 3.4, and 4 define required future passes; they are not a
claim that complex scalarization, normalization, or matrix diagnostics already
exist in Pyrilog 1.0.0.

The current executable slice implements hierarchy flattening, electrical
topology validation, explicit `R/C/L/V/I/E/G/Q` metadata, exact automatic
`R/C/L/V/I/E/G` relation classification, multi-terminal Verilog-A contributions
including `ddt`, and
SPICE/manifest emission. Unsupported optical, thermal, history, discrete-event,
general implicit-residual, normalization, and Jacobian-diagnostic paths fail or
remain outside the executable pipeline.

## 1. Classification axes

Every model is classified before backend selection. Backend emitters do not
discover semantics from Python classes independently.

| Axis | Classes | Compiler consequence |
| --- | --- | --- |
| topology | conservative node, directed connection | MNA node or directional scalar lanes |
| value domain | real scalar, complex scalar | one scalar or exact real/imag scalarization |
| time | static, `ddt`, history, discrete event | algebraic, DAE, history backend, scheduler |
| equation form | explicit native primitive, explicit contribution, implicit residual | SPICE primitive, Verilog-A contribution, residual realization |
| numerics | unknown scale, residual scale, structural rank, nominal Jacobian | normalization metadata and diagnostics |

Controller scheduling remains outside this pipeline until a backend provides
accepted-step read/write semantics.

## 2. Target pass order

```text
object graph
  -> hierarchy flattening
  -> topology validation
  -> relation and value-domain classification
  -> complex scalarization
  -> unknown/residual normalization
  -> structural and nominal-Jacobian diagnostics
  -> explicit native-primitive selection or exact relation classification
  -> Verilog-A residual/contribution lowering
  -> SPICE topology and instance emission
```

In the target architecture, each pass consumes typed output from the previous
pass. Explicit primitive
metadata is the correctness path for standard devices. Relation matching is an
optional optimization after semantics are fixed; failing a match must not
change the equation.

## 3. Exact backend classes

### 3.1 Native SPICE

The standard library defines independent sources, passive `R/C/L`, controlled
`E/G` sources, and a native NPN `Q` device as ordinary `Device` classes with
reserved compiler metadata:

```python
class Capacitor(Device):
    __pyrilog_spice__ = SpicePrimitiveSpec("C", "capacitance")
```

The metadata explicitly selects the SPICE element, ordered ports, optional
scalar value parameter, and optional model-card parameters. The compiler still
validates the exact port schema, parameter dimensions, finite real values,
positive `R/C/L` values, and positive native BJT model values. This path does
not infer physical semantics from a class name or arbitrary equations.

`E/G` retain inspectable local relations because their ideal controlled-source
semantics are exact. `NPN` deliberately delegates its nonlinear constitutive
law to the backend's standard `Q ... NPN` model. Pyrilog does not attach an
approximate Ebers-Moll relation and then claim it is identical to the backend's
temperature-dependent compact model.

Relation classification additionally proves when a custom relation is an exact
standard element, for example:

```text
v = R i          -> R
i = C ddt(v)     -> C, for constant C
v = L ddt(i)     -> L, for constant L
v = expression(p)-> independent voltage source, for parameter-only arithmetic
vout = gain*vin   -> E, with output conservation and zero control flows
iout = gm*vin     -> G, with output conservation and zero control flows
```

Sign, port orientation, dimensions, constant-parameter requirements and the
number of constitutive relations are part of this match. A failed match falls
through to multi-terminal Verilog-A or a capability error; it never produces an
approximate RLC network.

### 3.2 Local drive budget and Verilog-A fallback

For the current electrical slice, a device cannot declare more local relations
than electrical ports. Fewer relations remain admissible at the frontend for a
future controller or external completion, but compilation still requires every
active equation to become an explicit contribution. Exceeding the local budget
fails during relation analysis, before backend selection. Even within that
budget, multiple equations targeting the same physical branch fail immediately
after their drive targets are resolved because Verilog-A would add contributions
rather than enforce the original equalities independently. This is a
conservative structural guard, not a general symbolic rank proof.

After conservation and explicit zero-flow relations are classified, each
remaining supported relation becomes one contribution:

```text
V(a,b) = expression(port voltages, parameters) -> V(a,b) <+ expression
I(a,b) = expression(port voltages, parameters) -> I(a,b) <+ expression
```

Electrical port current is inward-positive at the Python boundary. For an
ordered branch `(p, n)`, `p.i` maps to `+I(p,n)`, `n.i` maps to `-I(p,n)`, and
the outward view `port.o` is normalized to `-port.i` before contribution
classification. These views share one flow unknown and do not consume separate
relation-budget slots.

The executable expression subset is constants, real parameters, port voltages,
arithmetic `+ - * / **`, and `exp`, `abs`, and `ddt`. Arbitrary Python execution,
right-hand-side flow references without an explicit branch, general implicit
residuals, internal-state realization, history, and discrete events still fail
with a capability error.

Native independent-source classification is intentionally narrower than the
Verilog-A expression subset: constants, parameters and arithmetic can be
evaluated statically, while function expressions such as `exp(parameter)` and
continuous operators such as `ddt(...)` remain Verilog-A contributions.

### 3.3 Verilog-A `ddt`

For a contribution form supported by the target:

```text
i = ddt(q(v, p))
```

lowers exactly to:

```verilog
I(p, n) <+ ddt(q(V(p, n), parameters));
```

The simulator owns DC, AC and transient integration semantics. Pyrilog does
not finite-difference the previous accepted value in Python.

This is the preferred dynamic path. A future native realization may replace a
`ddt` relation only after the compiler proves an exact equivalence for a
restricted model class. It must not guess an RLC network from an arbitrary
differential relation.

### 3.4 Target complex scalarization

Every complex expression `z` will be represented as `(z.re, z.im)`. The transform
is defined recursively over the supported expression algebra, for example:

```text
(a + jb) + (c + jd) = (a + c) + j(b + d)
(a + jb)(c + jd)    = (ac - bd) + j(ad + bc)
ddt(a + jb)         = ddt(a) + j ddt(b)
```

An optical connection between ports `A` and `B` becomes four real scalar
lanes:

```text
A.o.re <-> B.i.re
A.o.im <-> B.i.im
B.o.re <-> A.i.re
B.o.im <-> A.i.im
```

This is an exact representation of two independent bidirectional complex
travelling waves. Loss, phase and delay remain device relations, not connector
properties.

Unsupported complex operators fail during scalarization. They are not reduced
to magnitude-only signals.

## 4. Target normalization and conditioning

Let physical unknowns and residuals be

```text
x = Dx x_hat
F_hat = Df^-1 F
```

Then the normalized Jacobian is

```text
J_hat = Df^-1 J Dx
```

Positive finite diagonal scales make this an invertible coordinate transform,
so `F = 0` and `F_hat = 0` have the same solutions. The compiler records every
scale for parameter emission, result reconstruction and diagnostics.

Automatic scales come from declared units, defaults, initial values, source
levels and nominal parameter values. Iterative row/column equilibration may be
applied to a nominal Jacobian. It can reduce magnitude spread but cannot be
claimed to improve the condition number for every matrix.

The completed diagnostics pass will report separately:

- structural singularity: unmatched equation/unknown graph, zero rows or columns;
- nominal numerical ill-conditioning: scaled Jacobian estimate at declared defaults;
- backend convergence failure: simulator result for a specific analysis and tolerances.

No pass silently inserts `gmin`, parasitic RLC elements or delay approximations.
Such regularization changes the model and requires an explicit user or backend
policy.

## 5. RLC synthesis boundary

Pure passive RLC realization is not universal. It requires an appropriate
linear rational, positive-real relation. More general rational systems may
require controlled sources or an active state-space realization. Nonlinear and
time-varying DAEs generally remain Verilog-A residuals.

Therefore automatic RLC generation is an exact optimization for recognized
classes, not a fallback for arbitrary `ddt` expressions.
