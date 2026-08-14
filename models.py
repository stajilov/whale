import os

from dotenv import load_dotenv
from litellm import completion

load_dotenv()


DEFAULT_PROVIDER = "openai"


def completion_default(prompt: str) -> str | None:
    response = completion(
        model=f"{DEFAULT_PROVIDER}/{os.environ['OPENAI_MODEL']}",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
