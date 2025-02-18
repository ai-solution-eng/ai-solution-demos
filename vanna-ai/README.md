# Vanna AI
==========================

## Introduction

This Helm chart is designed to deploy a simple version of the Vanna AI application to HPE AI Essentials (Or other Kubernetes Clusters). The chart provides a flexible way to configure the application's settings and environment variables.

## What is Vanna.AI?
Vanna.AI is an open-source AI-powered tool that simplifies database interactions by converting natural language questions into accurate SQL queries. Its value lies in democratizing data access, allowing both technical and non-technical users to extract insights from databases without extensive SQL knowledge, while also improving efficiency for experienced data analysts. By leveraging large language models and retrieval-augmented generation techniques, Vanna enhances data exploration, enables non-technical teams, and accelerates the process of obtaining actionable insights from complex databases.

## Prerequisites

* Access to a HPE Private Cloud AI Cluster running AI Essentials (AIE)
* An LLM deployed on AIE using Machine Learning Inference Service with an OpenAI-compatible endpoint
  - Any model that has sql in its training set should work, but we suggest a Llama variant- 3B or 7B instruct or better.
* A token configured to access that endpoint
* A database you wish to connect to (it includes a sample sqlite Chinook DB)- currently supports SQLite and MS-SQL

## Configuration

The Vanna AI application requires several settings to be configured in the `values.yaml` file. These settings include:

### Settings

| Setting | Description | Required |
| --- | --- | --- |
| `Chatmodel` | The chat model to use | Yes |
| `Chatmodelbaseurl` | The base URL for the chat model | Yes |
| `Chatmodelkey` | The API key for the chat model | Yes |
| `Database` | The database connection string | Yes |
| `DatabasePath` | The path of where to persist the vector database | Yes |
| `DatabaseType` | The type of database | Yes |

## How it works
![](https://vanna.ai/docs/img/how-vanna-works.gif)

1. *User Input:* The user submits a natural language query.
1. *Embedding Generation:* Vanna.ai generates embeddings for the user's query.
1. *Context Retrieval:* The system performs a vector similarity search to find relevant context from the trained data.
1. *LLM Processing:* The query and relevant context are sent to a Large Language Model (LLM).
1. *SQL Generation:* The LLM generates an SQL query based on the provided information.
1. *Query Execution:* Vanna.ai executes the generated SQL query against the connected database.
1. *Result Formatting:* The system formats the query results, typically as a Pandas DataFrame or Plotly figure.
1. *Response Delivery:* Vanna.ai returns the results to the user, often including the SQL query, data, visualizations, and potential follow-up questions.

## Default Dataset

The default database included with the container image is the [Chinook](https://github.com/lerocha/chinook-database) database.
