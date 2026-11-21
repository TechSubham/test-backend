import os

# Must be set before LiteLLM is imported anywhere: use the bundled model cost map
# instead of fetching it over the network at import time (risk R14).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
