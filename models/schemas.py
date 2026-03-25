"""Pydantic models for API request/response schemas."""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# --- User Context (from X-User-Context header) ---

class UserScope(BaseModel):
    store_ids: Optional[List[int]] = None  # None = admin (all stores), [] = no access
    department_codes: List[str] = Field(default_factory=list)


class UserContext(BaseModel):
    user_id: int
    source_system: str = "angel-kpi"
    roles: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    scope: UserScope = Field(default_factory=UserScope)
    locale: str = "it"


# --- Chat API ---

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[str] = None
    context_hint: Optional[str] = None  # e.g. "bi_analysis", "bonus_explain"
    page_context: Optional[dict] = None  # injected by frontend (current page info)
    stream: bool = True


class ChatMessage(BaseModel):
    message_id: str
    role: str  # "user" | "assistant"
    content: str
    timestamp: datetime
    data_sources: Optional[List[str]] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None


class ChatResponse(BaseModel):
    conversation_id: str
    message: ChatMessage


# --- Usage ---

class UsageStats(BaseModel):
    period: str
    total_requests: int = 0
    total_cost_usd: float = 0.0
    avg_duration_ms: int = 0
    unique_users: int = 0


# --- Feedback ---

class FeedbackRequest(BaseModel):
    message_id: str
    rating: str = Field(..., pattern="^(helpful|not_helpful|wrong|harmful)$")
    accuracy: Optional[str] = Field(None, pattern="^(correct|incorrect|partial)$")
    resolved: Optional[bool] = None
    comment: Optional[str] = None


# --- Health ---

class HealthResponse(BaseModel):
    status: str
    version: str
    cli_available: bool
    uptime_seconds: int


# --- Prompt Versioning ---

class PromptVersionCreate(BaseModel):
    version_tag: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=10)
    description: Optional[str] = None
    activate: bool = False  # if True, immediately set as active


class PromptVersionResponse(BaseModel):
    id: int
    version_tag: str
    description: Optional[str]
    is_active: bool
    created_at: str
    created_by: Optional[str]
