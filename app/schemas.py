"""请求 / 响应模型（Swagger 文档即由此生成）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Pair(BaseModel):
    """一条"备件号 × 工况"配对录入。"""

    part_no: str = Field(..., alias="partNo", description="备件号", examples=["P-1001"])
    condition_code: str = Field(
        ..., alias="conditionCode", description="工况代码", examples=["C-HOT"]
    )

    model_config = {"populate_by_name": True}


class LoadRequest(BaseModel):
    model_id: str = Field(
        ..., alias="modelId", description="车型标识", examples=["SEDAN-A1"]
    )
    pairs: list[Pair] = Field(..., min_length=1, description="备件号与工况配对，重复配对只算一条")

    model_config = {"populate_by_name": True}


class CorrectionRequest(BaseModel):
    """批量更正：plus = 标记为"有"，minus = 标记为"无"。"""

    model_id: str = Field(..., alias="modelId", examples=["SEDAN-A1"])
    plus: list[Pair] = Field(
        default_factory=list, description="标记为有（新增/恢复）的组合"
    )
    minus: list[Pair] = Field(
        default_factory=list, description="标记为无（移除）的组合"
    )

    model_config = {"populate_by_name": True}


class ExtraCombination(BaseModel):
    part_no: str = Field(..., alias="partNo")
    condition_code: str = Field(..., alias="conditionCode")

    model_config = {"populate_by_name": True}


class ModelResponse(BaseModel):
    model_id: str = Field(..., alias="modelId")
    status: str
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")
    part_count: int = Field(..., alias="partCount")
    condition_count: int = Field(..., alias="conditionCount")
    effective_pair_count: int = Field(
        ..., alias="effectivePairCount", description="两表拼回后同时生效的配对数"
    )
    parts: list[str]
    conditions: list[str]
    before: dict | None = Field(
        default=None, description="仅批量更正响应中出现，记录更正前两表规模"
    )

    model_config = {"populate_by_name": True}


class ModelSummary(BaseModel):
    model_id: str = Field(..., alias="modelId")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    part_count: int = Field(..., alias="partCount")
    condition_count: int = Field(..., alias="conditionCount")
    effective_pair_count: int = Field(..., alias="effectivePairCount")

    model_config = {"populate_by_name": True}


class ModelListResponse(BaseModel):
    models: list[ModelSummary]
