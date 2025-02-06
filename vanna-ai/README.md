# Vanna AI
==========================

## Introduction

This Helm chart is designed to deploy a simple version of the Vanna AI application to HPE AI Essentials (Or other Kubernetes Clusters). The chart provides a flexible way to configure the application's settings and environment variables.

## Prerequisites

* Access to a HPE Private Cloud AI Cluster running AI Essentials (AIE)
* An LLM deployed on AIE using Machine Learning Inference Service with an OpenAI-compatible endpoint
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

