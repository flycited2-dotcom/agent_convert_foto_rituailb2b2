from config import get_mode


def test_kbt_prompt_locks_geometry_and_removes_only_supplier_watermark():
    prompt = get_mode("kbt").prompt
    assert "неизменяемым геометрическим паспортом" in prompt
    assert "превращать корпус в прямоугольный параллелепипед" in prompt
    assert "водяные знаки поставщика" in prompt
    assert "логотип производителя" in prompt
    assert "при несовпадении силуэта" in prompt
