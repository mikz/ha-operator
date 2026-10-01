"""Native timed-cover recipe, with deployment identifiers supplied by the caller.

This module builds configuration only. It has no HA connection, credentials, or
production bindings; the same generated actions are exercised by the offline lab.
"""

from __future__ import annotations

# Embedded native HA templates stay on one line for review against saved config.
# ruff: noqa: E501
import json


def literal(value):
    """JSON containers and strings also form valid Jinja literals."""
    return json.dumps(value)


def check(expression, message):
    return {
        "if": [{"condition": "template", "value_template": expression}],
        "then": [{"stop": message, "error": True}],
    }


def dispatcher(windows, group, *, duration=1800):
    """Always admit intent, including when observed position already matches."""
    mapping = {item["entity_id"]: item["resource_id"] for item in windows}
    entities = list(mapping)
    return {
        "alias": "Open Roof Windows",
        "description": "Durable per-window requests. No delayed closing callback. New calls supersede unfinished dispatch.",
        "mode": "restart",
        "fields": {
            "windows": {"name": "Windows", "selector": {"target": {"entity": {"domain": "cover"}}}},
            "position": {
                "name": "Position",
                "default": 100,
                "selector": {"number": {"min": 0, "max": 100}},
            },
            "duration": {"name": "Duration", "selector": {"duration": {}}},
            "expires_at": {
                "name": "Absolute UTC expiry",
                "advanced": True,
                "selector": {"number": {"min": 0, "max": 9999999999, "mode": "box"}},
            },
            "request_id": {
                "name": "Retry identity (requires absolute expiry)",
                "advanced": True,
                "selector": {"text": {}},
            },
        },
        "sequence": [
            {
                "variables": {
                    "bindings": mapping,
                    "all_windows": entities,
                    "selection": "{{ windows | default({'entity_id': all_windows}) }}",
                    "requested_position": "{{ position | default(100) }}",
                    "requested_duration": "{{ duration | default(" + str(duration) + ") }}",
                }
            },
            check(
                "{{ selection is not mapping or selection.keys() | list != ['entity_id'] }}",
                "Use the supported window entity/group selector.",
            ),
            {
                "variables": {
                    "selected_ids": "{{ [selection.entity_id] if selection.entity_id is string else selection.entity_id }}"
                }
            },
            {
                "variables": {
                    "selected": "{% set ns = namespace(ids=[]) %}{% for entity in selected_ids %}{% set ns.ids = ns.ids + (all_windows if entity == "
                    + literal(group)
                    + " else [entity]) %}{% endfor %}{{ ns.ids | unique | list }}"
                }
            },
            check(
                "{{ selected | length == 0 or selected | reject('in', all_windows) | list | length > 0 }}",
                "Only the configured logical windows are accepted; raw devices are not targets.",
            ),
            check(
                "{{ not is_number(requested_position) or requested_position | float < 0 or requested_position | float > 100 }}",
                "Position must be between 0 and 100.",
            ),
            {
                "variables": {
                    "seconds": "{{ (requested_duration.get('days',0)|float * 86400 + requested_duration.get('hours',0)|float * 3600 + requested_duration.get('minutes',0)|float * 60 + requested_duration.get('seconds',0)|float) if requested_duration is mapping else requested_duration | float }}"
                }
            },
            check(
                "{{ not is_number(seconds) or seconds | float <= 0 }}", "Duration must be positive."
            ),
            check(
                "{{ request_id is defined and expires_at is not defined }}",
                "Retry identity requires its original absolute expiry.",
            ),
            {
                "variables": {
                    "deadline": "{{ expires_at | default(as_timestamp(now()) + seconds | float) }}",
                    "invocation": "{{ request_id | default('window-' ~ now().isoformat()) }}",
                    "accepted": [],
                    "failed": [],
                }
            },
            check(
                "{{ not is_number(deadline) or deadline | float <= as_timestamp(now()) }}",
                "The requested opening has expired.",
            ),
            {
                "repeat": {
                    "for_each": "{{ selected }}",
                    "sequence": [
                        check(
                            "{{ not has_value(repeat.item) or state_attr(repeat.item, 'control_mode') != 'live' }}",
                            "All requested windows must be available and live before any request is submitted.",
                        )
                    ],
                }
            },
            {
                "repeat": {
                    "for_each": "{{ selected }}",
                    "sequence": [
                        {"variables": {"receipt": {}}},
                        {
                            "action": "ha_operator.request",
                            "data": {
                                "resource_id": "{{ bindings[repeat.item] }}",
                                "target": {"position": "{{ requested_position | float }}"},
                                "expires_at": "{{ deadline | float }}",
                                "request_id": "{{ invocation ~ ':' ~ bindings[repeat.item] }}",
                            },
                            "response_variable": "receipt",
                            "continue_on_error": True,
                        },
                        {
                            "variables": {
                                "accepted": "{{ accepted + ([dict(entity_id=repeat.item, receipt=receipt)] if receipt.get('accepted', false) else []) }}",
                                "failed": "{{ failed + ([] if receipt.get('accepted', false) else [repeat.item]) }}",
                            }
                        },
                    ],
                }
            },
            {
                "if": [{"condition": "template", "value_template": "{{ failed | length > 0 }}"}],
                "then": [
                    {
                        "action": "persistent_notification.create",
                        "data": {
                            "notification_id": "ha_operator_window_admission",
                            "title": "Window request partly failed",
                            "message": "Accepted: {{ accepted | map(attribute='entity_id') | list }}. Failed: {{ failed }}. Accepted requests still expire at {{ deadline | timestamp_local }}.",
                        },
                    },
                    {
                        "stop": "Some window requests failed; accepted resources retain their original expiry.",
                        "error": True,
                    },
                ],
            },
            {
                "variables": {
                    "result": {
                        "accepted": True,
                        "expires_at": "{{ deadline }}",
                        "requests": "{{ accepted }}",
                    }
                }
            },
            {
                "stop": "Requests accepted; physical position is checked separately.",
                "response_variable": "result",
            },
        ],
    }


def preset(name, dispatch_entity, window_entities, position, duration):
    return {
        "alias": name,
        "mode": "restart",
        "sequence": [
            {
                "action": dispatch_entity,
                "data": {
                    "windows": {"entity_id": window_entities},
                    "position": position,
                    "duration": duration,
                },
                "response_variable": "result",
            },
            check(
                "{{ result is not mapping or result.get('accepted') is not sameas true }}",
                "Window admission did not complete.",
            ),
            {"stop": "Window intent accepted", "response_variable": "result"},
        ],
    }


def raw_open_helper(raw_entities, threshold):
    values = (
        "{% set ns = namespace(positions=[]) %}{% for entity in "
        + literal(raw_entities)
        + " %}{% set ns.positions = ns.positions + [state_attr(entity, 'current_position') if has_value(entity) and not state_attr(entity, 'restored') else none] %}{% endfor %}{% set positions=ns.positions %}"
    )
    return {
        "name": "Window Opened",
        "state": values
        + "{{ positions | select('number') | select('gt', "
        + str(threshold)
        + ") | list | length > 0 }}",
        "additional_options": {
            "availability": values
            + "{{ positions | select('number') | select('gt', "
            + str(threshold)
            + ") | list | length > 0 or positions | select('number') | list | length == "
            + str(len(raw_entities))
            + " }}"
        },
    }


def timer_automation(timer, record, policy_id, managed, ready, *, qualify=60, duration=1800):
    """Do not catch up an unadmitted timer epoch after a restart.

    Helper restoration is weaker than Operator persistence. The helper only
    qualifies a new live event; the admitted occurrence owns the absolute expiry.
    """

    def save(value):
        return {
            "action": "input_text.set_value",
            "target": {"entity_id": record},
            "data": {"value": value},
        }

    submit = {
        "action": "ha_operator.submit_occurrence",
        "data": {
            "policy_id": policy_id,
            "occurrence_id": "{{ episode.id }}",
            "expires_at": "{{ episode.expires }}",
        },
        "response_variable": "receipt",
    }
    cancel = {
        "if": "{{ episode.get('id') and episode.get('expires', 0) > as_timestamp(now()) }}",
        "then": [
            {
                "action": "ha_operator.skip_occurrence",
                "data": {
                    "policy_id": policy_id,
                    "occurrence_id": "{{ episode.id }}",
                    "expires_at": "{{ episode.expires }}",
                },
            }
        ],
    }
    gate = "{{ trigger.id == 'startup' or is_state(" + literal(ready) + ", 'on') }}"

    def marker(phase):
        return save(
            "{{ dict(v=1, phase="
            + literal(phase)
            + ", initialized_at=episode.initialized_at) | to_json }}"
        )

    return {
        "alias": "Timed window occurrence",
        "mode": "queued",
        "max": 10,
        "triggers": [
            {
                "trigger": "event",
                "event_type": "timer.started",
                "event_data": {"entity_id": timer},
                "id": "start",
            },
            {
                "trigger": "event",
                "event_type": "timer.restarted",
                "event_data": {"entity_id": timer},
                "id": "restart",
            },
            *[
                {
                    "trigger": "event",
                    "event_type": "timer." + event,
                    "event_data": {"entity_id": timer},
                    "id": "pause" if event == "paused" else "cancel",
                }
                for event in ("cancelled", "paused")
            ],
            {"trigger": "time_pattern", "seconds": "/5", "id": "wake"},
            {"trigger": "homeassistant", "event": "start", "id": "startup"},
        ],
        "conditions": [{"condition": "template", "value_template": gate}],
        "actions": [
            {"condition": "template", "value_template": gate},
            {"variables": {"episode": "{{ states(" + literal(record) + ") | from_json(none) }}"}},
            {
                "if": "{{ trigger.id == 'startup' }}",
                "then": [
                    {"action": "input_boolean.turn_off", "target": {"entity_id": ready}},
                    check(
                        "{{ episode is not mapping or states("
                        + literal(timer)
                        + ") not in ['active','paused','idle'] or (episode and (episode.get('v') != 1 or episode.get('phase') not in ['idle','paused','armed','admitted','replacing'] or (episode.get('id') and (episode.get('id') is not string or episode.get('expires') is not number)))) }}",
                        "Cannot initialize timer clock from unknown state.",
                    ),
                    save(
                        "{{ dict(episode, v=1, initialized_at=as_timestamp(now()), phase=('paused' if is_state("
                        + literal(timer)
                        + ", 'paused') else ('admitted' if episode.get('phase') == 'admitted' else 'idle'))) | to_json }}"
                    ),
                    {
                        "if": "{{ is_state(" + literal(timer) + ", 'paused') }}",
                        "then": [
                            cancel,
                            save(
                                "{{ dict(v=1, phase='paused', initialized_at=as_timestamp(now())) | to_json }}"
                            ),
                        ],
                    },
                    {"action": "input_boolean.turn_on", "target": {"entity_id": ready}},
                    {"stop": "Timer clock initialized without a new occurrence."},
                ],
            },
            check(
                "{{ episode is not mapping or episode.get('v') != 1 or episode.get('initialized_at') is not number or episode.get('phase') not in ['idle','paused','armed','admitted','replacing'] or (episode.get('id') and (episode.get('expires') is not number or episode.get('id') is not string)) or (episode.get('phase') in ['armed','admitted'] and (episode.get('due') is not number or not episode.get('id'))) }}",
                "Malformed or uninitialized timer clock; initialize it before admitting events.",
            ),
            check(
                "{{ trigger.platform == 'event' and as_timestamp(trigger.event.time_fired) <= episode.initialized_at }}",
                "Discarding an event captured before timer-clock initialization.",
            ),
            {
                "choose": [
                    {
                        "conditions": "{{ trigger.id == 'restart' and episode.phase == 'paused' }}",
                        "sequence": [
                            cancel,
                            marker("idle"),
                            {"stop": "Paused timer resumed without a new opening."},
                        ],
                    },
                    {
                        "conditions": "{{ trigger.id in ['start','restart'] }}",
                        "sequence": [
                            save("{{ dict(episode, phase='replacing') | to_json }}"),
                            cancel,
                            {
                                "variables": {
                                    "epoch": "{{ as_timestamp(trigger.event.time_fired) }}"
                                }
                            },
                            save(
                                "{{ dict(v=1, initialized_at=episode.initialized_at, id='timer-' ~ epoch, due=epoch + "
                                + str(qualify)
                                + ", expires=epoch + "
                                + str(qualify + duration)
                                + ", finish=state_attr("
                                + literal(timer)
                                + ", 'finishes_at'), phase='armed') | to_json }}"
                            ),
                        ],
                    },
                    {
                        "conditions": "{{ trigger.id in ['pause','cancel'] }}",
                        "sequence": [
                            save(
                                "{{ dict(episode, phase=('paused' if trigger.id == 'pause' else 'idle')) | to_json }}"
                            ),
                            cancel,
                            save(
                                "{{ dict(v=1, initialized_at=episode.initialized_at, phase=('paused' if trigger.id == 'pause' else 'idle')) | to_json }}"
                            ),
                        ],
                    },
                    {
                        "conditions": "{{ trigger.id == 'wake' and episode.get('phase') == 'armed' and episode.get('due', 0) <= as_timestamp(now()) and episode.get('expires', 0) > as_timestamp(now()) }}",
                        "sequence": [
                            {"condition": "state", "entity_id": timer, "state": "active"},
                            {
                                "condition": "template",
                                "value_template": "{{ episode.get('finish') == state_attr("
                                + literal(timer)
                                + ", 'finishes_at') }}",
                            },
                            {
                                "condition": "state",
                                "entity_id": managed,
                                "attribute": "control_mode",
                                "state": "live",
                            },
                            {"condition": "state", "entity_id": ready, "state": "on"},
                            submit,
                            save("{{ dict(episode, phase='admitted') | to_json }}"),
                        ],
                    },
                ]
            },
        ],
    }


def cold_automation(sensor, record, eligible, ready, *, threshold=16, qualify=1800):
    """A native qualification clock; manual leases remain above the cold policy."""

    def save(value):
        return {
            "action": "input_text.set_value",
            "target": {"entity_id": record},
            "data": {"value": value},
        }

    return {
        "alias": "Window cold qualification",
        "mode": "queued",
        "max": 5,
        "triggers": [
            {"trigger": "state", "entity_id": sensor},
            {"trigger": "time_pattern", "seconds": "/5"},
            {"trigger": "homeassistant", "event": "start", "id": "startup"},
        ],
        "actions": [
            {
                "condition": "template",
                "value_template": "{{ trigger.id | default('') == 'startup' or is_state("
                + literal(ready)
                + ", 'on') }}",
            },
            {"variables": {"episode": "{{ states(" + literal(record) + ") | from_json({}) }}"}},
            {
                "if": "{{ episode is not mapping or (episode.get('due') is not none and not is_number(episode.get('due'))) }}",
                "then": [
                    save("{}"),
                    {"action": "input_boolean.turn_off", "target": {"entity_id": eligible}},
                    {"stop": "Invalid qualification record; disarmed."},
                ],
            },
            {
                "choose": [
                    {
                        "conditions": "{{ trigger.id | default('') == 'startup' }}",
                        "sequence": [
                            {"action": "input_boolean.turn_off", "target": {"entity_id": eligible}},
                            save("{{ dict(episode, fresh_after=as_timestamp(now())) | to_json }}"),
                            {"action": "input_boolean.turn_on", "target": {"entity_id": ready}},
                        ],
                    },
                    {
                        "conditions": "{{ not is_number(states(" + literal(sensor) + ")) }}",
                        "sequence": [
                            {
                                "stop": "Temperature unavailable; preserve the prior cold episode without inventing a reading."
                            }
                        ],
                    },
                    {
                        "conditions": [
                            {
                                "condition": "numeric_state",
                                "entity_id": sensor,
                                "below": threshold,
                            }
                        ],
                        "sequence": [
                            {
                                "condition": "template",
                                "value_template": "{{ not state_attr("
                                + literal(sensor)
                                + ", 'restored') and as_timestamp(states["
                                + literal(sensor)
                                + "].last_reported) >= episode.get('fresh_after', 0) }}",
                            },
                            {
                                "if": "{{ not episode.get('due') }}",
                                "then": [
                                    save(
                                        "{{ dict(v=1, fresh_after=episode.get('fresh_after',0), due=as_timestamp(now()) + "
                                        + str(qualify)
                                        + ") | to_json }}"
                                    )
                                ],
                                "else": [
                                    {
                                        "if": "{{ episode.due <= as_timestamp(now()) }}",
                                        "then": [
                                            {
                                                "action": "input_boolean.turn_on",
                                                "target": {"entity_id": eligible},
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
                "default": [
                    {"action": "input_boolean.turn_off", "target": {"entity_id": eligible}},
                    save("{}"),
                ],
            },
        ],
    }


def occurrence_actions(providers, *, duration=1800):
    """One bounded, deduplicated occurrence per actual numeric-state transition."""
    return [
        check(
            "{{ trigger.from_state is none or trigger.to_state is none or not is_number(trigger.from_state.state) or not is_number(trigger.to_state.state) or trigger.to_state.attributes.get('restored', false) }}",
            "A fresh numeric transition is required; no startup catch-up.",
        ),
        {
            "variables": {
                "epoch": "{{ as_timestamp(trigger.to_state.last_changed) }}",
                "providers": providers,
            }
        },
        {"variables": {"deadline": "{{ epoch + " + str(duration) + " }}"}},
        check("{{ deadline <= as_timestamp(now()) }}", "The occurrence has expired."),
        {
            "repeat": {
                "for_each": "{{ providers }}",
                "sequence": [
                    check(
                        "{{ not has_value(repeat.item.entity_id) or state_attr(repeat.item.entity_id, 'control_mode') != 'live' }}",
                        "All occurrence windows must be available and live.",
                    )
                ],
            }
        },
        {
            "repeat": {
                "for_each": "{{ providers }}",
                "sequence": [
                    {
                        "action": "ha_operator.submit_occurrence",
                        "data": {
                            "policy_id": "{{ repeat.item.policy_id }}",
                            "occurrence_id": "{{ 'transition-' ~ epoch }}",
                            "expires_at": "{{ deadline }}",
                        },
                        "response_variable": "receipt",
                    }
                ],
            }
        },
    ]


def overdue_automation(resource, managed, desired, status, record, *, baseline=7, timeout=300):
    """Notify once while a return target has lacked raw confirmation too long."""

    def save(value):
        return {
            "action": "input_text.set_value",
            "target": {"entity_id": record},
            "data": {"value": value},
        }

    notification_id = "ha_operator_return_" + resource.lower()
    return {
        "alias": "Window return confirmation",
        "mode": "queued",
        "max": 5,
        "triggers": [
            {"trigger": "state", "entity_id": [managed, desired, status]},
            {"trigger": "time_pattern", "seconds": "/5"},
            {"trigger": "homeassistant", "event": "start"},
        ],
        "actions": [
            {
                "variables": {
                    "episode": "{{ states(" + literal(record) + ") | from_json({}) }}",
                    "target": "{{ states(" + literal(desired) + ") }}",
                    "observed": "{{ state_attr("
                    + literal(managed)
                    + ", 'current_position') if has_value("
                    + literal(managed)
                    + ") else none }}",
                }
            },
            {
                "if": "{{ episode is not mapping or (episode and (episode.get('due') is not number or episode.get('target') is not number)) }}",
                "then": [save("{}"), {"stop": "Malformed alert clock reset."}],
            },
            {
                "if": "{{ not is_number(target) }}",
                "then": [
                    {
                        "if": "{{ states("
                        + literal(status)
                        + ") in ['idle', 'observe', 'hands_off'] }}",
                        "then": [
                            {
                                "action": "persistent_notification.dismiss",
                                "data": {"notification_id": notification_id},
                            },
                            save("{}"),
                        ],
                    },
                    {
                        "stop": "No current numeric return target; retain clocks only during unavailable initialization."
                    },
                ],
            },
            {
                "choose": [
                    {
                        "conditions": "{{ target | float <= "
                        + str(baseline)
                        + " and state_attr("
                        + literal(managed)
                        + ", 'control_mode') == 'live' and (observed is not number or (observed - target | float) | abs > 2) }}",
                        "sequence": [
                            {
                                "choose": [
                                    {
                                        "conditions": "{{ episode.get('target') != target | float }}",
                                        "sequence": [
                                            save(
                                                "{{ dict(target=target|float, due=as_timestamp(now()) + "
                                                + str(timeout)
                                                + ", notified=false) | to_json }}"
                                            )
                                        ],
                                    },
                                    {
                                        "conditions": "{{ episode.get('due', 0) <= as_timestamp(now()) and not episode.get('notified', false) }}",
                                        "sequence": [
                                            {
                                                "action": "ha_operator.explain",
                                                "data": {"resource_id": resource},
                                                "response_variable": "explanation",
                                            },
                                            {
                                                "action": "persistent_notification.create",
                                                "data": {
                                                    "notification_id": notification_id,
                                                    "title": "Window return not confirmed",
                                                    "message": "Window: "
                                                    + managed
                                                    + ". Desired: {{ target }}%; observed: {{ observed if observed is number else 'unavailable' }}. Details: {{ explanation.resources["
                                                    + literal(resource)
                                                    + "] | to_json }}",
                                                },
                                            },
                                            save("{{ dict(episode, notified=true) | to_json }}"),
                                        ],
                                    },
                                ]
                            }
                        ],
                    }
                ],
                "default": [
                    {
                        "if": "{{ episode | length > 0 }}",
                        "then": [
                            {
                                "action": "persistent_notification.dismiss",
                                "data": {"notification_id": notification_id},
                            },
                            save("{}"),
                        ],
                    },
                ],
            },
        ],
    }
