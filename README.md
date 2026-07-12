# Python Profiler

Runtime Conditions is currently seeking adoption by an established parent
project. The repositories in this organization are split for hands-on usability,
review, demos, and implementation feedback. They are not intended to present
Runtime Conditions as a standalone foundation or competing project.

Start here: https://runtimeconditions.github.io/

The Python profiler generates Runtime Conditions Profiles from declarative Python binding packages.

Current implementation:

- Supports Python 3.10 and newer.
- Uses standard `pyproject.toml` metadata so the profiler can be installed with `pip` or run with `uv`.
- Discovers Runtime Conditions artifacts from explicit package paths and project-configured package paths:
  - `runtimeconditions.bindings.yaml`
  - `runtimeconditions.extension.yaml`
- Supports local development override paths through `metadata.extensionDefinition`.
- Validates discovered artifacts before source extraction:
  - manifest kind and `metadata.language`
  - required Python manifest section
  - manifest extension ID against extension definition `metadata.id`
  - dependency closure, duplicate extension IDs, cycles, and vocabulary conflicts
  - binding references to unresolved kinds, interface types, fields, and field values
  - source class/function existence, string argument indexes, class argument indexes, and constant values
- Generates Runtime Conditions Profiles from `RuntimeConditionsBinding` declarative Python calls.
- Handles ordinary imports, aliased imports, wildcard imports, fully qualified calls, enum-like constants, cross-file string constants, nested option calls, type/class arguments, schema classes in separate files, and unused imported extension packages.
- Validates generated profiles against the resolved extension dependency closure and vocabulary before output.

Not implemented yet:

- SDK/runtime `RuntimeConditionsPackage` extraction.
- Extension JSON Schema execution during generated profile validation.
- Wheel-installed package metadata discovery beyond resolved source/package roots.

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
