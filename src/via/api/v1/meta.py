from fastapi import APIRouter

from via import API_VERSION, __version__, crypto
from via.deps import Ctx
from via.schemas import Features, InfoOut, Limits

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@router.get("/info")
async def info(ctx: Ctx) -> InfoOut:
    """Server version, limits and enabled features. Clients use this for feature detection."""
    s = ctx.settings
    return InfoOut(
        version=__version__,
        api_version=API_VERSION,
        signup=s.signup,
        encryption=crypto.SCHEME,
        limits=Limits(
            max_file_size=s.max_file_size,
            user_quota=s.user_quota,
            max_text_length=s.max_text_length,
            default_ttl=s.default_ttl,
            max_ttl=s.max_ttl,
        ),
        features=Features(
            fcm_relay=bool(s.push_relay_url), history=s.history_days > 0, web_ui=s.web_ui
        ),
    )
