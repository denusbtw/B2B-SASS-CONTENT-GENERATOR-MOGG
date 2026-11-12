from fastapi import FastAPI

from factory.api import mount

app = FastAPI()


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok"}


mount(app)
