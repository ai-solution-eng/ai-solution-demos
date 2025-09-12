# High-Level Architecture

```mermaid
flowchart LR
    subgraph User/UI
        U[Web Browser<br/>Single-page UI]
    end

    subgraph FlaskApp[Flask Application]
        A1[/Generate Synthetic Data Endpoint<br/>/generate_data/]
        A2[/List Datasets<br/>/list_datasets/]
        A3[/Start Fine-Tune<br/>/finetune/]
        A4[/Status Poll<br/>/finetune_status/]
        A5[/List Exported<br/>/list_exported_adapters/]
        A6[/Hot/Unload Adapter<br/>/hotload_adapter and /unload_adapter/]
        A7[/Compare Outputs<br/>/compare/]
    end

    subgraph DataGen[Synthetic Data Module]
        SD[(generate_synthetic_data<br/>OpenAI-compatible /v1/chat/completions)]
    end

    subgraph FT[Fine-Tune Module]
        FT1[(Dataset Loader<br/>JSONL -> text samples)]
        FT2[(Tokenizer + Base Model<br/>Phi-3 Mini)]
        FT3[(LoRA Adapter Training<br/>trl + peft)]
        FT4[(Adapter Export)]
    end

    subgraph Storage[Persistent Volumes]
        D1[(Datasets JSONL<br/>/app/data)]
        D2[(Fine-tune Runs<br/>/app/models/finetunes)]
        D3[(Exported Adapters<br/>/app/adapters)]
        C[(HF Cache<br/>/app/hf_cache)]
    end

    subgraph vLLM[vLLM Runtime External]
        VM1[/load_lora_adapter/]
        VM2[/unload_lora_adapter/]
        VM3[/chat completions/]
    end

    U -->|HTTP| A1
    U -->|HTTP| A2
    U -->|HTTP| A3
    U -->|HTTP| A4
    U -->|HTTP| A5
    U -->|HTTP| A6
    U -->|HTTP| A7

    A1 --> SD -->|Writes| D1
    A2 --> D1

    A3 --> FT1 --> FT2 --> FT3 --> FT4 --> D2
    FT4 -->|Copy| D3
    A5 --> D3

    A6 -->|Invoke load/unload| VM1
    A6 --> VM2

    A7 -->|Prompt Base| VM3
    A7 -->|Prompt FT LoRA active| VM3

    VM1 -->|Reads Adapter PVC mounted| D3
    VM3 -->|Model Inference| vLLM

    classDef store fill:#f6faff,stroke:#2f6bff,color:#0b2e59
    classDef ext fill:#fff7e6,stroke:#ff9a2f,color:#663c00
    class D1,D2,D3,C store;
    class VM1,VM2,VM3 ext;
```

## Description

1. The browser UI interacts only with the Flask REST endpoints.
2. Synthetic data generation calls an external OpenAI-compatible model endpoint and persists JSONL rows.
3. Fine-tuning runs in a background thread: loads datasets, tokenizes, applies LoRA via `peft` + `trl`.
4. Trained adapter artifacts are exported to a shared directory intended to be volume-mounted into a vLLM deployment.
5. User triggers hot-load/unload to dynamically attach/detach adapters in vLLM.
6. Comparison sends identical formatted prompts to base and fine-tuned (adapter-loaded) model endpoints.
