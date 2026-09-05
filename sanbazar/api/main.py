# Copyright 2026 SanBazar
# SPDX-License-Identifier: Apache-2.0

"""SanBazar shopping-agent API поверх живого каталога Sanity.

    uvicorn sanbazar.api.main:app --app-dir examples --reload --port 8000

Память и корзина — в памяти процесса (не переживают рестарт/несколько
воркеров) — нормально для одного маленького процесса на Railway/Render;
апгрейд на постоянное хранилище делается позже, если понадобится.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from starlette.middleware.cors import CORSMiddleware

from commerce_common.memory import InMemoryMemoryStore
from demo_common import MemorySeeder, build_storefront_host, load_demo_env
from shopping_agent_runtime import ShoppingAgent

from .agent_config import build_shopping_config
from .sanbazar_backend import SanBazarBackend

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = EXAMPLE_ROOT.parent
load_demo_env(EXAMPLE_ROOT)  # no-op in production: real env vars are already set

backend = SanBazarBackend()
asyncio.run(backend._ensure_fresh())  # load the catalog once before the app starts serving
agent = ShoppingAgent(
    backend=backend,
    skills_dir=PROJECT_ROOT / "shopping-agent-skills",
    config=build_shopping_config(),
    memory_store=InMemoryMemoryStore(),
)

host = build_storefront_host(
    title="SanBazar shopping-agent API",
    example_root=EXAMPLE_ROOT,
    backend=backend,
    agent=agent,
    memory_seeder=MemorySeeder(EXAMPLE_ROOT / "data" / "memory-seed.json"),
)
app = host.app
# demo_common's build_app only allows localhost origins (it assumes a demo running
# next to its own web app on the same machine). Real deployment: the site
# (sanbazar.com) and this API live on different domains, so CORS needs the site's
# real origin(s) — set ALLOWED_ORIGINS (comma-separated) on Railway/Render.
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.user_middleware = [m for m in app.user_middleware if m.cls is not CORSMiddleware]
    app.middleware_stack = None  # force Starlette to rebuild it with the line below
    app.add_middleware(
        CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"]
    )
