"""What Sentinel sends to Devin, and what it makes of the answers.

`playbooks.py` decides *what* to ask for — the playbook, the ACU cap, the prompt and the tags.
`client.py` and `schemas.py` carry it over the v3 API and give the result a type. Nothing here
decides *whether* to ask; that is the worker's and the poller's job.
"""
