"""
HybridQueryEngine — Unifica queries SQL + vectoriales.

Proporciona búsqueda semántica y clustering sobre ContextDB.
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("cerebro.hybrid_query")


class HybridQueryEngine:
    """
    Motor de queries híbridas que combina SQLite + Vector Store.
    """

    def __init__(self, context_db, vector_store):
        """
        Inicializa el motor híbrido.

        Args:
            context_db: Instancia de ContextDB (SQLite)
            vector_store: Instancia de VectorStore (Chroma)
        """
        self._context_db = context_db
        self._vector_store = vector_store
        logger.info("HybridQueryEngine inicializado")

    def semantic_search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Búsqueda semántica de eventos.

        Args:
            query: Texto de búsqueda natural
            filters: Filtros opcionales {source, severity, project}
            limit: Cantidad máxima de resultados

        Returns:
            Lista de eventos con metadata de SQLite + score vectorial
        """
        if not self._vector_store.is_available():
            logger.warning("VectorStore no disponible, semantic_search no funciona")
            return []

        # Buscar en Vector Store
        vector_results = self._vector_store.search_similar(query, filters, limit)

        if not vector_results:
            return []

        # Enriquecer con datos de SQLite
        enriched_results = []
        for result in vector_results:
            event_id = result["id"]

            # Obtener datos completos de SQLite
            sqlite_data = self._get_event_from_sqlite(event_id)
            if sqlite_data:
                result["event_data"] = sqlite_data

            enriched_results.append(result)

        return enriched_results

    def _get_event_from_sqlite(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene datos completos de un evento desde SQLite."""
        import json
        conn = self._context_db._get_connection()
        cursor = conn.execute(
            """
            SELECT id, file_path, event_type, source, severity, payload, timestamp
            FROM file_events WHERE id = ?
            """,
            (event_id,)
        )
        row = cursor.fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "event_type": row["event_type"],
            "source": row["source"],
            "severity": row["severity"],
            "payload": json.loads(row["payload"]) if row["payload"] else None,
            "timestamp": row["timestamp"]
        }

    def find_similar_findings(
        self,
        event_id: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Encuentra hallazgos similares a un evento existente.

        Args:
            event_id: ID del evento de referencia
            limit: Cantidad de resultados similares (excluyendo el original)

        Returns:
            Lista de eventos similares
        """
        if not self._vector_store.is_available():
            return []

        # Obtener el evento de referencia
        event_data = self._get_event_from_sqlite(event_id)
        if not event_data:
            return []

        # Construir query a partir del evento
        description = self._build_event_description(event_data)

        # Buscar similares
        similar = self._vector_store.search_similar(description, limit=limit + 1)

        # Filtrar el evento original y eventos del mismo archivo
        results = []
        for item in similar:
            if item["id"] != event_id:
                item["event_data"] = self._get_event_from_sqlite(item["id"])
                results.append(item)
                if len(results) >= limit:
                    break

        return results

    def _build_event_description(self, event_data: Dict[str, Any]) -> str:
        """Construye descripción textual de un evento para búsqueda."""
        payload = event_data.get("payload", {}) or {}
        desc_parts = [
            f"Event Type: {event_data.get('event_type', 'unknown')}",
            f"Source: {event_data.get('source', 'unknown')}",
            f"Severity: {event_data.get('severity', 'unknown')}",
        ]

        if payload.get("description"):
            desc_parts.append(f"Description: {payload['description']}")
        if payload.get("message"):
            desc_parts.append(f"Message: {payload['message']}")
        if payload.get("finding"):
            desc_parts.append(f"Finding: {payload['finding']}")

        return "\n".join(desc_parts)

    def get_file_clusters(
        self,
        project: Optional[str] = None,
        min_cluster_size: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Clustering de archivos por similitud semántica de eventos.

        Args:
            project: Filtrar por proyecto específico
            min_cluster_size: Tamaño mínimo de cluster a retornar

        Returns:
            Lista de clusters con archivos y tópico representativo
        """
        if not self._vector_store.is_available():
            return []

        try:
            from sklearn.cluster import DBSCAN
            import numpy as np
        except ImportError:
            logger.warning("sklearn no disponible, clustering deshabilitado")
            return []

        # Obtener todos los embeddings
        filters = {"project": project} if project else None

        try:
            all_embeddings = self._vector_store._collection.get(
                where=filters,
                include=["embeddings", "metadatas"]
            )
        except Exception as e:
            logger.error(f"Error obteniendo embeddings: {e}")
            return []

        if not all_embeddings["ids"] or len(all_embeddings["ids"]) < min_cluster_size:
            return []

        # Agrupar por archivo
        file_embeddings = {}
        for i, metadata in enumerate(all_embeddings["metadatas"]):
            file_path = metadata.get("file_path")
            if file_path:
                if file_path not in file_embeddings:
                    file_embeddings[file_path] = []
                file_embeddings[file_path].append(all_embeddings["embeddings"][i])

        if len(file_embeddings) < min_cluster_size:
            return []

        # Calcular embedding promedio por archivo
        files = list(file_embeddings.keys())
        file_centroids = []
        for file_path in files:
            embeddings = file_embeddings[file_path]
            centroid = np.mean(embeddings, axis=0)
            file_centroids.append(centroid)

        # Clustering con DBSCAN
        X = np.array(file_centroids)
        clustering = DBSCAN(eps=0.3, min_samples=2, metric="cosine").fit(X)

        # Agrupar resultados
        clusters = {}
        for i, label in enumerate(clustering.labels_):
            if label == -1:  # Noise
                continue
            if label not in clusters:
                clusters[label] = {"files": [], "centroid": []}
            clusters[label]["files"].append(files[i])

        # Formatear clusters
        result = []
        for label, data in clusters.items():
            if len(data["files"]) >= min_cluster_size:
                result.append({
                    "cluster_id": int(label),
                    "files": data["files"],
                    "file_count": len(data["files"]),
                    "topic": self._infer_cluster_topic(data["files"])
                })

        return sorted(result, key=lambda x: x["file_count"], reverse=True)

    def _infer_cluster_topic(self, files: List[str]) -> str:
        """Infiere tópico del cluster basado en paths de archivos."""
        from collections import Counter
        import re

        words = []
        for file in files:
            parts = re.findall(r'[a-zA-Z_]+', file)
            words.extend([p.lower() for p in parts if len(p) > 2])

        if not words:
            return "mixed"

        most_common = Counter(words).most_common(3)
        return "-".join([w for w, _ in most_common])
