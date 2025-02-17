# Open WebUI

Helm chart for Open WebUI version 5.4.0. 

Changes have been made to be able to import this application to PCAI, using the **Import Framework** button.

The following has been edited:
* Following [instructions from the documentation page](https://support.hpe.com/hpesc/public/docDisplay?docId=a00aie16hen_us&page=ManageClusters/importing-applications.html), we made these edits:
  * Adding **virtualservice.yaml** under the **templates** folder, replacing "test-app.fullname" with "open-webui.name" (open-webui.fullname isn't defined) and "test-app.labels" with "open-webui.labels"
  * Adding this section to **values.yaml**:
```    
ezua:
  virtualService:
    endpoint: "open-webui.hpepcai.ezmeral.demo.local" # change this part based on your instance - in doubt, look at the other frameworks' endpoints
    istioGateway: "istio-system/ezaf-gateway"
```
  * Adding **kyvernoclusterpolicy.yaml** under the **templates** folder, without any change
* In **values.yaml**, ollama has been disabled, as we can rely on already deployed models in MLIS
* To avoid EzLicense capacity issues that prevent pods to be scheduled, each pod deployed by the application must specify *resources.limits.cpu*. Hence, these additional changes:
  * In **workload-manager.yaml** under the **templates** folder, resources.limits.cpu have been defined (lines 99-101):
```
resources:
  limits:
    cpu: {{ .Values.resources.limits.cpu }}
```
  * In **values.yaml**, that cpu limit has been arbitrarily defined to 10:
```
resources:
  limits:
    cpu: 10
```
  * Since open-webui pipelines is going to be installed, in **deployment.yaml** under **charts/pipelines/templates**, we set the cpu limit to 10 as well (lines 44-46):
```
resources:
  limits:
    cpu: 10
```

## Notes

* Original helm chart for Open WebUI 5.4.0 can be found [here](https://github.com/open-webui/helm-charts/releases/tag/open-webui-5.4.0) and the latest versions [here](https://github.com/open-webui/helm-charts/releases)
* Ollama installation has been disabled. Should you enable installation of Ollama, or any other optional components (tika, redis), make sure to specify *resources.limits.cpu* for each pod these applications create. Otherwise, the deployment will fail.
* The CPU limit value of 10 is completely arbitrary. Feel free to try out other values.
* Version number in **Chart.yaml** has been changed multiple times, as PCAI does not allow for reuploading helm charts for the same application name and version (in case the few first uploads led to import failures). Feel free to set it back to 5.4.0.
