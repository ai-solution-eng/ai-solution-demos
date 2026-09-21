# **Predictive Maintenance**

| Owner                       | Name                              | Email                                     |
| ----------------------------|-----------------------------------|-------------------------------------------|
| Use Case Owner              | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |
| PCAI Deployment Owner       | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |

## Abstract

This demo shows how to train a model for predictive maintenance on HPEs PCAI leveraging Jupyter Notebook Server, track the training with MLFlow, package it with bentoML and then deploy it via MLIS. 


#### This repository contains steps for training a predictive maintenance model for the manufacturing industry. Of course any model can be trained of PCAI. The model packaging and deployment process will stay similar. The Jupyter Notebook is based on a Kaggle example, the dataset used is also from Kaggle. https://www.kaggle.com/code/sharmageetika/predictive-maintenance/notebook 

## Description

### Overview

We are building a predictive maintenance use case for production machines.

The dataset we are analyzing contains the following data points:
- Product IDs
- Types (Product Quality L for Low, M for Medium, H for High)
- Air temperature
- Process Temperature
- Rotational Speed
- Tool wear
- Machine failure
- tool wear failure (TWF)
- heat dissipation failure (HDF)
- power failure (PWF)
- overstrain failure (OSF)
- random failures (RNF)

We will be analyzing the dataset first, to clean it and decide which datapoints to drop and which to keep, then we will train our model, and at the end packaging and deploying the model to run it with HPE MLIS. With the inputs of air_temperature_k", process_temperature_k, rotational_speed_rpm, torque_nm and tool_wear_min the model will be able to predict the machine failure and probability.

![Use Case Overview](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/vertical-demos/predictive-maintenance/img/PredictiveMaintenanceDemo.jpg)

**Demo overview video**
[Demo Video](to be added)

**Tools and frameworks used:**

* Jupyter Notebook
* HPE MLIS
* MLFlow
* local S3 storage

## Deployment

### Prerequisites
In newer AIE versions the S3 Data can be browsed in the UI. We have provided a Notebook in order to upload programmatically a file to that S3 storage. Tested with AIE 1.9, 1.10, 1.12, older versions might still work.

We need one free GPU to run the model.

### Installation and configuration
### **1. Create a Jupyter Notebook Server**

The default Jupyter Notebook Server is a bit low on resources, especially if you want to try out the Hyperparameter Tuning. Therefore we are creating a new Jupyter Notebook Server.

- Navigate to **Notebooks**
- On the top select your **user Project** (applicable with AIE 1.9 for lower versions you can skip this)
- Click **New Notebook Server**

Fill in the details
- Any name 
- JupyterLab
- Select as Image the jupyter-tensorflow-full from the DropDown, eg ezmeral-common/hpe-kubeflow/notebooks/jupyter-tensorflow-full:aie-1.10.0-fdfeb8a0 
- At least 2 CPU, for example 5
- At least 5Gi Memory, for example 15Gi
- No GPU

Hit Launch.

### **2. Prep your Notebook**

To Prep your Notebook Server you will first need to connect to your JupyterNotebook Server. As soon as the status switched to running you can click on the name of it in order to open it.

You can either download the necessary files from here or you could also clone the repos via the GitHub integration on the very left side.


### **3. Train your model**

In order to train your model execute the cells within the predictive-maintenance_model-training Notebook. Each step is described within the notebook in Markdown. The hyperparamter tuning section in the code can also be commented out and skipped as it takes a moment.

The output will be in the end a .pkl file.

### **4. Package your model with BentoML**

Open a terminal at /shared/predictive-maintenance
Install libs with pip install -r requirements.txt
Save model with python3 import_model.py
Make sure the setup.sh script is executable, for example by running chmod 777 setup.sh

Run bentoml build -f bentofile.yaml in terminal
Run bentoml list and identify the model built
Serve this model with bentoml serve YOURMODELTAG
Open a new terminal window and send a CURL request, response should be{"machine_failure_prediction":1,"machine_failure_probability":1.0,"model_classes":[0,1]} It’s best if you copy from the curltotestbentoserve.txtto avoid accidental characters from this ReadMe
curl -X POST "http://127.0.0.1:3000/predict" \
  -H "Content-Type: application/json" \
  -d '{"air_temperature_k":300.0,"process_temperature_k":310.0,"rotational_speed_rpm":1200,"torque_nm":70.0,"tool_wear_min":250}'
Run bentoml export YOURMODELTAG this creates a .bento file

### **5. Store your model in local S3 storage**

Navigate to Data Engineering > Data Sources > Object Store Data

If you can click **Browse** use this UI to create a new Bucket and upload your .bento file.

If not (your AIE version does not support it yet) use the S3-Helper.ipynb to create a bucket, per default testbucket, and copy the file there.

### **6. Model Deployment**
**6.1 Create a Model Registry**
Navigate to Tools & Frameworks > HPE MLIS (in earlier version Tab Data Science).

Within MLIS create a Internal S3 registry:

- Name: locals3
- Object store: local-s3 from dropdown
- Bucket: testbucket

**6.2 Create a Model Package on HPE MLIS.**

Within MLIS add a Packaged Model with:

- Name: predictive-maintenance
- Registry: locals3
- Model format: bento-archive
- URL: s3://YOURBUCKET/YOUR.BENTOFILE
- (for 1.9 and greater) Model category: other
- (for 1.9 and greater) Enable local caching
- Resource Template: Custom
- CPU: 1-> 4
- Memory: 10Gi -> 30Gi
- GPU: 1 -> 1
- Arguments: --model /mnt/models --port 8080 

**6.3 Deploy your Model**

You can navigate to GenAI> Model Catalog and click on **Deploy** on the freshly created Model. As Scaling Policy you can for example pick Fixed 1.

### **7. Interact with your Model**

In order to interact with your deployed model, you need next to the Endpoint also a Token. In order to retrieve those navigate to GenAI> Model Endpoints. Identify your Model and click on its name. On the buttom you can create a new Token for this model, and on the top you can find the Model Endpoint.

The curl command goes to the Model Endpoint you copy adding /predict. 

It’s best if you copy from the curltotestbentoserve.txtto avoid accidental characters from this ReadMe

curl -X POST "YOURMODELENDPOINT/predict" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOURTOKEN" \
  -d '{"air_temperature_k":300.0,"process_temperature_k":310.0,"rotational_speed_rpm":1200,"torque_nm":70.0,"tool_wear_min":250}'

With that we are hosting our custom trained predictive maintenance model in HPEs MLIS, being able to define automatic scaling.