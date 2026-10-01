# Independent physical device simulator

Run `python -m tests.lab.sim.server --journal /data/simulator.jsonl`. The service
listens on port 8099. It has no operator or Home Assistant imports. HA sees only the
raw device API, while the test runner reads independently modelled physical state.
The simulator stays running when the host controller restarts or kills HA.

## Transport

- `GET /health`: `{instance_id, journal_seq}`.
- `GET /devices`: `{instance_id, devices:[{id,kind,name,observable,...capabilities}]}`.
- `GET /devices/{id}`: a single public descriptor.
- `POST /devices/{id}/command`: `{action, position?, percentage?, direction?}`.
  Covers accept `open`, `close`, `set_position`, `stop`; switches `turn_on`/`turn_off`;
  fans `turn_on`, `turn_off`, `set_percentage`, `set_direction` (forward/reverse).
  Receipt is `{accepted:true, command_seq, instance_id}`. A successful receipt does
  not assert a physical effect. Hidden rain deliberately refuses opening silently.
- `POST /admin/reset`: `{devices?:[descriptor]}`. Omitted devices installs sanitized
  defaults. Reset preserves instance identity and the append-only journal.
- `POST` or `PATCH /admin/devices/{id}`: scenario controls such as
  `{hidden_rain:true}`, `{hidden_rain:false}`, `{position:70}`, `{available:false}`,
  `{telemetry_delay:1}`, `{quantization:5}`, `{speed:100}`, `{stuck:true}`, or
  `{value:true}` for binary inputs. Rain causes autonomous closure by default;
  `rain_autoclose:false` can isolate refusal from closure.
  Reusable fan/relay faults are `refuse_actions:["turn_on","turn_off",...]`,
  `suppress_off_feedback:true`, and `unknown_direction:true`. Empty action lists
  and false flags clear faults. Refusal returns an accepted command receipt but
  records a `configured_refusal` without changing the actuator. Suppressed off
  feedback retains the previous on observation (and fan percentage) after the
  actuator physically turns off; clearing it publishes off after the configured
  telemetry delay. It also supports binary sensors such as the virtual on signal.
  Unknown direction applies to fan observations only: direction becomes null while
  physical direction, power, and airflow remain independently modelled facts.
  These flags never appear in public device descriptors. Availability flaps and
  telemetry delay can be combined with these faults through the same admin API.
- `GET /admin/state`: `{instance_id,journal_seq,devices:[{...descriptor,physical,controls}]}`.
- `GET /admin/journal?after=N`: `{instance_id,events:[...]}`.

Public cover observations contain available, position, moving, motion (opening or
closing when moving), and observed_at. Switches expose on; fans expose on,
percentage, direction; binary sensors and sensors expose value. Only sampled
feedback crosses this boundary: delayed telemetry trails physical effects,
quantization rounds position, and no target or rain control is exposed.

Journal events have `{seq,instance_id,time,monotonic,kind,device_id,data}`. Kinds are
reset, admin, command, effect, feedback, refusal, and unsafe_command. A feedback
event contains `data.observation` and is recorded when that sampled observation
is published, after any telemetry delay. Effects and feedback therefore have
separate sequence numbers and timestamps. Tests must assert no
unsafe_command events; the simulator refuses conflicting relays as a second line
of protection, so physical state alone cannot prove safe command ordering.

Custom reset descriptors require id (letters/digits/underscores) and kind
(cover/switch/fan/binary_sensor/sensor). Optional fields include name, position,
on, percentage, direction, value, supports_stop, speed_count, exclusive_group,
airflow_role (inlet/extractor), derived (airflow), and the scenario controls above.
An airflow-bearing relay can specify `airflow_requires_any:["power_a","power_b"]`;
at least one named switch must be physically energized before that relay contributes
airflow. Stale on feedback on a power channel never satisfies this dependency.
A binary sensor with `derived:"on", source:"fan_or_switch_id"` independently
observes that source's physical power. These fixture settings remain admin-only.
Speed is percentage points per second, default 25. Use 100 for a one-second full
stroke. Relay channels sharing exclusive_group cannot energize together.

Defaults are skylight, inlet, stopped_unsupported, exhaust, low_relay, high_relay,
inward_relay, outward_relay, passive_window, fireplace, extraction, demand, airflow.
The additional sanitized cellar fixtures are `cellar_fan`, its independent binary
signal `cellar_on`, `cellar_inlet`, `cellar_demand`, `cellar_policy` (policy eligibility),
`cellar_target` (numeric policy
input, initially 100), and `cellar_low_relay`, `cellar_high_relay`,
`cellar_inward_relay`, `cellar_outward_relay`. The cellar relay speed and direction
groups are independent of the original relay groups. Inward/outward relays in
both groups require an energized low/high power channel. `cellar_fan` contributes
incoming air in forward direction and extraction in reverse; direction alone
while off contributes no airflow. These are synthetic lab fixtures, not a model
of any particular historical household incident.
The derived airflow sensor is min(total physical inlet capacity, extraction) ×100;
it is independent of the operator's requests and desired targets.

The test-only HA bridge is configured as:

```yaml
ha_operator_sim:
  url: http://simulator:8099
  poll_interval: 1
```

Use a polling interval of at least one second. Native HA coordinator scheduling can
repeatedly poll for intervals below one second.

It creates native raw entities as `<domain>.sim_<id>`. Use `/admin/reset` with the
same descriptors after HA setup, or restart HA when changing the device inventory.
These unauthenticated endpoints are intentionally confined to the isolated lab
network. Never expose or publish this service on a production or host network.
