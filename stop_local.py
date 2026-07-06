"""«Стоп всё» перед выключением ПК: желаемое состояние всех дорожек → stopped,
агенты и ботовские Chrome убиты. Вотчдог остаётся жить (он и должен), но
воскрешать ничего не будет — desired=stopped. Без этого выключить ноут
невозможно: вотчдог поднимает Chrome каждую минуту (жалоба владельца 2026-07-06).

Запуск: stop_local.bat (двойной клик) или python stop_local.py.
Включить обратно: кнопка «🚀 Запустить агента» в Telegram-боте фотоагента.
"""
from __future__ import annotations

from agent_watchdog import LANES, kill_agent, kill_chrome, set_desired_state


def main() -> None:
    lanes = LANES or [None]            # без lanes.json — legacy-режим (одна дорожка)
    for lane in lanes:
        name = lane.id if lane else "legacy"
        set_desired_state("stopped", lane)
        killed_agent = kill_agent(lane)
        killed_chrome = kill_chrome(lane)
        print(f"[{name}] stopped: агент {'убит' if killed_agent else 'не работал'}, "
              f"Chrome {'убит' if killed_chrome else 'не работал'}")
    print("Готово — вотчдог ничего не поднимет, ноут можно выключать.")


if __name__ == "__main__":
    main()
