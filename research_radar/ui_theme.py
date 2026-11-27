"""Design System tokens, Phosphor-style SVG icons, and editorial HTML components."""

import html
from pathlib import Path
from typing import Literal

ThemeMode = Literal["light", "dark"]
DensityMode = Literal["comfortable", "compact"]

CSS_PATH = Path(__file__).with_name("web.css")

THEME_TOKENS: dict[ThemeMode, dict[str, str]] = {
    "light": {
        "color-primary": "#7C3AED",
        "color-primary-hover": "#6D28D9",
        "color-on-primary": "#FFFFFF",
        "color-secondary": "#A78BFA",
        "color-accent": "#0891B2",
        "color-accent-soft": "rgba(8, 145, 178, 0.10)",
        "color-primary-soft": "rgba(124, 58, 237, 0.10)",
        "color-background": "#FAF5FF",
        "color-surface": "#FFFFFF",
        "color-surface-elevated": "#FFFFFF",
        "color-foreground": "#1E1B4B",
        "color-muted": "#ECEEF9",
        "color-muted-foreground": "#475569",
        "color-border": "#DDD6FE",
        "color-success": "#059669",
        "color-success-soft": "rgba(5, 150, 105, 0.11)",
        "color-warning": "#D97706",
        "color-warning-soft": "rgba(217, 119, 6, 0.11)",
        "color-destructive": "#DC2626",
        "color-destructive-soft": "rgba(220, 38, 38, 0.10)",
        "color-ring": "#7C3AED",
        "hero-gradient": "linear-gradient(135deg, #312E81 0%, #5B21B6 55%, #0E7490 100%)",
        "hero-border": "#6D28D9",
        "hero-text": "#F8FAFC",
        "hero-subtext": "#E0E7FF",
        "shadow-card": "0 2px 10px rgba(30, 27, 75, 0.05)",
        "shadow-elevated": "0 8px 24px rgba(30, 27, 75, 0.09)",
    },
    "dark": {
        "color-primary": "#8B5CF6",
        "color-primary-hover": "#7C3AED",
        "color-on-primary": "#FFFFFF",
        "color-secondary": "#A78BFA",
        "color-accent": "#22D3EE",
        "color-accent-soft": "rgba(34, 211, 238, 0.14)",
        "color-primary-soft": "rgba(139, 92, 246, 0.16)",
        "color-background": "#0F0D1A",
        "color-surface": "#18142C",
        "color-surface-elevated": "#201B3A",
        "color-foreground": "#F3F0FF",
        "color-muted": "#221D3D",
        "color-muted-foreground": "#C4BFE0",
        "color-border": "#342D5C",
        "color-success": "#34D399",
        "color-success-soft": "rgba(52, 211, 153, 0.14)",
        "color-warning": "#FBBF24",
        "color-warning-soft": "rgba(251, 191, 36, 0.14)",
        "color-destructive": "#F87171",
        "color-destructive-soft": "rgba(248, 113, 113, 0.14)",
        "color-ring": "#A78BFA",
        "hero-gradient": "linear-gradient(135deg, #1E1B4B 0%, #4C1D95 55%, #164E63 100%)",
        "hero-border": "#4C1D95",
        "hero-text": "#F8FAFC",
        "hero-subtext": "#DDD6FE",
        "shadow-card": "0 2px 12px rgba(0, 0, 0, 0.35)",
        "shadow-elevated": "0 10px 28px rgba(0, 0, 0, 0.50)",
    },
}

DENSITY_TOKENS: dict[DensityMode, dict[str, str]] = {
    "comfortable": {
        "space-card": "1.12rem",
        "space-gap": "0.95rem",
        "line-height-body": "1.55",
        "font-size-body": "0.96rem",
    },
    "compact": {
        "space-card": "0.72rem",
        "space-gap": "0.55rem",
        "line-height-body": "1.42",
        "font-size-body": "0.89rem",
    },
}

_SVG_PATHS: dict[str, str] = {
    "radar": '<circle cx="12" cy="12" r="9"/><path d="M12 3v9l6.2 3.6"/><circle cx="12" cy="12" r="3"/>',
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "topics": '<path d="M4 6h16M4 12h10M4 18h7"/><circle cx="18" cy="15" r="3"/>',
    "library": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    "reports": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "projects": '<path d="M9 3h6l2 4h3v13H4V7h3l2-4z"/><path d="M9 13h6M12 10v6"/>',
    "runs": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "assistant": '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 20l1.2-4.6A8.3 8.3 0 0 1 3 11.5a8.5 8.5 0 0 1 8.5-8.5h1A8.5 8.5 0 0 1 21 11.5z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.8-5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "alert": '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
    "spark": '<path d="M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z"/>',
}


def svg_icon(name: str, size: int = 16) -> str:
    """Return a clean inline Phosphor-style SVG icon hidden from screen readers when paired with text."""
    body = _SVG_PATHS.get(name, _SVG_PATHS["radar"])
    return (
        f'<svg class="rr-icon" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def theme_css(mode: ThemeMode = "light", density: DensityMode = "comfortable") -> str:
    """Build complete <style> tag combining UI/UX Pro Max design tokens and base stylesheet."""
    tokens = {**THEME_TOKENS.get(mode, THEME_TOKENS["light"]), **DENSITY_TOKENS.get(density, DENSITY_TOKENS["comfortable"])}
    vars_block = "\n".join(f"  --{k}: {v};" for k, v in tokens.items())
    base_css = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
    return f"<style>\n:root {{\n{vars_block}\n}}\n{base_css}\n</style>"


def status_pill(text: str, tone: str = "neutral", icon: str = "radar") -> str:
    """Render an accessible status pill with icon + text (never relying on color alone)."""
    safe_tone = tone if tone in {"primary", "accent", "success", "warning", "danger", "neutral"} else "neutral"
    return f'<span class="rr-pill rr-pill-{safe_tone}">{svg_icon(icon, 14)}<span>{html.escape(str(text))}</span></span>'


def brand_header_html() -> str:
    return (
        '<div class="rr-brand">'
        f'<div class="rr-brand-mark">{svg_icon("radar", 20)}</div>'
        '<div class="rr-brand-text">'
        '<div class="rr-brand-title">ResearchRadar</div>'
        '<div class="rr-brand-sub">AI-Native 科研情报工作台</div>'
        '</div>'
        '</div>'
    )


def paper_card_html(title: str, url: str | None, authors: str, date_label: str, pub_label: str,
                    pub_tone: str = "neutral", relevance: float | None = None,
                    summary: str = "", inference: str = "", links: list[dict] | None = None) -> str:
    safe_title = html.escape(title)
    title_html = (
        f'<a class="rr-card-title" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{safe_title}</a>'
        if url else f'<span class="rr-card-title">{safe_title}</span>'
    )
    meta = " · ".join(filter(None, (authors, date_label)))
    rel_html = ""
    if relevance is not None:
        pct = max(0, min(100, int(round(relevance * 100))))
        rel_html = (
            f'<div class="rr-rel" title="主题相关性 {relevance:.2f}">'
            f'<span class="rr-rel-label">相关性 {pct}%</span>'
            f'<div class="rr-progress-track"><div class="rr-progress-fill" style="width:{pct}%"></div></div>'
            '</div>'
        )
    summary_html = f'<p class="rr-card-summary">{html.escape(summary)}</p>' if summary else ""
    inf_html = (
        f'<div class="rr-card-inference">{svg_icon("spark", 14)}<span>{html.escape(inference)}</span></div>'
        if inference else ""
    )
    chips = ""
    if links:
        chips = '<div class="rr-chip-row">' + "".join(
            f'<a class="rr-chip" href="{html.escape(item["url"], quote=True)}" target="_blank" rel="noopener">{html.escape(item["label"])}</a>'
            for item in links[:4]
        ) + "</div>"
    return (
        '<article class="rr-paper-card">'
        '<div class="rr-paper-head">'
        f'<div class="rr-paper-main">{title_html}<div class="rr-card-meta">{html.escape(meta or "作者或日期待确认")}</div></div>'
        f'<div class="rr-paper-aside">{status_pill(pub_label, pub_tone, "book")}{rel_html}</div>'
        '</div>'
        f'{summary_html}{inf_html}{chips}'
        '</article>'
    )
