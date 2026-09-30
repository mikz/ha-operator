"""Boolean intent and follower admission never derive ownership from telemetry."""

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from custom_components.ha_operator.configuration import (
    ConfigurationError,
    validate_configuration,
    validate_intent,
    validate_policy,
    validate_resource,
)
from custom_components.ha_operator.intents import apply_command, validate_graph

GRAPH = {"central": {"on_targets": ["room"]}, "room": {"on_targets": []}}


@given(st.lists(st.tuples(st.sampled_from(["central", "room"]), st.booleans()), max_size=100))
def test_asymmetric_attachment(commands):
    actual = {"central": False, "room": False}
    expected = dict(actual)
    for key, value in commands:
        if key == "central" and value and not expected["central"]:
            expected["room"] = True
        expected[key] = value
        apply_command(actual, GRAPH, key, value)
        assert actual == expected


def test_graph_branches_and_shared_descendants():
    graph = {
        **GRAPH,
        "other": {"on_targets": ["room"]},
        "root": {"on_targets": ["central", "other"]},
    }
    validate_graph(graph)
    actual = {}
    apply_command(actual, graph, "root", True)
    assert actual == dict.fromkeys(graph, True)


@pytest.mark.parametrize(
    "graph",
    [
        {"a": {"on_targets": ["a"]}},
        {"a": {"on_targets": ["b"]}, "b": {"on_targets": ["a"]}},
        {"a": {"on_targets": ["missing"]}},
    ],
)
def test_bad_graph_rejected_before_admission(graph):
    with pytest.raises(ValueError):
        validate_graph(graph)
    entries = [
        SimpleNamespace(
            subentry_type="intent",
            subentry_id=key,
            data={"name": key, "initial_value": False, **value},
        )
        for key, value in graph.items()
    ]
    with pytest.raises(ConfigurationError):
        validate_configuration(entries)


@pytest.mark.parametrize("key,on", [("missing", True), ("central", 1), ("central", "off")])
def test_bad_command(key, on):
    with pytest.raises(ValueError):
        apply_command({}, GRAPH, key, on)


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_value": None},
        {"initial_value": 1},
        {"on_targets": "room"},
        {"on_targets": ["room", "room"]},
        {"on_targets": [""]},
        {"unknown": True},
    ],
)
def test_bad_intent_configuration(changes):
    with pytest.raises(ConfigurationError):
        validate_intent({"name": "Mode", "initial_value": False, **changes})


def test_typed_source_and_manual_permission():
    resource = {
        "kind": "switch",
        "name": "Follower",
        "entity_id": "switch.raw",
        "manual_control": False,
    }
    assert validate_resource(resource)["manual_control"] is False
    with pytest.raises(ConfigurationError, match="manual_control"):
        validate_resource({**resource, "manual_control": "no"})
    policy = {"name": "Follow", "kind": "state", "resource_id": "follower", "intent_id": "room"}
    assert validate_policy(policy, {"follower": resource}, intents=GRAPH)["intent_id"] == "room"
    for changes in ({"intent_id": "missing"}, {"target": {"on": True}}, {"kind": "occurrence"}):
        with pytest.raises(ConfigurationError):
            validate_policy({**policy, **changes}, {"follower": resource}, intents=GRAPH)
    with pytest.raises(ConfigurationError):
        validate_policy(policy, {"follower": {"kind": "cover"}}, intents=GRAPH)
