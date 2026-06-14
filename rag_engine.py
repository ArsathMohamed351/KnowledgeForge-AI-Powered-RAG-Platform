import uuid
from langchain_ollama import ChatOllama
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from analytics_engine import is_analytic_question, analyze_dataframe
from config import *

llm = ChatOllama(model=LLM_MODEL, temperature=0)
splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)


def add_documents(vectorstore, docs):
    """Add documents to vector store with batch processing"""
    chunks = splitter.split_documents(docs)

    print(f"Total Chunks Created: {len(chunks)}")
    batch_size = 100

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        ids = [str(uuid.uuid4()) for _ in batch]

        try:
            vectorstore.add_documents(documents=batch, ids=ids)
            print(f"Indexed {min(i + batch_size, len(chunks))}/{len(chunks)}")
        except Exception as e:
            print(f"Batch Failed: {e}")
    print("Document Indexing Complete")

def select_best_dataset(docs):
    """Select best dataset from retrieved documents"""
    dataset_score = {}
    for doc in docs:
        dataset_id = doc.metadata.get("dataset_id")
        if dataset_id:
            dataset_score[dataset_id] = dataset_score.get(dataset_id, 0) + 1
    if not dataset_score:
        return None
    return max(dataset_score, key=dataset_score.get)

def hybrid_search(vectorstore, question, k=5):
    """
    Perform hybrid search: combine semantic search (vector) with keyword search (BM25)
    """
    try:
        # Semantic search from vector store
        semantic_results = vectorstore.similarity_search_with_score(question, k=k)
        all_docs = vectorstore.get()
        
        if all_docs and 'documents' in all_docs:
            # Create BM25 retriever
            docs_for_bm25 = []
            for i, doc in enumerate(all_docs.get('documents', [])):
                metadata = all_docs.get('metadatas', [{}])[i] if all_docs.get('metadatas') else {}
                docs_for_bm25.append(Document(page_content=doc, metadata=metadata))
            
            if docs_for_bm25:
                bm25_retriever = BM25Retriever.from_documents(docs_for_bm25)
                bm25_results = bm25_retriever.invoke(question)
                combined = {}
                for doc, score in semantic_results:
                    doc_id = id(doc)
                    combined[doc_id] = {'doc': doc, 'semantic_score': 1 - score}
                for doc in bm25_results[:k]:
                    doc_id = id(doc)
                    if doc_id in combined:
                        combined[doc_id]['bm25_score'] = 0.5
                    else:
                        combined[doc_id] = {'doc': doc, 'bm25_score': 0.5}          
                final_results = []
                for item in combined.values():
                    semantic = item.get('semantic_score', 0)
                    bm25 = item.get('bm25_score', 0)
                    final_score = (semantic * 0.6) + (bm25 * 0.4)
                    final_results.append((item['doc'], final_score))
                
                final_results.sort(key=lambda x: x[1], reverse=True)
                return [doc for doc, _ in final_results[:k]]
        
        return [doc for doc, _ in semantic_results]
        
    except Exception as e:
        print(f"Hybrid search error: {e}")
        return vectorstore.similarity_search(question, k=k)

def rerank_documents(docs, question, llm):
    """
    Rerank retrieved documents using LLM
    Keeps top documents relevant to the question
    """
    if len(docs) <= 3:
        return docs
    
    try:
        doc_summaries = "\n".join([f"{i+1}. {doc.page_content[:200]}..." for i, doc in enumerate(docs)])
        
        rerank_prompt = f"""
Given the question and document summaries, rank these documents by relevance (1=most relevant, {len(docs)}=least relevant).
Return ONLY the ranking as numbers separated by commas, like: 3,1,5,2,4
QUESTION: {question}
DOCUMENTS:
{doc_summaries}

RANKING (numbers only):
"""    
        response = llm.invoke(rerank_prompt)
        ranking_str = response.content.strip()
    
        try:
            ranking = [int(x.strip()) - 1 for x in ranking_str.split(',')]
            # Reorder documents based on LLM ranking
            reranked = [docs[i] for i in ranking if i < len(docs)]
            return reranked
        except:
            return docs
    except:
        return docs

def format_sources_with_context(retrieved_docs):
    """
    Format sources with page/section information
    """
    sources = []
    for doc in retrieved_docs:
        source_info = { "filename": doc.metadata.get("source", "Unknown"), "file_type": doc.metadata.get("file_type", "Unknown"), "file_path": doc.metadata.get("file_path", "Unknown"), }
        
        if "batch_start" in doc.metadata:
            source_info["rows"] = f"{doc.metadata.get('batch_start', 0)}-{doc.metadata.get('batch_end', 0)}"      
        sources.append(source_info)
    return sources

def build_conversation_context(conversation_history, max_history=5):
    """
    Build context from conversation history
    Includes last N exchanges to maintain context
    """
    context = "CONVERSATION HISTORY:\n"
    
    # Get last max_history exchanges
    recent_history = conversation_history[-max_history:] if len(conversation_history) > max_history else conversation_history
    
    for exchange in recent_history:
        context += f"\nUser: {exchange['question']}\nAssistant: {exchange['answer'][:500]}...\n"
    
    context += "\n---\n"
    return context

def ask_question(vectorstore, question, conversation_history=None):
    """
    Main question answering function with improved routing and context
    
    Args:
        vectorstore: Chroma vector store
        question: User question
        conversation_history: List of previous exchanges for context
    """
    try:
        if conversation_history is None:
            conversation_history = []
        if is_analytic_question(question):
            try:
                retrieved_docs = hybrid_search(vectorstore, question, k=30)
                selected_dataset = select_best_dataset(retrieved_docs)

                if selected_dataset:
                    result = analyze_dataframe(selected_dataset, question)
                    
                    if result:
                        return { "answer": result, "sources": format_sources_with_context(retrieved_docs), "is_analytics": True}
            except Exception as e:
                print(f"Analytics routing error: {e}")
            return { "answer": "No structured dataset found for your analytics query. Try asking about specific data or uploading a CSV/Excel file.", "sources": [], "is_analytics": True }

        retrieved_docs = hybrid_search(vectorstore, question, k=10)
        
        # Rerank documents using LLM for better relevance
        retrieved_docs = rerank_documents(retrieved_docs, question, llm)
        
        # top 5 after reranking
        retrieved_docs = retrieved_docs[:5]

        if not retrieved_docs:
            return { "answer": "No relevant information found in your documents. Try uploading more documents or rephrasing your question.", "sources": [], "is_analytics": False }
        context = "\n\n".join(doc.page_content for doc in retrieved_docs)
        context = context[:15000]

        # Build conversation context for better continuity
        conversation_context = ""
        if conversation_history:
            conversation_context = build_conversation_context(conversation_history, max_history=3)
        
        prompt = f"""{conversation_context}
You are a document QA assistant. Answer questions ONLY using the provided context.

PRIMARY RULES:
- Answer ONLY using the context provided
- When answering questions about a person, provide a complete summary using all relevant information found in the context, not just the person's name.
- Separate skills, education, experience, and certifications into their proper categories when present in the context.
- Do NOT hallucinate or make up information
- If information is not in context, say: "I couldn't find this information in your documents."
- If asked about previous questions, reference them naturally from conversation history
- No guessing or filling missing values
- No making up trends, causes, or business insights
- Do not add extra commentary beyond the answer
- Prioritize accuracy over completeness
- If prior question is referenced, respond only if supported by context

CONTEXT FROM DOCUMENTS:
{context}

QUESTION:
{question}

ANSWER:"""
        response = llm.invoke(prompt)
        sources = format_sources_with_context(retrieved_docs)
        return { "answer": response.content, "sources": sources, "is_analytics": False }
    except Exception as e:
        return { "answer": f"Error processing your question: {str(e)}", "sources": [], "is_analytics": False }