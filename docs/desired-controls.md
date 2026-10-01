# Desired controls and source followers

A desired control is an Operator-owned native switch that stores a boolean
intent. It holds that value until the next explicit command. It has no manual
lease or expiry. A policy can use this control directly as its target source;
there is no second helper to synchronize.

This feature coordinates boolean mode flags. The downstream integration still
owns brightness, color, and other lighting behavior.

## Configure a source

Add a **Desired control** subentry. Choose its name and initial value. The
initial value applies when no saved value exists.
Editing the initial value later does not overwrite existing intent.

Optionally select other desired controls under **Also turn on these desired
controls**. An OFF-to-ON command turns those controls on in the same state update. Repeating ON leaves independently changed dependents alone. OFF never
turns a dependent off. Cycles and missing references are rejected.

For example, configure `Central sleep` to turn on `Room sleep`:

| Command | Central desired | Room desired |
| --- | --- | --- |
| Initial values | off | off |
| Central on | on | on |
| Room off | on | off |
| Central on again | on | off |
| Central off | off | off |
| Room on | off | on |
| Central on, then off | off | on |

A restart restores the saved pair. It does not synthesize an ON transition.
A successful command means the runtime accepted the desired value; physical
convergence is a separate result.

## Configure followers

1. Add a switch resource bound to the downstream switch.
2. Turn off **Allow manual control** if this resource must always follow its
   source. This removes the managed command switch, manual-active sensor,
   expiry sensor, and resume-automatic button. Request, hands-off, release,
   and STOP admission is rejected for the resource.
   Existing manual entities are integration-disabled on reload; their registry
   identity and customizations remain. Re-enabling permission restores those
   entities, preserves user-disabled settings, and does not revive an old lease.
3. Add a state policy for the resource. Select its **Operator desired control**.
   Do not also configure a fixed target or external target entity.
4. Inspect the desired, observed, reason, and status sensors in observe mode.
5. Transfer output ownership before selecting live mode.

The source reference uses the stable subentry ID and reads the current desired value
directly. It does not depend on an entity ID slug or the timing of HA state
publication. Other managed-entity policy inputs remain prohibited.

The observed sensor reports the downstream integration's state. A reported KNX
bit or an Adaptive Lighting mode is not evidence of physical lamp brightness.
Unknown feedback remains unknown. Failed convergence retains the source's
current target and uses normal paced recovery.

Desired and reason sensors link to the authoritative desired switch through
`related_entities`. Native History records source and follower changes. Native
HA command context links the accepted mode change, coupled control changes,
and dispatched follower commands while the process is running. A restart does
not invent a command context for restored intent.

## Migrate existing controls

Keep installation names, addresses, configuration bodies, and receipts in a
private deployment record. Use these stages for a source-only migration:

1. Inventory every writer and reader: schedules, scripts, scenes, dashboards,
   public groups, HomeKit exposure, device actions, templates, and other
   integrations. Record the current central and room values separately. Do not
   infer one from the other or accept unknown as off.
2. Confirm the input contract. If only HA, HomeKit, and schedules change sleep
   mode, legacy KNX sleep bits become output followers. KNX state reports,
   echoes, and reconnect restoration never change desired intent. A deployment
   with physical KNX sleep buttons additionally needs a qualified command input;
   ordinary state telemetry cannot substitute for it.
3. Save the current working configurations and enabled states. Preserve fixed
   one-way synchronization definitions for rollback, not older definitions with
   a feedback loop. Record entity registry and HomeKit accessory identities.
4. Install the tested package. Add the room desired control, then the central
   control with its ON target. Add each downstream flag as a separate resource
   with manual control disabled and a state policy referencing its source.
   Keep every new follower in observe mode.
5. Check each configured initial value. The 0.2.0 upgrade starts fresh and does
   not import old runtime values. Use normal desired-switch commands while
   followers remain in observe mode to establish reviewed values. To request
   central ON with room OFF, turn central ON first, then turn room OFF.
6. At the handover, retire both synchronization automations and the separate
   central-ON-to-room-ON automation. Quiesce running actions. Existing schedule
   automations remain; repoint their actions to the desired controls. Repoint
   scripts, scenes, and conditions that used a legacy source directly.
7. Preserve public group entity identities where possible and replace their
   members with the corresponding desired switch only. A public group must
   not write directly to follower outputs. Retain the existing HomeKit bridge
   and pairing. Verify its accessory mapping; matching names alone do not
   establish identity preservation.
   If HomeKit currently exposes raw outputs directly, replacing those entries
   with Operator desired controls creates new accessory identities. Record and
   update affected Apple Home scenes, automations and favorites. Native HomeKit
   derives identity from the integration, domain and registry unique ID; an
   entity-name or slug change cannot preserve it across integrations.
   After editing the bridge options, wait for its diagnostics to report a
   running HAP server with the intended accessory mapping. The config entry
   can be loaded before the server is ready. Options already request a reload;
   do not add a second reload while it is starting.
8. Check for commands during the handover. Establish reviewed values while the old
   entry points remain authoritative. Once a new desired control has accepted a
   command, do not overwrite it with a later read of legacy feedback. If commands
   cross the transfer boundary ambiguously, pause activation and resolve their
   order from the receipt before continuing. Verify references and inspect the
   effective targets, then transfer room followers before central followers.
   Delayed legacy output feedback must not modify the new desired pair.
9. Verify natural control through each preserved entry point, independent room
   OFF, central OFF independence, the next central ON attachment, and the next
   scheduled wake. Compare desired and observed histories. Keep physical lamp
   behavior and reported mode convergence as separate observations.

The isolated `sleep` lab scenario exercises native setup, configured defaults, absence of
manual follower controls, group commands, SIGKILL recovery, genuine encrypted
HAP commands, preserved HAP identity across Operator reload, and the native
dashboard. It pairs existing native group helpers before replacing their raw
members with a desired control and checks the same accessory identity afterward.
Its simulator proves switch effects and ordering, not a KNX bus or
the downstream lighting integration. Production cutover requires its own
configuration readback and observation receipt.
An additional native bridge scenario replaces directly exposed raw switches
with desired controls. It verifies new accessory IDs, preserved pairing and
unrelated accessories, then issues encrypted HAP commands and checks simulator
effects. Neither test checks Apple Home scenes, automations or favorites;
client-side rebinding still requires its own review.

## Roll back the control migration

1. Put all new followers in observe mode and confirm their workers are quiescent.
2. Preserve the last saved central and room values independently.
3. Restore public group membership and direct caller references from the private
   receipt. Keep the corrected synchronization automations disabled while
   transferring the two source values, so their ON coupling cannot overwrite a
   detached room value during restoration.
   Restore the bridge's recorded exposure and check Apple Home references when
   the forward migration replaced directly exposed raw accessories.
4. Restore the corrected one-way mirrors and the previous coupling automation
   after both legacy sources reflect the preserved values. Verify each output
   has exactly one controller.
5. Retain the new integration and native Store while other resources remain
   active. A binary downgrade to v0.1.6 requires restoring its matching deployment
   backup; it cannot read the new native Store. Plan rollback separately from
   transferring current desired values.

Existing Recorder history remains on the original entities. New desired controls
start their own history; keeping a public group preserves its identity, not the
meaning of every historical attribute. Record the cutover time and old-to-new
entity mapping in the private receipt.
