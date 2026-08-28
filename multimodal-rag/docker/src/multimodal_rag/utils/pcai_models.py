from .model_adapters import MultiModalEmbeddings
from .pcai_model_classes import (
    ChatModel,
    EmbeddingModel,
    RerankerModel,
    VoiceModel,
    input_modalities,
)
from .preprocessors.qwen3_vl_8b import (
    prepare_vllm_inputs as qwen3_vl_8b_template,
)

__all__ = [
    "cohere_transcribe_3_2b",
    "deepseek_v4_flash_280B",
    "fish_s2_pro_4B",
    "gemma4_31B",
    "glm_52_753B",
    "qwen3_tts_1_7B",
    "qwen3_vl_8B",
    "qwen3_vl_reranker_8B",
    "qwen36_27B",
    "whisper_large_v3_turbo",
]


vlm_modalities: tuple[input_modalities, input_modalities, input_modalities] = (
    "text",
    "image",
    "video",
)

# LLMs
gemma4_31B = ChatModel(
    model_name="RedHatAI/gemma-4-31B-it-FP8-block",
    url_remote=(
        "https://gemma-4-31b-ab.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODExOTY2MzQ5LCJpYXQiOjE3ODA0MzAzNDksImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiY2NhNDMzZmYtY2QwMC00NzExLTllOTctNTg2MTE3ZWRkOTQ1Iiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODA0MzAzNDkwNjYiLCJ1aWQiOiI3NmUxZTdmNC05MWE2LTQzMDgtOTA2NC05YzVkY2E5ZDA3ZTAifX0sIm5iZiI6MTc4MDQzMDM0OSwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4MDQzMDM0OTA2NiJ9.hi2bZBWnUEnuMJPv7ujwomDiY21AN1Xk1Dr_Bh5pLHaL1Xph9i1UnqsCQnfmv6c0FOWrPzCoUoUL5kqBt_7MtqP2xw8XX-estZ4J1bKLr52_xUU3UIzJia5dRG4xSFfAKytts7IepLV8gs9PWPvfwgzwxHfNNYC9v-cEW3uf8ZgZP2QwQM9J7gDwwb9TJxU-x42b8s89xiCOEYkkBIC_YEOs1nN2GMHFGWTekNELodcEkZUxYA2XmqJSpEuMfXrBNf02cBeI2X3GNes71081ILPO3dJHmSmPVYtjU8kk71F2se4j3RbZ1epVXUOXzFnGQhGkCPDa13vszTE8s2t3mg"  # noqa: E501, RUF100
    ),
    allowable_modalities=vlm_modalities,
)

qwen36_27B = ChatModel(
    url_remote=("https://qwen36-27b.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODE2MzkxNDQ4LCJpYXQiOjE3ODQ4NTU0NDgsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiMTVmMDNiMGMtMzMyNC00YzcyLTgxNDQtZmJhNWQwZjQwMTg5Iiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODQ4NTU0NDg2MDkiLCJ1aWQiOiIxZTUwOGQzYS02Mzg5LTRlMzktYmE1MC0xM2I0MmVkODg1NzYifX0sIm5iZiI6MTc4NDg1NTQ0OCwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4NDg1NTQ0ODYwOSJ9.kayEkcg9epy2XWUdm8CbqL-I1mdIIvjYxWnqtW5QViJyoZGlhlzscLqg18KrsHvx7vGI83pw6Xvg4K4DNXeIC3unmjDM5PpnGuhUD75QTom2nT8Ja91wPfNxG5nhK3XZ8sA-rqmDyZ92mai0Y5-X2NZRqPBwoTmhlz4_wA5DpwuDeNP-Lt_H6Yaa-lYbyuQPmP8VrhqGmFFXJt1WIBdFC7Kdet47p4Tp4976efLYXh9vokuye6aHMGFvMy0m5oo52i22Kj5b7iUf28BzpxXYTlSSBTQXK5cI1BozzgTFTfBbi13WKiaKkKrQ4o82BMJ325-YxKryriQwtcqPP5k0sg"  # noqa: E501, RUF100
    ),
    allowable_modalities=vlm_modalities,
    currently_deployed=False,
)

deepseek_v4_flash_280B = ChatModel(
    model_name="deepseek-ai/DeepSeek-V4-Flash-0731",
    url_remote=(
        "https://deepseek-v4-flash-0731.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODE3MjI3MzY4LCJpYXQiOjE3ODU2OTEzNjgsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiZTkyOWUyMTktMDRmMC00MzA2LTg3YTktNWFiMzE3YzMyMTNhIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODU2OTEzNjgwODUiLCJ1aWQiOiIxMDEwOGU3Zi01M2Y3LTQ2MjUtYmFhMC1hM2EzNmIwNzQ4MjYifX0sIm5iZiI6MTc4NTY5MTM2OCwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4NTY5MTM2ODA4NSJ9.oDPZ_WE0jRDOMoKcsg1iateGcBx3RZNYzbT-NdgN3-m4E-R3sbz3N49SVauPEGKgdsVUY75GU_Ve4P-oZFU5R762jMS-Ob3NcztftLdpwM28yDZLWGGDciidf0LU58Nz8-awx0kOEwpa-1vtWiVS2aK6BjqccAn7AIeead54Vi8UmC3KWlnOKAZShW6MWjJTNfG414ppH8an5LUPnURpm2qRCVfZRikk7Cr-n5Xgi3xkr8z_5jiVv-45cmyGaqkBV4xL1pz24S6u4o66CQ4L47XPKrzXaeF1ibfFkIExS3rB66qLWuXBSpqsbh-5MPtZobxssXUXCcleypwWrmnX1w"  # noqa: E501, RUF100
    ),
    currently_deployed=True,
)

glm_52_753B = ChatModel(
    model_name="zai-org/GLM-5.2-FP8",
    url_remote=(
        "https://glm-5-2-fran.project-user-francesco-caliva.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODE2Nzc1ODkzLCJpYXQiOjE3ODUyMzk4OTMsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiMDg0NDFhNjAtYTBlZS00MTRhLWIwOTktN2Y4ZDI3OGQ0YWUzIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODUyMzk4OTM0ODYiLCJ1aWQiOiJiMjQ3MTFhNi1lZDE4LTQ2MGEtYjRhZC0yNGY1OTNhNmRhODgifX0sIm5iZiI6MTc4NTIzOTg5Mywic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4NTIzOTg5MzQ4NiJ9.TtvwIRteT2ujgQnAKAZQ-aW-xOo3eSfs_27oFwEUery2mCwcAZf67zzRDebe7sgj7T8zSMfhbNkGacgIrpWtTemiOzxr7cJl-zHCBd3TvwkRIAknd9rT6WWJnOFGlliAutyFy9rsIpl6duRCzJWUftNx1ohgppVY0mavwV7m-hqOfMjFLIUZ5lqszCNCWqoXd-yfBfHWWP4EUYBlb8zu7zCMwWZtfvSdJQjcou7Q6y82G9U5nhDOBo1x3XeJ54wIkiB0OaYSiOpkgFBaukAUq7rthjS4667FRQYCRgNYB6Jc98tOv3XDaIiwPMcgJt6AXUU52Js4fAnV2ECYt5pJRg"  # noqa: E501, RUF100
    ),
    currently_deployed=False,
)

_qwen3_vl_mm_proc_kwargs = dict(  # noqa: C408
    fps=1.0,
    max_frames=64,
    min_pixels=4096,
    max_pixels=720**2,
    total_pixels=5 * 720**2,
)

# Embeddings
qwen3_vl_8B = EmbeddingModel(
    model_name="Qwen/Qwen3-VL-Embedding-8B",
    model_instantiation_class=MultiModalEmbeddings,
    url_remote=(
        "https://qwen3-vl-embedding-8b.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODExNTM2OTU1LCJpYXQiOjE3ODAwMDA5NTUsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiNmIxMTIyNzgtNmMzMi00Zjk5LTk5NWEtNzYzMGZlOWNkODRjIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODAwMDA5NTU3ODUiLCJ1aWQiOiJhNWVlNzA4Ni05NDNhLTQyZTktYjI1ZS1lYzVlYTFhN2RlMzEifX0sIm5iZiI6MTc4MDAwMDk1NSwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4MDAwMDk1NTc4NSJ9.qgYrdEJ8kebRtev6OdbZjVbafGSkbPVev0Qiz3qkQ6f90Br6RXvuQsslYfuyNZLrCwt-6e7B7zAIJCPUDk-VZgmMRSf7iZZcEYAeE9ZCx9gNNNG9mlq7qpz9ztr4d0ltYmxgrLFAYUHCtZu12_XZme2f47iJ4KHU-_VRmwkT3zy2V1VK4OLlU_V9VRtYlQojfp9O2IWnYCZ13OL2hMsxzEXk31RoOEkMKPu57U-ob-pmARIHsC9Z7uOog3vGI3T86KWc3VYfYWM6pYoZ_pfpAc0kahqIocghyrDmGtQyhKo-Zz5RB0uLNbemlNmDjaQV7Y0dccQPRCwu_txCkjb9fQ"  # noqa: E501, RUF100
    ),
    embedding_dim=4096,
    model_instantiation_kwargs={
        "tiktoken_enabled": False,
        "check_embedding_ctx_length": False,
    },
    code_chunk_size=8192,
    code_chunk_overlap=512,
    tokenizer_name="Qwen/Qwen3-VL-Embedding-8B",
    tokenizer_type="HuggingFace",
    preprocessor=qwen3_vl_8b_template,
    allowable_modalities=vlm_modalities,
    mm_processor_kwargs=_qwen3_vl_mm_proc_kwargs,
)

# Reranker
qwen3_vl_reranker_8B = RerankerModel(
    model_name="Qwen/Qwen3-VL-Reranker-8B",
    url_remote=(
        "https://qwen3-vl-reranker-8b.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODExNTM2OTgyLCJpYXQiOjE3ODAwMDA5ODIsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiMTFjZGNiZjUtZGQxNS00NGUyLWFhNWUtYTA4N2UzNzUxM2FkIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODAwMDA5ODIxMjIiLCJ1aWQiOiJlM2ZhZjI3Ny1jNWE0LTQ4ODgtODVlYi05NGZlMWVhM2QxNzAifX0sIm5iZiI6MTc4MDAwMDk4Miwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4MDAwMDk4MjEyMiJ9.AoWV-tYYP_z7PhXLqKI9XmYsG2BLYqzjCwDyF1PAKKbWH2Op3nZK29RkLeltc6Xtq3v-0372eD1mekX3iPapJNNP3wnmjmLmvvj59SD9_8PvvkDBgdfB_1h0Qnx-nsOxLvrsqr9WCMIJLKmWAjReFUWG8fcnwtKoFJPbONmOinUde7o3U-205eqI3OoQ-bxSamyFGJkk3IfSUr_KEI8HGWDWYlz6dKxHj3fKugddDNtVR8l1hSpJW8eka3sFKb8SquYUkhL0w4eU8Te_DU994kl7odPcp9xV7XI1t424rxN5XNrBQRwMvKxC7zu8NB-1nSVd9TgJGFcANWAzuX32iQ"  # noqa: E501, RUF100
    ),
    preprocessor=qwen3_vl_8b_template,
    allowable_modalities=vlm_modalities,
    mm_processor_kwargs=_qwen3_vl_mm_proc_kwargs,
)

# Voice Models
whisper_large_v3_turbo = VoiceModel(
    url_remote=(
        "https://whisper-large-v3-turbo-ab.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"  # noqa: E501, RUF100
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODExOTY4OTExLCJpYXQiOjE3ODA0MzI5MTEsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiY2ViM2Q2MjctMDc0My00YTQ4LTk1OTAtMmIzMDYxYmEyNTQyIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODA0MzI5MTEzNTIiLCJ1aWQiOiI4MDY0ODZjYS02N2FiLTRiOTctODhjMy1kYzU4NTUyZmI5MmEifX0sIm5iZiI6MTc4MDQzMjkxMSwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4MDQzMjkxMTM1MiJ9.CKyShXeNA3EHbD7toKbMr7ogNmo7xsyQLcyQV3I4V-IJ9QWKAUCOFi7HMLw4phUO8SBuqmAnjK7KOWg1Dbd_M_UeMR9uJLUqimbHGvLOK7KkjFZwgKKleQDGHPwd9uRY0BzqxL8iFJmwHmANEX8Cm-__ehtlDqPEbJKv91knLQJAZYKEw1NVCYczfVueZ6U374EhzFoVVN4A1HbTYvlVMr8qwvZeyJ8mHl3HtYKK2p7v6yb5tQb_yb2Dt28oyZfwj496p1CGfMHARBMt32yN4NOBELQUGreUiwUungx34-EFQe_BJSNyt7pS2JEKD4ylPIRCVl7k6Aa3XY2n9H7vjA"  # noqa: E501, RUF100
    ),
    currently_deployed=False,
)

cohere_transcribe_3_2b = VoiceModel(
    model_name="CohereLabs/cohere-transcribe-03-2026",
    url_remote=(
        "https://cohere-transcribe-03-2026.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"  # noqa: E501, RUF100
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODA5NTUwNzg5LCJpYXQiOjE3NzgwMTQ3ODksImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiNDI0NjI1MTMtY2ExNi00ZTgxLWIyZTItZDA1OTg2MzA4ODZlIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3NzgwMTQ3ODk4OTAiLCJ1aWQiOiI4MTE2YmIwMS01OTljLTQ3MjMtOGZmNC0yNTkxYTU0NjhjMzUifX0sIm5iZiI6MTc3ODAxNDc4OSwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc3ODAxNDc4OTg5MCJ9.iMeplOjNYeTjHy0hEQ6_AsRhVsovEVOQJkm2YQeAra7Tez8txFx_QBUvKECSztv4R2pMd4g1MpBWF4Nk69DaCvXXem8VL5NFh2K9IzW6uA8D_AJ_OPpcXlDOb6xWtzgm0C8r8FOo1SsgHfpPVqgn8WvhBSlPIYx4uyrSs5LCJaZ-4-q_kHaCRu4xlLZ2wbVnlnaGCBQjbGFl1IcDVLy8FqgswX6_mexHc1fQxeRQ86CmNFrXYoMnJMDEpqU5sMPzAyyk3otxj-G7N3Y2WK_FpcJLCGApcmF3KaKcIU7IdsLC_FVSC3VTBrOp5gDxSUqN61mEowVD8R4PceslwQiBdg"  # noqa: E501, RUF100
    ),
)

qwen3_tts_1_7B = VoiceModel(
    url_remote=(
        "https://qwen3-tts-12hz-1-7b-customvoice-ab.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"  # noqa: E501, RUF100
    ),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODExNDI1MDIwLCJpYXQiOjE3Nzk4ODkwMjAsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiYzEzN2ExZDUtZTc3OC00NTY1LWE1OGYtYTQyN2Q2ZGUxZTcxIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3Nzk4ODkwMjA0NDIiLCJ1aWQiOiJlODU1ZDQ3Zi02ODcxLTQzYjgtYTcyYS0yNmMxMDdiOWQ4MzAifX0sIm5iZiI6MTc3OTg4OTAyMCwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc3OTg4OTAyMDQ0MiJ9.nxzsJMTnYuoY3BlCMvto0TUOT1Hu90mX5-wp1_33RLLDcl5F_bJX1U_F-qsyK1nHEoiJrsOvSQICMGuURob0kp373Yb87t_n1b5Y_uYQprt3ePqH6sgWYxDlG7Tx7hRhTlBTZA161w9oHRSeQlrEty6gorjN5gYexeHx73PurSq2WQse6cQ3gsMkDlzx-dqo8qJnfZLKzT3ZGLu_nXL_xEvpDeO7HDEZtKw8lBL6z4SfqrDyC6mehVF505lcI1-D3F8CwLHgWkEM3hUYMbSU2RTEjoDZqCiMku_7TWDa7UkbmuROSpUSuxC5llgOPBl95VTYH_wMOAHgUbPH0zqy8Q"  # noqa: E501, RUF100
    ),
    tts_supported_voices={
        "aiden",
        "dylan",
        "eric",
        "ono_anna",
        "ryan",
        "serena",
        "sohee",
        "uncle_fu",
        "vivian",
    },
    model_type="TTS",
    currently_deployed=False,
)

fish_s2_pro_4B = VoiceModel(
    model_name="fishaudio/s2-pro",
    url_remote=("https://fish-s2-pro.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net"),
    api_key=(
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNUd1NsQkIxTkE0WV9zMDRxVE5NeDBjTFlpTFJEbVVxU0dldDdja3V4dmsifQ.eyJhdWQiOlsiYXBpIiwiaXN0aW8tY2EiXSwiZXhwIjoxODE1NzM5MTcxLCJpYXQiOjE3ODQyMDMxNzEsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiNGNhNmM4OTItMWZhOC00Y2ViLTlmODMtOGVjYzU5NDk4NmRmIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJ1aSIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJpc3ZjLWVwLTE3ODQyMDMxNzE0MTEiLCJ1aWQiOiJmZjA4MzAyOC1hZDIzLTQ0NjUtYjYxNC0xYzYxYmZiMjBhYmMifX0sIm5iZiI6MTc4NDIwMzE3MSwic3ViIjoic3lzdGVtOnNlcnZpY2VhY2NvdW50OnVpOmlzdmMtZXAtMTc4NDIwMzE3MTQxMSJ9.SAJERzJ2hux4UPOlY14KmnTETjyIC7XMwGC6awLQedJn6vJDP80Lep65SSN6L0CXTeEMCqJTEx0VIat6HBy8sOuz3IUuzVGpH2tnhtVx9ylIrMW1EWH86ee3akd8O75tN6WPlbKj0jC14FAEx3IXwyjf47QK3KjRoX7DoUsmKMBCQr_6Kx4R9bQL5rQmsq3vHcjnHSpkjYlFav2shU284EPoGSCGCfKhnheiuK6cDFk_YdY6lXGpvk8X-vOrBlPdoWJyTE7d-ycpWukVKmF3ufO1heeMsyDMjn2EXl3KOHj5Olbywj1uTQBwdfNinJCZVMudd0sZbBHCCE0AfGZqEg"  # noqa: E501, RUF100
    ),
    tts_supported_voices=set(),
    tts_voice="alys",
    model_type="TTS",
)
