"""The physical collection never carries money, in any form.

A rule that lives only in a document is applied by half, so this reads every
name the module can store or send — columns, fields, identifiers, payload keys
and the strings in its code — and fails on any that names a price, a cost, a
payment or a currency. Prose is left out on purpose: the docstrings say, in so
many words, that money is never kept here, and that sentence is the rule.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from diglibrary.database.migrations import MIGRATIONS

SOURCE = Path(__file__).resolve().parents[2] / "src" / "diglibrary"

MODULE_FILES = (
    SOURCE / "collection" / "__init__.py",
    SOURCE / "collection" / "model.py",
    SOURCE / "collection" / "importing.py",
    SOURCE / "database" / "collection_store.py",
    SOURCE / "application" / "collection.py",
)

API_FUNCTIONS = (
    "_record_payload",
    "_collection_release",
    "_candidate_payload",
    "_typed_year",
    "_read_mapping",
    "_import_plan_payload",
    "_stored_candidate_payload",
)
"""The window's side of the module in `api.py`: these and every `collection*` method."""

MONEY = re.compile(
    r"(price|cost|paid|payment|pay|currency|money|amount|dollar|euro|usd|eur|brl|"
    r"reais|tax|shipping|freight|discount|valor|preco|preço|frete)",
    re.IGNORECASE,
)


def _words(name: str) -> list[str]:
    """Split an identifier or a phrase into lowercase words, snake or camel case."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [word.lower() for word in re.split(r"[^A-Za-zÀ-ÿ]+", spaced) if word]


def _names(tree: ast.AST) -> Iterator[str]:
    """Every identifier and non-docstring string constant in a tree."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def _api_trees() -> Iterator[ast.AST]:
    tree = ast.parse((SOURCE / "application" / "api.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (
            node.name.startswith("collection") or node.name in API_FUNCTIONS
        ):
            yield node


def _money_words(names: Iterator[str]) -> set[str]:
    return {word for name in names for word in _words(name) if MONEY.fullmatch(word)}


def test_no_name_the_module_stores_or_sends_is_about_money() -> None:
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in MODULE_FILES]
    trees.extend(_api_trees())
    assert len(trees) > len(MODULE_FILES), "the guard found none of the window's gestures"

    found = _money_words(name for tree in trees for name in _names(tree))

    assert found == set(), f"the collection names money: {sorted(found)}"


def test_no_column_of_the_collection_is_about_money() -> None:
    # Every migration that touches the collection's tables, not a list of the
    # ones written so far: the next one is covered without anybody remembering.
    touching = [
        migration
        for migration in MIGRATIONS
        if any(
            re.search(r"\b(physical_record\w*|collection_\w+)\b", statement)
            for statement in migration.statements
        )
    ]
    assert len(touching) >= 2, "the guard found none of the collection's migrations"
    sql = " ".join(statement for migration in touching for statement in migration.statements)
    without_comments = re.sub(r"--[^\n]*", "", sql)

    found = _money_words(iter(re.findall(r"[A-Za-z_]+", without_comments)))

    assert found == set(), f"a column of the collection names money: {sorted(found)}"


def test_the_guard_refuses_what_it_exists_to_refuse() -> None:
    """Seen failing: a guard that never refuses anything proves nothing."""
    tree = ast.parse("def unit_price(record):\n    return {'paidAmount': 1, 'currency': 'EUR'}\n")

    assert _money_words(_names(tree)) == {"price", "paid", "amount", "currency", "eur"}
