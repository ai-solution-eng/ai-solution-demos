# Finetune a tool calling LLM on PCAI using NVIDIA NeMo Microservices

## Why do we need to finetune LLMs for tool calling? 

Tool calling equips LLMs with the ability to interact with external applications, trigger program execution, and access up-to-date information beyond their static training data. With this capability, LLMs can interpret natural language queries, map them to the right APIs or functions, and automatically fill in parameters from user inputs. This forms the backbone of AI agents that can, for example, check stock availability, fetch weather updates, or orchestrate workflow steps.

[Demo](https://hpe-my.sharepoint.com/:v:/r/personal/daniel_cao_hpe_com/Documents/18_AISSE/31_PCAI/08%20-%20Demo/01_recorded_trials/pcai-finetune-tool-calling-llm-using-nemo-microservices.mp4?csf=1&web=1&nav=eyJyZWZlcnJhbEluZm8iOnsicmVmZXJyYWxBcHAiOiJPbmVEcml2ZUZvckJ1c2luZXNzIiwicmVmZXJyYWxBcHBQbGF0Zm9ybSI6IldlYiIsInJlZmVycmFsTW9kZSI6InZpZXciLCJyZWZlcnJhbFZpZXciOiJNeUZpbGVzTGlua0NvcHkifX0&e=eoDyti)

**Objectives:**
- Validate NVIDIA NeMo Microservices works on PCAI

Jupyter notebook sources: [Data flywheel tool calling repo by NVIDIA](https://github.com/NVIDIA/GenerativeAIExamples/tree/main/nemo/data-flywheel/tool-calling)

## config.py

We modify config.py to expose the right services endpoints exposed by PCAI and configure the relevant base model version.

```yaml
# (Required) NeMo Microservices URLs
NEMO_DATA_STORE_URL = "http://nemo-data-store.nemo.svc.cluster.local:3000" # Data Store

# (Required) NeMo Microservices — use dedicated endpoints 
# Customizer
NEMO_CUSTOMIZER_URL = "http://nemo-customizer.nemo.svc.cluster.local:8000"
# Entity Store
NEMO_ENTITY_STORE_URL = "http://nemo-entity-store.nemo.svc.cluster.local:8000"
# Evaluator
NEMO_EVALUATOR_URL = "http://nemo-evaluator.nemo.svc.cluster.local:7331"
# Guardrails
NEMO_GUARDRAILS_URL = "http://nemo-guardrails.nemo.svc.cluster.local:7331"

# NIM Gateway / Proxy
NIM_URL = "http://nemo-nim-proxy.nemo.svc.cluster.local:8000"
```

```yaml
# (Optional) Configure the base model. Must be one supported by the NeMo Customizer deployment!
BASE_MODEL = "meta/llama-3.2-1b-instruct"
BASE_MODEL_VERSION = "v1.0.0+L40"
```

## Packaged custom frameworks (packaged helm charts)

1. NeMo Microservices: 
- Contains Customizer, Evaluator, Guardrails, Data Store, Entity Store, NIM proxy, ...

NVIDIA Nemo Microservices is an API-first modular set of tools that you can use to customize, evaluate, and secure LLMs and embedding models.

**Customizer:** Facilitates the fine-tuning of LLMs and embedding models using full-supervised and parameter-efficient fine-tuning techniques/PEFT. 
**Evaluator:** Provides comprehensive evaluation capabilities for LLMs and embedding models, supporting academic benchmarks, custom automated evaluations, and LLM-as-a-Judge approaches. 
**Guardrails:** Adds safety checks and content moderation to LLM endpoints, protecting against hallucinations, harmful content, and security vulnerabilities.
**Data Store:** Serves as the default file storage solution, exposing APIs compatible with the Hugging Face Hub client.
**Entity Store:** Provides tools to manage and organize general entities such as namespaces, projects, datasets, and models.
**Deployment Management:** Provides an API to deploy NIM on a Kubernetes cluster and manage them through the NIM Operator microservice.
**NIM Proxy:** Provides a unified endpoint that you can use to access all deployed NIM for inference tasks.
**Operator:** Manages custom resource definitions (CRDs) for NeMo Customizer fine-tuning jobs
**DGX Cloud Admission Controller:** Enables multi-node training requirements for NeMo Customizer jobs through a mutating admission webhook.

- To be imported as a custom framework. Once installed, this serves as the back-end data flywheel allowing users to prepare and register datasets, finetune and evaluate models, add safety check, and deploy models using NIM.

2. OpenWebUI: Serves as the chat UI of this demo.

- Set up a connection to expose the finetuned models: "http://nemo-nim-proxy.nemo.svc.cluster.local:8000/v1"

- System message: 

```yaml
You are a tool selector. The input below contains two parts combined together:
1) A user query.
2) A list of available tools.

Your job:
- Analyze the query.
- Match it to one or more tools from the provided list.
- For each chosen tool, return the tool name and the arguments it needs.

Output format:
{
  [
    {
	"name": "tool_name",
	"arguments": { <arguments> }
	}
  ]
}

Rules:
- Only use tools from the provided list.
- Fill in arguments using values inferred from the query.
- Do not include unused or default arguments unless they are explicitly required.
- If multiple tools are needed, return them in order insidean array.
- If you think no tool matches, you can always fall back to return exactly: {[]}. You don't need to force to answer a tool that does not match the query.
- Do not include reasoning, explanations, or extra text.
- The output must be wrapped in a pretty-printed JSON with 2-space indentation.
```

- Seed: 42

- Example query:
```yaml
Query:
Future value of 2000 dollars at 2% for 8 years?
Tools:
[
  {
    "name": "calculate_standard_deviation",
    "description": "Calculates the standard deviation of a list of numbers.",
    "parameters": {
      "numbers": {
        "description": "The list of numbers.",
        "type": "List[Union[int, float]]"
      }
    }
  },
  {
    "name": "future_investment_value",
    "description": "Calculates the future value of an investment based on the present value, annual interest rate, and number of years.",
    "parameters": {
      "present_value": {
        "description": "The present value of the investment.",
        "type": "float"
      },
      "annual_interest_rate": {
        "description": "The annual interest rate as a decimal (e.g., 0.05 for 5%).",
        "type": "float"
      },
      "years": {
        "description": "The number of years the investment will grow.",
        "type": "int"
      }
    }
  }
]
```

- Ground truth answer for the query above:
```json
[
  {
    "name": "future_investment_value",
    "arguments": {
      "present_value": 2000,
      "annual_interest_rate": 0.02,
      "years": 8
    }
  }
]
```

**Note**: 
The Guardrails notebook uses the "nvidia/llama-3.1-nemoguard-8b-content-safety" model hosted on `build.nvidia.com`. Alternatively, we can use the same model or another model, ex. Qwen/Qwen2.5-7B-Instruct, self-hosted by HPE MLIS on PCAI.

