

def test_research_image_prompt_isolated_from_project_memory():
    # грабля 2026-07-09: ChatGPT рисовал ЧУЖОЙ товар (RDF-260DD вместо M514) —
    # память проекта тянет товары из предыдущих чатов; промпт должен это запрещать
    from config import RESEARCH_IMAGE_PROMPT
    assert "{{SPECS}}" in RESEARCH_IMAGE_PROMPT
    assert "предыдущих чатов" in RESEARCH_IMAGE_PROMPT
