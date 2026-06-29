"""Клиентский хелпер агента: спросить VPS, активен ли этот воркер (failover-лиз)."""
from __future__ import annotations
import logging

log = logging.getLogger("remote_agent")


async def claim_lease(client, api_url: str, token: str,
                      worker_id: str, priority: int) -> bool:
    """True = можно брать задачи. Пустой worker_id → всегда True (обратная
    совместимость: десктоп без WORKER_ID работает как раньше). Ошибка лиза/сети
    → True (короткий блип не должен стопорить основной воркер)."""
    if not worker_id:
        return True
    try:
        r = await client.post(
            f"{api_url}/api/worker/lease",
            headers={"x-agent-token": token},
            data={"worker_id": worker_id, "priority": priority},
        )
        r.raise_for_status()
        return bool((r.json() or {}).get("active", True))
    except Exception as e:  # noqa: BLE001
        log.warning("lease недоступен (%s) — продолжаю как active.", e)
        return True
