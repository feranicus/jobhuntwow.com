import os
from dotenv import load_dotenv
load_dotenv()

# DigitalOcean Serverless Inference (OpenAI-compatible)
DO_BASE_URL = os.getenv("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
DO_KEY      = os.getenv("DO_INFERENCE_KEY", "")          # doo_v1_...  (your model access key)
QWEN_MODEL  = os.getenv("QWEN_MODEL", "")                # exact model id from /api/models, e.g. Qwen 3.5 slug
DATA_DIR    = os.getenv("DATA_DIR", "/data")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

HERMES_SYSTEM = (
    "You are Hermes, the JobHuntWOW job-search agent. You help the user find roles, "
    "write TRUTH-CHECKED resumes and cover letters (never invent experience), drive the real "
    "Apply flow on ATS platforms (Workday, Taleo, SuccessFactors, Personio, HiBob), and handle "
    "recruiter emails up to the interview. You always ask for confirmation before any irreversible "
    "action (submitting an application, sending a message). Be concise, practical and honest."
)
