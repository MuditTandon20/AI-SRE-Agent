from fastapi import FastAPI       
import time
app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/crash")
def crash():
    raise Exception("Something went wrong ")


@app.get("/slow")
def slow():
    time.sleep(5)
    return {"status": "finally done"}