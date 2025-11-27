# **Natural Language to SQL (NL to SQL) leveraging MCP Example of Manufacturing Industry**

| Owner                       | Name                              | Email                                     |
| ----------------------------|-----------------------------------|-------------------------------------------|
| Use Case Owner              | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |
| PCAI Deployment Owner       | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |

#### This repository contains steps for deployment of an NLtoSQL use case leveraging MCP in the example of manufacturing industry on AIE software on a PCAI System. The demo can be easily adapted for another industry by swapping the dataset.

## **Demo overview video**
[Demo Video](to be added)

## **Tools and frameworks used:**

**1. Presto**

**2. HPE MLIS**

**3. Open-WebUI**

**4. Superset**

## ** Steps for installation**

### **1. Data Source**

To connect a datasource to HPEs PCAI we first need to create one. 

**1. Deploy Database**

If you already have a DB available outside of PCAI or also within your PCAI you can import the .db file there.
If not follow the steps below to import a Postgres into your environment.

- Login into AIE software stack.
- Navigate to **Tools & Frameworks.**
- Click on **Import Framework.**
- Fill in details as below:
  
  Framework Name*      : Postgres
  
  Description*         : Open-source relational database management system using SQL, supports ACID transactions, extensions, JSON, concurrent users, replication.
  
  Category (select)    : Data Engineering
  
  Framework Icon*      : Upload the [postgreslogo.png](https://github.com/ai-solution-eng/frameworks/blob/main/postgresql/logo.png)
  
  Helm Chart (select)  : Upload New Chart
  
  Select File          : Upload the [postgresql-latest.tgz](https://github.com/ai-solution-eng/frameworks/blob/main/postgresql/postgresql-latest.tar.gz)
  
  Namespace*           : postgres
  
  DEBUG                : TICK
  
- Review
If you want to define a custom password for the admin user define that ib line 38 postgresPassword
- Submit

**2. Load db file**

You can use the .db file in this folder to load into a Database. If you care to adapt the data for example to a different industry or specific locations take a look at create_manufacturing_data.py script and adapt. After execution you have a .db file you can use as well for the following steps.

Open your Jupyter Notebook server and upload the .db file and the loaddata.py. You will need to add the password you provided when Importing Postgres to line 9. of loaddata.py Also be aware if you adapt the db_name in line 13 you will need to change in the step of Connect Database to AIE in the URL manufacturing to whatever you decide for as a dbname.

When you have uploaded those to files open a terminal in the JupyterNotebook Server. Execute ls to make sure you can see both files at this location. Install the library psycopg2 via pip installpsycopg2 and then execute the loaddata.py with python loaddata.py

**3. Connect Database to AIE**

- Login into AIE software stack.
- Navigate to **Data Engineering > Data Sources**
- In **Structured Data** click on **Add New Data Source**
- Select **PostgreSQL** and click **Create Connection**
- Fill in details as below:
  Name*      : manufacturingdb
  Connection URL*: jdbc:postgresql://postgresql.postgres.svc.cluster.local:5432/manufacturing
  Connection User*: postgres
  Connection Password*: YOURDEFINEDPASSWORD
  Click on PostgreSQl Advanced Settings
  Case Insensitive Name Matching: Tick

**4. Explore the Data Catalog**

Once the Connection is made you can explore the available data via the Data Catalog. 

- Navigate to **Data Engineering > Data Catalog**
- Select **manufacturingdb**
- Tick **public** schema
- See three Tables, machine_metrics, machines and operators. Select one 
- Click **Data Preview** to get an overview of the data.

### **2. Model Deployment**
**3. Model deployment on HPE MLIS.**

1. Deploy **Qwen/Qwen3-8B-Instruct** :  This model has been deployed as it has the capabilities of **chat**, and good experience on working with the Presto MCP feature, especially compared with LLama 3.1 8B Instruct. 

One is however independent to choose models of their preference and deploy for usage.

At the end copy the Model Endpoint and the API tokens to a text file as we will need them in next steps.

Navigate to Tools & Frameworks > HPE MLIS (in earlier version Tab Data Science)

- Create a Packaged Model with:
  - Name: qwen3-8b
  - Registry: None
  - Model format: Custom
  - Image: vllm/vllm-openai:v0.9.0
  - (for 1.9 and greater) Model category: llm
  - (for 1.9 and greater) Enable local caching
  - Resource Template: Custom
  - CPU: 1-> 8
  - Memory: 8Gi -> 32Gi
  - GPU: 1 -> 1
  - Advanced Environment Variables: HUGGING_FACE_HUB_TOKEN your HuggingfaceToken
  - Arguments: --model Qwen/Qwen3-8B --enable-reasoning --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser hermes --port 8080

- Deploy the Model with Auto scaling template **fixed-1**

Once deployed you will need to create a token. For AIE 1.9 and greater you need to head to AIE -> Gen AI -> Model Endpoints -> 3 dots at Action -> Generate API Token.
For earlier AIE versions you need to create the API Token within MLIS.

### **3. Chat Interface**
**1. Download the Open-WebUI helm-chart and the Open-WebUI logo.**

If you already have deployed Open WebUI in the environment for a different use case, you can just reuse that.

[open-webui-8.12.2-pcai.tgz](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/open-webui-8.12.2-pcai.tgz)

[open-webui-logo.png](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/logo.png)

**2. Import the framework into AIE stack on PCAI system.**

- Login into AIE software stack.
- Navigate to **Tools & Frameworks.**
- Click on **Import Framework.**
- Fill in details as below:
  
  
  Framework Name*      : Open-WebUI
  
  Description*         : Open WebUI is an extensible, feature-rich, and user-friendly AI platform.
  
  Category (select)    : Data Science
  
  Framework Icon*      : Upload the [open-webui-logo.png](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/logo.png)
  
  Helm Chart (select)  : Upload New Chart
  
  Select File          : Upload the [open-webui-8.12.2-pcai.tgz](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/open-webui-8.12.2-pcai.tgz)
  
  Namespace*           : open-webui
  
  DEBUG                : TICK
  
- Review

When you see the values yaml in the wizard, take a moment to examine how you can log in later. By default it should have SSO enabled (oidc enabled true, line 640). If that is the case and you want it like that follow the instructions from the [readMe] (https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/ReadMe.md) to gather the clientSecret you need to add in line 646. Follow these instructions, add in the value for the clientsecret and hit submit.

- Submit

Once deployed yous hould be able to sign in leveraging Single Sign On.
**4. Open-WebUI Settings.**

We need to connect Open WebUI to our model deployed earlier in MLIS.

- Open OpenWebUI

- Navigate to **Admin Panel >> Settings >> Connections**

- Fill in the details below (Remember you saved the endpoint url and api token in the previous step):

  URL : Use model endpoint link from MLIS adn add /v1 to the end.
  
  Auth : Add the API Token generated from the model deployment and fill it in the API Key.

- Save

- Navigate to Admin Console >> Settings >> Connections and copy the pipeline key.


Now we need to as well add the MCP Server Connection. Therefore navigate in Open WebUI to **Admin Panel >> Settings >> Eexternal Tools**

- Add a new Tool Server
- Change the type to MCP Streamable HTTP 
- Add the URL of your ezPresto MCP Server (if you are not aware what this is and where to find it you might have an AIE version where MCP is not an official feature yet. For demo and non production use you can import it as custom framework still. If you are interested in that reach out to me.)
- Add the JWT token of the user you want to have the Presto Connections of
- Provide the ID and Name PrestoMCP

As next step we need to Create a Model that is leveraging the Qwen3 8b Base Model and has the MCP Server as Tool available. Therefore click in OpenWebUI on Workspace. Click **New Model**. Provide your Model a Name for example Manufacturing select Qwen/Qwen3-8B as base Model. Edit Visibility to Public in case you want the model to be available for everyone to chat. Add a System Prompt for example: "Always use 'manufacturingdb' catalog and the schema 'public' for SQL queries. Syntax: catalog.schema.table is how you reference a table in presto"

Click on the Advance Params and set Function Calling to Native, otherwise it will only make one call. You can edit this within your chat as well.
Underneath Tools tick the PrestoMCP Tool and click Save&Create.


### **4. Dashboard**

**1. Superset Installation**
Install Superset if not yet available in your cluster. Therefore go to Administration > Tools & Frameworks. Locate Superset, select the 3 Dots and click Install.

**2. Superset Presto Connection**
Open Superset ( BI Reporting ).
Within Superset we first need to configure the connection to Presto. 
Under Settings select Database Connections. 
Add a new Database by clicking + Database on the top right.
Select Presto.
Provide the following SQL Alchemy URL: presto://ezpresto.YOURDOMAINNAME:443/cache where you need to insert the domain name of the cluster you are working on. You can take that from the URL in your brwoser, eg home.YOURDOMAINNAME

**3. Superset Dataset Creation**
Now that we have connected Presto we need to create datasets in order to build charts and Dashboards out of them. 

**4. Superset Import Dahsboardn**
Import Dashboard



  






