# Remove HA Operator

Unload and uninstall withdraw control ownership without changing physical
outputs. Keep the raw source integration and replacement controls available.

1. Back up Home Assistant configuration and storage if rollback is needed.
2. Set resources to **Observe**, verify zero Operator commands, and transfer
   control to the intended replacement owner. Check independent physical feedback.
3. Remove dependent policies and airflow requirements before resources. Remove
   desired controls after their policy references and ON links. Use the subentry
   menu in **Settings > Devices & services > HA Operator**.
4. Remove the integration entry. Home Assistant removes its Operator Store data.
   Update automations, dashboards, scenes, and HomeKit references.
5. Remove the package through HACS, or delete
   `/config/custom_components/ha_operator/` for an archive install. Restart HA.

If invalid configuration prevents setup, native subentry removal remains
available. Correct or remove the named dependency and reload if keeping Operator.
Preserve unusable saved data before repairing it; do not replace intent with
telemetry. See [troubleshooting](troubleshooting.md).

The 0.2.0 upgrade removes only the entry's obsolete snapshot and Operator trace
paths after backup. Backups supply rollback; keep them private.
