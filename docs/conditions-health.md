# Command conditions and native observations

These optional resource settings require Home Assistant 2026.10.2 or later.
They use Home Assistant's native condition editor and condition evaluation.

Command conditions control whether Operator may send a particular command. A
blocked ON command keeps the selected target ON. An OFF request can still run
unless you also configure conditions for OFF. Conditions apply to manual requests
and policies. Permission is checked again immediately before each command.

## Configure command conditions

1. Open **Settings > Devices & services > HA Operator**.
2. Add a resource, or choose **Reconfigure** for an existing resource.
3. Expand **Command conditions**, choose a supported command, and use the native
   condition editor. Leave commands that need no conditions empty.
4. Submit the form. Use the same form to edit or remove command conditions.

The editor uses existing HA entities. Operator does not create helpers, templates,
groups, or automation entities. Configure those independently, then reference them.

## Supported command names

| Adapter | Command conditions |
| --- | --- |
| Number | `set_value` |
| Select | `select_option` |
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
```

An independently configured uptime sensor must measure the current process's uptime. An unchanged sensor's
`last_changed` does not establish freshness. For freshness checks, use an actual
measurement timestamp, its calculated age, or a source health sensor.

Native state, numeric, template, time, compound, and disabled conditions are
supported. Trigger conditions require an automation run and cannot be used here.
Native OR conditions can handle optional inputs without treating every unavailable
branch as a failed check.

Command permission follows the native condition result. Some native target
conditions with `behavior: all` pass when no available target remains. For a
required input, use a single-entity state condition, as in the ON example above,
or use `behavior: any` for each required entity joined by a native AND. Operator
reports the available source evidence without changing HA's truth rules.

## Read the result

The existing **Reason** details and native trace include command permission
alongside selected intent. `explain` reports current factual inputs. History keeps
the snapshot from each meaningful permission transition; ordinary measurement
updates within the same threshold do not rewrite Reason.

Evidence includes native conditions, thresholds, available source values, and
exposed evaluation errors. Static entity references are labelled as such:
templates, expanded targets, and some entity-valued thresholds can depend on other
inputs. Referenced branches do not establish that every branch ran. Downloadable
diagnostics omit private source names and condition text.

Use existing native sensors or helpers for independent service-health observations.
Device ON feedback confirms power state; a separate processing-rate sensor can
report degraded processing without changing the selected ON target. Open each
native observation for its own details and history. Operator does not evaluate or
aggregate health checks, create health entities, or restart a process on a fault.

Before upgrading from 0.3.0, migrate any configured `health_checks` and their
consumers through supported HA configuration tools, then clear the old checks.
The incompatible migration rejects remaining checks before changing configuration
or saved runtime state. Historical Operator Health registry entries and their
history are preserved, with the retired entity disabled by the integration.
See the [migration procedure](native-policies.md#upgrade-from-030).
