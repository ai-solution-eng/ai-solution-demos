from kubernetes import client, config
from .core import get_logger

logger = get_logger(__name__)

config.load_incluster_config()

# Initialize Kubernetes API clients
v1 = client.CoreV1Api()
customObjApi = client.CustomObjectsApi()
apps_v1 = client.AppsV1Api()


def get_custom_object_list(plural: str, group: str, version: str):
    """
    Selects a V1CustomObjectList object by its name from a list of V1CustomObject objects.
    :param plural: The plural name of the V1CustomObject.
    :param group: The API group of the V1CustomObject.
    :param version: The API version of the V1CustomObject.
    :return: The selected V1CustomObjectList object or None if not found.
    """

    # Display apps as cards with actions
    try:
        # Fetch the InferenceService object
        v1CustomObjectList = customObjApi.list_custom_object_for_all_namespaces(
            group=group,
            version=version,
            resource_plural=plural,
        )
        response = v1CustomObjectList
        items_list = response['items']  # Direct dict access to list
        return [item for item in items_list]

    except client.exceptions.ApiException as e:
        logger.error(
            f"Exception for {plural} when calling CustomObjectsApi->get_namespaced_custom_object: {e}"
        )
        return []


def get_all_services():
    """
    Fetches all services from the Kubernetes cluster.
    :return: A list of service objects as client.models.v1_service.V1Service type
    """
    services = v1.list_service_for_all_namespaces(
        # label_selector="hpe-ezua/type=vendor-service"
    ).items

    # Convert V1Service objects to dictionaries
    return [service.to_dict() for service in services]
