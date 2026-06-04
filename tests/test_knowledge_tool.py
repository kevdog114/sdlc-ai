import sys
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.knowledge_tool import (
    store_insight,
    query_knowledge,
    list_topics,
    _sanitize_filename,
    _keyword_score,
    _parse_insight,
    KNOWLEDGE_DIR,
)


def _mock_query_llm_success(content="Short summary."):
    """Return a mock query_llm that succeeds with the given content."""
    mock = MagicMock()
    mock.return_value = {"success": True, "content": content}
    return mock


class TestSanitizeFilename:
    def test_normal_topic(self):
        assert _sanitize_filename("API Design") == "api-design"

    def test_special_characters_removed(self):
        assert _sanitize_filename("Hello, World! @#$") == "hello-world"

    def test_underscores_become_dashes(self):
        assert _sanitize_filename("my_topic") == "my-topic"

    def test_empty_becomes_untitled(self):
        assert _sanitize_filename("") == "untitled"

    def test_only_special_chars(self):
        assert _sanitize_filename("@#$%^") == "untitled"


class TestKeywordScore:
    def test_exact_match(self):
        insight = {"topic": "API Design", "summary": "REST API", "content": "REST API details"}
        score = _keyword_score("REST API", insight)
        assert score == 1.0

    def test_partial_match(self):
        insight = {"topic": "API Design", "summary": "REST API", "content": "some details"}
        score = _keyword_score("REST GraphQL", insight)
        assert 0.0 < score < 1.0

    def test_no_match(self):
        insight = {"topic": "API Design", "summary": "REST", "content": "nothing"}
        score = _keyword_score("docker kubernetes", insight)
        assert score == 0.0

    def test_empty_query(self):
        insight = {"topic": "API", "summary": "x", "content": "y"}
        score = _keyword_score("", insight)
        assert score == 0.0

    def test_empty_insight(self):
        insight = {"topic": "", "summary": "", "content": ""}
        score = _keyword_score("anything", insight)
        assert score == 0.0


class TestParseInsight:
    def test_valid_file(self, tmp_path):
        content = (
            "---\n"
            "topic: Test Topic\n"
            "summary: A summary line\n"
            "created_at: 2026-01-01T00:00:00\n"
            "---\n\n"
            "Some insight body text."
        )
        f = tmp_path / "test.md"
        f.write_text(content)
        result = _parse_insight(f)
        assert result["topic"] == "Test Topic"
        assert result["summary"] == "A summary line"
        assert "insight body" in result["content"]

    def test_missing_metadata(self, tmp_path):
        f = tmp_path / "minimal.md"
        f.write_text("Just plain text.")
        result = _parse_insight(f)
        assert result["topic"] == ""
        assert result["content"] == "Just plain text."

    def test_nonexistent_file(self):
        result = _parse_insight(Path("/nonexistent/file.md"))
        assert result == {}


class TestStoreInsight:
    def test_successful_store(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    result = store_insight("API Design", "Use RESTful endpoints with JSON payloads.")
        assert result["success"] is True
        assert "error" not in result or result["error"] is None
        assert "Insight stored:" in result["output"]
        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) == 1

    def test_store_creates_markdown_with_frontmatter(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success("Summary here.")):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("Testing", "Run pytest.")
        md_file = list(tmp_path.glob("*.md"))[0]
        text = md_file.read_text()
        assert "---" in text
        assert "topic: Testing" in text
        assert "Summary here." in text
        assert "Run pytest." in text

    def test_empty_topic_fails(self):
        with patch('tools.knowledge_tool.append_event'):
            result = store_insight("", "Some content.")
        assert result["success"] is False
        assert result["error"] and "empty" in result["error"].lower()

    def test_empty_content_fails(self):
        with patch('tools.knowledge_tool.append_event'):
            result = store_insight("Topic", "")
        assert result["success"] is False
        assert result["error"] and "empty" in result["error"].lower()

    def test_duplicate_topic_appends_counter(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("Topic", "First.")
                    store_insight("Topic", "Second.")
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 2

    def test_llm_failure_fallback_summary(self, tmp_path):
        """When LLM fails, summary falls back to truncated content."""
        mock_fail = MagicMock()
        mock_fail.return_value = {"success": False, "error": "llm down"}
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', mock_fail):
                with patch('tools.knowledge_tool.append_event'):
                    result = store_insight("Fallback", "A longer piece of content that gets truncated.")
        assert result["success"] is True


class TestQueryKnowledge:
    def test_retrieves_stored_insight(self, tmp_path):
        """Store an insight, then query for it."""
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("Database", "PostgreSQL is the primary database.")
                    result = query_knowledge("PostgreSQL database")
        assert result["success"] is True
        assert "PostgreSQL" in result["output"] or "Database" in result["output"]

    def test_empty_query_fails(self):
        with patch('tools.knowledge_tool.append_event'):
            result = query_knowledge("")
        assert result["success"] is False
        assert result["error"] and "empty" in result["error"].lower()

    def test_no_insights_stored(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.append_event'):
                result = query_knowledge("anything")
        assert result["success"] is True
        assert "No insights stored" in result["output"]

    def test_returns_top_3_results(self, tmp_path):
        """Store 5 insights, verify query returns at most 3."""
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    for i in range(5):
                        store_insight(f"Topic-{i}", f"Content for topic number {i}.")
                    result = query_knowledge("topic")
        assert result["success"] is True
        # Count "### Result" headers
        count = result["output"].count("### Result")
        assert count <= 3

    def test_keyword_match_ranking(self, tmp_path):
        """More relevant insight should appear first."""
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("Docker", "Docker container orchestration details.")
                    store_insight("Lunch", "Eat sandwiches at noon.")
                    result = query_knowledge("Docker container")
        assert result["success"] is True
        output = result["output"]
        # Docker result should appear before Lunch result
        docker_pos = output.find("Docker")
        lunch_pos = output.find("Lunch")
        assert docker_pos < lunch_pos

    def test_semantic_fallback_used_when_keyword_zero(self, tmp_path):
        """When keyword score is 0 for all, LLM semantic fallback kicks in."""
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm') as mock_llm:
                mock_llm.return_value = {"success": True, "content": "Short summary."}
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("Alpha", "Content about alpha.")
                    # Query with no keyword overlap
                    result = query_knowledge("xyz nonexistent term")
        # The query should still succeed (semantic fallback)
        assert result["success"] is True


class TestListTopics:
    def test_returns_stored_topics(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("API Design", "REST endpoints.")
                    store_insight("Database", "PostgreSQL.")
                    result = list_topics()
        assert result["success"] is True
        assert "API Design" in result["topics"]
        assert "Database" in result["topics"]

    def test_no_topics_when_empty(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.append_event'):
                result = list_topics()
        assert result["success"] is True
        assert result["topics"] == []

    def test_duplicate_topics_deduplicated(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event'):
                    store_insight("API", "First.")
                    store_insight("API", "Second.")
                    result = list_topics()
        assert result["success"] is True
        assert result["topics"].count("API") == 1

    def test_append_event_called(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.append_event') as mock_event:
                list_topics()
            mock_event.assert_called_once()
            assert mock_event.call_args[0][0] == "tool:knowledge_access"
            assert mock_event.call_args[0][1]["action"] == "list_topics"


class TestStoreInsightLogging:
    def test_success_logs_event(self, tmp_path):
        with patch('tools.knowledge_tool.KNOWLEDGE_DIR', tmp_path):
            with patch('tools.knowledge_tool.query_llm', _mock_query_llm_success()):
                with patch('tools.knowledge_tool.append_event') as mock_event:
                    store_insight("Logging", "Test content.")
                mock_event.assert_called_once()
                payload = mock_event.call_args[0][1]
                assert payload["action"] == "store"
                assert payload["success"] is True
                assert payload["topic"] == "Logging"

    def test_failure_logs_event(self):
        with patch('tools.knowledge_tool.append_event') as mock_event:
            store_insight("", "content")
            mock_event.assert_called_once()
            payload = mock_event.call_args[0][1]
            assert payload["success"] is False
