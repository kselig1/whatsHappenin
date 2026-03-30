import openai 
from qdrant_client import QdrantClient 
from src.api.core.config import config


def get_embedding(text, model="text-embedding-3-small"):
    response = openai.embeddings.create(
        input = text,
        model = model,
    )
    return response.data[0].embedding

def retrieve_data(query, qdrant_client, k=5): 
    query_embedding = get_embedding(query)  
    results = qdrant_client.query_points(
        collection_name="Event-items-collection-01-hybrid-search",  
        query=query_embedding, 
        limit=k,  
    ) 
    retrieved_context_ids = []
    retrieved_context = [] 
    similarity_scores = [] 
    
    for result in results.points: 
        retrieved_context_ids.append(result.payload["parent_asin"]) 
        retrieved_context.append(result.payload["description"]) 
        similarity_scores.append(result.score) 

    return {
        "retrieved_context_ids": retrieved_context_ids, 
        "retrieved_context": retrieved_context, 
        "similarity_scores": similarity_scores, 
    }

def process_context(context):
    formatted_context = "" 

    for id, chunk in zip(context["retrieved_context_ids"], context["retrieved_context"]):  
        formatted_context += f"- {id}: {chunk}\n"  

    return formatted_context

def build_prompt(preprocessed_context, question): 

    prompt = f""" 
    You are an event finding assistant that can answer qeustions about upcoming events. 

    You will be given a question and a list of context. 

    Instructions: 
    - You need to answer the question based on the provided context only. 
    - Never use word context and refer to it as the available events. 

    Context: 
    {preprocessed_context} 

    Question: 
    {question} 
    """ 
    return prompt 

def generate_answer(prompt):  
    response = openai.chat.completions.create(
        model="gpt-4o-mini", 
        messages=[{"role": "system", "content": prompt}], 
        temperature=0.5, 
    ) 
    return response.choices[0].message.content

def rag_pipeline(question, top_k=5): 
    qdrant_client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY) 

    retrieved_context = retrieve_data(question, qdrant_client, top_k) 
    preprocessed_context = process_context(retrieved_context)
    prompt = build_prompt(preprocessed_context, question)
    answer = generate_answer(prompt)

    return answer