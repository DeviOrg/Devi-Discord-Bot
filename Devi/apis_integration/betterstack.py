import aiohttp

from paths import env_var, env_int
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cogs.ai.client import AiClient

API_HOST = env_var("DASHBOARD_API_HOST", "127.0.0.1")
API_PORT = env_int("DASHBOARD_API_PORT", 8765)

HEARTBEAT_URL = "https://uptime.betterstack.com/api/v1/heartbeat/"

BOT_KEY = env_var("HEARTBEAT_BOT_BETTERSTACK_KEY")
HEARTBEAT_BOT_URL = HEARTBEAT_URL + BOT_KEY if BOT_KEY else None

API_KEY = env_var("HEARTBEAT_API_BETTERSTACK_KEY")
HEARTBEAT_API_URL = HEARTBEAT_URL + API_KEY if API_KEY else None

AI_KEY = env_var("HEARTBEAT_AI_BETTERSTACK_KEY")
HEARTBEAT_AI_URL = HEARTBEAT_URL + AI_KEY if AI_KEY else None

API_CHECK_HOST = ("127.0.0.1" if API_HOST in ("0.0.0.0", "::") else API_HOST)


class HeartbeatError(Exception):
    """Heartbeat send error"""

async def send_heartbeat():
    if not BOT_KEY:
        raise HeartbeatError("Bot Heartbeat send error: HEARTBEAT_BOT_BETTERSTACK_KEY not set.")

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(HEARTBEAT_BOT_URL) as response:
                response.raise_for_status()

    except Exception as e:
        raise HeartbeatError("Bot heartbeat send error: " + str(e)) from e


async def send_api_heartbeat():
    if not API_HOST or not API_PORT:
        raise HeartbeatError("API heartbeat error: API_HOST or API_PORT not configured.")

    if not API_KEY:
        raise HeartbeatError("API heartbeat send error: HEARTBEAT_API_BETTERSTACK_KEY not set.")

    try:
        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{API_CHECK_HOST}:{API_PORT}/v1/ping") as response:
                if response.status != 200:
                    raise RuntimeError(f"Bot API returned HTTP {response.status}")

            async with session.get(HEARTBEAT_API_URL) as response:
                response.raise_for_status()

    except Exception as e:
        raise HeartbeatError("API heartbeat send error: " + str(e)) from e

async def send_ai_service_heartbeat(ai_client: "AiClient", use_tokens: bool = False):
    if not HEARTBEAT_AI_URL:
        raise HeartbeatError("AI heartbeat error: HEARTBEAT_AI_BETTERSTACK_KEY not set.")

    try:
        if not ai_client.configured:
            raise RuntimeError("AI_API_KEY is not configured")

        remaining = ai_client.blocked_for()
        if remaining:
            raise RuntimeError(f"AI provider is blocked for {remaining}s")

        if use_tokens:
            # Real generation test (uses tokens).
            result = await ai_client.chat(
                messages=[
                    {
                        "role": "user",
                        "content": "Reply with exactly: OK"
                    }
                ],
                max_tokens=8,
            )

            if not result.text.strip():
                raise RuntimeError("AI provider returned an empty response")

        else:
            # Check available models without generation.
            provider = ai_client.provider
            models_url = f"{provider.base_url.rstrip('/')}/models"

            headers = {
                "Authorization": f"Bearer {ai_client.api_key}",
                "Accept": "application/json",
            }

            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(models_url, headers=headers) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)

            if not isinstance(data, dict):
                raise RuntimeError("Invalid AI models response")

            models = data.get("data")

            if not models:
                models = data.get("output", {}).get("models")

            if not isinstance(models, list):
                raise RuntimeError("Invalid AI models list")

            available_models = {
                str(model.get("id") or model.get("model")).casefold()
                for model in models
                if isinstance(model, dict)
                and (model.get("id") or model.get("model"))
            }

            required_models = {
                provider.text_model,
                provider.vision_model,
            }

            missing = required_models - available_models

            if missing:
                raise RuntimeError("AI models unavailable: " + ", ".join(sorted(missing)))

    except Exception as e:
        try:
            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{HEARTBEAT_AI_URL}/fail") as response:
                    response.raise_for_status()

        except Exception as report_error:
            raise HeartbeatError(f"AI check failed: {e}; failed to report status: {report_error}") from e

        raise HeartbeatError(f"AI service check error: {e}") from e

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(HEARTBEAT_AI_URL) as response:
                response.raise_for_status()

    except Exception as e:
        raise HeartbeatError(f"AI heartbeat send error: {e}") from e