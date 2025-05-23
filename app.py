from flask import Flask, render_template, request, jsonify
import pandas as pd
import spacy
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from rank_bm25 import BM25Okapi
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Load NLP model
nlp = spacy.load("en_core_web_lg")

# Load and preprocess data
df = pd.read_csv("bio.csv")
df = df.drop_duplicates().reset_index(drop=True)

def preprocess(text, keep_keywords=False):
    text = str(text).lower().strip()
    doc = nlp(text)
    if keep_keywords:
        keywords = {chunk.text for chunk in doc.noun_chunks}
        processed = [token.lemma_ if token.lemma_ not in keywords else token.text
                     for token in doc if not token.is_stop and not token.is_punct]
    else:
        processed = [token.lemma_ for token in doc if not token.is_stop and not token.is_punct]
    return " ".join(processed)

# Prepare dataset
df['clean_questions'] = df['Question'].apply(preprocess)
df['keyword_questions'] = df['Question'].apply(lambda x: preprocess(x, keep_keywords=True))

# Initialize search systems
bm25 = BM25Okapi([q.split() for q in df['keyword_questions']])
tfidf_vectorizer = TfidfVectorizer(ngram_range=(1,3), max_features=10000)
tfidf_matrix = tfidf_vectorizer.fit_transform(df['clean_questions'])

class ConversationContext:
    def __init__(self):
        self.keyword_history = set()
        self.pending_ai_query = None
        self.last_query = None

    def update_context(self, query):
        doc = nlp(preprocess(query, keep_keywords=True))
        self.keyword_history.update([chunk.text for chunk in doc.noun_chunks])
        self.last_query = query

context = ConversationContext()

def hybrid_search(query):
    # Keyword search
    keyword_query = preprocess(query, keep_keywords=True).split()
    bm25_scores = bm25.get_scores(keyword_query)
    bm25_indices = bm25_scores.argsort()[::-1][:5]
    
    # Semantic search
    tfidf_query = tfidf_vectorizer.transform([preprocess(query)])
    tfidf_scores = cosine_similarity(tfidf_query, tfidf_matrix)[0]
    tfidf_indices = tfidf_scores.argsort()[::-1][:5]
    
    # Combine results
    combined_indices = list(set(bm25_indices.tolist() + tfidf_indices.tolist()))
    return combined_indices

def generate_answer(query):
    top_indices = hybrid_search(query)
    best_answer = None
    max_score = -1

    for idx in top_indices:
        answer = df.iloc[idx]['Answer']
        keyword_score = sum(1 for kw in context.keyword_history if kw in answer.lower())
        doc = nlp(answer)
        semantic_score = doc.similarity(nlp(query))
        total_score = (keyword_score * 0.7) + (semantic_score * 0.3)

        if total_score > max_score:
            max_score = total_score
            best_answer = answer

    if max_score < 0.4:
        context.pending_ai_query = query
        return {
            "message": "I'm still learning about this. Would you like me to ask ChatGPT?",
            "buttons": True
        }
    
    return {"message": best_answer}

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/ask', methods=['POST'])
def ask():
    user_input = request.json['message'].strip().lower()
    context.update_context(user_input)

    # Handle AI confirmation responses
    if context.pending_ai_query:
        if user_input == "__yes__":
            try:
                response = openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "You are a biology expert assistant. Answer concisely in 1-2 sentences."},
                        {"role": "user", "content": context.pending_ai_query}
                    ]
                )
                ai_response = f"[AI] {response.choices[0].message.content}"
                context.pending_ai_query = None
                return jsonify({'response': ai_response})
            
            except Exception as e:
                return jsonify({'response': f"Error accessing AI: {str(e)}"})
        
        elif user_input == "__no__":
            context.pending_ai_query = None
            return jsonify({'response': "Let's try another question!"})
        
        else:
            # If user sends regular message instead of button response
            context.pending_ai_query = None

    # Normal question processing
    response = generate_answer(user_input)
    if isinstance(response, dict) and response.get("buttons"):
        return jsonify(response)
    
    return jsonify({'response': response["message"]})

if __name__ == '__main__':
    app.run(debug=True)
