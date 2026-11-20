"""``python manage.py expense_ai_check`` - one round trip to the model, from where the code runs.

Moved from Flask's ``flask expense-ai check`` (2026-11-04). It sends a tiny generated image, never a
customer receipt, and prints the route (Vertex AI: its project and region), the models (``ai_policy``'s, in order), which one answered,
the latency and the token usage, so "is this
configured correctly" is a fact rather than a guess. Exits 1 when the model did not answer.
"""

from __future__ import annotations

import io

from django.core.management.base import BaseCommand, CommandError
from minty_shared import ai, ai_prompt
from minty_shared.django import ai_allowance
from PIL import Image

from petty_cash.services import receipt_reader as rr

_CONTEXT = {
    "entity": {"id": "check", "name": "Connectivity check", "country": "HK", "currency": "HKD"},
    "accounts": [{"id": "acc-1", "code": "5100", "name": "Courier Expense"}],
    "contacts": [{"id": "con-1", "name": "SF Express"}],
}


class Command(BaseCommand):
    help = "One round-trip call to the receipt reader's model (a generated image, no customer data)."

    def handle(self, *args, **options):
        self.stdout.write("route            : Vertex AI")
        self.stdout.write(f"project          : {ai.project() or '- (GOOGLE_CLOUD_PROJECT is not set)'}")
        policy = ai_allowance.load_policy()
        if policy is None:
            raise CommandError("No usable ai_policy row - the readers are OFF and there is no model to check.")
        config = policy.reader(ai_allowance.READER_RECEIPT)
        models = policy.models
        self.stdout.write(f"location         : {config.location}")
        self.stdout.write(f"receipt reader   : {'on' if config.enabled else 'off'}")
        self.stdout.write(f"models           : {', '.join(models)}")
        self.stdout.write(f"thinking_level   : {config.thinking_level}")

        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (255, 255, 255)).save(buffer, format="PNG")
        instruction, prefix, aliases = rr.prompt(_CONTEXT)
        result = ai.read(buffer.getvalue(), "image/png", config=config, instruction=instruction,
                         prefix_text=prefix, closing_text=ai_prompt.CLOSING, reply_model=rr.Reading,
                         validate=lambda parsed: rr.validate(parsed, _CONTEXT, aliases), models=models)

        audit = result.audit
        self.stdout.write(f"answered by      : {audit.get('model_answered') or '-'}")
        self.stdout.write(f"attempts         : {result.attempts_text()}")
        self.stdout.write(f"latency_ms       : {audit.get('latency_ms')}")
        self.stdout.write(f"request id       : {audit.get('provider_request_id')}")
        self.stdout.write(f"tokens in / out  : {audit.get('input_tokens')} / {audit.get('output_tokens')}")
        # Zero across repeated same-entity calls: the prefix is under the cache minimum or unstable.
        self.stdout.write(f"cached tokens    : {audit.get('cached_tokens')}")
        self.stdout.write(f"estimated cost   : {audit.get('estimated_cost')}")
        if result.reason not in (None, ai.REASON_NO_USABLE_FIELD):
            raise CommandError(f"FAILED: {result.reason}")
        self.stdout.write(self.style.SUCCESS(
            "Round trip OK - the model answered and the reply passed the schema. "
            "(A blank image yields no usable field, as expected.)"
        ))
