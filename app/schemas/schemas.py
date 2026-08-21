from pydantic import BaseModel, Field


class RequestData(BaseModel):
    upd_file: str = Field(...)
    registry_file: str = Field(None)
    protocol_file: str = Field(None)


class ResponseData(BaseModel):
    status: str
    content: str
