"""The HTTP surface: the webhook ingress, the analytics reads, and the health endpoint.

Each module here exports an `APIRouter` and registers nothing. `main.py` is the sole owner of
registration, so two routers can be built in parallel without either knowing the other exists.
"""
