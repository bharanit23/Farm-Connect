from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Farm Connect API is running"}
