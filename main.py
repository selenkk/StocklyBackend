from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


@app.get("/")
def home():
    return {"message": "Stockly Backend çalışıyor!"}


class ProductRequest(BaseModel):
    url: str
    size: str


@app.post("/product")
def add_product(product: ProductRequest):
    return {
        "url": product.url,
        "size": product.size,
        "status": "received"
    }
