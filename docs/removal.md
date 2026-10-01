# Remove HA Operator

Removing Operator withdraws its control ownership. Unload and uninstall do not
close a cover, stop a fan, or establish a safe physical state. Keep the raw source
integration and any replacement controls available.

1. Identify automations, scenes, dashboards, and HomeKit bridges that reference
   Operator entities or actions. Preserve any configuration needed for rollback.
2. With healthy storage, set each resource to **observe** and verify that
   Operator emits no commands. If storage is inhibited or setup failed, mode
   changes can be unavailable. Verify the inhibition and Repair instead, and use
   the raw source or replacement controller to establish the physical state.
   Transfer control to the intended replacement owner. Avoid two live owners of
   the same output. Verify the physical state through independent feedback.
3. Preserve a copy of `ha_operator.ENTRY_ID.json` in your Home Assistant
   configuration directory. Keep local trace files if you need incident evidence
   or rollback. These files can contain accepted intent and local observations.
4. Remove dependent policies and airflow requirements before their resources.
   Remove desired controls only after removing their policy references and ON
   links from other desired controls. Use the subentry menu in
   **Settings > Devices & services > HA Operator**.
5. Remove the **HA Operator** configuration entry. Update or remove dependent
   automations, dashboards, and bridge references to the removed entities.
6. Remove the package through HACS if HACS installed it. For an archive install,
   delete `/config/custom_components/ha_operator/`. Restart Home Assistant.

Removing the entry does not delete Operator's authoritative JSON snapshot or
local trace files. After deciding that rollback and evidence retention are no
longer needed, delete the files for the removed entry from the configuration
directory; traces for that entry are in `ha_operator_trace.ENTRY_ID/`. See [observability](observability.md) and
[privacy](privacy.md) for trace retention and export details. Keep snapshots
private; deleting them destroys accepted intent, not physical device state.

If invalid configuration prevents setup, native subentry removal remains
available. Remove only the offending dependencies, then select **Reload** if you
intend to keep using Operator. For storage failure, follow the Repair, preserve
the saved bytes, and reload after correcting the cause.
