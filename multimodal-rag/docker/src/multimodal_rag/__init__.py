# Release version — kept in sync with the helm charts by bump_version.sh
# (automation.sh bumps BEFORE the docker build, so the running image can
# self-identify: `python3 -c "import multimodal_rag; print(multimodal_rag.__version__)"`).
__version__ = "3.4.0"

from .dataset_manager import DatasetManager
from .rag_system import MultimodalRAG, MultiModalRAGSystem, Postprocessor, Preprocessor

__all__ = [
    "DatasetManager",
    "MultiModalRAGSystem",
    "MultimodalRAG",
    "Postprocessor",
    "Preprocessor",
]
