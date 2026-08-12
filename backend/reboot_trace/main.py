import uvicorn
from .api import create_app

app = create_app()

def run() -> None:
    uvicorn.run("reboot_trace.main:app",host="0.0.0.0",port=8080)
