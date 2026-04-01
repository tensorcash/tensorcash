"""Unit tests for default_prompt_generator"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from components.default_prompt_generator import IntelligentPromptGenerator


def test_generate_prompt_and_lists():
    gen = IntelligentPromptGenerator()
    p = gen.generate_prompt()
    assert isinstance(p, str)
    assert len(p) > 0

    # List and add template
    tpls = gen.list_templates()
    assert isinstance(tpls, list) and tpls
    gen.add_template("Hello {role_1}")
    assert "Hello {role_1}" in gen.list_templates()

    # Word categories
    cats = gen.list_categories()
    assert isinstance(cats, list) and cats
    gen.add_word("role_1", "new role")
    assert "new role" in gen.list_words("role_1")

    # Use explicit template to exercise fallback for unknown fields
    p2 = gen.generate_prompt("X {unknown_field} Y")
    assert "<missing_unknown_field>" in p2

