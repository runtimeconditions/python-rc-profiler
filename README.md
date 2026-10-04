# Python Profiler

Runtime Conditions is currently seeking adoption by an established parent
project. The repositories in this organization are split for hands-on usability,
review, demos, and implementation feedback. They are not intended to present
Runtime Conditions as a standalone foundation or competing project.

Start here: https://runtimeconditions.github.io/

The Python profiler generates Runtime Conditions Profiles from declarative Python binding packages and version-aligned SDK mappings.

Current implementation:

- Supports Python 3.11 and newer, matching the minimum runtime for generated Python bindings.
- Uses standard `pyproject.toml` metadata so the profiler can be installed with `pip` or run with `uv`.
- Resolves imports of generated binding packages through installed distribution metadata and reads their four fixed package resources without importing package code.
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

- Structural validation and extraction of installed generated extension bindings. Import-to-distribution resolution and fixed-resource loading are implemented.
- SDK/runtime `RuntimeConditionsPackage` extraction.
- Automatic retrieval or bundled-catalog resolution of the immutable extension release required by an installed SDK mapping; the current integration receives that exact release through an explicit package path.
- Additional condition transformations beyond direct calls, higher-order callable delegation, statically cataloged resource flows, and the generic typed-state contract currently exercised by NATS Python.
- AWS paginator, collection, transfer-class, branch-predicate, and exact execution-path selection patterns not exercised by the seven accepted fixtures.

## Install the CLI

Build the wheel from this repository, then install it in the Python 3.11 or
newer environment containing the workload's binding distributions. An editable
install is needed only for development.

```sh
python3.11 -m pip wheel . --no-deps --wheel-dir dist
python3.11 -m pip install dist/runtimeconditions_profiler-0.1.0-py3-none-any.whl
```

The installed command can be invoked from any working directory. Its profile
generation interface takes the workload directory and identity:

```sh
runtimeconditions-python-profiler profile generate \
  --project /absolute/path/to/workload \
  --name my-workload \
  --workload-uri example/my-workload \
  --workload-version 1.0.0 \
  --out /absolute/path/to/profile.yaml
```

The `profile generate` path now resolves imported binding packages and reads
their installed resources. It reports an error before writing a profile until
structural validation and extraction are implemented. For the existing
handwritten bindings, `generate` retains the development-only `--package-path`
option.

## Develop and test

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```
