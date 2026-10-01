"""Concrete records admitted by the existing pure configuration validators."""

from typing import Literal, NotRequired, TypedDict

type ResourceKind = Literal["cover", "switch", "fan", "relay_fan"]
type TargetField = Literal["position", "on", "percentage", "direction"]
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


class RelayProfileConfig(TypedDict):
    outputs: dict[str, bool]
    percentage: NotRequired[float]
    direction: NotRequired[Literal["forward", "reverse"]]


class ReturnMonitorConfig(TypedDict):
    target_at_most: float
    warning_after_seconds: float


class NumericInputConfig(TypedDict):
    type: Literal["qualified_numeric"]
    entity_id: str
    comparison: Literal["below"]
    threshold: float
    unit: str
    qualification_seconds: float


class TimerInputConfig(TypedDict):
    type: Literal["timer_episode"]
    entity_id: str
    qualification_seconds: float
    request_seconds: float


type PolicyInputConfig = NumericInputConfig | TimerInputConfig


class ResourceConfig(TypedDict):
    name: str
    kind: ResourceKind
    retry_interval: float
    command_interval: float
    movement_timeout: float
    tolerance: float
    manual_duration: float
    entity_id: NotRequired[str]
    outputs: NotRequired[list[str]]
    profiles: NotRequired[dict[str, RelayProfileConfig]]
    reversal_dead_time: NotRequired[float]
    default_target: NotRequired[TargetData]
    restriction_entity: NotRequired[str]
    fault_entity: NotRequired[str]
    manual_control: NotRequired[bool]
    return_monitor: NotRequired[ReturnMonitorConfig]


class PolicyConfig(TypedDict):
    name: str
    resource_id: str
    kind: Literal["state", "occurrence"]
    priority: int
    target: NotRequired[TargetData]
    eligibility_entity: NotRequired[str]
    eligibility_state: NotRequired[str]
    target_entity: NotRequired[str]
    target_attribute: NotRequired[str]
    target_field: NotRequired[TargetField]
    intent_id: NotRequired[str]
    input: NotRequired[PolicyInputConfig]


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


class NumericStateRecord(TypedDict, total=False):
    """Input state fields with model defaults."""

    due_at: float | None
    qualified: bool
    source_quality: Literal["numeric", "unknown"]
    recovery_pending: bool


class TimerStateRecord(TypedDict, total=False):
    episode_id: str | None
    phase: Literal["idle", "qualifying", "accepted", "suppressed", "expired"]
    due_at: float | None
    expires_at: float | None
    finish_at: float | None


class ReturnMonitorStateRecord(TypedDict):
    target_position: float | None
    due_at: float | None
    overdue: bool


class NumericInputRecord(TypedDict):
    type: Literal["qualified_numeric"]
    fingerprint: str
    state: NumericStateRecord


class TimerInputRecord(TypedDict):
    type: Literal["timer_episode"]
    fingerprint: str
    state: TimerStateRecord


type PolicyInputRecord = NumericInputRecord | TimerInputRecord


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
    policy_inputs: dict[str, PolicyInputRecord]
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
