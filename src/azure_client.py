"""Thin wrapper around Azure OpenAI for chat completions and embeddings."""
from openai import AzureOpenAI
from src import config

_client = AzureOpenAI(
    azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
    api_key=config.AZURE_OPENAI_API_KEY,
    api_version=config.AZURE_OPENAI_API_VERSION,
)


def chat(system: str, user: str, json_mode: bool = False, temperature: float = 0.2) -> str:
    """Single-turn chat completion. Returns raw text (or JSON text if json_mode=True)."""
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = _client.chat.completions.create(
        model=config.AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        **kwargs,
    )
    return resp.choices[0].message.content


def embed(text: str) -> list[float]:
    """Returns an embedding vector for the given text."""
    text = text.replace("\n", " ").strip()
    resp = _client.embeddings.create(
        model=config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        input=text,
    )
    return resp.data[0].embedding
