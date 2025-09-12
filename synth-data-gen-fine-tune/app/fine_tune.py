import os
import json
import time
import threading
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from trl import SFTTrainer, SFTConfig

BASE_MODEL_ID = "microsoft/Phi-3-mini-4k-instruct"
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
FINETUNE_ROOT = os.environ.get("FINETUNE_ROOT", "/app/models/finetunes")
os.makedirs(FINETUNE_ROOT, exist_ok=True)

_JOBS: Dict[str, "FineTuneJob"] = {}

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())

def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)

def _detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    # MPS for Apple Silicon
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"

def _can_use_4bit() -> bool:
    # 4-bit (bitsandbytes) only supported with CUDA
    if not torch.cuda.is_available():
        return False
    try:
        import bitsandbytes as _  # noqa: F401
        return True
    except Exception:
        return False

@dataclass
class FineTuneJob:
    job_id: str
    files: List[str]
    base_model_id: str = BASE_MODEL_ID
    output_dir: str = field(default_factory=str)
    status: str = "queued"
    progress: str = ""
    error: Optional[str] = None
    adapter_dir: Optional[str] = None

    # Conservative defaults for Mac/CPU
    num_epochs: int = 1
    lr: float = 2e-4
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 2
    max_seq_len: int = 512  # shorter seq helps on MPS/CPU

    # CUDA-friendly defaults (applied automatically in __post_init__)
    cuda_num_epochs: int = 3
    cuda_lr: float = 2e-4
    cuda_per_device_train_batch_size: int = 4
    cuda_gradient_accumulation_steps: int = 1
    cuda_max_seq_len: int = 1024

    def __post_init__(self):
        # If running on CUDA and the user didn't override the conservative defaults,
        # switch to CUDA-optimized defaults.
        try:
            device = _detect_device()
            if device == "cuda":
                if self.num_epochs == 1:
                    self.num_epochs = self.cuda_num_epochs
                if self.lr == 2e-4:
                    self.lr = self.cuda_lr
                if self.per_device_train_batch_size == 1:
                    self.per_device_train_batch_size = self.cuda_per_device_train_batch_size
                if self.gradient_accumulation_steps == 2:
                    self.gradient_accumulation_steps = self.cuda_gradient_accumulation_steps
                if self.max_seq_len == 512:
                    self.max_seq_len = self.cuda_max_seq_len
        except Exception:
            pass

    def run(self):
        try:
            self.status = "running"
            self.progress = "Preparing dataset…"

            # 1) Load & merge JSONL -> {"text": "..."}
            rows = []
            for f in self.files:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        instr = str(obj.get("instruction", "")).strip()
                        out = str(obj.get("output", "")).strip()
                        if instr and out:
                            rows.append({"text": self._format_example(instr, out)})

            if not rows:
                raise RuntimeError("No usable rows found in provided dataset files.")

            ds = Dataset.from_list(rows)

            # 2) Device/precision selection
            device = _detect_device()
            use_4bit = _can_use_4bit()  # only true on CUDA + bnb
            self.progress = f"Loading tokenizer and base model on {device}…"

            bnb_config = None
            if use_4bit:
                bnb_config = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_use_double_quant=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype=torch.bfloat16,
                )

            tokenizer = AutoTokenizer.from_pretrained(self.base_model_id, use_fast=True)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            tokenizer.padding_side = "right"

            # dtype logic: fp16 on cuda/mps, fp32 on cpu
            if device == "cuda":
                torch_dtype = torch.bfloat16  # or float16; bfloat16 often stable
            elif device == "mps":
                torch_dtype = torch.float16
            else:
                torch_dtype = torch.float32

            model = AutoModelForCausalLM.from_pretrained(
                self.base_model_id,
                device_map="auto" if device in ("cuda", "mps") else None,
                torch_dtype=torch_dtype,
                quantization_config=bnb_config,  # None on non-CUDA
            )
            if device == "mps":
                model.to("mps")

            # IMPORTANT for checkpointing/LoRA on MPS/CPU:
            if hasattr(model, "config"):
                model.config.use_cache = False    # avoid incompatible cache with checkpointing
            # Ensure inputs require grad so the graph is created
            try:
                model.enable_input_require_grads()
            except Exception:
                pass
            
            # 3) LoRA adapters
            self.progress = "Attaching LoRA adapters…"
            # Use a larger LoRA for CUDA (more capacity)
            if device == "cuda":
                lora_config = LoraConfig(
                    r=16,                # larger rank on GPU
                    lora_alpha=32,       # scaled alpha
                    target_modules=[
                        "q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"
                    ],
                    lora_dropout=0.1,    # slightly higher dropout for regularization
                    bias="none",
                    task_type="CAUSAL_LM"
                )
            else:
                lora_config = LoraConfig(
                    r=8,  # conservative for Mac/CPU
                    lora_alpha=16,
                    target_modules=[
                        "q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"
                    ],
                    lora_dropout=0.05,
                    bias="none",
                    task_type="CAUSAL_LM"
                )
            model = get_peft_model(model, lora_config)
            # Optional: enables memory savings (slower), helpful on low VRAM/CPU
            try:
                model.gradient_checkpointing_enable()
            except Exception:
                pass

            # 4) SFT training
            self.progress = "Starting SFT training…"
            time_tag = _ts()
            run_name = f"{time_tag}_{_sanitize(self.base_model_id)}"
            self.output_dir = os.path.join(FINETUNE_ROOT, run_name)
            adapter_dir = os.path.join(self.output_dir, "adapter")
            os.makedirs(adapter_dir, exist_ok=True)

            # Mixed precision flags only where supported
            # Ensure at most one of bf16 or fp16 is True to avoid conflicts
            if device == "cuda":
                # Prefer bf16 on CUDA where supported
                bf16_flag = True
                fp16_flag = False
            elif device == "mps":
                # Use fp16 on Apple MPS
                bf16_flag = False
                fp16_flag = True
            else:
                # CPU: no mixed precision
                bf16_flag = False
                fp16_flag = False

            training_cfg = SFTConfig(
                output_dir=self.output_dir,
                num_train_epochs=self.num_epochs,
                learning_rate=self.lr,
                per_device_train_batch_size=self.per_device_train_batch_size,
                gradient_accumulation_steps=self.gradient_accumulation_steps,
                max_seq_length=self.max_seq_len,
                bf16=bf16_flag,
                fp16=fp16_flag,
                logging_steps=1,
                save_steps=0,
                save_total_limit=1,
                report_to=[],
            )

            trainer = SFTTrainer(
                model=model,
                tokenizer=tokenizer,
                train_dataset=ds,
                dataset_text_field="text",
                args=training_cfg,
            )
            trainer.train()

            # 5) Save LoRA adapter (+ tokenizer)
            self.progress = "Saving LoRA adapter…"
            model.save_pretrained(adapter_dir)
            tokenizer.save_pretrained(self.output_dir)
            export_root = os.environ.get("ADAPTER_EXPORT_DIR", "/app/adapters")
            os.makedirs(export_root, exist_ok=True)
            user_name = os.environ.get("DEFAULT_ADAPTER_NAME")  # or pass via job args from UI
            stable_name = _sanitize(user_name) if user_name else _sanitize(os.path.basename(self.output_dir))
            export_path = os.path.join(export_root, stable_name)
            # replace if exists
            if os.path.exists(export_path):
                shutil.rmtree(export_path)
            shutil.copytree(adapter_dir, export_path)
            self.progress = f"Adapter exported to {export_path}"
            self.adapter_dir = adapter_dir
            self.status = "done"
            self.progress = "Training complete."
        except Exception as e:
            self.status = "error"
            self.error = str(e)

    @staticmethod
    def _format_example(instruction: str, output: str) -> str:
        return f"### Instruction:\n{instruction}\n\n### Response:\n{output}\n"


# ---- Public API used by Flask ----

def start_finetune(files: List[str], base_model_id: Optional[str] = None, **train_kwargs) -> str:
    job_id = f"job-{_ts()}"
    job = FineTuneJob(
        job_id=job_id,
        files=files,
        base_model_id=base_model_id or BASE_MODEL_ID,
        **train_kwargs
    )
    _JOBS[job_id] = job
    t = threading.Thread(target=job.run, daemon=True)
    t.start()
    return job_id

def get_finetune_status(job_id: str) -> Dict[str, Optional[str]]:
    job = _JOBS.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {
        "status": job.status,
        "progress": job.progress,
        "error": job.error,
        "adapter_dir": job.adapter_dir,
        "output_dir": job.output_dir,
        "base_model_id": job.base_model_id,
    }
