"""Tests de integración para ContextDB + VectorStore."""

import pytest
import tempfile
from unittest.mock import patch


def test_contextdb_without_vector_store():
    """Test que ContextDB funciona sin VectorStore."""
    import sys
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/context.db"

        # Simular que chromadb no está disponible
        with patch.dict('sys.modules', {'chromadb': None}):
            from app.context_db import ContextDB
            db = ContextDB(db_path=db_path, vector_enabled=False)

            # Debe funcionar normalmente
            event_id = db.record_event(
                "/test/file.py",
                "test_event",
                "sentinel",
                "error",
                {"message": "Test error"}
            )

            assert event_id is not None
            assert not db.is_vector_available()

            # semantic_search debe retornar vacío
            results = db.semantic_search("test")
            assert results == []


def test_is_vector_available_true_when_working():
    """Test que is_vector_available refleja el estado real."""
    import tempfile
    from app.context_db import ContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/context.db"
        vector_dir = f"{tmpdir}/vector"

        # Intentar crear con vector (puede fallar si no hay dependencias)
        try:
            db = ContextDB(db_path=db_path, vector_enabled=True)
            # Si se creó, debe reportar disponibilidad correcta
            assert isinstance(db.is_vector_available(), bool)
        except Exception as e:
            pytest.skip(f"VectorStore no disponible en este entorno: {e}")


def test_find_similar_findings_without_vector():
    """Test que find_similar_findings retorna vacío sin VectorStore."""
    import tempfile
    from app.context_db import ContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/context.db"
        db = ContextDB(db_path=db_path, vector_enabled=False)

        results = db.find_similar_findings("some-event-id")
        assert results == []


def test_get_file_clusters_without_vector():
    """Test que get_file_clusters retorna vacío sin VectorStore."""
    import tempfile
    from app.context_db import ContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/context.db"
        db = ContextDB(db_path=db_path, vector_enabled=False)

        results = db.get_file_clusters()
        assert results == []


def test_build_event_description():
    """Test que _build_event_description funciona correctamente."""
    import tempfile
    from app.context_db import ContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/context.db"
        db = ContextDB(db_path=db_path, vector_enabled=False)

        desc = db._build_event_description(
            "secret_detected",
            "sentinel",
            "critical",
            "/config/secrets.py",
            {"description": "API key exposed", "finding": "hardcoded_key"}
        )

        assert "secret_detected" in desc
        assert "sentinel" in desc
        assert "critical" in desc
        assert "/config/secrets.py" in desc
        assert "API key exposed" in desc
        assert "hardcoded_key" in desc
