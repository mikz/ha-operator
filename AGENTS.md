# ha-operator

Use the accepted implementation plan in this conversation and docs/development-contract.md.
Development and tests must never connect to production Home Assistant. The lab has no
production credentials, devices, host network, or external routes. Build dependencies
before starting the isolated runtime. Do not weaken isolation to make a test pass.

Use the persistent `/root/ha_platform` advisor for native HA API/lifecycle questions
while that identity exists. Advisors are read-only. Implementation, tests, and acceptance
remain with owners. Consult through the consult-advisor skill; do not spawn duplicates.

Keep requested, effective, dispatched, and observed state separate. Manual closure is
allowed even if airflow becomes unmet. Never stop extraction as an airflow fallback.
Never infer manual ownership from telemetry or claim physical feedback from intent.

Test the exact release archive; keep the simulator out of custom_components/ha_operator.
All shared-checkout workers must preserve one another's edits and respect file ownership.
