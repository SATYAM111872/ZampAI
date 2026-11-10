import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")

llm = ChatGoogleGenerativeAI(
    model="gemini-flash-lite-latest",
    google_api_key=API_KEY,
    temperature=0,
)

# Stage 3 ranking. If embedding calls fail, ranking falls back to keyword overlap.
embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001",
    google_api_key=API_KEY,
)
