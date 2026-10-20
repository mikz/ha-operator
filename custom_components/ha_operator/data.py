"""Concrete records admitted by the existing pure configuration validators."""

from typing import Any, Literal, NotRequired, TypedDict

type ResourceKind = Literal["cover", "switch", "fan", "relay_fan", "number", "select", "light"]
type TargetField = Literal[
    "position",
    "on",
    "percentage",
    "direction",
    "value",
    "option",
    "brightness",
    "color_temp_kelvin",
]
type Scalar = str | bool | int | float
type JSONValue = Scalar | None | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]


class TargetData(TypedDict, total=False):
    """Normalized target fields; omitted fields impose no constraint."""

    position: float
    on: bool
    percentage: float
    direction: str
    profile: str
    value: float
    option: str
    brightness: float
    white: float
    color_temp_kelvin: float
    hs_color: list[float]
    xy_color: list[float]
    rgb_color: list[float]
    rgbw_color: list[float]
    rgbww_color: list[float]
    effect: str


class RelayProfileConfig(TypedDict):
    outputs: dict[str, bool]
    percentage: NotRequired[float]
    direction: NotRequired[Literal["forward", "reverse"]]


class ReturnMonitorConfig(TypedDict):
    target_at_most: float
    warning_after_seconds: float


class ResourceConfig(TypedDict):
    name: str
    kind: ResourceKind
    retry_interval: float
    command_interval: float
    movement_timeout: float
    tolerance: float
    manual_duration: float
    manual_duration_entity: NotRequired[str]
    entity_id: NotRequired[str]
    unit_of_measurement: NotRequired[str | None]
    outputs: NotRequired[list[str]]
    profiles: NotRequired[dict[str, RelayProfileConfig]]
    reversal_dead_time: NotRequired[float]
    default_target: NotRequired[TargetData]
    restriction_entity: NotRequired[str]
    fault_entity: NotRequired[str]
    manual_control: NotRequired[bool]
    return_monitor: NotRequired[ReturnMonitorConfig]
    command_conditions: NotRequired[dict[str, list[Any]]]


class PolicyConfig(TypedDict):
    name: str
    resource_id: str
    kind: Literal["state", "occurrence"]
    priority: int
    target: NotRequired[TargetData]
    conditions: NotRequired[list[Any]]
    triggers: NotRequired[list[Any]]
    cancel_triggers: NotRequired[list[Any]]
    duration: NotRequired[dict[str, float]]
    duration_entity: NotRequired[str]
    target_entity: NotRequired[str]
    target_attribute: NotRequired[str]
    target_field: NotRequired[TargetField]
    intent_id: NotRequired[str]


class EvidenceConfig(TypedDict):
    entity_id: str
    kind: Literal["position", "contact", "relay", "airflow"]
    operator: Literal["eq", "gte", "lte"]
    value: Scalar
    attribute: NotRequired[str]


class ProviderConfig(TypedDict):
    id: str
    evidence: list[EvidenceConfig]
    resource_id: NotRequired[str]
    target: NotRequired[TargetData]


class RequirementConfig(TypedDict):
    name: str
    activation_entities: list[str]
    acquisition_timeout: float
    providers: list[ProviderConfig]


class IntentConfig(TypedDict):
    """Normalized logical intent configuration; IDs belong to native subentries."""

    name: str
    initial_value: bool
    on_targets: list[str]


class OperatorConfiguration(TypedDict):
    resources: dict[str, ResourceConfig]
    policies: dict[str, PolicyConfig]
    requirements: dict[str, RequirementConfig]
    intents: dict[str, IntentConfig]


type SubentryConfig = ResourceConfig | PolicyConfig | RequirementConfig | IntentConfig


class ReturnMonitorStateRecord(TypedDict):
    target_position: float | None
    due_at: float | None
    overdue: bool


class ReturnMonitorRecord(TypedDict):
    fingerprint: str
    state: ReturnMonitorStateRecord


class ManualRecord(TypedDict):
    mode: Literal["target", "hands_off"]
    target: NotRequired[TargetData | JSONValue]
    expires_at: NotRequired[float | None]
    request_id: NotRequired[str | None]
    source: NotRequired[str | None]
    fan_settings: NotRequired[JSONObject]


class OccurrenceRecord(TypedDict):
    policy_id: str
    occurrence_id: str
    expires_at: float
    skipped: NotRequired[bool]
    trigger_owned: NotRequired[bool]
    target: NotRequired[TargetData | JSONValue]


class RequestReceipt(TypedDict):
    request_id: str
    resource_id: str
    expires_at: float | None


class AcceptedRequestReceipt(RequestReceipt):
    accepted: bool


class RequestRecord(TypedDict):
    fingerprint: str
    receipt: RequestReceipt


class _SnapshotMaps(TypedDict):
    modes: dict[str, Literal["observe", "live"]]
    policy_enabled: dict[str, bool]
    intents: dict[str, bool]
    return_monitors: dict[str, ReturnMonitorRecord]


class StoredSnapshot(_SnapshotMaps):
    """Envelope-validated JSON; manual/occurrence/request semantics are unchecked."""

    manuals: dict[str, JSONValue]
    occurrences: dict[str, JSONValue]
    requests: dict[str, JSONValue]


class RuntimeSnapshot(_SnapshotMaps):
    """Saved intent after runtime semantic validation, or validated state changes."""

    manuals: dict[str, ManualRecord]
    occurrences: dict[str, OccurrenceRecord]
    requests: dict[str, RequestRecord]
