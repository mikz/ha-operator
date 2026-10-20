# Command conditions and health checks

These optional resource settings require Home Assistant 2026.10.2 or later.
They use Home Assistant's native condition editor and condition evaluation.

Command conditions control whether Operator may send a particular command. A
blocked ON command keeps the selected target ON. An OFF request can still run
unless you also configure conditions for OFF. Conditions apply to manual requests
and policies. Permission is checked again immediately before each command.

Health checks report whether named conditions hold. A device can report ON while a
processing rate remains below its configured threshold. In that case, Operator
reports unhealthy and keeps the selected target. Health checks never stop, start,
or restart a device. Keep any existing desired controls and policies.

## Configure checks in Home Assistant

1. Open **Settings → Devices & services → HA Operator**.
2. Add a resource, or open an existing resource's menu and choose **Reconfigure**.
3. Expand **Command conditions**, choose a supported command, and add conditions
   with the native editor. Leave commands that need no conditions empty.
4. Add a **Health checks** row. Give it a name and edit its health conditions.
5. If the check needs a startup period, add **Startup qualification** conditions.
   For example, require a sensor that measures the actual process uptime to exceed
   60 seconds. Until qualification passes, the check reports waiting.
6. Submit the form. The integration reloads with the same resource and entity IDs.

Use the same form to edit conditions or remove a check row. Removing all health
checks disables the Health entity while preserving its registry identity. Adding
checks again restores that entity, including its history. A user-disabled entity
stays disabled.

These settings do not create an automation entity or require Python changes.

## Supported command names

| Adapter | Command conditions |
| --- | --- |
| Switch | `turn_on`, `turn_off` |
| Cover | `set_cover_position` |
| Native fan | `turn_on`, `turn_off`, `set_direction`, `set_percentage` |
| Relay fan | `turn_on`, `turn_off` |

Native fan ON/OFF conditions also apply when its integration uses a percentage
service for power control. Operator checks all required commands before starting a
fan transaction. The `set_percentage` guard also applies when a native `turn_on`
command includes a percentage. A direction request while the fan is OFF stays OFF
and uses the OFF permission.

Supported cover STOP retains its safety behavior and bypasses movement conditions.
For relay fans, a deliberate OFF target uses `turn_off` conditions. The mandatory
all-off step of a start or reversal retains the relay interlock behavior. Operator
checks ON permission before the transaction and before energizing each output.

Existing restrictions, faults, and relay feedback checks still apply.

## Example resource data

This shows the data stored by the UI, using synthetic entities:

```yaml
name: Capture service
kind: switch
entity_id: switch.capture_service
command_conditions:
  turn_on:
    - condition: state
      entity_id: binary_sensor.capture_start_permitted
      state: "on"
health_checks:
  - alias: Processing rate
    qualification:
      - condition: numeric_state
        entity_id: sensor.capture_process_uptime
        above: 60
    conditions:
      - condition: numeric_state
        entity_id: sensor.capture_processing_rate
        above: 10
```

The uptime sensor must measure the current process's uptime. An unchanged sensor's
`last_changed` does not establish freshness. For freshness checks, use an actual
measurement timestamp, its calculated age, or a source health sensor.

Native state, numeric, template, time, compound, and disabled conditions are
supported. Trigger conditions require an automation run and cannot be used here.
Native OR conditions can handle optional inputs without treating every unavailable
branch as a failed check.

Permission and health follow the native condition result. Some native target
conditions with `behavior: all` pass when no available target remains. For a
required input, use a single-entity state condition, as in the ON example above,
or use `behavior: any` for each required entity joined by a native AND. Operator
reports the available source evidence without changing HA's truth rules.

## Read the result

Configured resources gain a diagnostic **Health** sensor. Open it for its native
details and history. Each named check reports one of these states:

| State | Meaning |
| --- | --- |
| Healthy | Qualification passed, if configured, and the health conditions passed. |
| Unhealthy | A qualified check failed its configured conditions. |
| Waiting | Startup qualification has not passed. |
| Unknown | Evaluation failed or a required direct input is unavailable. |
| Skipped | The check or qualification contains no enabled conditions. |

A known unhealthy check keeps the overall health unhealthy even if another check
is unknown or waiting. Empty or disabled command conditions permit that command;
empty or disabled health conditions never establish health.
Enabled compound conditions retain HA's result: an OR with no passing members
blocks the command, including when its member list is empty or all members are disabled.

The existing **Reason** details and native trace include command permission and
health evidence alongside the selected target. `explain` reports current factual
inputs. History keeps the snapshot from each meaningful result change, so ordinary
measurement updates within the same threshold do not rewrite Health or Reason.

Evidence includes native conditions, thresholds, available source values, and
evaluation errors. Static entity references are labelled as such: templates,
expanded targets, and some entity-valued thresholds can depend on additional
inputs. Referenced branches are not a claim that every branch ran. Downloadable
diagnostics include structural results and error indicators with private source
names and condition text removed.
