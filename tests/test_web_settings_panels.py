"""Structure contracts for the settings dialog.

The settings dialog is a left navigation plus a card panel per category. These
checks pin the wiring that makes it usable: every nav item has a matching
panel, only one of each is active, and search filters cards through the shared
``data-settings-card`` hook.
"""

from __future__ import annotations

import re

import tsm_agt.web.app as web_app

_SETTINGS_TABS = ("skills", "builtin", "directories", "mcp")
_NAV_CLASS_RE = re.compile(r'class="settings-nav-item[^"]*"')
_PANEL_CLASS_RE = re.compile(r'class="settings-panel[^"]*"')


def test_settings_dialog_renders_a_navigation_and_one_panel_per_category() -> None:
    html = web_app.INDEX_HTML

    assert 'class="settings-shell"' in html
    assert 'class="settings-nav"' in html
    assert 'id="settings-content"' in html

    for tab in _SETTINGS_TABS:
        assert f'id="settings-nav-{tab}"' in html
        assert f'id="settings-panel-{tab}"' in html
        assert f'data-settings-panel="{tab}"' in html
        assert f'aria-controls="settings-panel-{tab}"' in html
        assert f"showSettingsTab('{tab}')" in html
        assert f'id="settings-tab-count-{tab}"' in html


def test_settings_dialog_opens_on_the_installed_skills_panel() -> None:
    html = web_app.INDEX_HTML

    nav_classes = _NAV_CLASS_RE.findall(html)
    panel_classes = _PANEL_CLASS_RE.findall(html)

    assert len(nav_classes) == len(_SETTINGS_TABS)
    assert len(panel_classes) == len(_SETTINGS_TABS)
    assert [item for item in nav_classes if "active" in item] == [
        'class="settings-nav-item active"'
    ]
    assert [item for item in panel_classes if "active" in item] == [
        'class="settings-panel active"'
    ]
    assert html.index('class="settings-nav-item active"') < html.index(
        'id="settings-panel-skills"'
    )


def test_settings_search_filters_cards_and_reports_the_empty_state() -> None:
    html = web_app.INDEX_HTML

    assert "const SETTINGS_TAB_IDS = ['skills', 'builtin', 'directories', 'mcp'];" in html
    assert "function showSettingsTab(name) {" in html
    assert "navItem.classList.toggle('active', active);" in html
    assert "panel.classList.toggle('active', active);" in html
    assert "setAttribute('aria-selected', active ? 'true' : 'false');" in html

    assert "function filterSettingsCards(panelName, query) {" in html
    assert "querySelectorAll('[data-settings-card]')" in html
    assert "item.style.display = matched ? '' : 'none';" in html
    assert "refreshSettingsCounts(panelName);" in html
    assert "function refreshSettingsCounts(panelName) {" in html

    for tab in ("skills", "mcp"):
        assert f'id="settings-search-{tab}"' in html
        assert f"filterSettingsCards('{tab}', this.value)" in html
        assert f'id="settings-empty-{tab}"' in html

    for tab in ("skills", "builtin", "mcp"):
        assert f'id="settings-empty-{tab}"' in html


def test_settings_cards_share_one_renderer_and_one_card_layout() -> None:
    html = web_app.INDEX_HTML

    assert "function settingsCard(" in html
    assert "card.dataset.settingsCard = '';" in html
    assert "card.dataset.searchText =" in html
    assert ".settings-card {" in html
    assert ".settings-card-head {" in html
    assert ".settings-card-desc {" in html
    assert ".settings-card-foot {" in html


def test_settings_navigation_collapses_to_a_wrapping_toolbar_on_narrow_screens() -> None:
    html = web_app.INDEX_HTML

    assert ".settings-nav {" in html
    assert ".settings-nav-item {" in html
    assert ".settings-nav-group { display: none; }" in html


def test_settings_dialog_reserves_room_for_the_card_panels() -> None:
    html = web_app.INDEX_HTML

    assert "width: min(1320px, 96vw); height: min(864px, 92vh)" in html, (
        "the settings dialog should stay generous on desktop instead of "
        "squeezing the card grid"
    )
    assert "grid-template-columns: 244px minmax(0, 1fr)" in html
    assert "padding: 22px 30px 30px" in html
    # Narrow viewports fall back to full width and a horizontal nav strip so the
    # content column never collapses.
    assert "@media (max-width: 980px)" in html
    assert "@media (max-width: 640px)" in html
