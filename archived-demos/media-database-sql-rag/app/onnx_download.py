from chromadb.utils import embedding_functions
default_ef = embedding_functions.DefaultEmbeddingFunction()
default_ef(["test"])
