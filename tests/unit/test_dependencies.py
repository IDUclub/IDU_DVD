"""Unit tests for src/dependencies — the Dependencies singleton container and its getters.

Covers: singleton identity, pre-init guard, getter wiring, as_dict, __repr__, and reset.
Uses placeholder objects (the container does no work on them — it only stores and returns).
"""

from __future__ import annotations

import pytest

from src.dependencies import Dependencies, get_dependencies


def _make_field_objects() -> dict:
    return {name: f"<{name}>" for name in Dependencies._FIELDS}


@pytest.fixture(autouse=True)
def _reset_singleton():
    Dependencies.reset()
    yield
    Dependencies.reset()


class TestSingleton:
    def test_same_instance_every_time(self):
        assert Dependencies() is Dependencies()

    def test_reset_allows_fresh_instance(self):
        first = Dependencies()
        Dependencies.reset()
        assert Dependencies() is not first


class TestPreInitGuards:
    def test_instance_raises_before_set(self):
        with pytest.raises(RuntimeError):
            Dependencies.instance()

    def test_getter_raises_before_set(self):
        with pytest.raises(RuntimeError):
            Dependencies.get_search()

    def test_get_dependencies_raises_before_set(self):
        with pytest.raises(RuntimeError):
            get_dependencies()

    def test_repr_uninitialized(self):
        assert repr(Dependencies()) == "Dependencies(uninitialized)"


class TestAfterSet:
    def test_getters_return_stored_values(self):
        Dependencies().set(**_make_field_objects())
        assert Dependencies.get_settings() == "<settings>"
        assert Dependencies.get_search() == "<search>"
        assert Dependencies.get_qdrant() == "<qdrant>"
        assert get_dependencies() is Dependencies.instance()

    def test_as_dict_has_all_fields_in_order(self):
        Dependencies().set(**_make_field_objects())
        d = Dependencies.instance().as_dict()
        assert tuple(d.keys()) == Dependencies._FIELDS

    def test_repr_lists_all_fields(self):
        Dependencies().set(**_make_field_objects())
        r = repr(Dependencies.instance())
        assert r.startswith("Dependencies(")
        for name in Dependencies._FIELDS:
            assert f"{name}=" in r

    def test_getters_usable_as_fastapi_dependencies(self):
        # bound classmethods take no params -> valid Depends callables
        import inspect

        for getter in (
            Dependencies.get_settings,
            Dependencies.get_jobs,
            Dependencies.get_search,
        ):
            assert list(inspect.signature(getter).parameters) == []
