# Langflow Agent Flight Assistant Demo

| Owner                       | Name                                | Email                                                        |
| ----------------------------|-------------------------------------|--------------------------------------------------------------|
| Use Case Owner              | Alejandro Morales                   | alejandro.morales-martinez@hpe.com                           |
| PCAI Deployment Owner       | Alejandro Morales                   | alejandro.morales-martinez@hpe.com                           |

# Summary
- [Demo overview video](#demo-overview-video)
- [Langflow installation in PCAI](#langflow-installation-in-pcai)
- [MLIS setup in PCAI](#mlis-setup-in-pcai)
- [Langflow setup in PCAI](#setup-before-the-demo)

## Demo overview video

- [Short version](https://storage.googleapis.com/ai-solution-engineering-videos/public/PCAI%20Demo%20-%20AI%20User%20(Agentic%20Workflow%20with%20OSS%20Langflow).mp4)
- [Long version](https://hpe-my.sharepoint.com/:v:/p/hoang_phan/EUVnjGRr8wJGlCk2Hw5IwOkB8WQ0gpZ5zV9KKUXIHCSl5g?e=uexDHe&nav=eyJyZWZlcnJhbEluZm8iOnsicmVmZXJyYWxBcHAiOiJTdHJlYW1XZWJBcHAiLCJyZWZlcnJhbFZpZXciOiJTaGFyZURpYWxvZy1MaW5rIiwicmVmZXJyYWxBcHBQbGF0Zm9ybSI6IldlYiIsInJlZmVycmFsTW9kZSI6InZpZXcifX0%3D)

## Qdrant installation in PCAI (only for langflow-agent-v1-4.json)

1. [Install Qdrant within a PCAI environment using the following chart](https://github.com/ai-solution-eng/frameworks/tree/main/qdrant)
2. [Follow these instructions to install the helm chart](https://support.hpe.com/hpesc/public/docDisplay?docId=a00aie16hen_us&docLocale=en_US&page=ManageClusters/importing-applications.html)

## Langflow installation in PCAI

1. [Install Langflow within a PCAI environment using the following chart](https://github.com/ai-solution-eng/frameworks/tree/main/langflow)
2. [Follow these instructions to install the helm chart](https://support.hpe.com/hpesc/public/docDisplay?docId=a00aie16hen_us&docLocale=en_US&page=ManageClusters/importing-applications.html)

**Note:** The latest workflow version (i.e. `langflow-agent-v1-4.json`) was tested in Langflow version `1.7.3`. Please consider installing this version to avoid conflicts or issues with it in older/newer versions.

---

# MLIS setup in PCAI

1. Deploy the `Qwen/Qwen2.5-7B-Instruct` model using the following configuration:
    - General:
        - Registry: `None`
        - Image: `vllm/vllm-openai:latest`
    - Advanced:
        - Environment Variables (add you HF token)
            - HUGGING_FACE_HUB_TOKEN
        - Arguments (vLLM serve arguments)
            - `--model Qwen/Qwen2.5-7B-Instruct --enable-auto-tool-choice --tool-call-parser hermes --port 8080`
2. Once successfully deployed, save the endpoint url and append `/v1` for later, as well as add an API token under the `Users` and save that as well.
3. Deploy the `nvidia/nv-embedqa-e5-v5` model using the following configuration:
    - General:
        - Registry: `NGC` (create an NGC registry if there is none)
        - NGC Supported Models: `nvidia/nv-embedqa-e5-v5`
4. Once successfully deployed, save the endpoint url and append `/v1` for later, as well as add an API token under the `Users` and save that as well.

---

# Fake customer data setup

## EzPresto Table Setup - Adding as Data Source in PCAI (only for `langflow-agent-v1-4.json`)

Upload the [provided fake customer data](data/fake_customer_info.csv) in the  `/shared` folder.

### Option 1: Upload via Jupyter Notebook
- Launch a Jupyter Notebook and navigate to `/shared`
- Create a new folder there named `langflow-demo` and a sub-directory named `fake_customer_data`
- Upload the CSV file into this folder (drag and drop in the Notebook)
- Open a terminal in the Notebook. Navigate to  `/shared` and execute the following command to grant permissions on the file: `chmod 777 -R langflow-demo`

![alt text](imgs/upload_customer_data_via_notebook.png)

### Option 2: Upload from Data Volumes
- In AI Essentials, navigate to **Data Engineering --> Data Sources --> Data Volumes**
- Click in `shared`
- Create a folder named `langflow-demo` and click it to navigate inside
- Create a sub-directory named `fake_customer_data` and click it to navigate inside
- Click "Upload", select the CSV file and upload it

![alt text](imgs/upload_customer_data_aie_ui.png)

## Add Datasource
Once the file is uploaded, Add New Data Source in the Structured Data section
- In AI Essentials, navigate to **Data Engineering --> Data Sources --> Structured Data**
- Click "Add New Data Source"
- Under the "Hive" card, select "Create Connection" and fill it up with the following values:
```
  Name: fakecustomerinfo
  Hive Metastore: Discovery
  Data Dir: file:/data/shared/langflow-demo
  File Type: CSV
```
![alt text](imgs/hive_connection_settings.png)

- Once the connection is successful, if it was created as private (lock icon), click the three docs on the new `fakecustomerinfo` card and select "Change to public access" to make it public (globe icon)

## AstraDB Setup (only for `langflow-agent-v1-2.json` and `langflow-agent-v1-3.json`)

1. [Sign in to AstraDB or create an account if you don't have one already](https://accounts.datastax.com/session-service/v1/login)
2. Create a new vector database and a new collection called `customers` where you can upload the [provided fake customer data](data/fake_customer_info.csv)
![alt text](imgs/dataupload.png)
3. Once the data is uploaded, copy the endpoint and generate a token, you will need both of these for later

![alt text](imgs/astradb.png)

---

# Setup before the demo

## Qdrant Setup (only for `langflow-agent-v1-4.json`)

1. Once Qdrant has been deployed, in AI Essentials navigate to "Tools & Frameworks", search for the Qdrant card and click "Open" to access the UI (you may need to append `/dashboard` at the end of the URL)
2. Navigate to "Console" in the Qdrant UI
3. Copy paste the following code and click the "RUN" button above it to create a new Collection named `deltaflight`
    ```
    PUT /collections/deltaflight
    {
      "vectors": {
        "size": 1024,
        "distance": "Cosine"
      }
    }
    ```
    ![alt text](imgs/create_qdrant_collection.png)
4. Save the Qdrant endpoint URL without the `https://` at the beginning and the `/dashboard` part we added earlier (e.g. `qdrant.<your.domain>.com`)

## Retrieve AI Essentials User Token
1. From the AI Essentials UI open your Jupyter Notebook.
2. Create a new Python file and execute the following code to retrieve your user token and save it
```
secret_file_path = "/etc/secrets/ezua/.auth_token"
with open(secret_file_path, "r") as file:
    token = file.read().strip()
print(token)
 ```

**CAUTION:** User tokens expire after 30 minutes. Please ensure to get an updated token when needed. If needed, it's possible to extend the lifetime of these tokens following [this procedure](https://support.hpe.com/hpesc/public/docDisplay?docId=a00aie110hen_us&page=Security/keycloak-change-auth-token-settings.html).

## Langflow setup

1. Once Langflow has been deployed, go to the top right menu next to the profile picture as shown in the image below:
![alt text](imgs/settings.png)
2. In the settings select `Global Variables` and select `Add New` as shown below:
![alt text](imgs/globalvariables.png)
3. You will need to add the following variables that are configured previously:
    ![alt text](imgs/newvariable.png)
    - **`NV_EMBEDQA_E5_V5_NIM` (Generic):**
        - Set this to your embedding model endpoint URL created earlier during MLIS setup step
        - If you want to skip SSL verify, set this to the predictor's internal K8s service name (e.g. `http://<model_deployment_name>.<deployment_namespace>.svc.cluster.local/v1`)
    - **`NV_EMBEDQA_E5_V5_NIM_TOKEN` (Credential):**
        - Set this to your embedding model API Token created earlier during MLIS setup step
    - **`QWEN_2_5_INSTRUCT` (Generic):**
        - Set this to your Qwen LLM model endpoint URL created earlier during MLIS setup step
        - If you want to skip SSL verify, set this to the predictor's internal K8s service name (e.g. `http://<model_deployment_name>.<deployment_namespace>.svc.cluster.local/v1`)
    - **`QWEN_2_5_INSTRUCT_TOKEN` (Credential):**
        - Set this to your Qwen LLM model API Token created earlier during MLIS setup step
    - **Only for `langflow-agent-v1-4.json`:**
        - **`QDRANT_ENDPOINT` (Generic):**
            - Set this to the Qdrant endpoint URL from Qdrant setup step
            - If you want to skip SSL verify, set this to the Qdrant internal K8s service name (e.g. `http://<qdrant_service_name>.<qdrant_namespace>.svc.cluster.local`)
        - **`QDRANT_COLLECTION` (Generic):**
            - Set this to the name of the collection we created during Qdrant setup step (i.e. `deltaflight`)
    - **Only for `langflow-agent-v1-2.json` and `langflow-agent-v1-3.json` :**
        - **`ASTRA_DB_ENDPOINT` (Generic):**
            - Set this to the AstraDB endpoint URL from AstraDB setup step
        - **`ASTRA_DB_TOKEN` (Credential):**
            - Set this to the AstraDB token created earlier during AstraDB setup step
5. Upload the [provided flow if using `Langflow 1.4.X`](langflow-agent-v1-4.json) into any project by clicking the upload button
    - If planning to use AstraDB to load the fake customer data, use either [`Langflow 1.3.X`](langflow-agent-v1-2.json) or [`Langflow 1.2.X`](langflow-agent-v1-2.json) older workflows instead
![alt text](imgs/flowupload.png)
6. Upload the [provided `EzPresto Tool`](EzPresto_tool.json) by clicking the upload button
7. Open the flow and make sure to [re-upload the flight policy file](data/delta_refund_policy.pdf) to the `File` component
![alt text](imgs/fileupload_v1-4.png)
8. Ensure you configure the necessary components' fields with the Langflow Global Environment Variables we configured earlier (**Note:** If you cannot see any of the fields mentioned before for a given component, hover over the component and click "Controls", select the fields to display them in the component and close)
    - **NVIDIA Embeddings Component** (there are two of these)
        - NVIDIA Base URL: Click in the Globe icon and select the `NV_EMBEDQA_E5_V5_NIM` environment variable
        - NVIDIA API Key: Click in the Globe icon and select the `NV_EMBEDQA_E5_V5_NIM_TOKEN` environment variable
    - **NVIDIA Component** (LLM connected to the Agent Component)
        - NVIDIA Base URL: Click in the Globe icon and select the `QWEN_2_5_INSTRUCT` environment variable
        - NVIDIA API Key: Click in the Globe icon and select the `QWEN_2_5_INSTRUCT_TOKEN` environment variable
    - **Qdrant Component** (there are two of these)
        - Collection Name: Click in the Globe icon and select the `QDRANT_COLLECTION` environment variable
        - If your `QDRANT_ENDPOINT` variable is set to the Qdrant VirtualService endpoint URL
            - Host: Click in the Globe icon and select the `QDRANT_ENDPOINT` environment variable
            - Port: `443`
            - URL: Leave blank
        - If you want to skip SSL verify and your `QDRANT_ENDPOINT` variable is set to the Qdrant internal K8s service name
            - Host: Leave blank
            - Port: `6333`
            - URL: Click in the Globe icon and select the `QDRANT_ENDPOINT` environment variable       
        - gRPC Port: `6334`
    - **EzPresto tool Component**
        - User Token: Set this to your AI Essentials user token you saved in an earlier step (remember that by default these tokens expire after 30 minutes)
10. Open the playground and test the workflow with a question like:
    > "My name is John and my flight is A105, I was downgraded to coach from first class, what is my refund?"
    - The model should return the correct answer of $90 after calling both tools included (`FlightPolicy` and `FlightPassenger`)
