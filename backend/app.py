"""FS Risk Mapping: FastAPI app serving the map frontend and its API."""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="FS Risk Mapping")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config() -> dict:
    """Browser-side config. The Maps JS key is public by design; restrict it by referrer."""
    return {
        "googleMapsApiKey": os.getenv("GOOGLE_MAPS_API_KEY", ""),
        "googleMapsMapId": os.getenv("GOOGLE_MAPS_MAP_ID", "") or None,
    }


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
