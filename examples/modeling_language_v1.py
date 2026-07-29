"""Full target syntax for the Pyrilog modeling language.

The current frontend can build this object graph and flatten electrical
composites. Optical, thermal, controller scheduling, delay, output, and
interactive-session lowering remain explicit backend capability boundaries.
"""

from pyrilog import *
from pyrilog.control import Controller, output
from pyrilog.devices import Resistor, VoltageSource
from pyrilog.simulation import Output, Spice, Transient


# -----------------------------------------------------------------------------
# Primitive device definitions
# -----------------------------------------------------------------------------


# Standard R/C/L/V/I devices come from pyrilog.devices and carry explicit
# native-SPICE metadata. Custom devices below only describe their own relations.


class FirstOrderController(Device):
    sense = eport()
    drive = eport()
    time_constant = param(10 * ns, min=0 * ns)
    gain = 1.0
    state = val(0 * V)
    relation = (
        sense.i == 0 * A,
        drive.i == 0 * A,
        time_constant * ddt(state) + state == gain * sense.v,
        drive.v == state,
    )


class Laser(Device):
    out = oport()
    wavelength = 1550 * nm
    power = 1 * mW
    phase = 0 * rad
    relation = (
        out.i == 0,
        out.o == (power**0.5) * exp(1j * phase),
    )


class Waveguide(Device):
    input = oport()
    output = oport()
    length = param(100 * um, min=0 * um)
    loss = param(2 * dB / cm, min=0 * dB / cm)
    reference_wavelength = 1550 * nm
    effective_index = param(2.4, min=1.0)
    group_index = param(4.2, min=1.0)
    propagation_delay = param(0 * ps, min=0 * ps)
    relation = (
        output.o
        == delay(input.i, propagation_delay, initial=0)
        * 10 ** (-loss * length / 20)
        * exp(-1j * 2 * pi * effective_index * length / reference_wavelength),
        input.o
        == delay(output.i, propagation_delay, initial=0)
        * 10 ** (-loss * length / 20)
        * exp(-1j * 2 * pi * effective_index * length / reference_wavelength),
    )


# A thermal-capacitance C activates internal T/P and the optional TP port.
class Ring(Device):
    input = oport()
    through = oport()
    C = param(10 * uJ / K, min=1e-15 * J / K)
    radius = param(10 * um, min=0 * um)
    coupling = param(0.12, min=0.0, max=1.0)
    reference_wavelength = 1550 * nm
    reference_temperature = 300 * K
    thermal_phase_coefficient = 0 * rad / K

    relation = (
        through.o
        == (1 - coupling) ** 0.5
        * input.i
        * exp(
            -1j
            * thermal_phase_coefficient
            * (T - reference_temperature)
        ),
        input.o
        == (1 - coupling) ** 0.5
        * through.i
        * exp(
            -1j
            * thermal_phase_coefficient
            * (T - reference_temperature)
        ),
        P == 0 * W,
    )


class Photodiode(Device):
    optical = oport()
    p = eport()
    n = eport()
    responsivity = param(0.8 * A / W, min=0 * A / W)
    relation = (
        p.i + n.i == 0,
        p.i.o == responsivity * optical.i.power,
    )


class ElectroThermalHeater(Device):
    p = eport()
    n = eport()
    C = param(10 * uJ / K, min=1e-15 * J / K)
    resistance = param(0.5 * kohm, min=1e-15 * ohm)
    temperature_coefficient = 0 / K
    reference_temperature = 300 * K
    efficiency = param(0.9, min=0.0, max=1.0)

    relation = (
        p.i + n.i == 0,
        p.v - n.v
        == resistance
        * (1 + temperature_coefficient * (T - reference_temperature))
        * p.i.i,
        P == efficiency * (p.v - n.v) * p.i.i,
    )


class ThermalResistance(Device):
    a = tport()
    b = tport()
    resistance = 20 * K / W
    relation = (
        a.p + b.p == 0,
        a.p.i == (a.t - b.t) / resistance,
    )


# -----------------------------------------------------------------------------
# Composite device definitions
# -----------------------------------------------------------------------------


class RingChannel(Device):
    opt_in = oport()
    opt_out = oport()
    heater_p = eport()
    heater_n = eport()
    ambient = tport()

    radius = 10 * um
    coupling = param(0.12, min=0.0, max=1.0)
    waveguide_loss = 2 * dB / cm
    heater_resistance = 0.5 * kohm
    heater_efficiency = param(0.9, min=0.0, max=1.0)
    heater_heat_capacity = 10 * uJ / K
    ring_heat_capacity = 10 * uJ / K
    heater_ring_resistance = 20 * K / W
    heater_ambient_resistance = 100 * K / W
    ring_ambient_resistance = 100 * K / W

    input_waveguide = Waveguide(length=100 * um, loss=waveguide_loss)
    ring = Ring(radius=radius, coupling=coupling, C=ring_heat_capacity)
    output_waveguide = Waveguide(length=50 * um, loss=waveguide_loss)
    heater = ElectroThermalHeater(
        resistance=heater_resistance,
        efficiency=heater_efficiency,
        C=heater_heat_capacity,
    )
    heater_to_ring = ThermalResistance(resistance=heater_ring_resistance)
    heater_to_ambient = ThermalResistance(resistance=heater_ambient_resistance)
    ring_to_ambient = ThermalResistance(resistance=ring_ambient_resistance)

    opt_in | input_waveguide.input
    input_waveguide.output | ring.input
    ring.through | output_waveguide.input
    output_waveguide.output | opt_out

    heater_drive = heater_p | heater.p
    heater_return = heater_n | heater.n

    heater.TP |= (heater_to_ring.a, heater_to_ambient.a)
    ring.TP |= (heater_to_ring.b, ring_to_ambient.a)
    ambient_node = ambient | heater_to_ambient.b
    ambient_node |= ring_to_ambient.b


# -----------------------------------------------------------------------------
# Optional sampled controllers
# -----------------------------------------------------------------------------


class ReceiverController(Controller):
    sample = 10 * ns
    delay = 0 * ns
    hold = "zoh"

    drive = output(V)
    previous_error = val(0 * V)

    def step(self, measured_voltage):
        target_voltage = 0.6 * V
        bias = 1.2 * V
        gain = 0.1
        return {"drive": bias + gain * (target_voltage - measured_voltage)}


# -----------------------------------------------------------------------------
# Root circuit construction
# -----------------------------------------------------------------------------


with Circuit() as system:
    system.AMBIENT.t = 298.15 * K

    vdrive = VoltageSource(dc=1.2 * V)
    load = Resistor()
    load.resistance = 1.0 * kohm
    laser = Laser(wavelength=1550 * nm, power=1.0 * mW)

    channel_parameters = {"radius": 12 * um, "coupling": 0.15}
    channel = RingChannel(**channel_parameters)

    detector = Photodiode(responsivity=0.8 * A / W)
    controller = ReceiverController()

    drive = vdrive.p | channel.heater_p

    system.GND |= (vdrive.n, channel.heater_n, load.n, detector.n)
    system.AMBIENT |= channel.ambient

    detector_output = detector.p | load.p

    laser.out | channel.opt_in
    channel.opt_out | detector.optical

    # The call creates a typed output pack. Binding a named output records a
    # control dependency without pretending that it is a physical MNA node.
    command = controller(detector_output.v)
    vdrive.dc = command.drive


# -----------------------------------------------------------------------------
# Environment, compilation, analysis, and output
# -----------------------------------------------------------------------------


observables = (
    drive.v.mV,
    detector_output.v.mV,
    channel.ring.T.degC,
    detector.p.i.o.mA,
    channel.opt_out.o.abs,
    channel.opt_out.o.power.mW,
    channel.opt_out.o.phase.deg,
    channel.opt_out.i.power.mW,
)

output = Output(
    *observables,
    file="ring_receiver.csv",
)

target = Spice(
    simulator="xyce",
    netlist="build/ring_receiver.sp",
    verilog_a_dir="build/verilog_a",
)

# Standard devices lower through explicit SPICE primitive metadata. Other
# supported local relations emit shared Verilog-A models; the netlist stores
# only instance topology, parameter overrides, and model references.
compiled = system.compile(target)
analysis = Transient(stop=100 * ns, step=1 * ns)


# The backend scheduler will sample controller inputs, call step(), wait delay,
# then apply and hold command until the next accepted controller event.
result = compiled.run(analysis, output=output)
