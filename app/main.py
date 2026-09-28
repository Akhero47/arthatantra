from fastapi import FastAPI

from app.account.account_api import router as account_router
from app.api_errors import register_error_handlers
from app.transfer.transfer_api import router as transfer_router

app = FastAPI(title="Arthatantra Money Transfer API")
register_error_handlers(app)
app.include_router(account_router)
app.include_router(transfer_router)