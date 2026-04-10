"""KServe Model Registry - Discovers available models from InferenceServices."""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import structlog
from kubernetes import client, config

from common.config import get_settings
from common.schemas import InferenceServiceInfo

logger = structlog.get_logger(__name__)
settings = get_settings()


class ModelRegistry:
    """Discovers and manages available models from KServe.
    
    Attributes:
        namespace: Kubernetes namespace to search for models.
        api_version: KServe API version.
        custom_api: Kubernetes CustomObjectsApi instance.
    """

    def __init__(self, namespace: Optional[str] = None) -> None:
        """Initializes the ModelRegistry and loads Kubernetes config.
        
        Args:
            namespace: Optional namespace override.
        """
        self.namespace = namespace or settings.kserve_namespace
        self.api_version = settings.kserve_api_version

        # Load kubeconfig (will work in-cluster or local)
        try:
            config.load_incluster_config()
            logger.info("loaded_k8s_config", source="in-cluster")
        except Exception:
            try:
                config.load_kube_config()
                logger.info("loaded_k8s_config", source="kubeconfig")
            except Exception as e:
                logger.error("failed_to_load_k8s_config", error=str(e))

        self.custom_api = client.CustomObjectsApi()

    def list_inference_services(self) -> List[InferenceServiceInfo]:
        """Lists all InferenceService resources.
        
        Returns:
            A list of InferenceServiceInfo objects.
        """
        try:
            # Attempt to list across all namespaces first
            try:
                response = self.custom_api.list_cluster_custom_object(
                    group="serving.kserve.io",
                    version="v1beta1",
                    plural="inferenceservices",
                )
            except Exception as cluster_err:
                # If cluster-wide fails (e.g., RBAC), fallback to the local namespace
                logger.warning(
                    "cluster_wide_listing_failed",
                    error=str(cluster_err),
                    trying_namespace=self.namespace,
                )
                response = self.custom_api.list_namespaced_custom_object(
                    group="serving.kserve.io",
                    version="v1beta1",
                    namespace=self.namespace,
                    plural="inferenceservices",
                )

            models: List[InferenceServiceInfo] = []
            for item in response.get("items", []):
                model = self._parse_inference_service(item)
                if model:
                    models.append(model)

            logger.info("listed_inference_services", count=len(models))
            return models

        except Exception as e:
            logger.error("failed_to_list_inference_services", error=str(e))
            return []

    def _parse_inference_service(self, isvc: Dict[str, Any]) -> Optional[InferenceServiceInfo]:
        """Parses InferenceService custom resource to model info.
        
        Args:
            isvc: The InferenceService custom resource dictionary.
            
        Returns:
            An InferenceServiceInfo object or None if parsing fails.
        """
        try:
            metadata = isvc.get("metadata", {})
            spec = isvc.get("spec", {})
            status = isvc.get("status", {})

            name = metadata.get("name", "")
            namespace = metadata.get("namespace", self.namespace)

            # Determine model type from labels or annotations
            labels = metadata.get("labels", {})
            annotations = metadata.get("annotations", {})

            model_type = labels.get("model-type", "llm")
            framework = labels.get("framework", "vllm")

            # Get status
            conditions = status.get("conditions", [])
            is_ready = any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
            )
            model_status = "ready" if is_ready else "pending"

            # Get URL
            url = status.get("url", None)
            if not url and "address" in status:
                url = status["address"].get("url")

            # Extract additional metadata
            predictor = spec.get("predictor", {})
            model_metadata = {
                "runtime": predictor.get("model", {}).get("runtime", framework),
                "storage_uri": predictor.get("model", {}).get("storageUri", ""),
                "resources": predictor.get("resources", {}),
                "labels": labels,
                "annotations": annotations,
            }

            return InferenceServiceInfo(
                id=f"{namespace}/{name}",
                name=name,
                namespace=namespace,
                type=model_type,
                framework=framework,
                status=model_status,
                url=url,
                metadata=model_metadata,
                created_at=metadata.get("creationTimestamp") or datetime.now(timezone.utc),
            )

        except Exception as e:
            logger.error(
                "failed_to_parse_inference_service",
                name=isvc.get("metadata", {}).get("name", "unknown"),
                error=str(e),
            )
            return None

    def get_model_by_name(self, name: str) -> Optional[InferenceServiceInfo]:
        """Gets a specific model by name.
        
        Args:
            name: Name of the model.
            
        Returns:
            The model info if found, else None.
        """
        models = self.list_inference_services()
        return next((m for m in models if m.name == name), None)

    def get_models_by_type(self, model_type: str) -> List[InferenceServiceInfo]:
        """Gets all models of a specific type.
        
        Args:
            model_type: The type of model (e.g., vlm, llm, embedding).
            
        Returns:
            A list of matching model info objects.
        """
        models = self.list_inference_services()
        return [m for m in models if m.type == model_type]

