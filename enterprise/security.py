"""Only sanitized business data may enter the database or browser responses."""
import os
import re


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'api.?key|authorization|password|secret|token$', k, re.I)
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        key = os.getenv('DEEPSEEK_API_KEY')
        if key:
            value = value.replace(key, '[REDACTED]')
        return re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[REDACTED]', value)
    return value
