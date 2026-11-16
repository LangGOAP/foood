"""The error body the Delivery Plus apps parse: ``{statusCode, error, message}``."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_REASONS = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
}


class ApiError(HTTPException):
    """An HTTP error with the gateway's body; ``error`` is the short reason the apps switch on."""

    def __init__(self, status_code: int, message: str, error: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.error = error or _REASONS.get(status_code, "Error")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"statusCode": exc.status_code, "error": exc.error, "message": exc.detail},
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "statusCode": exc.status_code,
                "error": _REASONS.get(exc.status_code, "Error"),
                "message": str(exc.detail),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "statusCode": 400,
                "error": "Bad Request",
                "message": str(exc.errors()[0]["msg"]),
            },
        )
