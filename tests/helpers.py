"""Shared test fakes for the 2.0 pipeline."""
from kickass_loop_engineer.providers.base import Provider, ProviderResult


class FakeProvider(Provider):
    """Returns queued canned responses; records prompts for assertions."""

    name = "fake"

    def __init__(self, responses, model="fake-model", cost_usd=0.0):
        self.responses = list(responses)
        self.calls = []
        self.model = model
        self.cost_usd = cost_usd

    def complete(self, system, user):
        self.calls.append((system, user))
        text = self.responses.pop(0) if self.responses else ""
        return ProviderResult(text=text, tokens=10, cost_usd=self.cost_usd, model=self.model)
