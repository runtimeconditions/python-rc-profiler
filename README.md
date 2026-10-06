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

## Build and check distributions (maintainers)

From the profiler source directory, use a separate build environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python -m build
.venv/bin/python scripts/check_distributions.py dist/*.whl dist/*.tar.gz
```

The default `build` command creates a source distribution and builds the wheel
from that archive. The check compares every runtime module and resource with
the source, verifies the console entry point and runtime dependency metadata,
and rejects missing resources, stale runtime files, and build output in the
source archive. Run it against a fresh `dist` directory containing one release.

Both distributions contain the CLI implementation, the four binding validation
schemas in `schemas.json`, and the pinned v0.2.0 core profile schema. Runtime
dependencies are declared in `pyproject.toml`; generated bindings supply their
own extension definitions. The source archive also includes the distribution
checker, tests, and local test fixtures. This maintainer build needs no sibling
repository. End users install the published package and their bindings as shown
above.

## Tagged GitHub Releases (maintainers)

Set `project.version` in `pyproject.toml`, commit the release changes, then push
the matching tag `v<version>` (for example `v0.1.0`). The
[release workflow](.github/workflows/release.yml) rejects a tag/version mismatch
or a noncanonical package version before building.

The workflow builds the wheel and source archive once, runs strict Twine
metadata validation and distribution-content checks, and records `SHA256SUMS`.
Separate jobs install those exact files in fresh environments on Linux with
Python 3.11–3.14 and on macOS and Windows with Python 3.12. Each job checks the
installed CLI and schemas, verifies bundled binding identities and dependency
closure, generates the expected profile, and checks a schema-only dependency
rejection. These checks use test-only bindings stored in this repository and
run outside the checkout. Logs and JSON results remain as Actions artifacts.

After every check passes, a job with `contents: write` verifies that the remote
tag still points at the tested commit and attaches the wheel, source archive,
and `SHA256SUMS` to the GitHub Release. A new release remains a draft until its
assets are uploaded. An existing draft can be completed; retries verify any
existing assets and refuse to replace different bytes. Released versions need
new version numbers and tags for changed artifacts. The workflow uses the
repository's `GITHUB_TOKEN`; no publication secret is needed for this step.

To reproduce the checks locally, after building distributions:

```sh
.venv/bin/python -m pip install -r requirements/release.txt
.venv/bin/python scripts/release_artifacts.py check-tag v0.1.0
.venv/bin/python -m twine check --strict dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_distributions.py dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/release_artifacts.py checksums --dist-dir dist
.venv/bin/python scripts/smoke_install.py --dist-dir dist
```

## PyPI publication

The release workflow has a separate `pypi` job. It runs after the verified
GitHub Release is published and only when the **repository Actions variable**
`PYPI_PUBLISH_ENABLED` equals `true`. Keep that variable `false` while the PyPI
organization **`runtimeconditions.io`** is awaiting approval.

The protected GitHub environment is `pypi`: deployments are restricted to tags
matching `v*` and require approval from the repository's current sole
maintainer, `colinjlacy`; administrator bypass is disabled. Self-review is allowed so that maintainer can approve
their own tagged releases. Add another reviewer and prevent self-review if the
maintainer team grows. Environment settings live in GitHub, independently of
the workflow file.

The job downloads the published Release's wheel, source distribution, and
`SHA256SUMS`; checks the tag's commit, package version, checksums, metadata, and
contents against the tagged source; then copies only the two distributions
into the upload directory. It does not rebuild them. Only this job has
`id-token: write`; the pinned PyPA upload action uses Trusted Publishing and
creates publication attestations. No PyPI password or API token is required.
Existing PyPI versions are not silently skipped or overwritten.

### Activate publishing after organization approval

1. In PyPI, open **Your organizations → runtimeconditions.io → Manage →
   Projects**, and create `runtimeconditions-profiler` (or transfer that
   project into the organization if it already belongs to you). The PyPI
   organization name and the installable project name are separate.
2. Open the project's **Publishing** settings and add this GitHub Trusted
   Publisher:

   | Setting | Exact value |
   | --- | --- |
   | PyPI project | `runtimeconditions-profiler` |
   | GitHub owner | `runtimeconditions` |
   | GitHub repository | `python-rc-profiler` |
   | Workflow filename | `release.yml` |
   | Environment | `pypi` |

   Use just the workflow filename, without `.github/workflows/`. See the
   [PyPI organization project instructions](https://docs.pypi.org/organization-accounts/actions/project-actions/)
   and [Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).
3. Set the repository Actions variable `PYPI_PUBLISH_ENABLED` to `true`.
   Future matching release tags will request `pypi` environment approval after
   all build and installed-package checks pass.

To publish a previously verified GitHub Release after activation, dispatch
this same workflow **at its existing release tag**, for example:

```sh
gh workflow run release.yml --repo runtimeconditions/python-rc-profiler --ref v0.1.0
```

The workflow must exist on the default branch and at that tag. A manual dispatch
skips building, smoke checks, and GitHub Release creation; it promotes the
existing Release's assets. The environment reviewer should confirm the
original tagged build and smoke jobs passed before approving this promotion.
Dispatching at a branch cannot publish to PyPI. If an upload fails, retry the
PyPI job or dispatch at the same tag after fixing the cause; a partially
published version requires inspection because PyPI files cannot be replaced.

**TODO — external PyPI activation:** After `runtimeconditions.io` is approved
and activated, create the organization-owned project, register the exact
Trusted Publisher above, and set `PYPI_PUBLISH_ENABLED=true`. The workflow and
artifact promotion code are ready; publishing remains disabled until this
setup is complete.
