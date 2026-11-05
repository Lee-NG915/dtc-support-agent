"""Chat routes: single-shot JSON and SSE streaming."""

from fastapi import APIRouter, Depends, Request
from sse_starlette import JSONServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_container, get_optional_staff
from app.schemas.after_sales import StaffActor
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.container import AppContainer

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    staff: StaffActor | None = Depends(get_optional_staff),
    container: AppContainer = Depends(get_container),
) -> ChatResponse:
    """Answer one turn and return the full JSON response."""
    return await container.chat_service.chat(
        payload,
        trace_id=request.state.trace_id,
        staff=staff,
    )


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    staff: StaffActor | None = Depends(get_optional_staff),
    container: AppContainer = Depends(get_container),
) -> EventSourceResponse:
    """Answer one turn as an SSE stream of node, token and answer events."""
    prepared = await container.chat_service.prepare_stream_request(payload, staff=staff)

    async def events():
        async for item in container.chat_service.stream(
            prepared,
            trace_id=request.state.trace_id,
            staff=staff,
        ):
            yield JSONServerSentEvent(item)

    return EventSourceResponse(events())
