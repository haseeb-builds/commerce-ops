"""Entry point: python -m commerceops_ui.run"""
import uvicorn

if __name__ == "__main__":
    # local-only harness; binds to loopback by default
    uvicorn.run("commerceops_ui.app:app", host="127.0.0.1", port=8000, reload=False)
