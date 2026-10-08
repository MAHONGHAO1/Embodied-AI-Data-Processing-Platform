# QRDF Cross-Repository Contract Fixtures

`qrdf_source_group_vectors.json` is the checked-in copy of the QRDF source
group normalization contract used by QuicData's projection tests. It keeps the
platform test suite self-contained: `backend/vendor/qrdf` is a deployment-time
SDK checkout and must not be used as a test fixture dependency.

When the QRDF contract changes, update the QRDF canonical fixture and this
copy in the same integration change. The vector values intentionally cover
Unicode normalization, whitespace folding, case sensitivity, and rejected
control characters.
