import os
from time import perf_counter

from dotenv import load_dotenv
from litellm import completion

from logger import logger

load_dotenv()


DEFAULT_PROVIDER = "openai"


def completion_default(prompt: str) -> str | None:
    started_at = perf_counter()
    model = f"{DEFAULT_PROVIDER}/{os.environ['OPENAI_MODEL']}"
    logger.info("completion request", model=model, prompt=prompt)

    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        choice = response.choices[0]
        content = choice.message.content
        usage = getattr(response, "usage", None)

        logger.info(
            "completion response",
            model=model,
            response=content,
            response_id=getattr(response, "id", None),
            finish_reason=getattr(choice, "finish_reason", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        return content
    except Exception as exc:
        logger.error(
            "completion error",
            model=model,
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
            exc_info=True,
        )
        raise
