from qdrant_client import QdrantClient 
from qdrant_client.models import VectorParams, Distance, PayloadSchemaType, PointStruct, SparseVectorParams, Document, Prefetch, FusionQuery 
from qdrant_client import models 

import pandas as pd 
import openai 
# import fastembed 
import numpy as np 
# import tiktoken 

# Get api key value 

from dotenv import load_dotenv
from pathlib import Path
import os

def get_embedding(text, model="text-embedding-3-small"): 
    response = openai.embeddings.create(  
        input=[text], 
        model=model 
    )
    return response.data[0].embedding

def get_embeddings_batch(text_list, model="text-embedding-3-small", batch_size=100):
    
    if len(text_list) <= batch_size:
        response = openai.embeddings.create(input=text_list, model=model)
        return [embedding.embedding for embedding in response.data]
    
    all_embeddings = []
    counter = 1
    for i in range(0, len(text_list), batch_size):
        batch = text_list[i:i + batch_size]
        response = openai.embeddings.create(input=batch, model=model)
        all_embeddings.extend([embedding.embedding for embedding in response.data])
        print(f"Processed {counter * batch_size} of {len(text_list)}")
        counter += 1
    
    return all_embeddings

# Path to .env two levels up
env_path = Path.cwd().parents[1] / ".env"
print(env_path)

load_dotenv(env_path)

# Access your key
MY_KEY = os.getenv("QDRANT_API_KEY")
cluster_url = "https://6aa5631b-cbfd-40dd-b931-bf820f82a65e.us-east4-0.gcp.cloud.qdrant.io"

qdrant_client = QdrantClient( 
    url=cluster_url, 
    api_key = str(MY_KEY) 
)

# Create Qdrant Collection for Hybrid Search
COLLECTION = "Event-items-collection-01-hybrid-search"
if not qdrant_client.collection_exists(collection_name=COLLECTION):

    qdrant_client.create_collection( 
        collection_name=COLLECTION,
        vectors_config={"text-embedding-3-small": VectorParams(size=1536, distance=Distance.COSINE)}, 
        sparse_vectors_config={"bm25": SparseVectorParams(modifier=models.Modifier.IDF)}
    )

    qdrant_client.create_payload_index(  
        collection_name=COLLECTION,   
        field_name="event_id", 
        field_schema=PayloadSchemaType.KEYWORD, 
    )

# Process and Embed Amazon Items Data
df_items = pd.read_json("data/events.jsonl", lines=True)

def _description_for_embed(ev: dict) -> str:
    """Title + body for embedding; supports str or list descriptions."""
    if not ev:
        return ""
    title = (ev.get("title") or "").strip()
    raw = ev.get("description")
    if raw is None:
        body = ""
    elif isinstance(raw, list):
        body = " ".join(str(x) for x in raw)
    else:
        body = str(raw).strip()
    return f"{title} {body}".strip()


def _event_with_embed_text(row):
    ev = row["event"] or {}
    return {**ev, "description": _description_for_embed(ev)}


df_items["event"] = df_items.apply(_event_with_embed_text, axis=1)

# df_sample = df_items.sample(60, random_state=42)
df_sample = df_items

data_to_embed = (
    df_sample["event"]
    .apply(lambda e: {
        "description": (e or {}).get("description"),
        "date_range": (e or {}).get("date_range"),
        "schedule": (e or {}).get("schedule"),
        "price": (e or {}).get("price"),
        "recommended_ages": (e or {}).get("recommended_ages"),
        "venue": (e or {}).get("venue"),
        "address": (e or {}).get("address"),
        "event_id": (e or {}).get("event_id"),
        "occurrence_ids": (e or {}).get("occurrence_ids"),
    })
    .tolist()
)

text_to_embed = [data["description"] for data in data_to_embed]

embeddings = get_embeddings_batch(text_to_embed)

pointstructs = [] 
i = 1 
for embedding, data in zip(embeddings, data_to_embed): 
    pointstructs.append(
        PointStruct( 
            id=i, 
            vector={
                "text-embedding-3-small": embedding, 
                "bm25": Document( 
                    text=data["description"],  
                    model="qdrant/bm25"
                )
            }, 
            payload=data 
        )
    )
    i += 1 

qdrant_client.upsert(
    collection_name="Event-items-collection-01-hybrid-search",
    points=pointstructs
)