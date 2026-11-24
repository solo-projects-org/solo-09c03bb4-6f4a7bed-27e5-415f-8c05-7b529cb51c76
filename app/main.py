"""FastAPI 入口。

接口一览
========
* ``GET  /``                         健康检查
* ``POST /api/v1/models/load``       按车型录入配对并拆成备件表/工况表
* ``POST /api/v1/models/correct``    已入库车型成批更正组合有无
* ``GET  /api/v1/models/{model_id}`` 一次返回该车型同时生效的两张表
* ``GET  /api/v1/models``            车型清单
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import service
from .database import init_db
from .schemas import (
    CorrectionRequest,
    LoadRequest,
    ModelListResponse,
    ModelResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="售后数仓 · 车型适配拆库服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "把车型适配明细拆成**备件表**与**工况表**分别存储，不留逐对明细；"
        "仅当两表能原样拼回录入配对（配对集合为完整矩形）时才入库。"
        "核定只做 COUNT 聚合，不遍历两表全部配对。"
    ),
)


@app.get("/", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/models/load",
    response_model=ModelResponse,
    tags=["models"],
    summary="按车型录入备件号-工况配对并拆库入库",
    responses={
        409: {"description": "车型已入库"},
        422: {
            "description": "配对非完整矩形：返回一个会被多拼出的组合示例，原数据照常可查",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "配对集合不是完整矩形，拆成两表后会拼出拟入库数据中没有的组合",
                            "extraCombinationExample": {
                                "partNo": "P-1002",
                                "conditionCode": "C-COLD",
                            },
                        }
                    }
                }
            },
        }
    },
)
def load_model(payload: LoadRequest) -> ModelResponse:
    return service.load_model(payload.model_id, payload.pairs)


@app.post(
    "/api/v1/models/correct",
    response_model=ModelResponse,
    tags=["models"],
    summary="已入库车型成批更正组合有无（整批核定，通过才一起更新两表）",
    responses={
        404: {"description": "车型不存在"},
        422: {
            "description": "整批更正后核定失败：返回一个会被多拼出的组合，两表保持原样",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "按整批更正后的结果核定失败，两表会拼出更正后集合中不存在的组合，本批已整体拒绝",
                            "extraCombinationExample": {
                                "partNo": "P-1001",
                                "conditionCode": "C-DUST",
                            },
                        }
                    }
                }
            },
        }
    },
)
def correct_model(payload: CorrectionRequest) -> ModelResponse:
    return service.apply_correction(payload.model_id, payload.plus, payload.minus)


@app.get(
    "/api/v1/models/{model_id}",
    response_model=ModelResponse,
    tags=["models"],
    summary="取数：一次返回该车型同时生效的备件表与工况表",
)
def get_model(model_id: str) -> ModelResponse:
    return service.get_model(model_id)


@app.get(
    "/api/v1/models",
    response_model=ModelListResponse,
    tags=["models"],
    summary="已入库车型清单",
)
def list_models() -> ModelListResponse:
    return service.list_models()
