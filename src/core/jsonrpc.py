from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, Field

class JsonRpcMessage(BaseModel):
    jsonrpc: str = Field(default="2.0", pattern="^2.0$")

class JsonRpcRequest(JsonRpcMessage):
    id: Union[str, int]
    method: str
    params: Optional[Dict[str, Any]] = None

class JsonRpcNotification(JsonRpcMessage):
    method: str
    params: Optional[Dict[str, Any]] = None

class JsonRpcErrorObj(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None

class JsonRpcErrorResponse(JsonRpcMessage):
    id: Union[str, int, None]
    error: JsonRpcErrorObj

class JsonRpcSuccessResponse(JsonRpcMessage):
    id: Union[str, int]
    result: Any

JsonRpcResponse = Union[JsonRpcSuccessResponse, JsonRpcErrorResponse]

# Parser utility
def parse_jsonrpc_message(data: dict) -> Union[JsonRpcRequest, JsonRpcNotification, JsonRpcSuccessResponse, JsonRpcErrorResponse]:
    if "method" in data:
        if "id" in data:
            return JsonRpcRequest(**data)
        else:
            return JsonRpcNotification(**data)
    elif "result" in data:
        return JsonRpcSuccessResponse(**data)
    elif "error" in data:
        return JsonRpcErrorResponse(**data)
    else:
        raise ValueError("Invalid JSON-RPC 2.0 payload")
