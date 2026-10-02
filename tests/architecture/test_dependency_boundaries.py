"""Architecture-boundary tests for the Phase 1 dependency graph."""

import ast
from collections.abc import Iterator
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "diglibrary"


def test_internal_import_graph_has_no_cycles() -> None:
    """Production modules form an acyclic directed import graph."""
    graph = _internal_import_graph()

    assert _find_cycles(graph) == []


def test_provider_contract_does_not_depend_on_engine_contracts() -> None:
    """Provider lifecycle contracts remain independent from all Engine contracts."""
    dependencies = _internal_import_graph()["diglibrary.providers.contracts"]

    assert "diglibrary.engines.contracts" not in dependencies


def test_slskd_connector_does_not_depend_on_providers_or_engines() -> None:
    """The reusable connector remains below provider and Engine architectural boundaries."""
    graph = _internal_import_graph()
    connector_dependencies = {
        dependency
        for module, dependencies in graph.items()
        if module.startswith("diglibrary.connectors.slskd")
        for dependency in dependencies
    }

    assert not any(
        dependency.startswith("diglibrary.providers") for dependency in connector_dependencies
    )
    assert not any(
        dependency.startswith("diglibrary.engines") for dependency in connector_dependencies
    )


def test_providers_may_depend_only_on_connector_abstractions() -> None:
    """Provider modules cannot import a concrete connector implementation."""
    graph = _internal_import_graph()
    provider_dependencies = {
        dependency
        for module, dependencies in graph.items()
        if module.startswith("diglibrary.providers")
        for dependency in dependencies
        if dependency.startswith("diglibrary.connectors")
    }

    assert provider_dependencies <= {
        "diglibrary.connectors.contracts",
        "diglibrary.connectors.models",
    }


def test_no_connector_imports_provider_modules() -> None:
    """Connectors stay beneath providers in the frozen dependency direction."""
    graph = _internal_import_graph()
    connector_dependencies = {
        dependency
        for module, dependencies in graph.items()
        if module.startswith("diglibrary.connectors")
        for dependency in dependencies
    }

    assert not any(
        dependency.startswith("diglibrary.providers") for dependency in connector_dependencies
    )


def test_the_application_layer_never_imports_the_interface() -> None:
    """The window depends on the application, never the other way.

    The API must stay drivable without a window at all — which is also exactly
    what makes it testable.
    """
    graph = _internal_import_graph()
    application_dependencies = {
        dependency
        for module, dependencies in graph.items()
        if module.startswith("diglibrary.application")
        for dependency in dependencies
    }

    assert not any(
        dependency.startswith("diglibrary.ui") for dependency in application_dependencies
    )


def test_only_the_interface_touches_pywebview() -> None:
    """pywebview is a platform detail of the window, invisible everywhere else."""
    offenders = {
        _module_name(path)
        for path in SOURCE_ROOT.rglob("*.py")
        if not _module_name(path).startswith("diglibrary.ui")
        for imported in _external_imports(path)
        if imported == "webview"
    }

    assert offenders == set()


def _external_imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_composition_root_is_the_only_service_assembly_point() -> None:
    """Only the composition root constructs infrastructure services and provider coordination."""
    assembly_calls = {
        "AlbumMatcher",
        "ArtworkService",
        "ChangeExecutor",
        "ChangePlanner",
        "CoverArtArchiveClient",
        "Database",
        "FilesystemArtworkStore",
        "IdentificationWorkflow",
        "LibraryApi",
        "LibraryScanner",
        "LibraryStore",
        "MutagenAudioProbe",
        "MutagenTagStore",
        "ProviderManager",
        "ProviderRegistry",
        "SlskdClient",
        "SlskdConnector",
        "SlskdMapper",
        "UrllibSlskdTransport",
        "configure_logging",
    }
    found_calls = {
        call_name: path
        for path in SOURCE_ROOT.rglob("*.py")
        for call_name in _called_names(path)
        if call_name in assembly_calls
    }

    assert found_calls == {
        "AlbumMatcher": SOURCE_ROOT / "application" / "composition.py",
        "ArtworkService": SOURCE_ROOT / "application" / "composition.py",
        "ChangeExecutor": SOURCE_ROOT / "application" / "composition.py",
        "ChangePlanner": SOURCE_ROOT / "application" / "composition.py",
        "CoverArtArchiveClient": SOURCE_ROOT / "application" / "composition.py",
        "Database": SOURCE_ROOT / "application" / "composition.py",
        "FilesystemArtworkStore": SOURCE_ROOT / "application" / "composition.py",
        "IdentificationWorkflow": SOURCE_ROOT / "application" / "composition.py",
        "LibraryApi": SOURCE_ROOT / "application" / "composition.py",
        "LibraryScanner": SOURCE_ROOT / "application" / "composition.py",
        "LibraryStore": SOURCE_ROOT / "application" / "composition.py",
        "MutagenAudioProbe": SOURCE_ROOT / "application" / "composition.py",
        "MutagenTagStore": SOURCE_ROOT / "application" / "composition.py",
        "ProviderManager": SOURCE_ROOT / "application" / "composition.py",
        "ProviderRegistry": SOURCE_ROOT / "application" / "composition.py",
        "SlskdClient": SOURCE_ROOT / "application" / "composition.py",
        "SlskdConnector": SOURCE_ROOT / "application" / "composition.py",
        "SlskdMapper": SOURCE_ROOT / "application" / "composition.py",
        "UrllibSlskdTransport": SOURCE_ROOT / "application" / "composition.py",
        "configure_logging": SOURCE_ROOT / "application" / "composition.py",
    }


CONCRETE_PROVIDERS = frozenset({"soulseek.py"})
"""Provider implementations, which drive their own injected connector.

This rule reads the *names* of the methods a module calls, so it cannot tell a
provider commanding a peer provider — which is forbidden — from a provider
calling ``initialize`` on the connector it was handed, which is it looking after
its own house. Implementations are therefore exempted by file name.

The boundary that actually matters is enforced by imports, not by names:
``test_providers_may_depend_only_on_connector_abstractions`` proves an
implementation reaches a connector only through
``diglibrary.connectors.contracts`` and ``.models``, never through a concrete
one. Membership here is deliberate: a new file under ``providers/`` is covered
by this rule until someone adds it to this set on purpose.
"""


def test_provider_manager_is_the_only_provider_lifecycle_coordinator() -> None:
    """No provider-framework module other than the manager invokes provider lifecycle methods."""
    lifecycle_methods = {
        "initialize",
        "health",
        "search",
        "resolve",
        "download",
        "validate",
        "shutdown",
    }
    provider_root = SOURCE_ROOT / "providers"
    lifecycle_calls = {
        path
        for path in provider_root.rglob("*.py")
        if path.name != "manager.py" and path.name not in CONCRETE_PROVIDERS
        for method in _called_attributes(path)
        if method in lifecycle_methods
    }

    assert lifecycle_calls == set()


def test_a_provider_implementation_still_reaches_no_concrete_connector() -> None:
    """What the name rule stops policing, the import graph must still prove.

    Exempting an implementation from the lifecycle-name rule buys nothing if it
    can then reach into ``connectors.slskd`` directly, so the exemption and this
    check belong together.
    """
    graph = _internal_import_graph()

    for module_file in CONCRETE_PROVIDERS:
        module = f"diglibrary.providers.{module_file.removesuffix('.py')}"
        assert {
            dependency
            for dependency in graph.get(module, set())
            if dependency.startswith("diglibrary.connectors")
        } <= {"diglibrary.connectors.contracts", "diglibrary.connectors.models"}


def _internal_import_graph() -> dict[str, set[str]]:
    modules = {_module_name(path) for path in SOURCE_ROOT.rglob("*.py")}
    graph = {module: set() for module in modules}
    for path in SOURCE_ROOT.rglob("*.py"):
        module = _module_name(path)
        graph[module].update(dependency for dependency in _imports(path) if dependency in modules)
    return graph


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT.parent).with_suffix("")
    parts = relative.parts
    return ".".join(parts[:-1]) if parts[-1] == "__init__" else ".".join(parts)


def _imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names if alias.name.startswith("diglibrary"))
        is_internal_from_import = (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("diglibrary")
        )
        if is_internal_from_import:
            yield node.module


def _called_names(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            yield node.func.id


def _called_attributes(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node.func.attr


def _find_cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    visited: set[str] = set()
    active: list[str] = []
    cycles: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        if module in active:
            cycles.append(tuple([*active[active.index(module) :], module]))
            return
        if module in visited:
            return
        visited.add(module)
        active.append(module)
        for dependency in graph[module]:
            visit(dependency)
        active.pop()

    for module in graph:
        visit(module)
    return cycles
