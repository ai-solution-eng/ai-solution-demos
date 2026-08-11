import base64
import os
from io import BytesIO
from typing import Any

__all__ = ["prepare_vllm_inputs"]


def format_input_to_conversation(
    input_dict: dict[str, Any], instruction: str = "Represent the user's input."
) -> list[dict]:
    content = []

    text = input_dict.get("text")
    image = input_dict.get("image")

    if image:
        image_content = None
        if isinstance(image, str):
            if image.startswith(("http", "https", "oss")):
                image_content = image
            else:
                abs_image_path = os.path.abspath(image)
                image_content = "file://" + abs_image_path
        else:
            image_content = image

        if image_content:
            content.append(
                {
                    "type": "image",
                    "image": image_content,
                }
            )

    if text:
        content.append({"type": "text", "text": text})

    if not content:
        content.append({"type": "text", "text": ""})

    conversation = [
        {"role": "system", "content": [{"type": "text", "text": instruction}]},
        {"role": "user", "content": content},
    ]

    return conversation


def prepare_vllm_inputs(
    text: str,
    image: str | None = None,
    instruction: str = "Represent the user's input.",
    convert_base64: bool = False,
) -> str | dict[str, Any]:
    # conversation = format_input_to_conversation(dict(text=text, image=image), instruction)

    multi_modal_data: dict[str, Any] | None = None
    if image:
        if isinstance(image, str):
            if image.startswith(("http", "https", "oss")):
                try:
                    from vllm.multimodal.utils import fetch_image

                    image_obj = fetch_image(image)
                    multi_modal_data = {"image": image_obj}
                except Exception as e:
                    print(f"Warning: Failed to fetch image {image}: {e}")
            else:
                abs_image_path = os.path.abspath(image)
                if os.path.exists(abs_image_path):
                    from PIL import Image

                    image_obj = Image.open(abs_image_path)
                    multi_modal_data = {"image": image_obj}
                else:
                    print(f"Warning: Image file not found: {abs_image_path}")

            if multi_modal_data is not None and convert_base64:
                storage = BytesIO()
                multi_modal_data["image"].save(storage, format="jpeg")
                img_byte = storage.getvalue()
                image_data = base64.b64encode(img_byte).decode("utf-8")
                multi_modal_data["image"] = f"data:image/jpeg;base64,{image_data}"
        else:
            multi_modal_data = {"image": image}

    vision_text = "" if multi_modal_data is None else "<|vision_start|><|image_pad|><|vision_end|>"
    prompt_text = (
        f"<|im_start|>system\n{instruction}<|im_end|>\n<|im_start|>user\n{vision_text}{text}"
        "<|im_end|>\n<|im_start|>assistant\n"
    )

    result = {"prompt": prompt_text, "multi_modal_data": multi_modal_data}

    return result
