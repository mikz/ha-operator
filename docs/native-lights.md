# Native light resources

A `light` resource owns one native `light` entity and exposes a managed LightEntity.
Use that managed entity for dashboards, voice assistants, and HomeKit commands. It
reports the source's observed on/off, brightness, color, and effect, and preserves
its native supported color modes, temperature range, and effect list. Commands
enter the same timed manual request boundary as other managed resources; successful
service dispatch does not change the reported physical state.

Light targets accept `on`, `brightness` (0–255), native `white` (0–255), `color_temp_kelvin`, `hs_color`,
`xy_color`, `rgb_color`, `rgbw_color`, `rgbww_color`, and `effect`. Specify at most
one color field, in a mode supported by the source. Native LightEntity services
convert UI service input to the source's advertised color modes. Effects must be
in the source's effect list. Zero brightness means OFF. A bare ON target contains
only `on: true`; it does not copy brightness or color from feedback. A bare OFF
target contains only `on: false`. Partial manual settings preserve the active manual
brightness/color/effect selection; changing color mode replaces the previous mode.
Bare ON clears Operator's brightness/color/effect constraints. It does not clear
Adaptive Lighting's manual-control flag. Color temperature feedback allows one
whole-mired quantization step used by native lights. Source observations never
create or renew a manual lease.

Keep Adaptive Lighting configured against the underlying source light. It remains
the owner of automatic brightness, color, and sleep behavior. When the physical
source is OFF, a managed bare ON dispatches a native source ON; the observed
physical OFF-to-ON transition allows Adaptive Lighting's native reset behavior.
When the source is already ON, an on-only target is already satisfied and can
produce no service call. Clearing Operator's style constraints therefore does
not itself restart adaptation on an already-ON source.

Explicit managed brightness/color/effect
commands are forwarded to the same native source service, where Adaptive Lighting
can apply its native manual-control handling. Its source updates do not enter the
managed manual-request boundary. Verify the configured Adaptive Lighting takeover
behavior in the isolated integration lab before migration.

To resume adaptation when a manual lease ends while the source stays ON, configure
an external native HA automation for the ownership transition from manual to the
intended automatic bare-ON policy. Check the selected policy, exact target
`{on: true}`, and source ON state before calling
`adaptive_lighting.set_manual_control` for the Adaptive Lighting switch and raw
source light with `manual_control: false`. Adaptive Lighting 1.32.0 then resets its
manual flag and requests adaptation for lights that are ON. This release action
belongs to deployment wiring; Operator's lease expiry does not call Adaptive
Lighting. The isolated lab must prove the configured transition, adaptation, and
handling of a new manual request during the native adaptation delay before
migration.

The managed facade does not advertise flash or transition features. Adaptive
Lighting can still use native transitions directly on its source light. HomeKit
capability publication uses the native LightEntity contract; actual HAP behavior
requires the separate HomeKit lab acceptance gate.
