# Python Profiler

Runtime Conditions is currently seeking adoption by an established parent
project. The repositories in this organization are split for hands-on usability,
review, demos, and implementation feedback. They are not intended to present
Runtime Conditions as a standalone foundation or competing project.

Start here: https://runtimeconditions.github.io/

The Python profiler generates Runtime Conditions Profiles from declarative Python binding packages and version-aligned SDK mappings.

Current implementation:

- Supports Python 3.10 and newer.
- Uses standard `pyproject.toml` metadata so the profiler can be installed with `pip` or run with `uv`.
- Discovers Runtime Conditions artifacts from explicit package paths and project-configured package paths:
  - `runtimeconditions.bindings.yaml`
  - `runtimeconditions.extension.yaml`
- Discovers `runtimeconditions/index.yaml` and its digest-pinned SDK mappings from installed Python distributions without importing or executing the SDK.
- Supports local development override paths through `metadata.extensionDefinition`.
- Validates discovered artifacts before source extraction:
  - manifest kind and `metadata.language`
  - required Python manifest section
  - manifest extension ID against extension definition `metadata.id`
  - dependency closure, duplicate extension IDs, cycles, and vocabulary conflicts
  - binding references to unresolved kinds, interface types, fields, and field values
  - source class/function existence, string argument indexes, class argument indexes, and constant values
- Generates Runtime Conditions Profiles from `RuntimeConditionsBinding` declarative Python calls.
- Resolves direct mapped SDK method calls, the source-verified callable-delegation transformation represented by Kubernetes Python `Watch.stream`, and the first producer/state/method flow represented by Kubernetes Python `DynamicClient`.
- Resolves a generic SDK-owned state contract for awaited factories, receiver-produced values, inherited or new dependency identities, positional and keyword arguments, typed configuration fields, literal lists, alternative binding sources, and values retained on returned SDK objects. SDK-specific names and semantics remain entirely in generated mapping metadata.
- Resolves the accepted AWS Python client and resource factories, aliases, generated botocore methods, cross-module application factories, constructor injection, resource relations and actions, owner-qualified calls, and nested s3transfer operation paths.
- Verifies SDK mapping file and semantic digests, installed distribution identity and version, and exact extension release coordinates before extraction.
- Validates SDK-derived conditions against the exact extension JSON Schema and emits no inferred condition when a delegated callable, required dynamic coordinate, or state-producing resource selector cannot be resolved statically.
- Handles ordinary imports, aliased imports, wildcard imports, fully qualified calls, enum-like constants, cross-file string constants, nested option calls, type/class arguments, schema classes in separate files, and unused imported extension packages.
- Validates generated profiles against the resolved extension dependency closure and vocabulary before output.

Not implemented yet:

- SDK/runtime `RuntimeConditionsPackage` extraction.
- Automatic retrieval or bundled-catalog resolution of the immutable extension release required by an installed SDK mapping; the current integration receives that exact release through an explicit package path.
- Additional condition transformations beyond direct calls, higher-order callable delegation, statically cataloged resource flows, and the generic typed-state contract currently exercised by NATS Python.
- AWS paginator, collection, transfer-class, branch-predicate, and exact execution-path selection patterns not exercised by the seven accepted fixtures.

## Setup

Using pip:

```sh
python3.10 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

Using uv:

```sh
uv venv
uv pip install -e '.[test]'
```

## Run

Discover artifacts:

```sh
python profiler.py discover \
  --project testdata/profile-generation/declarative-app \
  --resolve-package-paths
```

Validate first-party Python bindings:

```sh
python profiler.py validate-extensions --root ../extensions
```

Generate a fixture profile:

```sh
python profiler.py generate \
  --project testdata/profile-generation/declarative-app \
  --package-path ../extensions/common-integrations/python \
  --package-path ../extensions/env-configuration/python \
  --name python-declarative-app \
  --workload-uri example/python-declarative-app \
  --workload-version test
```

Generate the Python request logger demo profile:

```sh
python profiler.py generate \
  --project ../rc-demos/apps/request-logger-http-python \
  --package-path ../extensions/common-integrations/python \
  --package-path ../extensions/env-configuration/python \
  --name request-logger-http \
  --workload-uri github.com/runtimeconditions/rc-demos/apps/request-logger-http-python \
  --workload-version dev
```

## Test

```sh
python -m pytest
```
