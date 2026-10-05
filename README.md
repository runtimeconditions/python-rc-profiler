# Runtime Conditions Python profiler

The profiler turns declarations in a Python codebase into a validated Runtime
Conditions Profile. Your code imports **generated binding distributions
installed in the same Python environment as the profiler**. The profiler
finds those distributions through Python package metadata, reads their fixed
binding resources, and analyzes your source without importing the bindings or
running the application. Python 3.11 or newer is required.

## Install from a package index

Create an environment for the workload and install the profiler and the
binding distributions that your code imports. Resolve distributions by package
name and version from PyPI or your organization's configured Python package
index. The example names and version below are illustrative:

```sh
PROFILE_ENV=/Users/alex/.venvs/orders-profile
PROJECT_DIR=/Users/alex/work/orders
python3 -m venv "$PROFILE_ENV"
"$PROFILE_ENV/bin/python" -m pip install \
  runtimeconditions-profiler==0.1.0 \
  acme-runtimeconditions-jobs==1.2.3
```

Keep `PROFILE_ENV` and `PROJECT_DIR` set in the same shell for the commands
below.

The binding publisher must include Python distribution metadata and these four
resources at the installed import package's fixed location:
`runtimeconditions.bindings.yaml`, `runtimeconditions.binding-model.yaml`,
`runtimeconditions.extension.yaml`, and
`runtimeconditions.binding-release.yaml`. The profiler verifies their package
ownership, identities, versions, digests, and dependency closure. Installing
loose generated `.py` files does not provide that contract.

The Phase 4 fixtures are test-only; the profiler and example binding names in
this command are not currently public PyPI releases. An end-user release needs
the publisher to make the profiler and bindings available through a configured
package index. No `extensions` repository checkout, local binding file path,
editable install, or checkout-based `PYTHONPATH` is part of this workflow.

## Declare conditions in your workload

Use the API exported by your installed generated binding. For example, an
organization's jobs binding might provide:

```python
from acme_runtimeconditions_jobs import Command, Process, job

job(Command(value="sample"), Process())
```

The exact import, declaration functions, data classes, enum members, and
fields come from the binding publisher. Declarations may use supported static
values, collections, maps, optional fields, unions, aliases, and references
across your project modules. A recognized declaration whose value cannot be
determined statically produces a source-located error.

## Verify bindings and generate a profile

Run the installed console script from the same environment. The project and
output paths below are absolute paths to **your workload**, independent of
where the profiler or binding packages were built:

```sh
"$PROFILE_ENV/bin/runtimeconditions-python-profiler" \
  profile verify-bindings --project "$PROJECT_DIR"

"$PROFILE_ENV/bin/runtimeconditions-python-profiler" \
  profile generate \
  --project "$PROJECT_DIR" \
  --name orders \
  --workload-uri https://services.example.com/orders \
  --workload-version 1.0.0 \
  --out "$PROJECT_DIR/runtimeconditions.profile.yaml"
```

`verify-bindings` lists the distributions and extension IDs found through
workload imports; add `--json` for machine-readable output. `profile generate`
uses the installed profiler's approved core schema and the verified extension
definitions from the binding distributions. It validates the complete profile,
including schemas from transitive dependencies, before atomically writing the
YAML file. The profile lists extensions that directly contribute declarations;
validation-only dependencies remain in the checked closure. An error exits
nonzero and leaves an existing output file untouched. Omit `--out` to print
the profile to standard output.

Use the `profile` subcommands for generated bindings. The older top-level
`generate`, `discover`, and `mappings` commands belong to a separate legacy
mapping workflow.

## Extension IDs and remote resolution

An extension identifier in a production profile has the Section 5.1 form
`<uri>:<version>`, where `<uri>` is an absolute HTTP or HTTPS URI and the
version follows the **final colon**. For example:

```yaml
extensions:
  - https://extensions.example.com/runtimeconditions/jobs:1.2.3
```

The Python distribution name used by `pip` is separate from this extension
ID. The publisher must make the extension definition for that ID available to
profile consumers. An Adapter interpreting the generated profile resolves the
declared IDs and their transitive dependencies through its configured remote
extension resolver. Section 5.1 defines the identifier syntax; it does not
specify a network fetch protocol or make the identifier itself a local file
path.

During **profile generation**, this CLI validates the definitions packaged in
the installed binding distributions. It does not fetch definitions directly
from extension URIs. URI-only network resolution without an installed binding
package is outside the current profiler command. No step requires an end user
to pull the `extensions` repository or point the profiler at an extension file.
