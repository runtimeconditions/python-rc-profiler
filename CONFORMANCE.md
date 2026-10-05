# Python profiler Phase 4 conformance

**Result (2026-10-04): accepted in the tested local scope.** The separately
installed Python profiler passed **19 positive** and **six negative** official
consumer workloads, then **29 supplemental** trust-chain and execution-boundary
checks: **54 passed, zero failed, zero pending**. The
[consolidated Phase 4 review](../phase4-dependency-schema-review-20261004/consolidated-review.yaml)
accepts all applicable Section 13 gates (**1–14 and 16**) across the 14 Python
distributions and 14 Go modules. The [Phase 4 summary](../extensions/tooling/extension-bindings/PHASE-4.md)
records the shared package-generation checks. This report explains the Python
profiler's installed CLI evidence and the limits of that result.

## Contract and installed boundary

| Piece | Role |
| --- | --- |
| [Implementation contract](../extensions/tooling/extension-bindings/IMPLEMENTATION.md) | Section 11 defines the structural manifest and four fixed package resources; Section 13 defines verification gates and the Phase 4 exit criteria. |
| [Approved core profile schema](../spec/schema/runtimeconditions.profile.v0.1.0.schema.yaml) | Included in the profiler wheel. The installed CLI checks its identity and digest and validates each complete profile without reading the schema checkout. |
| [Installed binding discovery](runtimeconditions_profiler/project/installed.py) | Parse workload imports, map import packages to installed distributions through Python metadata, and read the four fixed resources without importing binding or application code. |
| [Binding verification](runtimeconditions_profiler/project/verify.py) | Validate resource schemas, distribution ownership, package and extension identities, versions, semantic digests, release provenance, dependency locks, and exact extension closure. |
| [Structural extraction](runtimeconditions_profiler/profile/generated.py) | Reconstruct generated declaration calls statically from verified manifest coordinates, including optional values, enums, collections, maps, unions, aliases, exact serialized names, and cross-module references. Unresolved recognized calls fail at their source location. |
| [Profile validation](runtimeconditions_profiler/profile/semantic.py) | Apply the bundled core schema, vocabulary ownership rules, and every applicable Draft 2020-12 extension schema in the full verified closure. Emit only directly contributing extension IDs and write only after validation succeeds. |

The workload imports installed binding distributions by their normal Python
module names. The profiler wheel and those distributions are installed in the
same environment through `pip`. The profiler receives no `extensions` source
checkout, local extension-definition path, editable installation, or
checkout-based `PYTHONPATH`. Each binding distribution provides
`runtimeconditions.bindings.yaml`, `runtimeconditions.binding-model.yaml`,
`runtimeconditions.extension.yaml`, and
`runtimeconditions.binding-release.yaml` beside its installed import package.
The CLI reads their bytes through distribution file records; it does not
execute package initialization to discover them.

## Reviewed consumer corpus

The [reviewed catalog](../extensions/tooling/extension-bindings/fixtures/conformance.yaml)
defines complete Conditions, direct contributors, positive declarations, and
negative constraints independently of profiler output. The official
[v4 assembly](../phase4-dependency-schema-fixtures-v4-20261004/) contains
**14 test-only Python wheels** from ten positive cases. The
[v4 prepared inventory](../phase4-dependency-schema-results-v4-20261004/inventory.yaml)
has **19 positive** and **six negative** Python workloads. Its workloads cover
all **13 emitted calls** plus **two completed deferred declarations**. The
leaf and middle consumers supply the core-required `interface` through
installed dependency bindings, so every counted positive is a complete
profile rather than a successful rejection.

Each ID below has `python/workloads/<ID>/app.py` and pinned
`requirements.txt` under the prepared result tree. A positive has a complete
`python/expected/<ID>.yaml`; a negative has an exact
`python/expected/<ID>.error.yaml`. The inventory records source and oracle
hashes, commands, direct contributors, and full closure.

| Source case | Positive profile workload IDs | Negative workload IDs | Main check |
| --- | --- | --- | --- |
| `01-owned-kind-interface` | `owned` | — | Owned kind, interface, and field |
| `02-additive-field` | `additive-owner`, `additive-consumer` | — | Direct additive ownership |
| `03-transitive-closure` | `transitive-leaf-consumer`, `transitive-middle-consumer`, `transitive-root-consumer` | — | Complete declarations and transitive dependencies |
| `06-recursive-reference` | `recursive` | — | Recursive object and required values |
| `07-object-alternatives` | `alternatives-command`, `alternatives-image` | `alternatives-missing`, `alternatives-both` | Branch-dependent `oneOf` |
| `08-heterogeneous-union` | `union-object`, `union-string` | `union-missing-id` | Object and scalar union variants |
| `09-collections-and-maps` | `collections`, `collection-second-value` | `collection-invalid-value` | Arrays, maps, and allowed numbers |
| `10-scoped-domains-collisions` | `scoped-grpc`, `scoped-http`, `scoped-http-proxy`, `scoped-optional-omitted` | `scoped-invalid-value` | Scoped values, collisions, optional omission |
| `11-source-name-preservation` | `source-names` | — | Exact serialized names, including Unicode |
| `13-dependency-schema-only` | `dependency-schema-only` | `dependency-schema-invalid` | Validation-only dependency |

The three normalizer negatives, `04-dependency-cycle`,
`05-vocabulary-conflict`, and `12-unsupported-structural-keyword`, have
separate exact diagnostics under the [shared cases](../extensions/tooling/extension-bindings/model/conformance/cases/).
They produce no Python binding wheels and are covered by the shared gate
review, not counted among the six installed-profiler negatives.

Case 13 tests a distinction that the earlier corpus lacked. Its root owns
`job`, `process`, and `command`; its dependency owns **no vocabulary** and
provides the sole `command-limit` schema (`maxLength: 6`). The positive
profile lists only the root ID, while `verify-bindings` finds both root and
dependency in the closure. The negative's `too-long` value passes the other
applicable schemas but fails that dependency schema at the recorded `schema`
stage. The exact normalized diagnostic names the dependency schema, and no
profile is written. The [YAML result](../python-profiler-dependency-schema-acceptance-final-20261004/summary.yaml)
records the direct IDs, closure, coordinate, hashes, and failure result.

## Installed CLI acceptance

The external [acceptance runner](../python-profiler-cli-acceptance/run_phase4_acceptance.py)
served the tested profiler wheel and binding wheels from a temporary PEP 503
HTTP package index. It created isolated Python environments, installed the
profiler and binding distributions **by name and version** through `pip`, and
checked the `pip --report` URLs for HTTP retrieval. It removed ambient
`PYTHONPATH`, `PYTHONHOME`, virtual-environment, and `PIP_` settings from the
profiler runtime environment. The workload sources use absolute imports from
those installed distributions; they contain a sentinel that raises if
executed. The installed `runtimeconditions-python-profiler` console script,
not source code from this repository, ran `profile verify-bindings` and
`profile generate` for every case.

| Check | Final result |
| --- | --- |
| Positive profiles | **19/19** successful; full YAML matches the independently reviewed bytes and repeats identically. Emitted extension IDs match direct contributors. |
| Negative declarations | **6/6** have the expected failure stage and exact stderr after replacing the absolute workload path with `<workload>`; each exits nonzero and leaves no output. |
| Installed identities and closure | All **14** wheels, their four fixed resources, release provenance, installed distribution identities and versions, and every workload's full extension closure match the prepared inventory. |
| Schema-only dependency | Direct contributors are a proper subset of closure; the negative specifically names the omitted dependency's `command-limit` schema. |
| Supplemental suite | **29/29** checks pass: 16 profiles and 13 expected rejections covering altered or absent resources, wrong identities and digests, missing dependencies, dynamic source, and code-execution boundaries. |
| Regression suite | `86 passed` after the scoped-enum correction; no later profiler source change was needed for case 13. |

The supplemental [runner](../python-profiler-cli-acceptance/run_acceptance.py)
also installs a modified wheel whose package initialization raises and profiles
a workload that raises if executed. It still produces the exact expected
profile, demonstrating static operation. Its other negatives exercise the
trust chain and expression boundary beyond the six official declarations.

The installed run used **Python 3.12.10** and profiler wheel SHA-256
`1a845ba0edd27eb02d877b7acd001878cf5b587b3fba36143eb7b6dfd19abbf6`.
The approved core schema source SHA-256 is
`ad101336b676b468ec975aff45c21749df22fddf422371156c586ed62abf3223`.
The profiler wheel supplies that schema; no source checkout is a runtime
input. The [artifact manifest](../python-profiler-dependency-schema-acceptance-final-20261004/artifact-manifest.json)
records the runner, wheel, resource, tool, oracle, and result hashes.

## Evidence and repeatable commands

The definitive [machine-readable YAML summary](../python-profiler-dependency-schema-acceptance-final-20261004/summary.yaml)
reports **54 passed, zero failed, zero pending**. The
[official details](../python-profiler-dependency-schema-acceptance-final-20261004/official/results.json)
and [supplemental details](../python-profiler-dependency-schema-acceptance-final-20261004/supplemental/results.json)
retain commands, exits, exact diagnostics, profile hashes, verified closures,
wheel hashes, and Python version. The evidence directory also contains actual
and repeated profiles, six normalized diagnostic files, copied inventory and
catalog, and `pip` installation reports. The
[durable bundle](../python-profiler-dependency-schema-acceptance-bundle-20261004.tar.gz)
preserves those results, the runner, wheels, prepared workloads, reviewed
expectations, and tool hashes without disposable virtual environments or
HTTP-index copies. Its companion `.sha256` file verifies the archive.

The exact final installed CLI command was run from the external
`python-profiler-cli-acceptance` directory:

```sh
cd /Users/colacy/code/github.com/runtimeconditions/python-profiler-cli-acceptance
python3 run_phase4_acceptance.py \
  --inventory /Users/colacy/code/github.com/runtimeconditions/phase4-dependency-schema-results-20261004/inventory.yaml \
  --catalog /Users/colacy/code/github.com/runtimeconditions/extensions/tooling/extension-bindings/fixtures/conformance.yaml \
  --profiler-wheel /Users/colacy/code/github.com/runtimeconditions/python-profiler-phase4-build-20261004-v2/wheels/runtimeconditions_profiler-0.1.0-py3-none-any.whl \
  --binding-wheel-dir /Users/colacy/code/github.com/runtimeconditions/phase4-dependency-schema-fixtures-v3-20261004/python/wheels \
  --supplemental-binding-wheel-dir /Users/colacy/code/github.com/runtimeconditions/python-profiler-cli-acceptance-evidence/inputs/bindings \
  --output /Users/colacy/code/github.com/runtimeconditions/python-profiler-dependency-schema-acceptance-final-20261004 \
  --pypi-dependencies
```

For a repeat run, choose a new empty output directory. The
[fixture README](../extensions/tooling/extension-bindings/fixtures/README.md)
documents the official assembler and preparer commands and pinned build
tools. Their `inventory.yaml` is labeled
`prepared-awaiting-installed-cli-acceptance` because preparation does not
invoke a profiler; the separate installed CLI result above records the actual
acceptance. The regression command was:

```sh
cd /Users/colacy/code/github.com/runtimeconditions/python-rc-profiler
/private/tmp/rc-profiler-step3-venv/bin/python -m pytest -q
```

It reported `86 passed`. This test command uses profiler source for regression
checks; the installed CLI acceptance command uses only the built wheel.

## Evidence provenance and final decision

| Evidence | Result and contribution |
| --- | --- |
| [2026-10-03 external suite](../python-profiler-cli-acceptance-evidence/results.json) | **29/29** passed: 16 exact profiles and 13 expected rejections. It covered 12 generated positive calls; bare transitive leaf and middle calls lacked the core-required `interface` and therefore could not establish the complete positive-profile gate. Its input wheels and command are preserved with the evidence. |
| [First 18+5 attempt](../python-profiler-phase4-acceptance-20261004/official/results.json) | The supplied profiler wheel stopped at `union-missing-id`: it predated the union fallback already present in current source. The failure command and diagnostic were retained; no oracle was changed. |
| [Rebuilt intermediate attempt](../python-profiler-phase4-acceptance-rebuilt-20261004/official/results.json) | **22/23** official cases passed. `scoped-invalid-value` exposed a wrong-scope verified enum member rejected structurally before vocabulary validation. The profiler was corrected to let that negative reach vocabulary validation while rejecting structural fallback for a valid profile; the regression suite reached 86 tests. |
| [Complete 18+5 run](../python-profiler-phase4-acceptance-final-20261004/summary.json) | **52/52** passed: 18 complete positive profiles, five exact negatives, and all 29 supplemental checks. Its independently reviewed workload and expectation trees matched the supplied ones byte for byte. The [bundle](../python-profiler-phase4-acceptance-bundle-20261004.tar.gz) remains preserved. |
| [Expanded 19+6 run](../python-profiler-dependency-schema-acceptance-final-20261004/summary.yaml) | **54/54** passed with the schema-only dependency case and matching profiler release provenance. This is the definitive Python installed CLI result. |

The later [v4 assembly](../phase4-dependency-schema-fixtures-v4-20261004/)
was rebuilt for repaired Go profiler provenance. The independent
[Python v4 equivalence check](../phase4-dependency-schema-review-20261004/python-v4-equivalence.yaml)
matched **all 14 Python wheel bytes** to the passing installed inputs,
including **56 fixed resources**; all **50 workload files** and **25 reviewed
expectation files** were also byte-identical. The Python inventory matched
after substituting only assembly and result paths. The unchanged 54/54
Python run therefore applies to the v4 Python inputs without a rerun.

The user approved case 13 registration in the normalizer and Go emitter
regression suites and its reviewed model checkpoint. Python's existing emitter
suite discovered the new checkpoint automatically and passed **98 tests**;
the registration changed no Python profiler source or assembled package bytes.
The [consolidated review](../phase4-dependency-schema-review-20261004/consolidated-review.yaml)
then accepted all applicable Phase 4 gates across both languages and records
**no remaining acceptance blocker in the tested local scope**.

This acceptance uses test-only fixture assembly and local mock package
retrieval. It does not establish production publication or distribution,
broader platform qualification, or live HTTP resolution of extension IDs.
For end users, binding distributions must be published to a configured Python
package index; the generated profile's versioned HTTP(S) extension IDs are
resolved remotely by a consuming Adapter. This profiler currently validates
the definitions bundled in its installed bindings rather than fetching those
definitions by URI during generation.
