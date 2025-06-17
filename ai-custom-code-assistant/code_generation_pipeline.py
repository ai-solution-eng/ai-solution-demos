from typing import List, Union, Generator, Iterator
from pydantic import BaseModel, Field
import requests

class Pipeline:

    class Valves(BaseModel):
        MLIS_ENDPOINT: str = Field(
            default="https://qwen25-coder-7b-instruct-predictor-nishant-chanduk-cc427e73.ingress.pcai0103.sy6.hpecolo.net/v1",
            description="MLIS MLIS API compatible endpoints.",
        )
        MODEL_ID: str = Field(
            default="Qwen/Qwen2.5-Coder-7B-Instruct",
            description="Model name",
        )
        API_TOKEN: str = Field(
            default="",
            description="API key for authenticating requests to the MLIS API.",
        )
      

    def __init__(self):
        self.valves = self.Valves()

    def pipe(
        self, user_message: str, model_id: str, messages: List[dict], body: dict
    ) -> Union[str, Generator, Iterator]:
        print(f"pipe:{__name__}")
        headers = {
            "Authorization": f"Bearer {self.valves.API_TOKEN}",
            "Content-Type": "application/json",
        }

        system_prompt = """
You are a highly skilled AI model tasked with generating high-quality code based on user inputs. 
Please follow the instructions below carefully to ensure consistency, best practices, and compliance with standards:

1. Must adhere to PEP8 Code Standards:
   - If the user specifies Python as the programming language, ensure the code adheres to PEP8 standards.

2. Must add Code Description:
   - At the beginning of the code, include a detailed comment describing the purpose of the code, its functionality, and any assumptions made.

3. Inline Comments:
   - Add appropriate inline comments to describe each significant step or logic within the code.

4. Must Add Intellectual Property Notice:
   - Add the following comment at the top of the code to specify that the code is proprietary intellectual property:
     # Copyright (c) Hewlett Packard Enterprise (HPE). All rights reserved.
     # This code is proprietary and may not be shared, reproduced, or modified without prior written consent from HPE.

5. Must add Test Cases:
   - Provide comprehensive test cases for the code to verify its correctness and functionality.
   - Use Python's built-in unittest module to create and run the test cases.

Generate the code based on the user input while adhering to the guidelines above.

Notes for the Model:
- Always include the proprietary notice regardless of the language or functionality.
- For Python code, prioritize readability, maintainability, and adherence to PEP8 standards.
- Ensure test cases cover edge cases, typical use cases, and invalid inputs.
"""

        # Create the payload for the inference endpoint
        payload = {
            "model": self.valves.MODEL_ID,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.2,
        }
   
        try:
            r = requests.post(
                url=f"{self.valves.MLIS_ENDPOINT}/chat/completions",
                json=payload,
                headers=headers,
                stream=True,
                verify=False
            )

            r.raise_for_status()
            data = r.json()
            result = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            
            if body.get("stream", False):
                return result
            else:
                return result.json()
        except Exception as e:
            return f"Error: {e}"
