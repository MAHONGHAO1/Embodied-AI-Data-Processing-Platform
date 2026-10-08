# Capture Source Compatibility Fixtures

These fixtures pin the metadata shapes that QuicData must continue to read
while the generic capture-source contract evolves. They are deliberately
small, contain no production identifiers or signed URLs, and do not include
MCAP payloads.

| Fixture | Producer shape | Expected platform reader |
| --- | --- | --- |
| `v1_legacy` | Historical QRDF EGO V1 object under `raw/v1/tasks/...` | Legacy EGO reader |
| `ego_v2_legacy` | Historical EGO metadata with `collection_task_id` | Legacy EGO reader |
| `generic_v2_flat` | Existing generic source package under `raw/v2/sources/<episode>/` | Generic source reader |
| `generic_v2_partitioned` | New generic source package under `raw/v2/sources/date=.../hour=.../<episode>/` | Generic source reader |

`source_group`, operator ID, and device serial number in the generic fixtures
are untrusted provenance hints. The core QRDF model is validated after those
hints are removed; QuicData projects them separately within the authorized
ImportSession workspace.
