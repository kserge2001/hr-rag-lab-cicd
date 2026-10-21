"""Turn text into vectors with Amazon Titan Text Embeddings.

Questions and documents must always be embedded by the same model, and the search
index is built with the size from config.EMBEDDING_SIZE. Change the model and you
have to rebuild the index, because the old vectors are the wrong shape.
"""

import json
from functools import cache

from app import config
from app.telemetry import observed, update_current_span


@cache
def bedrock():
    import boto3

    return boto3.client("bedrock-runtime", region_name=config.AWS_REGION)


@observed("Titan embedding", "embedding")
def embed(texts):
    """Return one vector (a list of numbers) per text."""
    vectors = []
    input_tokens = 0
    for text in texts:  # Titan embeds one text per request
        response = bedrock().invoke_model(
            modelId=config.EMBEDDING_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputText": text,
                "dimensions": config.EMBEDDING_SIZE,
                "normalize": True,
            }),
        )
        body = json.loads(response["body"].read())
        vectors.append(body["embedding"])
        input_tokens += body.get("inputTextTokenCount", 0)

    # Tracing: which model turned which text into a vector, and how many tokens it read.
    update_current_span(
        input={"texts": texts},
        model=config.EMBEDDING_MODEL,
        model_parameters={"dimensions": config.EMBEDDING_SIZE, "normalize": True},
        usage={"input": input_tokens},
        metadata={"texts": len(texts), "dimensions": config.EMBEDDING_SIZE},
    )
    return vectors
