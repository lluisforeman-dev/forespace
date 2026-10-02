"""EigenSearch web-search wiring — ':online' suffix vs explicit tools array."""
from ingest.tasks.research import _eigensearch_web_tools


def test_online_suffix_needs_no_tools():
    assert _eigensearch_web_tools('openai/gpt-5.6-luna:online') is None


def test_plain_model_gets_web_search_tool():
    tools = _eigensearch_web_tools('openai/gpt-5.6-luna')
    assert tools == [{'type': 'openrouter:web_search'}]


def test_free_model_gets_web_search_tool():
    tools = _eigensearch_web_tools('deepseek/deepseek-chat-v3.1:free')
    assert tools == [{'type': 'openrouter:web_search'}]
