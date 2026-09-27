# Publication privacy

Public examples and tests use synthetic identifiers and inputs. Keep actual
installation inventories, schedules, trace captures, incident histories, and
replay bindings in a private directory outside the repository. Do not reuse
conversation, device, or configuration IDs as convenient fixture values.

The lab uses disposable credentials and independent simulated devices. Its
authentication values still require redaction before publication. The sanitizer
covers exact secrets, authentication response fields, credentials in URL queries,
JSON-encoded request bodies, pairing keys, and local home paths, including text
inside nested browser trace archives. It preserves binary images; inspect any
screenshots or other binary evidence before sharing them.

Each CI artifact upload requires the read-only publication privacy gate to pass.
A failed gate prevents that upload. Execution receipts bind the delivered file
hashes after sanitization; post hoc evidence corrections must carry explicit
provenance and must not claim that altered bytes are the original bundle.

Before a release, scan a fresh public-side copy of all refs and every published
asset, including nested archives. Check known private indicators locally without
committing those indicators to the repository. A scanner does not establish
that a narrative or screenshot is safe to publish; review those explicitly.
