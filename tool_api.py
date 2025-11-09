# tool_api.py
from fastapi import FastAPI, Header, HTTPException
from form_filler_tool import fill_hsh_form  # <-- your Playwright tool

API_KEY = "changeme"  # set to None to disable auth

app = FastAPI(title="HSH Form Filler Tool")

def _require_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/fill_hsh_form")
def fill_hsh_form_endpoint(data: dict, x_api_key: str | None = Header(default=None)):
    _require_key(x_api_key)
    headless = bool(data.pop("headless", True))
    slow = int(data.pop("slow_mo_ms", 0))
    timeout = int(data.pop("wait_timeout_ms", 15_000))
    return fill_hsh_form(data, headless=headless, slow_mo_ms=slow, wait_timeout_ms=timeout)
