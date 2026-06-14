from fastapi import FastAPI, Request, Form, Response
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Farm Connect API is running"}

@app.post("/webhook")
async def whatsapp_webhook(
    Body: str = Form(...),
    From: str = Form(...)
):
    print(f"Message from {From}: {Body}")
    
    response = MessagingResponse()
    response.message("Welcome to Farm Connect! Reply with FARMER or LABOURER to get started.")
    
    return Response(content=str(response), media_type="application/xml")
