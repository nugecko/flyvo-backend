from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Flyvo backend is running"}

@app.get("/health")
def health():
    return {"status": "ok"}
