from sklearn.neighbors import NearestNeighbors
from transformers import AutoTokenizer,AutoModel
import torch
import yaml
import pandas as pd
import numpy as np
import httpx
import pandas as pd
import psycopg
from config_handler import load_config, validate_config

config = load_config()

host = config["postgresql"]["host"]
dbname = config["postgresql"]["dbname"]
user = config["postgresql"]["user"]
password = config["postgresql"]["password"]
port = config["postgresql"]["port"]
tablename = config["postgresql"]["tablename"]

with psycopg.connect(
    f"host={host} dbname={dbname} user={user} password={password} port={port}"
) as conn:
    ticket_df = pd.read_sql(f'select * from {tablename}',con=conn)

# ticket_df = pd.read_csv(config["ticket_data"],low_memory=False)
data = ticket_df.head(50)
data.fillna('', inplace=True)
data['ticket_details'] = data['ticketList_subject'] + " " + data['ticketList_detailproblem'] + " " + data['ticketList_source_cause'] + " " + data['ticketList_product_category'] 
test_data = ticket_df.iloc[50:]

model_path = config["resolution_model"]["embeddings_model"]
embed_tokenizer = AutoTokenizer.from_pretrained(model_path)

embed_model = AutoModel.from_pretrained(model_path, trust_remote_code=True)

embeddings = np.load(config["resolution_model"]["embeddings_path"])
retriever = NearestNeighbors(n_neighbors=5, metric='cosine').fit(embeddings)

def call_llm(prompt: str):
    """Call the LLM inference server directly"""
    # Reload config dynamically to get latest environment variables
    current_config = load_config()
    config_errors = validate_config(current_config)
    if config_errors:
        raise ValueError(f"LLM configuration errors: {', '.join(config_errors)}")
    
    headers = {"Authorization": f"Bearer {current_config['resolution_model']['inference_server_token']}"}
    payload = {
        "model": current_config["resolution_model"]["llm_model"],
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 2000,
        "temperature": 0
    }
    
    try:
        resp = httpx.post(
            current_config["resolution_model"]["inference_server_url"], 
            json=payload, 
            headers=headers, 
            timeout=60.0
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except httpx.ConnectError as e:
        raise ConnectionError(f"Unable to connect to LLM service. Please check the endpoint configuration.") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"Request to LLM service timed out.") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"LLM service returned error {e.response.status_code}: {e.response.text}") from e
    except KeyError as e:
        raise RuntimeError(f"Unexpected response format from LLM service. Missing key: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error calling LLM service: {str(e)}") from e

# def get_embeddings_in_batches(texts, batch_size=32):
#     all_embeddings = []
#     for i in range(0, len(texts), batch_size):
#         batch_texts = texts[i:i + batch_size]
#         inputs = embed_tokenizer(batch_texts, padding=True, truncation=True, return_tensors='pt')
#         with torch.no_grad():
#             outputs = embed_model(**inputs)
#         batch_embeddings = outputs.last_hidden_state.mean(dim=1).numpy()
#         all_embeddings.append(batch_embeddings)
#     return np.vstack(all_embeddings)

def get_embeddings(texts):
    inputs = embed_tokenizer(texts, padding=True, truncation=True, return_tensors='pt')
    with torch.no_grad():
        outputs = embed_model(**inputs)
    return outputs.last_hidden_state.mean(dim=1).numpy() 

def retrieve_similar_issues(query):
    query_embedding = get_embeddings([query])
    _, indices = retriever.kneighbors(query_embedding)
    # Retrieve both issue text and resolution for similar cases
    similar_issues = data.iloc[indices[0]]
    return similar_issues

def build_few_shot_prompt_resolution(query, ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category):
    # Retrieve similar issues dynamically based on the input query
    similar_issues = retrieve_similar_issues(query)
    
    # Build few-shot examples as text
    examples_text = "You are a helpful assistant. Use the following examples to guide your response.\n\n"
    
    for _, issue in similar_issues.iterrows():
        examples_text += f"Ticket subject: {issue['ticketList_subject']}\n"
        examples_text += f"Detailed problem: {issue['ticketList_detailproblem']}\n"
        examples_text += f"Source cause: {issue['ticketList_source_cause']}\n"
        examples_text += f"Product category: {issue['ticketList_product_category']}\n"
        examples_text += f"Resolution: {issue['ticketList_resolution']}\n\n"
    
    # Add the current ticket for prediction
    examples_text += "Based on the similar cases above, please provide a resolution for the following ticket:\n"
    examples_text += f"Ticket subject: {ticketList_subject}\n"
    examples_text += f"Detailed problem: {ticketList_detailproblem}\n"
    examples_text += f"Source cause: {ticketList_source_cause}\n"
    examples_text += f"Product category: {ticketList_product_category}\n"
    examples_text += "What is the appropriate resolution for this ticket?"
    
    return examples_text

# Function to predict resolution dynamically based on the user's input ticket
def predict_resolution(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category):
    # Create a dynamic sample query using input variables
    sample_query = f"{ticketList_subject} {ticketList_detailproblem} {ticketList_source_cause} {ticketList_product_category}"
    
    # Build the prompt with few-shot examples
    prompt = build_few_shot_prompt_resolution(sample_query, ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category)
    
    try:
        result = call_llm(prompt)
        # Create a simple result object to match the expected interface
        class SimpleResult:
            def __init__(self, content):
                self.content = content
        
        return SimpleResult(result)
    except Exception as e:
        raise RuntimeError(f"Failed to predict resolution: {str(e)}") from e



def build_few_shot_prompt_mttr(query, ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category):
    # Retrieve similar issues dynamically based on the input query
    similar_issues = retrieve_similar_issues(query)
    
    # Build few-shot examples as text
    examples_text = "You are a helpful assistant. Use the following examples to guide your response.\n\n"
    
    for _, issue in similar_issues.iterrows():
        examples_text += f"Ticket subject: {issue['ticketList_subject']}\n"
        examples_text += f"Detailed problem: {issue['ticketList_detailproblem']}\n"
        examples_text += f"Source cause: {issue['ticketList_source_cause']}\n"
        examples_text += f"Product category: {issue['ticketList_product_category']}\n"
        examples_text += f"Mean time to resolution: {issue['ticketList_mttrall']}\n\n"
    
    # Add the current ticket for prediction
    examples_text += "Based on the similar cases above, please provide the 'mean time to resolution' for the following ticket:\n"
    examples_text += f"Ticket subject: {ticketList_subject}\n"
    examples_text += f"Detailed problem: {ticketList_detailproblem}\n"
    examples_text += f"Source cause: {ticketList_source_cause}\n"
    examples_text += f"Product category: {ticketList_product_category}\n"
    examples_text += "What would be the appropriate 'mean time to resolution' for this ticket?\n"
    examples_text += "Provide only the time estimate, do not give any explanation."
    
    return examples_text

def predict_mttr(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category):
    # Create a dynamic sample query using input variables
    sample_query = f"{ticketList_subject} {ticketList_detailproblem} {ticketList_source_cause} {ticketList_product_category}"
    
    # Build the prompt with few-shot examples
    prompt = build_few_shot_prompt_mttr(sample_query, ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category)
    
    try:
        result = call_llm(prompt)
        # Create a simple result object to match the expected interface
        class SimpleResult:
            def __init__(self, content):
                self.content = content
        
        return SimpleResult(result)
    except Exception as e:
        raise RuntimeError(f"Failed to predict MTTR: {str(e)}") from e