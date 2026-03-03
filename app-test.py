from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Hola, soy tu LLM de prueba"}