"""Instrumented Dashboard entry point for Render and local Uvicorn.

Use ``uvicorn observed:app`` from dashboard/ or
``uvicorn dashboard.observed:app`` from the repository root.
The original application loads its environment and retains all routes/lifecycle.
"""
if __package__:
    from .main import app as application
    from .observability import LokiEmitter, ObserveASGI
else:
    from main import app as application
    from observability import LokiEmitter, ObserveASGI

emitter = LokiEmitter("job-agent-dashboard")
app = ObserveASGI(application, emitter)
