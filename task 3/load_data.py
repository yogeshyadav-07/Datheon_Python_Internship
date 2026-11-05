"""
load_data.py - create the schema and populate it with sample data.

    python load_data.py                                   # SQLite (frontier.db)
    DATABASE_URL=postgresql+psycopg2://u:p@host/db python load_data.py
"""
import logging
from datetime import date

from db_loader import DatabaseLoader

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

COMPANIES = [
    {"slug": "openai", "name": "OpenAI", "website": "https://openai.com", "country": "US", "founded_year": 2015},
    {"slug": "anthropic", "name": "Anthropic", "website": "https://anthropic.com", "country": "US", "founded_year": 2021},
    {"slug": "mistral", "name": "Mistral AI", "website": "https://mistral.ai", "country": "FR", "founded_year": 2023},
    {"slug": "sarvam", "name": "Sarvam AI", "website": "https://sarvam.ai", "country": "IN", "founded_year": 2023},
]

PRODUCTS = [
    {"product_id": "openai-gpt", "company_slug": "openai", "name": "GPT", "category": "LLM", "description": "General-purpose language models"},
    {"product_id": "anthropic-claude", "company_slug": "anthropic", "name": "Claude", "category": "LLM", "description": "AI assistant and API"},
    {"product_id": "mistral-lechat", "company_slug": "mistral", "name": "Le Chat", "category": "Assistant", "description": "Chat assistant"},
    {"product_id": "mistral-codestral", "company_slug": "mistral", "name": "Codestral", "category": "Code", "description": "Code generation model"},
    {"product_id": "sarvam-m", "company_slug": "sarvam", "name": "Sarvam-M", "category": "LLM", "description": "Indic-language LLM"},
]

PAPERS = [
    {"arxiv_id": "1706.03762", "title": "Attention Is All You Need", "published_date": date(2017, 6, 12),
     "url": "https://arxiv.org/abs/1706.03762", "company_slug": None},
    {"arxiv_id": "2310.06825", "title": "Mistral 7B", "published_date": date(2023, 10, 10),
     "url": "https://arxiv.org/abs/2310.06825", "company_slug": "mistral"},
    {"arxiv_id": "2212.08073", "title": "Constitutional AI: Harmlessness from AI Feedback", "published_date": date(2022, 12, 15),
     "url": "https://arxiv.org/abs/2212.08073", "company_slug": "anthropic"},
]


def load_all(loader: DatabaseLoader) -> None:
    """Load every table in ONE transaction, parents before children."""
    with loader.transaction() as conn:
        loader.upsert("companies", COMPANIES, conn=conn)
        loader.upsert("products", PRODUCTS, conn=conn)
        loader.upsert("papers", PAPERS, conn=conn)


if __name__ == "__main__":
    loader = DatabaseLoader()
    loader.create_schema()
    load_all(loader)
    print("Load complete.")
