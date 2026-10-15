"""Local testkit: the email pipeline end to end on synthetic mail, with a web UI.

Local only. It refuses to run unless DEPLOYMENT_ENVIRONMENT=local, uses the
fixture Graph backend and fake screener/classifier/embedder, and binds to
127.0.0.1. Nothing here is deployed.
"""
