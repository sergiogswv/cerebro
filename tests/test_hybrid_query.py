"""Tests para HybridQueryEngine."""

import pytest
import tempfile
from unittest.mock import Mock, MagicMock


def test_semantic_search_returns_empty_when_vector_unavailable():
    """Test que semantic_search retorna vacío si VectorStore no está disponible."""
    from app.hybrid_query_engine import HybridQueryEngine

    mock_db = Mock()
    mock_vector = Mock()
    mock_vector.is_available.return_value = False

    engine = HybridQueryEngine(mock_db, mock_vector)
    results = engine.semantic_search("test query")

    assert results == []


def test_find_similar_findings_returns_empty_when_vector_unavailable():
    """Test que find_similar_findings retorna vacío si no hay VectorStore."""
    from app.hybrid_query_engine import HybridQueryEngine

    mock_db = Mock()
    mock_vector = Mock()
    mock_vector.is_available.return_value = False

    engine = HybridQueryEngine(mock_db, mock_vector)
    results = engine.find_similar_findings("event-123")

    assert results == []


def test_get_file_clusters_returns_empty_when_vector_unavailable():
    """Test que get_file_clusters retorna vacío sin VectorStore."""
    from app.hybrid_query_engine import HybridQueryEngine

    mock_db = Mock()
    mock_vector = Mock()
    mock_vector.is_available.return_value = False

    engine = HybridQueryEngine(mock_db, mock_vector)
    results = engine.get_file_clusters()

    assert results == []


def test_build_event_description():
    """Test que _build_event_description funciona correctamente."""
    from app.hybrid_query_engine import HybridQueryEngine

    mock_db = Mock()
    mock_vector = Mock()

    engine = HybridQueryEngine(mock_db, mock_vector)

    event_data = {
        "event_type": "secret_detected",
        "source": "sentinel",
        "severity": "critical",
        "payload": {"description": "API key found in code"}
    }

    desc = engine._build_event_description(event_data)

    assert "secret_detected" in desc
    assert "sentinel" in desc
    assert "critical" in desc
    assert "API key found" in desc


def test_infer_cluster_topic():
    """Test que _infer_cluster_topic extrae palabras comunes de paths."""
    from app.hybrid_query_engine import HybridQueryEngine

    mock_db = Mock()
    mock_vector = Mock()

    engine = HybridQueryEngine(mock_db, mock_vector)

    files = [
        "/src/auth/login.py",
        "/src/auth/oauth.py",
        "/src/auth/jwt.py"
    ]

    topic = engine._infer_cluster_topic(files)

    assert "auth" in topic or "src" in topic
