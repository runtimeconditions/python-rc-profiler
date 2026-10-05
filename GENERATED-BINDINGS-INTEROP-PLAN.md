# Generated Extension Bindings Interoperability Plan: Python Profiler

## Purpose and acceptance boundary

The Python profiler must generate a complete Runtime Conditions Profile from
Python source that uses automatically generated extension bindings. The
end-user environment contains an installed profiler wheel/CLI, a Python
environment with binding distributions installed through dependency management,
and exact extension definitions available from installed bindings, a verified
local cache, or supported URI resolution. Normal profiling must not require an
`extensions` checkout, explicit sibling package paths, an editable install, or
`python profiler.py` from the profiler source tree.

This is a profiler implementation plan, not a change to the extension binding
generator. The Phase 3 Python API exposes inert declaration functions,
keyword-only frozen dataclasses, `Sequence` and `dict` aliases, `StrEnum` value
domains, optional `None` fields, and imported marker protocols. Phase 4 will
complete the structural binding manifest. The profiler must consume that
versioned contract without preserving the earlier handwritten mapping as the
meaning of generated bindings.

**Priority:** Correct extraction and complete validation of generated binding
declarations take precedence over the previous SDK mapping feature. SDK mapping
discovery or extraction may be disabled, separated, or removed if it conflicts
with installed-package discovery, static analysis, exact extension identity,
or fail-closed behavior. The old SDK mapping work is not a compatibility gate
for generated bindings.

## Current strengths and functional gaps

| Area | Current implementation and gap | Required remediation |
| --- | --- | --- |
| Installed CLI | `pyproject.toml` defines `runtimeconditions-python-profiler`, but README examples use the source-tree script and explicit sibling package paths. | Build a wheel and install it in an isolated environment. Exercise the console script from an unrelated project with both repositories unavailable. Keep editable installs for development only. |
| Binding discovery | `project/discovery.py` scans the project and configured package paths recursively. It does not discover generated binding resources by following the workload's installed import-to-distribution mapping. SDK mapping discovery already uses installed distribution metadata, but that is a separate path. | Resolve imported binding packages to installed distributions with native packaging metadata. Locate the fixed package-data resources within only those resolved distributions; do not scan `site-packages` or caches recursively. Read resources without importing or executing the binding package's `__init__.py`. |
| Binding contract | `manifest/parser.py` and `extension/artifact_validator.py` read the earlier `RuntimeConditionsBinding` mapping. Phase 3 emits `RuntimeConditionsBindingManifest` with a provisional symbol inventory. | Strictly parse the completed structural manifest version and verify its distribution/import-package identity, native symbols, model digest, root extension identity and digest, and relationships against packaged resources. Fail on unknown or inconsistent contract versions. |
| Source reconstruction | `profile/extractor.py` follows legacy declaration/option mappings and several known targets. It does not reconstruct arbitrary extension-defined fields from generated dataclass construction. | Match generated top-level declaration functions through the resolved import and distribution. Interpret keyword-only dataclass calls, named fields, nested objects, tuples/sequences, lists, dictionaries, `StrEnum` members, union alternatives, optional `None`, and imported marker protocols through manifest-to-model coordinates. Preserve exact serialized source names, including Unicode and collision-resolved Python identifiers. |
| Static-value boundary | Existing extraction handles selected string constants, calls, and class arguments; other expressions can be skipped or fail under legacy rules. | Define a bounded AST evaluator for literals, supported constructors, enum members, containers, and simple statically provable references across project modules. Never import application or binding code and never use `eval` or `exec`. A recognized declaration with a dynamic or ambiguous value must fail with file, line, column, symbol, and model coordinate. |
| Validation and identity | `profile/validator.py` checks core markers and selected vocabulary paths, but does not apply every extension JSON Schema to binding-derived Conditions. `PythonExtractionScanner.extension_closure()` places transitive dependencies in the emitted extension list. | Validate complete core profile structure, exact extension identity and dependency closure, ownership of every field and value, and all applicable Draft 2020-12 schemas. Emit only directly contributing extension IDs; resolve transitive dependencies for validation. Reject missing or altered package resources and extension definitions. |
| Failure behavior | The current project path and artifact scans can make a source checkout appear sufficient. The profiler can produce a profile through a path different from the installed binding's package metadata. | Make the installed distribution and imported symbol authoritative. Reject ambiguous import ownership, absent metadata, resource/digest mismatches, unsupported expressions, and semantic errors before writing output. Do not silently produce an empty or narrower profile for a recognized generated declaration. |
| SDK mapping interaction | `ProfileExtractor.extract` combines binding and SDK-derived Conditions automatically. | Make the generated-binding path independently testable. Make SDK mapping behavior opt-in or remove it if automatic discovery or merging changes correctness, deterministic output, or failure semantics. |

## Remediation sequence

1. **Freeze the installed-artifact contract.** Define CLI inputs, the mapping
   from source imports to installed distributions, fixed package-data paths,
   cache/URI extension resolution, and manifest/model identity checks. Audit
   reusable parser and validation components without carrying over legacy
   `writes[]` semantics. If a resource API would import the package, use
   distribution file metadata to read the resource bytes statically.
2. **Prove packaged discovery.** Install the profiler wheel and generated
   binding wheels, including direct and transitive additive dependencies, in a
   fresh virtual environment. Resolve resources from those distributions with
   no explicit `--package-path` and no repository checkout. Reject coordinate,
   version, digest, and extension-ID mismatches.
3. **Implement structural extraction.** Start with one owned declaration and
   nested interface object, then additive fields, omitted optionals, enums,
   collections, maps, recursive shapes, heterogeneous unions, Unicode source
   names, and aliases or direct imports. Use a generic coordinate-driven value
   tree rather than extension-specific branches.
4. **Make semantic validation mandatory.** Build a complete YAML profile with
   caller-provided workload identity. Derive direct extension contributors
   from used generated symbols, resolve the transitive closure, and apply every
   matching extension schema. Invalid output must not be written.
5. **Stabilize diagnostics and output.** Give deterministic source-located
   errors for dynamic values, missing resources, digest mismatches, ambiguous
   imports, invalid dataclass fields, dependency failures, and schema errors.
   Use stable ordering for files, declarations, extension IDs, and mappings.
6. **Decide the SDK path last.** Retain SDK mappings only if they meet the same
   installed-artifact and validation guarantees without affecting generated
   binding extraction.

## Evidence required before declaring interoperability

- Run the installed console script against an unrelated project in a fresh
  virtual environment containing only the profiler and installed binding
  distributions. The successful invocation uses no editable install, explicit
  sibling package path, or mounted repository checkout.
- Compare complete profile YAML with reviewed fixtures for every profile-capable
  generated construct, including source and installed-wheel dependency cases.
  Structural type-coverage fixtures that cannot form valid Conditions must
  have explicit expected rejection.
- Show exact failures for malformed manifests, missing package data, altered
  digests, unresolved extension definitions, unsupported dynamic expressions,
  and each branch-dependent schema constraint.
- Verify that unused installed or imported bindings contribute no extension
  IDs, transitive dependencies are used for validation, and no application or
  binding package code is imported or executed during profiling.
- Run the installed-artifact workflow on Linux and macOS with the supported
  minimum Python version. Check deterministic output and diagnostics, bounded
  resource use on a representative large model, and no output after failure.

Phase 4 integration should use this evidence as the profiler readiness gate;
source-tree demos and legacy-manifest golden tests alone do not satisfy it.
