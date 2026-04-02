"""Tests para VectorStore."""

import pytest
import tempfile
import shutil
from pathlib import Path


def test_vector_store_unavailable_without_chroma():
    """Test que VectorStore detecta cuando Chroma no está disponible."""
    from unittest.mock import patch

    with patch.dict('sys.modules', {'chromadb': None}):
        # Reimportar para que tome el mock
        import importlib
        import sys
        # Remover del cache si existe
        if 'cerebro.app.vector_store' in sys.modules:
            del sys.modules['cerebro.app.vector_store']
        if 'app.vector_store' in sys.modules:
            del sys.modules['app.vector_store']

        from app.vector_store import VectorStore
        store = VectorStore(persist_dir="/tmp/test_vector")
        assert not store.is_available()


def test_vector_store_initialization():
    """Test que VectorStore se inicializa correctamente con Chroma."""
    from app.vector_store import VectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(persist_dir=tmpdir)
        # Nota: puede no estar available si sentence-transformers no está instalado
        # pero no debería lanzar excepción
        assert store._persist_dir == tmpdir


def test_add_event_returns_false_when_unavailable():
    """Test que add_event retorna False cuando Chroma no está disponible."""
    from app.vector_store import VectorStore

    store = VectorStore.__new__(VectorStore)
    store._available = False

    result = store.add_event("test-id", "test description", {"source": "test"})
    assert result is False


def test_search_similar_returns_empty_when_unavailable():
    """Test que search_similar retorna lista vacía cuando no está disponible."""
    from app.vector_store import VectorStore

    store = VectorStore.__new__(VectorStore)
    store._available = False

    results = store.search_similar("test query")
    assert results == []
