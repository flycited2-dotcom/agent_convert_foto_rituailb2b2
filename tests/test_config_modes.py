

def test_research_image_prompt_isolated_from_project_memory():
    # грабля 2026-07-09: ChatGPT рисовал ЧУЖОЙ товар (RDF-260DD вместо M514) —
    # память проекта тянет товары из предыдущих чатов; промпт должен это запрещать
    from config import RESEARCH_IMAGE_PROMPT
    assert "{{SPECS}}" in RESEARCH_IMAGE_PROMPT
    assert "предыдущих чатов" in RESEARCH_IMAGE_PROMPT


def test_card_prompts_isolated_from_project_memory():
    # грабля 2026-07-10: на карточках kbt/mcp ChatGPT рисовал бренд HOMELINE
    # (из памяти проекта) вместо Midea/Gorenje из задания — карточные промпты
    # должны запрещать бренды/модели из предыдущих чатов и памяти проекта
    from config import get_mode
    for key in ("kbt", "mcp"):
        prompt = get_mode(key).prompt
        assert "памяти проекта" in prompt, key
        assert "предыдущих чатов" in prompt, key
