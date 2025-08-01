# **AI CUSTOM CODE ASSISTANT**

| Owner                       | Name                              | Email                                     |
| ----------------------------|-----------------------------------|-------------------------------------------|
| Use Case Owner              | Nishant Chaduka                   | nishant.chanduka@hpe.com                  |
| PCAI Deployment Owner       | Nishant Chaduka                   | nishant.chanduka@hpe.com                  |

#### This repository contains package and steps for deployment of an AI Coding assitant on AIE software on a PCAI System.

## **Demo overview video**
[Demo Video](https://storage.googleapis.com/ai-solution-engineering-videos/public/AI-coding-assistant.mp4)

## **Tools and frameworks used:**

**1. Open-WebUI**

**2. HPE MLIS**

**3. Continue.Dev VSCODE Extension**

## ** Steps for installation**

**1. Download the Open-WebUI helm-chart and the Open-WebUI logo.**

[open-webui-5.4.0.tar.gz](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/open-webui-5.4.0.tar.gz)

[open-webui-logo.png](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/open-webui-logo.png)

**2. Import the framework into AIE stack on PCAI system.**

- Login into AIE software stack.
- Navigate to **Tools & Frameworks.**
- Click on **Import Framework.**
- Fill in details as below:
  
**Refer images below.**
  
  Framework Name*      : UC - AI Coding Assistant
  
  Description*         : Open-WebUI
  
  Category (select)    : Data Science
  
  Framework Icon*      : Upload the [open-webui-logo.png](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/open-webui-logo.png)
  
  Helm Chart (select)  : Upload New Chart
  
  Select File          : Upload the [open-webui-5.4.0.tar.gz](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/open-webui-5.4.0.tar.gz)
  
  Namespace*           : open-webui
  
  DEBUG                : TICK
  
- Review
  
- Submit

![Import Framework Step 1](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/import_framework_step_1.PNG)

![Import Framework Step 2](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/import_framework_step_2.PNG)

![Import Framework Step 3](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/import_framework_step_3.PNG)

![Import Framework Step 4](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/import_framework_step_4.PNG)

![Import Framework Step 5](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/import_framework_step_5.PNG)

**Once imported the login credetials are set as below**

**User Name   : admin**

**User Email  : admin@hpe.com**

**Passowrd    : admin**

**3. Model deployment on HPE MLIS.**

1. Deploy **Qwen/Qwen2.5-Coder-7B-Instruct** :  This model has been deployed as it has the capabilities of **chat**, **edit** and **autocomplete** feature.

One is however independent to choose models of their preference and deploy for usage.

At the end copy the Model Endpoints and the API tokens to a text file as we will need them in next steps.

- Follow the below images to deploy the vllm model Qwen/Qwen2.5-Coder-7B-Instruct.

![Hpe Mlis Packaged Model Deployment Step 1](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_1.PNG)

![Hpe Mlis Packaged Model Deployment Step 2](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_2.PNG)

![Hpe Mlis Packaged Model Deployment Step 3](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_3.PNG)

![Hpe Mlis Packaged Model Deployment Step 4](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_4.PNG)

![Hpe Mlis Packaged Model Deployment Step 5](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_5.PNG)

![Hpe Mlis Packaged Model Deployment Step 6](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_6.PNG)

![Hpe Mlis Packaged Model Deployment Step 7](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_7.PNG)

![Hpe Mlis Packaged Model Deployment Step 8](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_8.PNG)

![Hpe Mlis Packaged Model Deployment Step 9](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_packaged_model_deployment_step_9.PNG)

![Hpe Mlis Model Endpoint Step 10](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_model_endpoint_step_10.PNG)

![Hpe Mlis Model Api Token Step 11](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_api_token_step_11.PNG)

![Hpe Mlis Model Api Token Step 12](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/hpe_mlis_api_token_step_12.PNG)

**4. Open-WebUI [UC - AI Coding Assistant] Settings.**

You will see the **code_generation_pipeline** set as default pipeline.

** Refer the images below.**

- Navigate to **Admin Panel >> Settings >> Pipelines**

- Fill in the details below (Remember you saved the endpoint url and api token in the previosu step):

  Mlis Endpoint : Replace the vllm **Qwen/Qwen2.5-Coder-7B-Instruct** model endpoint link from MLIS.
  
  Model Id      : Add the actual model-id/model-name i.e **Qwen/Qwen2.5-Coder-7B-Instruct**

  Api Token     : Add the API Token generated from HPE MLIS for the model deployment.

- Save

- Navigate to Admin Console >> Settings >> Connections and copy the pipeline key.

![Open WebUI Pipelines Step 1](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/open_webui_pipelines_step_1.PNG)

![Open WebUI Pipelines Step 2](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/open_webui_pipelines_step_2.PNG)

![Open WebUI Pipelines Step 3](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/open_webui_pipelines_step_3.PNG)

![Open WebUI Pipelines key_Step 4](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/open_webui_pipelines_key.PNG)

**5. Install and configure Continue Dev extension on VSCODE on your laptop**

**Refer the below images.**

1. Install the Continue Dev extension on VS Code.

2. Open the config file and replace the entire content of the file with the entry in the file [continue_dev_config.yaml](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/continue_dev_config.yaml)

**NOTE : Remeber to fill/replace the correct apiBase for the open-webui pipeline in the config yaml file.**

**The format is https://open-webui-pipelines.<cluster_domain_name>**

**NOTE : Remeber to fill/replace the correct apiBase and apikey for qwen25-coder-7b-instruct in the config yaml file**

3. Save the configuration and you are ready.

![Install VSCODE Extension Continue Dev](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/install_vscode_extension_continue_dev.PNG)

![Config of Continue Dev](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/coding-assistant/images/config_vscode_extension_continue_dev.PNG)

  






