#!/usr/bin/env python3
"""
Standalone demo for MongoDB Atlas Hybrid Search using the $rankFusion aggregation stage.
This script is designed for MongoDB 8.1+
"""
import os
import time
from dotenv import load_dotenv
from pymongo import MongoClient
from openai import AzureOpenAI
from pymongo.errors import OperationFailure, ConnectionFailure, ConfigurationError

# --- 1. Configuration & Clients ---
def configure_clients():
    """Load environment variables and initialize clients."""
    load_dotenv()

    # Using the specified configuration
    mongo_uri = os.getenv("MONGODB_CONNECTION_URI")
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = "https://<endpoint>.openai.azure.com"
    azure_deployment = "text-embedding-3-small"

    if not mongo_uri or not azure_key:
        raise ValueError(
            "Please set all required environment variables: MONGODB_CONNECTION_URI and AZURE_OPENAI_API_KEY"
        )

    print("✅ Clients configured.")
    mongo_client = MongoClient(mongo_uri)
    # Ping the server to verify a successful connection
    mongo_client.admin.command('ping')
    print("✅ MongoDB connection successful.")
    
    return (
        mongo_client,
        AzureOpenAI(api_version="2024-02-01", api_key=azure_key, azure_endpoint=azure_endpoint),
        azure_deployment
    )

# --- 2. Data & Index Setup ---
def setup_database(db, coll_name, az_client, embed_model):
    """Create collection and sample data with embeddings if they don't exist."""
    collection = db[coll_name]
    
    if coll_name in db.list_collection_names() and collection.count_documents({}) > 0:
        print(f"ℹ️ Collection '{coll_name}' already exists. Skipping data setup.")
        return collection
        
    print(f"⚠️ Collection '{coll_name}' not found. Creating sample data...")
    collection.drop()

    movies = [
        {"title": "Star Wars: A New Hope", "plot": "Luke Skywalker joins forces with a Jedi Knight, a cocky pilot, a Wookiee and two droids to save the galaxy from the Empire's world-destroying battle station, while also attempting to rescue Princess Leia from the evil Darth Vader.", "genre": "Sci-Fi"},
        {"title": "The Godfather", "plot": "The aging patriarch of an organized crime dynasty transfers control of his empire to his reluctant son.", "genre": "Crime"},
        {"title": "The Dark Knight", "plot": "Batman must face the Joker, a criminal mastermind who wants to watch the world burn.", "genre": "Action"},
        {"title": "Pulp Fiction", "plot": "The lives of two mob hitmen, a boxer, and a gangster's wife intertwine in tales of violence and redemption.", "genre": "Crime"},
        {"title": "Forrest Gump", "plot": "A simple man from Alabama experiences several historical events in the 20th century.", "genre": "Drama"}
    ]
    
    print("📝 Generating embeddings for plots...")
    for movie in movies:
        movie["plot_embedding"] = get_embedding(az_client, movie["plot"], embed_model)
    
    collection.insert_many(movies)
    print(f"✅ Created and populated collection '{coll_name}'.")
    return collection

def get_embedding(client: AzureOpenAI, text: str, model: str) -> list[float]:
    """Generate vector embedding for a text string."""
    return client.embeddings.create(input=[text], model=model).data[0].embedding

def wait_for_index(coll, index_name: str, timeout: int = 300):
    """Poll search indexes until the specified index is ready."""
    print(f"⏳ Waiting for index '{index_name}' to be ready...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            indexes = list(coll.list_search_indexes(name=index_name))
            if indexes and (indexes[0].get('status') == 'READY' or indexes[0].get('queryable') == True):
                print(f"✅ Index '{index_name}' is ready.")
                return True
            time.sleep(5)
        except OperationFailure:
            time.sleep(5)
    raise TimeoutError(f"Index '{index_name}' did not become ready in {timeout}s.")

def create_indexes_if_needed(collection, text_idx, vec_idx, vector_dims):
    """Checks for and creates the required text and vector indexes."""
    existing_indexes = [idx['name'] for idx in collection.list_search_indexes()]
    
    if text_idx not in existing_indexes:
        print(f"🛠️ Creating text index: '{text_idx}'...")
        text_index_model = { "name": text_idx, "definition": { "mappings": { "dynamic": True } } }
        collection.create_search_index(model=text_index_model)
        wait_for_index(collection, text_idx)
    else:
        print(f"ℹ️ Text index '{text_idx}' already exists.")

    if vec_idx not in existing_indexes:
        print(f"🛠️ Creating vector index: '{vec_idx}'...")
        vector_index_model = {
            "name": vec_idx,
            "definition": {
                "mappings": { "fields": { "plot_embedding": {
                    "type": "knnVector", "dimensions": vector_dims, "similarity": "cosine"
                }}}
            }
        }
        collection.create_search_index(model=vector_index_model)
        wait_for_index(collection, vec_idx)
    else:
        print(f"ℹ️ Vector index '{vec_idx}' already exists.")

# --- 3. Main Execution ---
def main():
    """Main function to run the hybrid search demo."""
    mongo_client = None
    try:
        mongo_client, azure_client, embed_deployment = configure_clients()
        DB_NAME = "hybrid_search_db"
        COLLECTION_NAME = "movies"
        TEXT_INDEX_NAME = "movies_text_index"
        VECTOR_INDEX_NAME = "movies_vector_index"
        EMBEDDING_DIMENSIONS = 1536

        db = mongo_client[DB_NAME]
        
        collection = setup_database(db, COLLECTION_NAME, azure_client, embed_deployment)
        create_indexes_if_needed(collection, TEXT_INDEX_NAME, VECTOR_INDEX_NAME, EMBEDDING_DIMENSIONS)

        query = "space galaxy adventure"
        print(f"\n🚀 Performing Hybrid Search for: '{query}'")
        query_embedding = get_embedding(azure_client, query, embed_deployment)

        # Using the MongoDB 8.1+ top-level $rankFusion stage syntax
        pipeline = [
            {
                "$rankFusion": {
                    "input": {
                        "pipelines": {
                            "vectorPipeline": [
                                {
                                    "$vectorSearch": {
                                        "index": VECTOR_INDEX_NAME,
                                        "path": "plot_embedding",
                                        "queryVector": query_embedding,
                                        "numCandidates": 100,
                                        "limit": 10
                                    }
                                }
                            ],
                            "fullTextPipeline": [
                                {
                                    "$search": {
                                        "index": TEXT_INDEX_NAME,
                                        "text": {
                                            "query": query,
                                            "path": ["title", "plot"]
                                        }
                                    }
                                },
                                { "$limit": 10 }
                            ]
                        }
                    },
                    "combination": {
                        "weights": {
                            "vectorPipeline": 0.7,
                            "fullTextPipeline": 0.3
                        }
                    },
                    "scoreDetails": True
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "title": 1,
                    "plot": 1,
                    "scoreDetails": {"$meta": "scoreDetails"}
                }
            },
            {"$limit": 5}
        ]
        
        results = list(collection.aggregate(pipeline))
        
        print("\n🏆 Fused Search Results (Ranked by Weighted Score):")
        for doc in results:
            print(f"  Score: {doc['scoreDetails']['value']:.4f} | {doc['title']}\n    Plot: {doc['plot']}\n")

    except (ValueError, ConfigurationError, ConnectionFailure, OperationFailure, TimeoutError) as e:
        print(f"\n❌ FATAL ERROR: {e}")
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
    finally:
        if mongo_client:
            user_input = input(f"🧹 Do you want to drop the collection '{COLLECTION_NAME}'? (yes/no): ").lower()
            if user_input == 'yes':
                mongo_client[DB_NAME][COLLECTION_NAME].drop()
                print("✅ Collection dropped.")
            mongo_client.close()
            print("🚪 MongoDB connection closed.")

if __name__ == "__main__":
    main()

"""
If you run into:

❌ FATAL ERROR: Unrecognized pipeline stage name: '$rankFusion', full error: {'ok': 0.0, 'errmsg': "Unrecognized pipeline stage name: '$rankFusion'", 'code': 40324, 'codeName': 'Location40324', '$clusterTime': {'clusterTime': Timestamp(1755181535, 2), 'signature': {'hash': b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', 'keyId': 0}}, 'operationTime': Timestamp(1755181535, 2)}

Here is why:

The $rankFusion aggregation stage is a newer feature, currently only supported in MongoDB 8.1+

After upgrading to 8.1, you will not be able to downgrade to previous versions, 

Here is the output:

✅ Clients configured.
✅ MongoDB connection successful.
⚠️ Collection 'movies' not found. Creating sample data...
📝 Generating embeddings for plots...
✅ Created and populated collection 'movies'.
🛠️ Creating text index: 'movies_text_index'...
⏳ Waiting for index 'movies_text_index' to be ready...
✅ Index 'movies_text_index' is ready.
🛠️ Creating vector index: 'movies_vector_index'...
⏳ Waiting for index 'movies_vector_index' to be ready...
✅ Index 'movies_vector_index' is ready.

🚀 Performing Hybrid Search for: 'space galaxy adventure'

🏆 Fused Search Results (Ranked by Weighted Score):
  Score: 0.0164 | Star Wars: A New Hope
    Plot: Luke Skywalker joins forces with a Jedi Knight, a cocky pilot, a Wookiee and two droids to save the galaxy from the Empire's world-destroying battle station, while also attempting to rescue Princess Leia from the evil Darth Vader.

  Score: 0.0113 | Pulp Fiction
    Plot: The lives of two mob hitmen, a boxer, and a gangster's wife intertwine in tales of violence and redemption.

  Score: 0.0111 | The Dark Knight
    Plot: Batman must face the Joker, a criminal mastermind who wants to watch the world burn.

  Score: 0.0109 | The Godfather
    Plot: The aging patriarch of an organized crime dynasty transfers control of his empire to his reluctant son.

  Score: 0.0108 | Forrest Gump
    Plot: A simple man from Alabama experiences several historical events in the 20th century.

🧹 Do you want to drop the collection 'movies'? (yes/no): no
🚪 MongoDB connection closed.

"""
