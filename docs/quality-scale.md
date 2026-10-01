# Quality checklist scope

HA Operator is a HACS custom integration. Its
[rule ledger](../custom_components/ha_operator/quality_scale.yaml) uses the Home
Assistant Integration Quality Scale as a maintenance checklist. The ledger is a
self-assessment dated September 30, 2026. It does not award Bronze, Silver, Gold,
or Platinum recognition or claim acceptance into Home Assistant Core.

Each rule records implementation evidence, remaining work, or an applicable
published exception. Local branding is supported for custom integrations and
ships in the package. The literal central brands requirement remains pending
for any future Core submission; it is not a fabricated exemption. Native subentry
changes add devices through automatic reload. Formal applicability of this
mechanism to the dynamic-devices rule remains under review.

The checklist supplements the integration's safety and release gates. Runtime
acceptance, separate physical observation, restart deadlines, one worker per resource owning all its outputs,
STOP behavior, relay reversal, airflow confirmation, and zero-output observe mode
remain required. See [architecture](architecture.md),
[scenario validation](scenarios.md), and [configuration reference](reference.md).
Core classification, external review, and official recognition are separate
processes. The manifest does not claim a medal.

Strict typing, lint, and format checks run against every production module in both
supported Home Assistant environments. The release evidence gate requires both
results and binds them to the archive, source, configuration, dependency locks,
and checked module list. Source checks supplement installed-package validation;
they do not establish device behavior or official recognition.
