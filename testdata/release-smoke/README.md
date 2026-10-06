# Installed release smoke fixtures

These test-only bindings come from the Python binding emitter 0.1.0 and the
`13-dependency-schema-only` conformance case for core profile schema 0.2.0.
The dependency model contains only its validation schema. All models, manifests,
and extension definitions are stored here so release checks need no sibling
repository or published binding package.

`scripts/smoke_install.py` assembles two wheels with recorded SHA-256 file hashes
and test-fixture release provenance referring to the profiler artifact under
test. The root wheel records the exact dependency wheel digest. Both package
initializers raise if imported; the workload also raises if executed.

The positive workload declares a job with command `sample`. The negative
command `too-long` must fail the dependency's `command-limit` schema, even though
that dependency contributes no declarations and is omitted from the profile's
extension list. These fixture wheels are never published as release assets.
