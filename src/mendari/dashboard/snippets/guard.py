# AI Guard in Python: the same check as `guard.sh`, without a server. Needs `mendari`.
# CI runs this file (scripts/check_doc_snippets.py).

# step: check — Check a model's draft in Python
from mendari import Decision
from mendari.guard import check

guard = Decision.from_hub("mendari/ai-guard")
question = "My back hurts. What can I take?"
draft = "Take 2 Advil tablets of 400 mg three times a day."  # what your model wrote
result = check(guard, {"question": question, "draft": draft})
print(result.status)  # blocked: never send a blocked draft
print(result.decision.fallback)  # what to do instead
