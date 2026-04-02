"""
VectorStore — Wrapper para ChromaDB.
Maneja embeddings y búsqueda semántica de eventos.
"""

import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("cerebro.vector_store")


class VectorStore:
    """
    Almacenamiento vectorial para eventos usando ChromaDB.
    Fallback graceful si chromadb no está instalado.
    """

    def __init__(self, persist_dir: Optional[str] = None, embedding_model: str = "all-MiniLM-L6-v2"):
        """
        Inicializa VectorStore.

        Args:
            persist_dir: Directorio para persistencia. Default: ~/.cerebro/vector/
            embedding_model: Modelo de sentence-transformers
        """
        self._available = False
        self._collection = None
        self._embedding_model_name = embedding_model

        try:
            import chromadb
            from chromadb.config import Settings
            self._chroma_available = True
        except ImportError:
            self._chroma_available = False
            logger.warning("chromadb no instalado. Vector search deshabilitado.")
            return

        if persist_dir is None:
            persist_dir = str(Path.home() / ".cerebro" / "vector")

        self._persist_dir = persist_dir
        Path(persist_dir).mkdir(parents=True, exist_ok=True)

        # Inicializar Chroma
        try:
            self._client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False)
            )
            self._collection = self._client.get_or_create_collection(
                name="context_events",
                metadata={"hnsw:space": "cosine"}
            )
            self._available = True
            logger.info(f"VectorStore inicializado: {persist_dir}")
        except Exception as e:
            logger.error(f"Error inicializando Chroma: {e}")
            self._available = False

    def is_available(self) -> bool:
        """Retorna True si Chroma está disponible y funcionando."""
        return self._available

    def _get_embedding_model(self):
        """Lazy load del modelo de embeddings."""
        if not hasattr(self, '_embedding_model'):
            try:
                from sentence_transformers import SentenceTransformer
                self._embedding_model = SentenceTransformer(self._embedding_model_name)
                logger.info(f"Modelo de embeddings cargado: {self._embedding_model_name}")
            except Exception as e:
                logger.error(f"Error cargando modelo de embeddings: {e}")
                self._embedding_model = None
        return self._embedding_model

    def add_event(
        self,
        event_id: str,
        description: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Indexa un evento en el store vectorial.

        Args:
            event_id: UUID del evento
            description: Texto descriptivo para embedding
            metadata: Dict con source, severity, file_path, timestamp, project, event_type

        Returns:
            bool: True si se indexó correctamente
        """
        if not self._available:
            return False

        model = self._get_embedding_model()
        if model is None:
            logger.warning("Modelo de embeddings no disponible, skip indexación")
            return False

        try:
            # Generar embedding
            embedding = model.encode(description).tolist()

            # Indexar en Chroma
            self._collection.add(
                ids=[event_id],
                documents=[description],
                embeddings=[embedding],
                metadatas=[metadata]
            )
            logger.debug(f"Evento indexado: {event_id}")
            return True
        except Exception as e:
            logger.error(f"Error indexando evento {event_id}: {e}")
            return False

    def search_similar(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Busca eventos similares semánticamente.

        Args:
            query: Texto de búsqueda
            filters: Filtros opcionales (source, severity, project, etc.)
            limit: Cantidad máxima de resultados

        Returns:
            Lista de eventos similares con score de similitud
        """
        if not self._available:
            return []

        model = self._get_embedding_model()
        if model is None:
            return []

        try:
            query_embedding = model.encode(query).tolist()

            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                where=filters,
                include=["documents", "metadatas", "distances"]
            )

            # Formatear resultados
            events = []
            if results["ids"] and results["ids"][0]:
                for i, event_id in enumerate(results["ids"][0]):
                    events.append({
                        "id": event_id,
                        "description": results["documents"][0][i] if results["documents"] else "",
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "distance": results["distances"][0][i] if results["distances"] else 0.0,
                        "score": 1.0 - (results["distances"][0][i] if results["distances"] else 0.0)
                    })

            return events
        except Exception as e:
            logger.error(f"Error en búsqueda semántica: {e}")
            return []

    def get_embeddings_for_file(
        self,
        file_path: str
    ) -> List[Dict[str, Any]]:
        """
        Obtiene todos los embeddings de eventos de un archivo específico.

        Args:
            file_path: Ruta del archivo

        Returns:
            Lista de eventos con sus embeddings
        """
        if not self._available:
            return []

        try:
            results = self._collection.get(
                where={"file_path": file_path},
                include=["embeddings", "documents", "metadatas"]
            )

            events = []
            if results["ids"]:
                for i, event_id in enumerate(results["ids"]):
                    events.append({
                        "id": event_id,
                        "embedding": results["embeddings"][i] if results["embeddings"] else None,
                        "description": results["documents"][i] if results["documents"] else "",
                        "metadata": results["metadatas"][i] if results["metadatas"] else {}
                    })

            return events
        except Exception as e:
            logger.error(f"Error obteniendo embeddings para {file_path}: {e}")
            return []
