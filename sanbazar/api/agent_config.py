# Copyright 2026 SanBazar
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from shopping_agent import ShoppingAgentConfig


def build_shopping_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name="SanBazar",
        assistant_name="Консультант SanBazar",
        brand_voice="дружелюбный, по делу, честно про то, чего нет",
        domain_search_notes=(
            "Оптовый склад сантехники: смесители, сифоны, санфаянс, душевые системы, "
            "инсталляции, комплектующие. У сифонов и смесителей есть подкатегории по "
            "назначению (для кухни/ванны/раковины/унитаза и т.д.) в attributes.subcategory."
        ),
    )
