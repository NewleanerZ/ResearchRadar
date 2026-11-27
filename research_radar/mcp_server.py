"""Read-only MCP Server exposing ResearchRadar search, hybrid RAG, trends, and project tools."""

from mcp.server.fastmcp import FastMCP

from .clustering import latest_dynamic_clusters
from .events import list_events
from .models import Settings
from .period_reports import period_report_history
from .projects import list_projects, protocol_comparison_matrix
from .rag import hybrid_search
from .ranking import best_record
from .service import find_card, library, open_store, report_history, trend_history
from .store import Store


def create_mcp_server(store: Store | None = None, settings: Settings | None = None) -> FastMCP:
    cfg = settings or Settings.from_env()
    db = store or open_store(cfg)
    mcp = FastMCP("ResearchRadar")

    @mcp.tool()
    def search_papers(topic_id: str = "", query: str = "", limit: int = 10) -> dict:
        """Search papers in local ResearchRadar SQLite library by topic and keywords."""
        topics = [db.topic(topic_id)[0]] if topic_id else [t for t in db.topics() if not t.archived]
        matches = []
        for t in topics:
            for item in library(db, t, query):
                rec = best_record(item)
                matches.append({
                    "topic_id": t.id, "paper_id": item["paper_id"], "title": rec.title,
                    "date": item["date_label"], "url": rec.url, "favorite": bool(item["favorite"]),
                })
        matches.sort(key=lambda x: (x["date"] or "", x["paper_id"]), reverse=True)
        return {"total": len(matches), "papers": matches[:max(1, min(limit, 30))]}

    @mcp.tool()
    def hybrid_rag_search(query: str, topic_id: str = "", limit: int = 6) -> dict:
        """Run hybrid FTS5 + BGE vector search over indexed full-text paper chunks with page numbers."""
        hits = hybrid_search(db, cfg, query, topic_id=topic_id or None, limit=max(1, min(limit, 15)))
        return {"query": query, "total": len(hits), "chunks": hits}

    @mcp.tool()
    def get_paper_details(topic_id: str, paper_id: int) -> dict:
        """Return paper metadata, abstract, L1 summary card, and cached L2/L3 analysis."""
        topic, _ = db.topic(topic_id)
        papers = library(db, topic, paper_id=paper_id)
        if not papers:
            raise ValueError("找不到论文")
        rec = best_record(papers[0])
        return {
            "paper_id": paper_id, "title": rec.title, "authors": rec.authors,
            "abstract": rec.abstract, "url": rec.url, "card": find_card(db, topic_id, paper_id),
        }

    @mcp.tool()
    def get_trends(topic_id: str) -> dict:
        """Return fixed 7/30/90-day trend snapshots and latest dynamic semantic clusters."""
        return {
            "fixed_trends": trend_history(db, topic_id)[:5],
            "dynamic_clusters": latest_dynamic_clusters(db, topic_id),
        }

    @mcp.tool()
    def get_report(topic_id: str, report_type: str = "daily") -> dict:
        """Return the latest daily, weekly, or monthly Markdown report for a topic."""
        if report_type == "daily":
            items = report_history(db, topic_id)
            return items[0] if items else {"message": "暂无日报"}
        items = period_report_history(db, topic_id, report_type)
        return items[0] if items else {"message": f"暂无 {report_type} 报告"}

    @mcp.tool()
    def get_projects(topic_id: str = "") -> list[dict]:
        """List structured research project archives."""
        return list_projects(db, topic_id)

    @mcp.tool()
    def compare_methods(project_id: str) -> list[dict]:
        """Return protocol-aware method comparison groups for a research project."""
        return protocol_comparison_matrix(db, project_id)

    @mcp.tool()
    def get_research_events(topic_id: str = "", limit: int = 20) -> list[dict]:
        """List detected research events (code/dataset releases, venue acceptance, heating alerts)."""
        return list_events(db, topic_id, limit=max(1, min(limit, 50)))

    return mcp


def main():
    create_mcp_server().run()


if __name__ == "__main__":
    main()
