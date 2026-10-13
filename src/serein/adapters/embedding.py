"""Explicit embedding calls using the preserved index profile and env credentials."""

import json
import os
from urllib.parse import urlparse

from serein.recall.index import Search, unit_vector


_VOYAGE_HOSTS = frozenset(('api.voyageai.com', 'ai.mongodb.com', 'us.ai.mongodb.com', 'eu.ai.mongodb.com'))
_VOYAGE_INPUT_CONTRACT = 'voyage-retrieval-v1'


def embedding_profile_contract(endpoint):
    """Version retrieval-specific inputs separately from old untyped vectors."""
    return {'input_contract': _VOYAGE_INPUT_CONTRACT} if urlparse(endpoint).hostname in _VOYAGE_HOSTS else {}


def validate_profile_contract(profile, endpoint):
    expected = embedding_profile_contract(endpoint)
    actual = {key: profile[key] for key in ('input_contract',) if key in profile}
    if actual != expected:
        raise ValueError('Embedding input contract changed; prepare a separate index before re-embedding')


def embedding_payload(model, inputs, endpoint, *, input_type, dimension=None):
    payload = {'model': model, 'input': inputs}
    if embedding_profile_contract(endpoint):
        if input_type not in ('query', 'document'):
            raise ValueError('Voyage embeddings require query or document input_type')
        payload.update(input_type=input_type, output_dtype='float', truncation=False)
        if dimension is not None:
            payload['output_dimension'] = dimension
    else:
        payload['encoding_format'] = 'float'
    return payload


def embedding_vectors(result, model, count, dimension=None, *, require_positions=False):
    """Validate provider identity, positions and vectors, including discovery replies."""
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, list) or result.get('model') != model or len(data) != count:
        raise ValueError('Embedding provider returned a different model or unexpected number of vectors')
    vectors = {}
    for row in data:
        if not isinstance(row, dict):
            raise ValueError('Embedding provider returned invalid vector data')
        position = row.get('index', 0 if count == 1 and not require_positions else None)
        if type(position) is not int or not 0 <= position < count or position in vectors:
            raise ValueError('Embedding provider returned invalid or duplicate input positions')
        vector = row.get('embedding')
        if dimension is None:
            dimension = len(vector) if isinstance(vector, list) else 0
        vectors[position] = unit_vector(vector, dimension)
    return [vectors[position] for position in range(count)]


class EmbeddingClient:
    def __init__(self, database, index, endpoint, api_key_env=None, api_key=None, tokenizer=None):
        with Search(database, index) as search:
            settings = dict(search.conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        if "embedding_profile" not in settings or "embedding_dimension" not in settings:
            raise ValueError("Semantic query requires a configured embedding profile in the index")
        self.profile = json.loads(settings["embedding_profile"])
        self.dimension = json.loads(settings["embedding_dimension"])
        url = urlparse(endpoint)
        local_http = url.scheme == 'http' and url.hostname in ('localhost','127.0.0.1','::1')
        if (url.scheme != "https" and not local_http) or url.hostname != self.profile["provider_host"] or url.username or url.password:
            raise ValueError("Embedding endpoint must use HTTPS and match the cached provider host")
        validate_profile_contract(self.profile, endpoint)
        self.endpoint, self.api_key_env = endpoint, api_key_env
        self.api_key = api_key
        from .token_window import TokenWindow
        self.window = TokenWindow(tokenizer) if tokenizer else None

    def query(self, text, *, client=None):
        prepared = self.prepare(text, self.profile["query_instruction"])
        vector = self._request(prepared, 1, input_type='query', client=client)[0]
        return {"query": text, "profile": self.profile, "embedding": vector}

    def prepare(self, text, instruction, *, kind="Query"):
        if not text.strip() or not self.dimension:
            raise ValueError("Text and a cached embedding dimension are required")
        prepared = f"Instruct: {instruction}\n{kind}: {text}" if instruction else text
        if self.window and not self.window.fits(prepared):
            raise ValueError('Embedding input exceeds token window; prepare source passages first')
        if self.window and len(prepared) > self.profile['max_chars']:
            raise ValueError('Embedding input exceeds character budget; prepare source passages first')
        return prepared[:self.profile['max_chars']]

    def documents(self, texts, *, client=None):
        """Embed body inputs in batch; response positions, not array order, own IDs."""
        if not texts:
            return []
        prepared = [self.prepare(text, self.profile["document_instruction"], kind="Document") for text in texts]
        return self._request(prepared, len(texts), input_type='document', client=client)

    def _request(self, inputs, count, *, input_type, client=None):
        import httpx
        key = self.api_key if self.api_key is not None else os.environ.get(self.api_key_env or '')
        if self.api_key is None and not key:
            raise ValueError(f"Set the configured embedding credential environment variable: {self.api_key_env}")
        payload = embedding_payload(self.profile['model'], inputs, self.endpoint,
                                    input_type=input_type, dimension=self.dimension)
        owned = client is None
        client = client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            response = client.post(self.endpoint, json=payload, headers={"Authorization": f"Bearer {key}"} if key else {})
            if response.status_code != 200:
                raise ValueError(f"Embedding provider returned HTTP {response.status_code}; response body omitted")
            return embedding_vectors(response.json(), self.profile['model'], count, self.dimension,
                                     require_positions=bool(embedding_profile_contract(self.endpoint)))
        except httpx.HTTPError:
            raise ValueError("Embedding provider request failed") from None
        finally:
            if owned:
                client.close()
