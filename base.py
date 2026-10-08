import os
import logging
from anthropic import Anthropic
from pydantic import ValidationError

logger = logging.getLogger("AgentBase")

class ClaudeAgent:
    def __init__(self, model="claude-3-haiku-20240307"):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Falta ANTHROPIC_API_KEY en las variables de entorno")
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def analyze(self, prompt: str, schema_class):
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1000,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )
            content = response.content[0].text
            clean_content = content.replace("```json", "").replace("```", "").strip()
            validated = schema_class.model_validate_json(clean_content)
            return validated.model_dump()
        except ValidationError as e:
            logger.error(f"Error de validación del JSON de Claude: {e}")
            raise ValueError("El bot devolvió un formato inválido")
        except Exception as e:
            logger.error(f"Error llamando a Claude: {e}")
            raise
