from sklearn.neighbors import NearestNeighbors
from langchain_openai import ChatOpenAI
from langchain.prompts import PromptTemplate
from langchain_core.prompts import FewShotPromptTemplate
from transformers import AutoTokenizer,AutoModel
import torch
import yaml
import pandas as pd
import numpy as np
import httpx
import pandas as pd
import psycopg
from config_handler import load_config

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
data = ticket_df.head(10000)
data.fillna('', inplace=True)
data['ticket_details'] = data['ticketList_subject'] + " " + data['ticketList_detailproblem'] + " " + data['ticketList_source_cause'] + " " + data['ticketList_product_category'] 
test_data = ticket_df.iloc[10000:]

model_path = config["resolution_model"]["embeddings_model"]
embed_tokenizer = AutoTokenizer.from_pretrained(model_path)

embed_model = AutoModel.from_pretrained(model_path, trust_remote_code=True)

llm_local = ChatOpenAI(
    model=config["resolution_model"]["llm_model"],
    openai_api_key=config["resolution_model"]["inference_server_token"],
    openai_api_base=config["resolution_model"]["inference_server_url"],
    max_tokens=2000,
    temperature=0,
    # http_client=httpx.Client(verify=False)
)
embeddings = np.load(config["resolution_model"]["embeddings_path"])
retriever = NearestNeighbors(n_neighbors=5, metric='cosine').fit(embeddings)

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
    distances, indices = retriever.kneighbors(query_embedding)
    # Retrieve both issue text and resolution for similar cases
    similar_issues = data.iloc[indices[0]]
    return similar_issues

# Function to dynamically generate few-shot examples based on the user's input
def get_few_shot_examples(query):
    # Retrieve similar issues dynamically based on the input query
    similar_issues = retrieve_similar_issues(query)
    
    # Create few-shot examples from the retrieved similar issues
    few_shot_examples = [
        {
            "ticketList_subject": issue['ticketList_subject'],
            "ticketList_detailproblem": issue['ticketList_detailproblem'],
            "ticketList_source_cause": issue['ticketList_source_cause'],
            "ticketList_product_category": issue['ticketList_product_category'],
            "output": f"Resolution: {issue['ticketList_resolution']}"
        }
        for _, issue in similar_issues.iterrows()  # Assuming similar_issues is a DataFrame
    ]
    return few_shot_examples

# Function to predict resolution dynamically based on the user's input ticket
def predict_resolution(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category,llm_local=llm_local):
    # Create a dynamic sample query using input variables
    sample_query = f"{ticketList_subject} {ticketList_detailproblem} {ticketList_source_cause} {ticketList_product_category}"
    
    # Generate few-shot examples dynamically
    few_shot_examples = get_few_shot_examples(sample_query)

    # print(few_shot_examples)
    # Define example prompt template using PromptTemplate
    example_prompt = PromptTemplate(
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category", "output"],
        template="Ticket subject: {ticketList_subject}\n"
                 "Detailed problem: {ticketList_detailproblem}\n"
                 "Source cause: {ticketList_source_cause}\n"
                 "Product category: {ticketList_product_category}\n"
                 "{output}"
    )

    # Define a suffix template for the final prediction
    suffix = PromptTemplate(
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category"],
        template="Based on the similar cases above, please provide a resolution for the following ticket:\n"
                 "Ticket subject: {ticketList_subject}\n"
                 "Detailed problem: {ticketList_detailproblem}\n"
                 "Source cause: {ticketList_source_cause}\n"
                 "Product category: {ticketList_product_category}\n"
                 "What is the appropriate resolution for this ticket?"
    )

    # Create FewShotPromptTemplate with examples and templates
    few_shot_template = FewShotPromptTemplate(
        examples=few_shot_examples,
        example_prompt=example_prompt,
        suffix=suffix.template,
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category"],
        prefix="You are a helpful assistant. Use the following examples to guide your response.\n"
    )
    print(few_shot_template)
    #chain = prompt | llm_local
    chain = few_shot_template | llm_local
    
    # Use LLMChain to predict resolution based on dynamically generated few-shot examples
    print("Invoking chain")
    result = chain.invoke({
        "ticketList_subject": ticketList_subject,
        "ticketList_detailproblem": ticketList_detailproblem,
        "ticketList_source_cause": ticketList_source_cause,
        "ticketList_product_category": ticketList_product_category
    })
    return result



def get_few_shot_examples_mttr(query):
    # Retrieve similar issues dynamically based on the input query
    similar_issues = retrieve_similar_issues(query)
    
    # Create few-shot examples from the retrieved similar issues
    few_shot_examples = [
        {
            "ticketList_subject": issue['ticketList_subject'],
            "ticketList_detailproblem": issue['ticketList_detailproblem'],
            "ticketList_source_cause": issue['ticketList_source_cause'],
            "ticketList_product_category": issue['ticketList_product_category'],
            "output": f"Mean time to resolution: {issue['ticketList_mttrall']}"
        }
        for _, issue in similar_issues.iterrows()  # Assuming similar_issues is a DataFrame
    ]
    return few_shot_examples


def predict_mttr(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category,llm_local=llm_local):
    # Create a dynamic sample query using input variables
    sample_query = f"{ticketList_subject} {ticketList_detailproblem} {ticketList_source_cause} {ticketList_product_category}"
    
    # Generate few-shot examples dynamically
    few_shot_examples = get_few_shot_examples_mttr(sample_query)

    print(few_shot_examples)
    # Define example prompt template using PromptTemplate
    example_prompt = PromptTemplate(
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category", "output"],
        template="Ticket subject: {ticketList_subject}\n"
                 "Detailed problem: {ticketList_detailproblem}\n"
                 "Source cause: {ticketList_source_cause}\n"
                 "Product category: {ticketList_product_category}\n"
                 "{output}"
    )

    # Define a suffix template for the final prediction
    suffix = PromptTemplate(
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category"],
        template="Based on the similar cases above, please provide the 'mean time to resolution' for the following ticket:\n"
                 "Ticket subject: {ticketList_subject}\n"
                 "Detailed problem: {ticketList_detailproblem}\n"
                 "Source cause: {ticketList_source_cause}\n"
                 "Product category: {ticketList_product_category}\n"
                 "What would be the appropriate 'mean time to resolution' for this ticket?\n"
                 "Provide only the time estimate, do not give any explanation."
    )

    # Create FewShotPromptTemplate with examples and templates
    few_shot_template = FewShotPromptTemplate(
        examples=few_shot_examples,
        example_prompt=example_prompt,
        suffix=suffix.template,
        input_variables=["ticketList_subject", "ticketList_detailproblem", "ticketList_source_cause", "ticketList_product_category"],
        prefix="You are a helpful assistant. Use the following examples to guide your response.\n"
    )

    #chain = prompt | llm_local
    chain = few_shot_template | llm_local
    
    # Use LLMChain to predict resolution based on dynamically generated few-shot examples
    result = chain.invoke({
        "ticketList_subject": ticketList_subject,
        "ticketList_detailproblem": ticketList_detailproblem,
        "ticketList_source_cause": ticketList_source_cause,
        "ticketList_product_category": ticketList_product_category
    })
    return result