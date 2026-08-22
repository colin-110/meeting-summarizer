"""Sanity checks that the backend package skeleton is importable.

Real feature tests land alongside the phases that introduce them
(transcription, summarization, the API routes, etc.) — this file just
guards against a broken package layout before there's anything else
to test.
"""

import importlib


def test_backend_app_package_imports():
    module = importlib.import_module("backend.app")
    assert module is not None


def test_backend_subpackages_import():
    subpackages = [
        "backend.app.api",
        "backend.app.services",
        "backend.app.models",
        "backend.app.schemas",
        "backend.app.database",
        "backend.app.core",
        "backend.app.utils",
    ]
    for name in subpackages:
        assert importlib.import_module(name) is not None
