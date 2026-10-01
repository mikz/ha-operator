# Observe before live control

Use an isolated lab to validate the release archive before changing a live
installation. Keep deployment inventories and evidence outside the public
repository.

1. Back up Home Assistant configuration and storage.
2. Install the tested archive and enable **Shadow lock** in integration options.
3. Add resources and policies. Verify raw source availability and independent
   physical feedback. Inspect Desired, Observed, Reason, expiry, Recorder history,
   Activity, diagnostics, and `ha_operator.explain`.
4. Exercise relevant inputs while locked. Confirm zero Operator actuator effects
   through the existing integration or independent simulator. Inspect competing
   owners before transferring control.
5. At the approved handover, retire conflicting owners, disable Shadow lock, and
   activate selected resources explicitly. Check configured desired defaults and
   current commands before selecting Live.
6. Verify raw physical effects and expiry. Return resources to observe mode if
   behavior differs from the reviewed configuration.

The lock covers Operator outputs. It does not stop another automation or direct
raw-device command. Native Recorder and Activity retention follow Home Assistant
configuration. See [observability](observability.md) and [migration](migration.md).
