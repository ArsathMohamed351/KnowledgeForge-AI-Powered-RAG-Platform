from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
from config import *

embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)

def get_vectorstore():
    return Chroma( persist_directory=CHROMA_PATH, embedding_function=embeddings )