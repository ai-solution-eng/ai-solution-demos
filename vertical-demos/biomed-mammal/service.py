"""
BiomedMammalService — BentoML inference for ibm-research/biomed.omics.bl.sm.ma-ted-458m

CPU by default (DEVICE=cpu). Set DEVICE=cuda for GPU (H100, etc.).
Each task's model loads lazily on first request and stays cached.
All inference code paths mirror the official biomed-multi-alignment repo.
"""
import os
import logging
from typing import Any

import torch
import bentoml
from pydantic import BaseModel

from mammal.model import Mammal
from mammal.keys import (
    ENCODER_INPUTS_STR,
    ENCODER_INPUTS_TOKENS,
    ENCODER_INPUTS_ATTENTION_MASK,
    CLS_PRED,
    SCORES,
)
from huggingface_hub import snapshot_download
from fuse.data.tokenizers.modular_tokenizer.op import ModularTokenizerOp

from task_helpers import (
    dti_data_preprocessing,
    dti_process_model_output,
    solubility_data_preprocessing,
    solubility_process_model_output,
    tcr_build_prompt,
    tcr_process_model_output,
)

logger = logging.getLogger(__name__)
DEVICE = os.environ.get("DEVICE", "cpu")
HF_TOKEN = os.environ.get("HF_TOKEN", None)

TASKS = {
    "protein_protein_interaction": "ibm/biomed.omics.bl.sm.ma-ted-458m",
    "drug_target_binding": "ibm/biomed.omics.bl.sm.ma-ted-458m.dti_bindingdb_pkd",
    "protein_solubility": "ibm/biomed.omics.bl.sm.ma-ted-458m.protein_solubility",
    "tcr_epitope_binding": "ibm-research/biomed.omics.bl.sm.ma-ted-458m.tcr_epitope_bind",
}


class TaskInput(BaseModel):
    task: str
    inputs: dict[str, Any]


@bentoml.service(
    resources={"cpu": "4", "memory": "8Gi"},
    traffic={"timeout": 600},
)
class BiomedMammalService:
    def __init__(self):
        self.device = torch.device(DEVICE)
        logger.info("BiomedMammalService initializing — device=%s", self.device)
        self._models = {}

        # Pre-cache all model files at startup so first inference is instant.
        for model_id in set(TASKS.values()):
            logger.info("Pre-caching %s ...", model_id)
            snapshot_download(model_id, token=HF_TOKEN)
            logger.info("Cached %s", model_id)

    def _load(self, task: str):
        if task not in self._models:
            model_id = TASKS[task]
            logger.info("Loading %s for task '%s'", model_id, task)
            model = Mammal.from_pretrained(
                model_id, allow_config_mismatch=True
            )
            model.eval()
            model.to(self.device)
            tokenizer = ModularTokenizerOp.from_pretrained(model_id)
            self._models[task] = (model, tokenizer)
            logger.info("Loaded %s", model_id)
        return self._models[task]

    @bentoml.api(input_spec=TaskInput)
    def predict(self, task: str, inputs: dict[str, Any]) -> dict:
        if task not in TASKS:
            return {"error": f"Unknown task '{task}'. Available: {list(TASKS.keys())}"}
        model, tokenizer = self._load(task)
        handler = getattr(self, f"_{task}")
        return handler(model, tokenizer, inputs)

    # ── Protein-Protein Interaction (zero-shot on base model) ──────────
    def _protein_protein_interaction(self, model, tokenizer, inputs):
        seq_a = inputs.get("sequence_a")
        seq_b = inputs.get("sequence_b")
        if not seq_a or not seq_b:
            raise ValueError("'sequence_a' and 'sequence_b' are required")

        sample = {}
        sample[ENCODER_INPUTS_STR] = (
            "<@TOKENIZER-TYPE=AA><BINDING_AFFINITY_CLASS><SENTINEL_ID_0>"
            "<MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN>"
            f"<SEQUENCE_NATURAL_START>{seq_a}<SEQUENCE_NATURAL_END>"
            "<MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN>"
            f"<SEQUENCE_NATURAL_START>{seq_b}<SEQUENCE_NATURAL_END><EOS>"
        )
        tokenizer(
            sample,
            key_in=ENCODER_INPUTS_STR,
            key_out_tokens_ids=ENCODER_INPUTS_TOKENS,
            key_out_attention_mask=ENCODER_INPUTS_ATTENTION_MASK,
        )
        sample[ENCODER_INPUTS_TOKENS] = torch.tensor(
            sample[ENCODER_INPUTS_TOKENS], device=self.device
        )
        sample[ENCODER_INPUTS_ATTENTION_MASK] = torch.tensor(
            sample[ENCODER_INPUTS_ATTENTION_MASK], device=self.device
        )

        batch = model.generate(
            [sample],
            output_scores=True,
            return_dict_in_generate=True,
            max_new_tokens=5,
        )
        cls_pred = batch[CLS_PRED][0]
        scores = batch[SCORES][0] if batch[SCORES] is not None else None

        pos_id = tokenizer.get_token_id("<1>")
        prediction = 1 if int(cls_pred[1]) == pos_id else 0
        confidence = float(scores[1, int(cls_pred[1])]) if scores is not None else None
        return {"prediction": prediction, "confidence": confidence}

    # ── Drug-Target Binding (finetuned, encoder-only scalar regression) ─
    def _drug_target_binding(self, model, tokenizer, inputs):
        target_seq = inputs.get("target_sequence")
        drug_smiles = inputs.get("drug_smiles")
        if not target_seq or not drug_smiles:
            raise ValueError("'target_sequence' and 'drug_smiles' are required")

        sample = dti_data_preprocessing(
            {"target_seq": target_seq, "drug_seq": drug_smiles},
            tokenizer_op=tokenizer,
            target_sequence_key="target_seq",
            drug_sequence_key="drug_seq",
            norm_y_mean=0.0,
            norm_y_std=1.0,
            device=self.device,
        )
        batch = model.forward_encoder_only([sample])
        dti_process_model_output(
            batch,
            norm_y_mean=5.79384684128215,
            norm_y_std=1.33808027428196,
        )
        return {"pKd": float(batch["model.out.dti_bindingdb_kd"][0])}

    # ── Protein Solubility (finetuned, generation classification) ─────
    def _protein_solubility(self, model, tokenizer, inputs):
        protein_seq = inputs.get("protein_sequence")
        if not protein_seq:
            raise ValueError("'protein_sequence' is required")

        sample = solubility_data_preprocessing(
            {"protein_seq": protein_seq},
            protein_sequence_key="protein_seq",
            tokenizer_op=tokenizer,
            device=self.device,
        )
        batch = model.generate(
            [sample],
            output_scores=True,
            return_dict_in_generate=True,
            max_new_tokens=5,
        )
        result = solubility_process_model_output(
            tokenizer_op=tokenizer,
            decoder_output=batch[CLS_PRED][0],
            decoder_output_scores=batch[SCORES][0],
        )
        return result

    # ── TCR-Epitope Binding (finetuned, generation classification) ────
    def _tcr_epitope_binding(self, model, tokenizer, inputs):
        tcr_beta = inputs.get("tcr_beta_sequence")
        epitope = inputs.get("epitope_sequence")
        if not tcr_beta or not epitope:
            raise ValueError("'tcr_beta_sequence' and 'epitope_sequence' are required")

        sample = {}
        sample[ENCODER_INPUTS_STR] = tcr_build_prompt(tcr_beta, epitope)
        tokenizer(
            sample,
            key_in=ENCODER_INPUTS_STR,
            key_out_tokens_ids=ENCODER_INPUTS_TOKENS,
            key_out_attention_mask=ENCODER_INPUTS_ATTENTION_MASK,
        )
        sample[ENCODER_INPUTS_TOKENS] = torch.tensor(
            sample[ENCODER_INPUTS_TOKENS], device=self.device
        )
        sample[ENCODER_INPUTS_ATTENTION_MASK] = torch.tensor(
            sample[ENCODER_INPUTS_ATTENTION_MASK], device=self.device
        )

        batch = model.generate(
            [sample],
            output_scores=True,
            return_dict_in_generate=True,
            max_new_tokens=5,
        )
        result = tcr_process_model_output(
            tokenizer_op=tokenizer,
            decoder_output=batch[CLS_PRED][0],
            decoder_output_scores=batch[SCORES][0],
        )
        return result
