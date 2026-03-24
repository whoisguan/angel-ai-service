"""Output sanitization — PII detection and removal.

Runs on every AI response before returning to the user.
"""

import re
from typing import List, Tuple


# PII patterns (Italian-focused)
PII_PATTERNS: List[Tuple[str, str]] = [
    # Codice Fiscale (Italian tax ID)
    (r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", "CODICE_FISCALE"),
    # IBAN (Italian)
    (r"\bIT\d{2}[A-Z]\d{22}\b", "IBAN"),
    # Italian phone numbers
    (r"\b\+?39\s?\d{3}\s?\d{6,7}\b", "PHONE"),
    # Email addresses
    (r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", "EMAIL"),
    # IP addresses (internal)
    (r"\b(?:192\.168\.|10\.)\d{1,3}\.\d{1,3}\b", "INTERNAL_IP"),
]

# Patterns that suggest prompt injection in output
INJECTION_OUTPUT_PATTERNS = [
    r"system\s*prompt",
    r"ANTHROPIC_API_KEY",
    r"DATABASE_URL",
    r"SERVICE_TOKEN",
    r"password\s*[:=]",
]


def detect_pii(text: str) -> List[dict]:
    """Detect PII in text. Returns list of findings."""
    findings = []
    for pattern, pii_type in PII_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            findings.append({
                "type": pii_type,
                "start": match.start(),
                "end": match.end(),
                "value": match.group(),
            })
    return findings


def redact_pii(text: str) -> str:
    """Remove PII from text, replacing with type labels."""
    for pattern, pii_type in PII_PATTERNS:
        text = re.sub(pattern, f"[{pii_type}_REDACTED]", text, flags=re.IGNORECASE)
    return text


def check_injection_leaks(text: str) -> bool:
    """Check if AI output contains signs of prompt injection success."""
    for pattern in INJECTION_OUTPUT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def sanitize_output(text: str) -> str:
    """Full sanitization pipeline for AI output."""
    # 1. Check for injection leaks
    if check_injection_leaks(text):
        return "[Response filtered due to security policy]"

    # 2. Redact any PII that slipped through
    text = redact_pii(text)

    return text
