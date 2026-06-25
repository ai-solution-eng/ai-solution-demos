"""
Exact inference code from the official biomed-multi-alignment repo.

Each function is copied verbatim from the corresponding example module
so the deployed model produces identical results to running IBM's code.
"""

from typing import Any

import torch
import numpy as np

from mammal.keys import (
    ENCODER_INPUTS_STR,
    ENCODER_INPUTS_TOKENS,
    ENCODER_INPUTS_ATTENTION_MASK,
    ENCODER_INPUTS_SCALARS,
    LABELS_STR,
    LABELS_TOKENS,
    LABELS_ATTENTION_MASK,
    LABELS_SCALARS,
    LABELS_SCALARS_VALUES,
    LABELS_SCALARS_VALID_MASK,
    DECODER_INPUTS_STR,
    DECODER_INPUTS_TOKENS,
    DECODER_INPUTS_ATTENTION_MASK,
    CLS_PRED,
    SCORES,
    SCALARS_PREDICTION_HEAD_LOGITS,
)
from fuse.data.tokenizers.modular_tokenizer.op import ModularTokenizerOp


# ── Drug-Target Binding (DTI) ──────────────────────────────────────────
# Source: mammal/examples/dti_bindingdb_kd/task.py (exact static methods)

def dti_data_preprocessing(
    sample_dict: dict,
    *,
    target_sequence_key: str,
    drug_sequence_key: str,
    ground_truth_key: int | None = None,
    target_max_seq_length: int = 1250,
    drug_max_seq_length: int = 256,
    encoder_input_max_seq_len: int = 1512,
    tokenizer_op: ModularTokenizerOp,
    norm_y_mean: float = 0.0,
    norm_y_std: float = 1.0,
    device: str | torch.device = "cpu",
) -> dict:
    target_sequence = sample_dict[target_sequence_key]
    drug_sequence = sample_dict[drug_sequence_key]
    sample_dict[ENCODER_INPUTS_STR] = (
        "<@TOKENIZER-TYPE=AA><MASK>"
        f"<@TOKENIZER-TYPE=AA@MAX-LEN={target_max_seq_length}><MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN><SEQUENCE_NATURAL_START>{target_sequence}<SEQUENCE_NATURAL_END>"
        f"<@TOKENIZER-TYPE=SMILES@MAX-LEN={drug_max_seq_length}><MOLECULAR_ENTITY><MOLECULAR_ENTITY_SMALL_MOLECULE><SEQUENCE_NATURAL_START>{drug_sequence}<SEQUENCE_NATURAL_END>"
        "<EOS>"
    )
    tokenizer_op(
        sample_dict,
        key_in=ENCODER_INPUTS_STR,
        key_out_tokens_ids=ENCODER_INPUTS_TOKENS,
        key_out_attention_mask=ENCODER_INPUTS_ATTENTION_MASK,
        max_seq_len=encoder_input_max_seq_len,
        key_out_scalars=ENCODER_INPUTS_SCALARS,
    )
    sample_dict[ENCODER_INPUTS_TOKENS] = torch.tensor(
        sample_dict[ENCODER_INPUTS_TOKENS], device=device
    )
    sample_dict[ENCODER_INPUTS_ATTENTION_MASK] = torch.tensor(
        sample_dict[ENCODER_INPUTS_ATTENTION_MASK], device=device
    )
    return sample_dict


def dti_process_model_output(
    batch_dict: dict,
    *,
    scalars_preds_key: str = SCALARS_PREDICTION_HEAD_LOGITS,
    scalars_preds_processed_key: str = "model.out.dti_bindingdb_kd",
    norm_y_mean: float,
    norm_y_std: float,
) -> dict:
    scalars_preds = batch_dict[scalars_preds_key]
    batch_dict[scalars_preds_processed_key] = (
        scalars_preds[:, 0] * norm_y_std + norm_y_mean
    )
    return batch_dict


# ── Protein Solubility ─────────────────────────────────────────────────
# Source: mammal/examples/protein_solubility/task.py (exact static methods)

def solubility_data_preprocessing(
    sample_dict: dict,
    *,
    protein_sequence_key: str,
    tokenizer_op: ModularTokenizerOp,
    solubility_label_key: int | None = None,
    protein_max_seq_length: int = 1250,
    encoder_input_max_seq_len: int | None = 1260,
    labels_max_seq_len: int | None = 4,
    device: str | torch.device = "cpu",
) -> dict:
    protein_sequence = sample_dict[protein_sequence_key]
    sample_dict[ENCODER_INPUTS_STR] = (
        f"<@TOKENIZER-TYPE=AA><MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN><SOLUBILITY><SENTINEL_ID_0><@TOKENIZER-TYPE=AA@MAX-LEN={protein_max_seq_length}><SEQUENCE_NATURAL_START>{protein_sequence}<SEQUENCE_NATURAL_END><EOS>"
    )
    tokenizer_op(
        sample_dict=sample_dict,
        key_in=ENCODER_INPUTS_STR,
        key_out_tokens_ids=ENCODER_INPUTS_TOKENS,
        key_out_attention_mask=ENCODER_INPUTS_ATTENTION_MASK,
        max_seq_len=encoder_input_max_seq_len,
    )
    sample_dict[ENCODER_INPUTS_TOKENS] = torch.tensor(
        sample_dict[ENCODER_INPUTS_TOKENS], device=device
    )
    sample_dict[ENCODER_INPUTS_ATTENTION_MASK] = torch.tensor(
        sample_dict[ENCODER_INPUTS_ATTENTION_MASK], device=device
    )
    return sample_dict


def solubility_process_model_output(
    tokenizer_op: ModularTokenizerOp,
    decoder_output: np.ndarray,
    decoder_output_scores: np.ndarray | None,
) -> dict:
    negative_token_id = tokenizer_op.get_token_id("<0>")
    positive_token_id = tokenizer_op.get_token_id("<1>")
    label_id_to_int = {
        negative_token_id: 0,
        positive_token_id: 1,
    }
    classification_position = 1

    if decoder_output_scores is not None:
        not_normalized_score = decoder_output_scores[
            classification_position, positive_token_id
        ]
        normalized_score = not_normalized_score / (
            not_normalized_score
            + decoder_output_scores[classification_position, negative_token_id]
            + 1e-10
        )

    ans = dict(
        pred=label_id_to_int.get(
            int(decoder_output[classification_position]), -1
        ),
        normalized_score=float(normalized_score),
        raw_positive_score=float(not_normalized_score),
    )
    return ans


# ── TCR-Epitope Binding ────────────────────────────────────────────────
# Source: mammal/examples/tcr_epitope_binding/main_infer.py (exact functions)

def tcr_process_model_output(
    tokenizer_op: ModularTokenizerOp,
    decoder_output: np.ndarray,
    decoder_output_scores: np.ndarray | None,
) -> dict:
    negative_token_id = tokenizer_op.get_token_id("<0>")
    positive_token_id = tokenizer_op.get_token_id("<1>")
    label_id_to_int = {
        negative_token_id: 0,
        positive_token_id: 1,
    }
    classification_position = 1

    score = None
    if decoder_output_scores is not None:
        score = float(
            decoder_output_scores[classification_position, positive_token_id]
        )

    ans = dict(
        pred=label_id_to_int.get(
            int(decoder_output[classification_position]), -1
        ),
        score=score,
    )
    return ans


def tcr_build_prompt(
    tcr_beta_seq: str,
    epitope_seq: str,
    treat_as_general_proteins: bool = False,
) -> str:
    if treat_as_general_proteins:
        return (
            f"<@TOKENIZER-TYPE=AA><BINDING_AFFINITY_CLASS><SENTINEL_ID_0>"
            f"<@TOKENIZER-TYPE=AA><MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN>"
            f"<SEQUENCE_NATURAL_START>{tcr_beta_seq}<SEQUENCE_NATURAL_END>"
            f"<@TOKENIZER-TYPE=AA><MOLECULAR_ENTITY><MOLECULAR_ENTITY_GENERAL_PROTEIN>"
            f"<SEQUENCE_NATURAL_START>{epitope_seq}<SEQUENCE_NATURAL_END><EOS>"
        )
    else:
        return (
            f"<@TOKENIZER-TYPE=AA><BINDING_AFFINITY_CLASS><SENTINEL_ID_0>"
            f"<@TOKENIZER-TYPE=AA><MOLECULAR_ENTITY><MOLECULAR_ENTITY_TCR_BETA_VDJ>"
            f"<SEQUENCE_NATURAL_START>{tcr_beta_seq}<SEQUENCE_NATURAL_END>"
            f"<@TOKENIZER-TYPE=AA><MOLECULAR_ENTITY><MOLECULAR_ENTITY_EPITOPE>"
            f"<SEQUENCE_NATURAL_START>{epitope_seq}<SEQUENCE_NATURAL_END><EOS>"
        )
