from fastapi import FastAPI

from account_api import router as account_router
from api_errors import register_error_handlers
from transfer_api import router as transfer_router

app = FastAPI(title="Arthatantra Money Transfer API")
register_error_handlers(app)
app.include_router(account_router)
app.include_router(transfer_router)