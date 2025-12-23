# **Natural Language to SQL (NL to SQL) leveraging MCP Example of Manufacturing Industry**

| Owner                       | Name                              | Email                                     |
| ----------------------------|-----------------------------------|-------------------------------------------|
| Use Case Owner              | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |
| PCAI Deployment Owner       | Isabelle Steinhauser              | isabelle.steinhauser@hpe.com              |

## Abstract

This demo shows how a NL to SQL use case can be implemented levaraging HPE PCAIs capbalities to connect a datasource, deploy a model, Presto MCP server and build a Dashboard while using a manufactruing example dataset.

#### This repository contains steps for deployment of an NLtoSQL use case leveraging MCP in the example of manufacturing industry on AIE software on a PCAI System. The demo can be easily adapted for another industry by swapping the dataset.

## **Demo overview video**
[Demo Video](https://hpe-my.sharepoint.com/:v:/r/personal/isabelle_steinhauser_hpe_com/Documents/PCAI%20Demo%20Videos/NL2SQL%20MCP.mp4?csf=1&web=1&e=iHew87)

## **Tools and frameworks used:**

* Presto
* HPE MLIS
* Open-WebUI
* MCP
* Superset

## Requirements

The minimum OpenWebUI version needed is v0.6.31, which supports the MCP server as an external tool. In the installation steps it's pointed out how to install an OpenWebUI version that is recent enough.

AIE version with MCP feature (AIE 1.12 or greater) or manual install of EzPrestoMCP (not publicly available as of now, but possible to get for demo purposes, contact us for advice).


## Steps for installation

### **1. Data Source**

To connect a datasource to HPEs PCAI we first need to create one. 

**1.1 Deploy Database**

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

**1.2 Load db file**

If you care to adapt the data for example to a different industry or specific locations take a look at create_manufacturing_data.py script and adapt before executing. Execute the create_manufacturing_data.py script with python create_manufacturing_data.py . After execution you have a .db file you can use as for the following steps.

Open your Jupyter Notebook server and upload the .db file and the loaddata.py. You will need to add the password you provided when Importing Postgres to line 9. of loaddata.py Also be aware if you adapt the db_name in line 13 you will need to change in the step of Connect Database to AIE in the URL manufacturing to whatever you decide for as a dbname.

When you have uploaded those to files open a terminal in the JupyterNotebook Server. Execute ls to make sure you can see both files at this location. Install the library psycopg2 via pip installpsycopg2 and then execute the loaddata.py with python loaddata.py

**1.3 Connect Database to AIE**

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

**1.4 Explore the Data Catalog**

Once the Connection is made you can explore the available data via the Data Catalog. 

- Navigate to **Data Engineering > Data Catalog**
- Select **manufacturingdb**
- Tick **public** schema
- See three Tables, machine_metrics, machines and operators. Select one 
- Click **Data Preview** to get an overview of the data.

### **2. Model Deployment**
Deploy **Qwen/Qwen3-8B-Instruct** :  This model has been deployed as it has the capabilities of **chat**, and good experience on working with the Presto MCP feature, especially compared with LLama 3.1 8B Instruct. 

One is however independent to choose models of their preference and deploy for usage.

At the end copy the Model Endpoint and the API tokens to a text file as we will need them in next steps.

**2.1 Create a Model Package on HPE MLIS.**

Navigate to Tools & Frameworks > HPE MLIS (in earlier version Tab Data Science)

Add a Packaged Model with:
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

**2.2 Deloy the model with HPE MLIS.**
- Deploy the Model with Auto scaling template **fixed-1**

**2.3 Create a token**

Once deployed you will need to create a token. For AIE 1.9 and greater you need to head to AIE -> Gen AI -> Model Endpoints -> 3 dots at Action -> Generate API Token.
For earlier AIE versions you need to create the API Token within MLIS.

### **3. Chat Interface**
**3.1 Download the Open-WebUI helm-chart and the Open-WebUI logo**

If you already have deployed Open WebUI in the environment for a different use case, you can just reuse that.

[open-webui-8.12.2-pcai.tgz](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/open-webui-8.12.2-pcai.tgz)

[open-webui-logo.png](https://github.com/ai-solution-eng/frameworks/blob/main/open-webui/logo.png)

**3.2 Import the framework into AIE stack on PCAI system.**

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

**3.3 Open-WebUI Settings.**

We need to connect Open WebUI to our model deployed earlier in MLIS.

- Open OpenWebUI

- Navigate to **Admin Panel >> Settings >> Connections**
![OpenWebUI Navigate to AdminPanel](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_AdminPanel.png) ![OpenWebUI Navigate to Settings](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_Settings.png) ![OpenWebUI Navigate to Connections](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_Connections.png)
![OpenWebUI Add Connection to Open WebUI](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_AddConnection.png)
- Fill in the details below (Remember you saved the endpoint url and api token in the previous step):

  URL : Use model endpoint link from MLIS or Gen AI-> Model Endpoints when you are on AIE 1.9 or newer and add /v1 to the end.
  
  Auth : Add the API Token generated from the model deployment and fill it in the API Key.
![OpenWebUI Add PCAI Model to Open WebUI](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_AddModel.png)
- Save


Now we need to as well add the MCP Server Connection. Therefore navigate in Open WebUI to **Admin Panel >> Settings >> External Tools**
![OpenWebUI Add Presto MCP Server to Open WebUI](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_ExternalTools.png)

- Add a new Tool Server
- Change the type to MCP Streamable HTTP 
- Add the URL of your ezPresto MCP Server (if you are not aware what this is and where to find it you might have an AIE version where MCP is not an official feature yet. For demo and non production use you can import it as custom framework still. If you are interested in that reach out to me.)
- Add the JWT token of the user you want to have the Presto Connections of. The AI gets access to all datasources this user has access to.
- Provide the ID and Name PrestoMCP
![OpenWebUI Add Presto MCP Server to Open WebUI](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/OpenWebUI_PrestoMCP.png)

As next step we need to Create a Model that is leveraging the Qwen3 8b Base Model and has the MCP Server as Tool available. Therefore click in OpenWebUI on Workspace. Click **New Model**. Provide your Model a Name for example Manufacturing select Qwen/Qwen3-8B as base Model. Edit Visibility to Public in case you want the model to be available for everyone to chat. Add a System Prompt for example: "Always use 'manufacturingdb' catalog and the schema 'public' for SQL queries. Syntax: catalog.schema.table is how you reference a table in presto"

Click on the Advance Params and set Function Calling to Native, otherwise it will only make one call. You can edit this within your chat as well.
Underneath Tools tick the PrestoMCP Tool and click Save&Create.


### **4. Dashboard**

**4.1 Superset Installation**

Install Superset if not yet available in your cluster. Therefore go to Administration > Tools & Frameworks. Locate Superset, select the 3 Dots and click Install.
![Install Superset](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/DeploySuperset.png)

**4.2 Superset Presto Connection**

Open Superset ( BI Reporting ).
Within Superset we first need to configure the connection to Presto. 
Under Settings select Database Connections. 
Add a new Database by clicking + Database on the top right.
Select Presto.
Provide the following SQL Alchemy URL: presto://ezpresto.YOURDOMAINNAME:443/cache where you need to insert the domain name of the cluster you are working on. You can take that from the URL in your brwoser, eg home.YOURDOMAINNAME

**4.3 Superset Dataset Creation**

Now that we have connected Presto we need to create datasets in order to build charts and Dashboards out of them. 

Therefore you will need to create Cashed Assets within Presto. In order to do that go to Data Engineering -> Query editor within your AI Essentials. Paste the following query into the SQl Query Field:

```sql
SELECT operators.location , "sum"(machine_metrics.units_produced) total_units , "sum"(machine_metrics.defect_rate) total_defects FROM (manufacturingdb.public.operators INNER JOIN manufacturingdb.public.machine_metrics ON (operators.machine_id = machine_metrics.machine_id)) GROUP BY operators.location
```

![Presto Query](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/Presto_Query.png)

Save it as cached asset by going to Actions -> Save as View.

Name this one location_performance and use the default schema.

![Presto Cached Asset](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/Presto_CachedAsset.png)
This will only work if you used the created script to create the data as well as if you imported it as manufacturingdb into datasources, if you have changed anything there you will need to replicate the changes here as well.

If you name the Cached Assets differently you will need to incorporate the changes within Superset as well.

Please create these two additional cached assets:

**shiftperformance** with the default schema and the query
```sql
SELECT o.shift , "sum"(m.units_produced) total_units , "sum"((m.defect_rate * m.units_produced)) total_defects FROM (manufacturingdb.public.operators o INNER JOIN manufacturingdb.public.machine_metrics m ON (o.machine_id = m.machine_id)) GROUP BY o.shift LIMIT 1000
```

**operatorperformance** with the default schema and the query
```sql
WITH operator_performance AS ( SELECT m.location , o.name operator_name , "sum"(mm.units_produced) total_units , "sum"((mm.units_produced * (mm.defect_rate / DECIMAL '100.0'))) total_defects FROM ((manufacturingdb.public.operators o INNER JOIN manufacturingdb.public.machines m ON (o.machine_id = m.machine_id)) INNER JOIN manufacturingdb.public.machine_metrics mm ON (m.machine_id = mm.machine_id)) GROUP BY m.location, o.name ) SELECT location , operator_name , total_units , total_defects , "rank"() OVER (PARTITION BY location ORDER BY total_units DESC, total_defects ASC) rank FROM operator_performance ORDER BY location ASC, rank ASC
```

**4.4 Superset Import Dahsboard**

To import the Dashboard open Superset. Naviagte to Dashboards and click import Dashboard. Use the .zip file in this folder to upload. 

This will create the dashboard, the charts and the datasets for you. Again only if you created the Cached Assets with the same names as well as the Database connection to Presto. If you named something differently you will need to edit it accordingly.
![Import Dashboard in Superset](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/Superset_ImportDashboard.png)

The final Dashboard should look similar to this:

![Final Dashboard in Superset](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/Superset_FinalDashboard.png)

In order to change the color of the Dashboard you can click on Edit Dashboard then Edit Properties. Play around with different Color Schemes.

![Change Color Dashboard in Superset](https://github.com/ai-solution-eng/ai-solution-demos/blob/nl2sql/nl-to-sql-mcp-manufacturing/images/Superset_EditColor.png)

## Production-ready considerations

When thinking about moving this into production you should make sure to deploy open-webui with an external postgres with large storage size. The default storage for open-webui is a embedded sqlite with 2GB storage, especially with several users and longer chat history we've seen this becoming a limitation.

When adding the MCP server to OpenWebUI it auhtorizes with a token. This token is user specific. If you want to cover different data access for different groups/users, you will need to manage the different data access in AIE, retrieve the JWT token of users with that data access and configure several PrestoMCP servers within OpenWebUI granting access for those dfferent groups.