from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest
import yaml

from runtimeconditions_profiler.errors import RuntimeConditionsError
from runtimeconditions_profiler.extension.identity import parse_identifier
from runtimeconditions_profiler.profile.generated import GeneratedBindingExtractor
from runtimeconditions_profiler.project.verify import VerifiedBindingPackage, VerifiedBindingSet


DECLARATION = "kind:service"
SCHEMA = "schema:service"


def ref(coordinate: str) -> dict[str, str]:
    return {"coordinate": coordinate}


def field(name: str, source: str, value: dict, required: bool = True) -> dict:
    return {"modelRef": ref(f"{SCHEMA}/{source}"), "nativeName": name,
            "sourceName": source, "required": required, "value": value}


def named(name: str, construct: str, **extra: object) -> dict:
    return {"modelRef": ref(f"{SCHEMA}/{name}"), "sourceName": name,
            "nativeName": name, "construct": construct, "file": "bindings.py", **extra}


def root(name: str, path: str, role: str = "condition-field", **extra: object) -> dict:
    result = {
        "role": role, "modelRef": ref(f"{SCHEMA}/{path}"),
        "declarationCoordinate": DECLARATION, "scope": {"kind": "service"},
        "sourceName": path, "path": [{"name": path}],
        "value": {"type": name}, "schemaCoordinate": SCHEMA,
    }
    result.update(extra)
    return result


def package(import_name: str = "example_binding", extension: str = "urn:example:base",
            declarations: bool = True) -> VerifiedBindingPackage:
    marker = [{"declarationCoordinate": DECLARATION, "markerMethod": "service_marker"}]
    types = [
        named("Region", "wrapper", fields=[
            {**field("value", "café", {"builtin": "str"}), "synthetic": True},
        ], implements=marker),
        named("Mode", "scalar", underlying="str", members=[
            {"modelRef": ref(f"{SCHEMA}/Mode"), "nativeName": "FAST", "value": "rapide"},
        ]),
        named("OtherMode", "scalar", underlying="str", members=[
            {"modelRef": ref(f"{SCHEMA}/OtherMode"), "nativeName": "DIRECT", "value": "direct"},
        ]),
        named("Http", "object", fields=[
            field("endpoint", "endpoint-url", {"builtin": "str"}),
            field("mode", "mode", {"type": "Mode", "nullable": True}, False),
            field("note", "note", {"builtin": "str", "nullable": True}, False),
        ], implements=marker),
        named("Tags", "collection", element={"modelRef": ref(f"{SCHEMA}/tags/items"), "value": {"builtin": "str"}}),
        named("Data", "map", element={"modelRef": ref(f"{SCHEMA}/data/values"), "value": {"builtin": "JSONValue"}}),
        named("Choice", "union", variants=[
            {"modelRef": ref(f"{SCHEMA}/choice/0"), "value": {"builtin": "str"}},
            {"modelRef": ref(f"{SCHEMA}/choice/1"), "value": {"builtin": "int"}},
        ]),
        named("Details", "object", fields=[
            field("tags", "tags", {"type": "Tags"}),
            field("data", "data", {"type": "Data"}),
            field("choice", "choice", {"type": "Choice"}),
        ], implements=marker),
    ]
    roots = [
        root("Region", "café"),
        root("Http", "interface", "interface", fixedInterfaceType="http",
             scope={"kind": "service", "interfaceType": "http"}),
        root("Details", "details"),
    ]
    if not declarations:
        types = [named("Addon", "wrapper", fields=[
            {**field("value", "extra", {"builtin": "str"}), "synthetic": True},
        ], implements=marker)]
        roots = [root(
            "Addon", "extra", schemaCoordinate="schema:addon",
            scope={"kind": "service", "interfaceType": "http"},
        )]
    manifest = {"declarations": [{"modelRef": ref(DECLARATION), "owner": extension,
        "sourceName": "service", "function": "service"}] if declarations else [],
        "rootBindings": roots, "types": types}
    installed = SimpleNamespace(import_package=import_name, distribution=import_name, version="1.0.0")
    return VerifiedBindingPackage(installed, manifest, {"rootExtension": {"id": extension, "version": "1.0.0"}}, {}, {})


def extract(tmp_path: Path, source: str, helper: str | None = None,
            addon: bool = False) -> tuple:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text(source)
    if helper is not None:
        (workload / "helper.py").write_text(helper)
    packages = (package(),)
    if addon:
        packages += (package("addon_binding", "urn:example:addon", False),)
    return GeneratedBindingExtractor(VerifiedBindingSet(packages, packages)).extract(workload)


def test_extracts_cross_module_objects_collections_maps_union_enum_and_exact_names(tmp_path: Path) -> None:
    found = extract(
        tmp_path,
        "import example_binding as b\nfrom helper import details as d\n"
        "def exercise():\n"
        "    region = b.Region(value='eu')\n"
        "    b.service(region, b.Http(endpoint='https://example.test', mode=b.Mode.FAST, note=None), d)\n",
        "from example_binding import Details, Http, Mode\nimport example_binding as b\n"
        "tags = ('one', 'two')\n"
        "details = Details(tags=tags, data={'café': ['a', 1, None]}, choice=7)\n",
    )
    assert len(found) == 1
    assert found[0].condition == {
        "kind": "service", "café": "eu",
        "interface": {"type": "http", "endpoint-url": "https://example.test", "mode": "rapide"},
        "details": {"tags": ["one", "two"], "data": {"café": ["a", 1, None]}, "choice": 7},
    }
    assert found[0].direct_extensions == (("urn:example:base", "1.0.0"),)
    assert found[0].line == 5
    assert found[0].structural_fallbacks == ()


def test_wrong_scoped_enum_reaches_semantic_validation_with_fallback(tmp_path: Path) -> None:
    found = extract(
        tmp_path,
        "import example_binding as b\n"
        "b.service(b.Http(endpoint='ok', mode=b.OtherMode.DIRECT))\n",
    )
    assert found[0].condition["interface"]["mode"] == "direct"
    assert found[0].structural_fallbacks == (f"{SCHEMA}/mode",)


def test_additive_field_tracks_direct_contributors(tmp_path: Path) -> None:
    found = extract(
        tmp_path,
        "import example_binding as b\nimport addon_binding as a\n"
        "b.service(b.Region(value='eu'), a.Addon(value='enabled'))\n",
        addon=True,
    )
    assert found[0].condition == {
        "kind": "service", "café": "eu", "extra": "enabled", "interface": {"type": "http"},
    }
    assert found[0].direct_extensions == (("urn:example:addon", "1.0.0"), ("urn:example:base", "1.0.0"))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import example_binding as b\nb.service(b.Region(value=dynamic()))\n", "unresolved name dynamic"),
        ("import example_binding as b\nb.service(b.Http(mode=b.Mode.FAST))\n", "missing required field endpoint"),
        ("import example_binding as b\nb.service(b.Region(value=4))\n", "expected str value"),
        ("import example_binding as b\nb.service(b.Http(endpoint='ok', mode=b.Mode.SLOW))\n", "unknown Mode enum member"),
        ("import example_binding as b\nb = 3\nb.service()\n", "b is rebound"),
        ("import example_binding as b\nif dynamic:\n    b.service()\n", "dynamic control flow"),
        ("if dynamic:\n    import example_binding as b\n    b.service()\n", "dynamic control flow"),
        ("if dynamic:\n    import example_binding as b\nb.service()\n", "b is rebound"),
        ("import example_binding as b\n[b.service() for _ in items]\n", "dynamic control flow"),
        ("import example_binding as b\nb.service(b.Region('eu'))\n", "keyword-only"),
        ("import example_binding as b\nb.service(b.Region(value='a'), b.Region(value='b'))\n", "duplicate serialized field"),
    ],
)
def test_recognized_declarations_fail_with_source_and_coordinate(
    tmp_path: Path, source: str, message: str,
) -> None:
    with pytest.raises(RuntimeConditionsError) as failure:
        extract(tmp_path, source)
    assert message in str(failure.value)
    assert "/app.py:" in str(failure.value)
    assert DECLARATION in str(failure.value) or SCHEMA in str(failure.value)


def test_nested_function_and_method_declarations_are_not_silently_skipped(tmp_path: Path) -> None:
    found = extract(
        tmp_path,
        "import example_binding as b\n"
        "def outer():\n"
        "    def inner():\n"
        "        b.service()\n"
        "class Example:\n"
        "    def method(self):\n"
        "        b.service()\n",
    )
    assert [item.line for item in found] == [4, 7]


def test_src_layout_relative_module_aliases_resolve_statically(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "config.py").write_text(
        "from example_binding.bindings import Region\nregion = Region(value='west')\n"
    )
    (source / "main.py").write_text(
        "from . import config as c\nfrom example_binding import service as declare\n"
        "declare(c.region)\n"
    )
    packages = (package(),)
    found = GeneratedBindingExtractor(VerifiedBindingSet(packages, packages)).extract(tmp_path)
    assert [item.condition for item in found] == [{"kind": "service", "café": "west"}]


def test_conditional_local_reexport_does_not_hide_declaration(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConditionsError, match="declare is rebound") as failure:
        extract(
            tmp_path,
            "if enabled:\n    from helper import declare\ndeclare()\n",
            "from example_binding import service as declare\n",
        )
    assert "/app.py:3:1" in str(failure.value)


def test_local_star_import_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConditionsError, match="star import cannot be resolved statically"):
        extract(
            tmp_path,
            "from helper import *\nservice()\n",
            "from example_binding import service\n",
        )


def test_cross_module_cycle_is_source_located(tmp_path: Path) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text(
        "import example_binding as b\nfrom helper import region\nb.service(region)\n"
    )
    (workload / "helper.py").write_text("from app import region\n")
    with pytest.raises(RuntimeConditionsError, match="cyclic module reference|cyclic static reference") as failure:
        GeneratedBindingExtractor(VerifiedBindingSet((package(),), (package(),))).extract(workload)
    assert "/app.py:3:" in str(failure.value)


@pytest.mark.parametrize(
    ("case", "count"),
    [
        ("01-owned-kind-interface", 1),
        ("06-recursive-reference", 1),
        ("07-object-alternatives", 2),
        ("08-heterogeneous-union", 1),
        ("09-collections-and-maps", 1),
        ("10-scoped-domains-collisions", 2),
        ("11-source-name-preservation", 1),
    ],
)
def test_generated_conformance_sources_extract_in_memory_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, count: int,
) -> None:
    # This is an in-memory contract test. Installable integration fixtures and
    # their package-manager commands are owned by the separate fixture work.
    tooling = Path(__file__).resolve().parents[2] / "extensions/tooling/extension-bindings"
    emitter = tooling / "emitters/python/src"
    model_file = tooling / "model/conformance/expected" / case / "runtimeconditions.binding-model.yaml"
    if not model_file.is_file():
        pytest.skip("shared conformance model is unavailable")
    monkeypatch.syspath_prepend(str(emitter))
    from runtimeconditions_binding_emitter import build_plan, load_model, load_target, render_resources
    from runtimeconditions_binding_emitter.source import _conformance_source

    model = load_model(model_file)
    target = replace(
        load_target(tooling / "emitters/python/testdata/package-target.yaml"),
        root_extension=model["rootExtension"]["id"],
    )
    plan = build_plan(model, target)
    prefix = f"src/{target.import_package}/"
    manifest = yaml.safe_load(render_resources(plan, model)[prefix + "runtimeconditions.bindings.yaml"])
    source = _conformance_source(plan, model).replace(
        "from . import bindings as b", f"import {target.import_package} as b",
    )
    (tmp_path / "app.py").write_text(source)
    installed = SimpleNamespace(import_package=target.import_package)
    package = VerifiedBindingPackage(installed, manifest, model, {}, {})
    found = GeneratedBindingExtractor(VerifiedBindingSet((package,), (package,))).extract(tmp_path)
    assert len(found) == count
    assert all(item.direct_extensions == (parse_identifier(model["rootExtension"]),) for item in found)
    assert all(item.condition["kind"] for item in found)
    if case == "11-source-name-preservation":
        assert "café" in found[0].condition
