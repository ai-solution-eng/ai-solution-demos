The following table lists the configurable parameters of the Vessels Analysis and Reconstruction chart and their default values, as defined in `values.yaml`.

| Parameter | Description | Default |
| :--- | :--- | :--- |
| `replicaCount` | The number of application pods to run. | `1` |
| `namespace` | The Kubernetes namespace where all resources will be deployed. Please create the namespace beforehand. | `"vessel-analysis-ns"` |
| **HPE Ezmeral (EzUA) Settings** | | |
| `ezua.enabled` | If `true`, an Istio VirtualService will be created to expose the app. | `true` |
| `ezua.virtualService.endpoint` | The external hostname for the service. `\${DOMAIN_NAME}` is a placeholder for your cluster's domain. | `"reconstruction.${DOMAIN_NAME}"` |
| `ezua.virtualService.istioGateway` | The Istio gateway to bind the VirtualService to. | `"istio-system/ezaf-gateway"` |
| **Image Settings** | | |
| `image.repository` | The Docker image to use for the application. | `"mendeza/vessel-analysis-app"` |
| `image.pullPolicy` | The image pull policy. | `"IfNotPresent"` |
| `image.tag` | The tag of the Docker image to pull. | `"0.0.1"` |
| **Resource Settings** | | |
| `resources.requests.cpu` | CPU requested for the pod. | `"4"` |
| `resources.requests.memory` | Memory requested for the pod. | `"32Gi"` |
| `resources.limits.cpu` | CPU limit for the pod. | `"4"` |
| `resources.limits.memory` | Memory limit for the pod. | `"32Gi"` |
| **Service Settings** | | |
| `service.type` | The type of Kubernetes service to create. | `"ClusterIP"` |
| `service.port` | The port the service will expose. | `8501` |
| `service.name` | The name of the service port. | `"http-streamlit"` |
| **Persistence Settings** | | |
| `persistence.enabled` | If `true`, mounts a volume for persistent data. | `true` |
| `persistence.existingClaim` | The name of the pre-existing PersistentVolumeClaim to use. | `"kubeflow-shared-pvc"` |
| `persistence.subPath` | The sub-path within the PVC to mount into the container. | `"califra/outputs"` |
| `persistence.mountPath` | The path inside the container where the volume will be mounted. | `"/app/outputs"` |
